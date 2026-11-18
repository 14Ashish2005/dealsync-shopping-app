import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.tools import tool

from backend.currency import format_major_amount, validate_currency_code
from backend.database_paths import BUSINESS_SQLITE_PATH
from backend.visual_search.image_processing import (
    MAX_UPLOAD_SIZE_BYTES,
    normalize_image_bytes,
)
from backend.visual_search.groq_client import create_vision_analyzer
from langchain_groq import ChatGroq

from reviews_api import get_product_rating, get_ratings_for_products

ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path=ENV_PATH)

groq_api_key = os.environ.get("GROQ_API_KEY")
if not groq_api_key or not groq_api_key.strip():
    raise ValueError(
        "GROQ_API_KEY is missing. Please set your GROQ_API_KEY in the .env file inside the project directory."
    )

DB_PATH = str(BUSINESS_SQLITE_PATH)
INITIAL_ORDER_STATUS = "Processing & On Schedule"

llm = ChatGroq(model="qwen/qwen3.8-27b", temperature=0)
vision_analyzer = create_vision_analyzer()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def _table_columns(cursor: sqlite3.Cursor, table: str) -> set[str]:
    return {row[1] for row in cursor.execute(f'PRAGMA table_info("{table}")')}


def _shared_known_currency(items: list[dict]) -> str | None:
    currencies = {item.get("currency") for item in items}
    if len(currencies) != 1 or None in currencies:
        return None
    currency = next(iter(currencies))
    try:
        return validate_currency_code(currency)
    except ValueError:
        return None


@tool
def search_products(
    query: str,
    max_price: Optional[float] = None,
    is_organic: Optional[bool] = None,
    currency: Optional[str] = None,
) -> str:
    """
    Search the product database by keyword (matched against name, description, and category).
    Optionally filter by maximum price and/or organic status.
    Returns a JSON array of matching products, each with: id, name, category, price,
    currency, description, is_organic. Price filters require an explicit currency code.
    """
    if max_price is not None:
        if currency is None:
            raise ValueError("A currency code is required for a maximum-price filter.")
        currency = validate_currency_code(currency)

    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()

        currency_column = (
            "currency" if "currency" in _table_columns(cursor, "products") else "NULL"
        )
        sql = (
            "SELECT id, name, category, price, description, is_organic, "
            f"{currency_column} FROM products WHERE 1=1"
        )
        params: list = []

        if query:
            sql += " AND (name LIKE ? OR description LIKE ? OR category LIKE ?)"
            like = f"%{query}%"
            params.extend([like, like, like])

        if max_price is not None:
            sql += f" AND {currency_column} = ? AND price <= ?"
            params.extend((currency, max_price))

        if is_organic is not None:
            sql += " AND is_organic = ?"
            params.append(1 if is_organic else 0)

        cursor.execute(sql, params)
        rows = cursor.fetchall()
    finally:
        conn.close()

    products = [
        {
            "id":          row[0],
            "name":        row[1],
            "category":    row[2],
            "price":       row[3],
            "description": row[4],
            "is_organic":  bool(row[5]),
            "currency":    row[6],
        }
        for row in rows
    ]
    return json.dumps(products)


@tool
def get_rating(product_id: int) -> str:
    """
    Get the average customer rating and total review count for a single product by its ID.
    Returns a JSON object with: product_id, average_rating, review_count.
    """
    result = get_product_rating(product_id)
    return json.dumps(result)


@tool
def get_batch_ratings(product_ids: list[int]) -> str:
    """
    Get average customer ratings and review counts for multiple products at once by their IDs.
    Returns a JSON array of objects, each with: product_id, average_rating, review_count.
    """
    results = get_ratings_for_products(product_ids)
    return json.dumps(results)


@tool
def get_order_history(limit: int = 5) -> str:
    """
    Retrieve the most recent orders placed by the authenticated user.
    Returns a JSON array of recent orders, each with: order_id, product_id, product_name,
    price, currency, ordered_at.
    """
    owner_id = _get_active_order_owner()
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        _require_order_ownership(cursor)
        currency_column = (
            "currency" if "currency" in _table_columns(cursor, "orders") else "NULL"
        )
        cursor.execute(
            "SELECT id, product_id, product_name, price, ordered_at, "
            f"{currency_column} FROM orders "
            "WHERE owner_id = ? ORDER BY id DESC LIMIT ?",
            (owner_id, limit),
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    orders = [
        {
            "order_id": row[0],
            "product_id": row[1],
            "product_name": row[2],
            "price": row[3],
            "ordered_at": row[4],
            "currency": row[5],
        }
        for row in rows
    ]
    return json.dumps(orders)


@tool
def get_order_status(order_id: int) -> str:
    """
    Look up an order owned by the authenticated user by its order ID.
    Returns a JSON object with the order details, currency, and shipping status.
    """
    owner_id = _get_active_order_owner()
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        _require_order_ownership(cursor)
        currency_column = (
            "currency" if "currency" in _table_columns(cursor, "orders") else "NULL"
        )
        cursor.execute(
            "SELECT id, product_id, product_name, price, ordered_at, status, "
            f"{currency_column} FROM orders "
            "WHERE id = ? AND owner_id = ?",
            (order_id, owner_id),
        )
        row = cursor.fetchone()
    finally:
        conn.close()

    if not row:
        return f"Order #{order_id} was not found. Please check your order ID."

    oid, pid, name, price, ordered_at, status, currency = row
    return json.dumps({
        "order_id": oid,
        "product_id": pid,
        "product_name": name,
        "price": price,
        "currency": currency,
        "ordered_at": ordered_at,
        "status": status or "Status unavailable (not recorded)",
        **(
            {"estimated_delivery": "3-5 business days from order date"}
            if status == INITIAL_ORDER_STATUS
            else {}
        ),
    })


@tool
def list_categories() -> str:
    """
    Get all available product categories in the store along with the count of items in each category.
    Returns a JSON array of categories.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT category, COUNT(*) as count FROM products WHERE category IS NOT NULL GROUP BY category ORDER BY category ASC"
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    categories = [{"category": row[0], "product_count": row[1]} for row in rows]
    return json.dumps(categories)


_active_cart: ContextVar[dict[int, int] | None] = ContextVar("active_cart", default=None)
_active_order_owner: ContextVar[str | None] = ContextVar("active_order_owner", default=None)


class OrderOwnershipMigrationRequired(RuntimeError):
    """Raised when owner-scoped order storage has not been migrated."""


ORDER_OWNERSHIP_MIGRATION_MESSAGE = (
    "Order history and checkout are unavailable until the order-ownership/status migration "
    "is applied. Run `python migrate_order_ownership.py`."
)


@contextmanager
def use_cart(session_cart: dict[int, int]) -> Iterator[None]:
    """Bind cart tools to the current Streamlit session for one agent call."""
    token = _active_cart.set(session_cart)
    try:
        yield
    finally:
        _active_cart.reset(token)


def _get_active_cart() -> dict[int, int]:
    cart = _active_cart.get()
    if cart is None:
        raise RuntimeError("Cart tools require an active session cart.")
    return cart


@contextmanager
def use_order_owner(owner_id: str) -> Iterator[None]:
    """Bind the authenticated OIDC identity to order tools for one agent call."""
    if not owner_id:
        raise ValueError("An authenticated order owner is required.")
    token = _active_order_owner.set(owner_id)
    try:
        yield
    finally:
        _active_order_owner.reset(token)


def _get_active_order_owner() -> str:
    owner_id = _active_order_owner.get()
    if owner_id is None:
        raise RuntimeError("Order tools require an authenticated user.")
    return owner_id


def _require_order_ownership(cursor: sqlite3.Cursor) -> None:
    columns = {row[1] for row in cursor.execute("PRAGMA table_info(orders)")}
    if not {"owner_id", "status"}.issubset(columns):
        raise OrderOwnershipMigrationRequired(ORDER_OWNERSHIP_MIGRATION_MESSAGE)


def get_cart_items(cart: dict[int, int] | None = None) -> list[dict]:
    """
    Helper to return detailed cart items with product info and subtotal.
    If `cart` is provided, uses it; otherwise uses the cart bound to this call.
    """
    active_cart = cart if cart is not None else _get_active_cart()
    if not active_cart:
        return []
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        placeholders = ",".join("?" * len(active_cart))
        currency_column = (
            "currency" if "currency" in _table_columns(cursor, "products") else "NULL"
        )
        cursor.execute(
            f"SELECT id, name, price, {currency_column} "
            f"FROM products WHERE id IN ({placeholders})",
            list(active_cart.keys()),
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    items = []
    for row in rows:
        pid, name, price, currency = row
        qty = active_cart.get(pid, 0)
        items.append({
            "product_id": pid,
            "name": name,
            "price": price,
            "currency": currency,
            "quantity": qty,
            "subtotal": round(price * qty, 2),
        })
    return items


def clear_cart_items(cart: dict[int, int] | None = None) -> None:
    """Empty the provided cart or the cart bound to this call."""
    active_cart = cart if cart is not None else _get_active_cart()
    active_cart.clear()


@tool
def add_to_cart(product_id: int, quantity: int = 1) -> str:
    """
    Add a product to the user's shopping cart by product ID with an optional quantity (default is 1).
    Returns a confirmation message with the updated cart contents.
    """
    if isinstance(quantity, bool):
        return "Error: Quantity must be a valid positive whole number."

    try:
        num = float(quantity)
        if not num.is_integer() or num <= 0:
            return "Error: Quantity must be a positive whole number (e.g. 1, 2, 3)."
        qty = int(num)
    except (ValueError, TypeError):
        return "Error: Quantity must be a valid positive whole number."

    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        currency_column = (
            "currency" if "currency" in _table_columns(cursor, "products") else "NULL"
        )
        cursor.execute(
            f"SELECT name, price, {currency_column} FROM products WHERE id = ?",
            (product_id,),
        )
        row = cursor.fetchone()
    finally:
        conn.close()

    if not row:
        return f"Error: Product with ID {product_id} not found."

    name, price, currency = row
    cart = _get_active_cart()
    cart[product_id] = cart.get(product_id, 0) + qty
    total_qty = cart[product_id]
    items = get_cart_items()
    cart_currency = _shared_known_currency(items)
    total_cart_price = (
        sum(item["subtotal"] for item in items)
        if cart_currency is not None
        else None
    )
    cart_total_text = (
        format_major_amount(total_cart_price, cart_currency)
        if total_cart_price is not None
        else "unavailable because cart currency is unverified or mixed"
    )

    return (
        f"Added {qty}x '{name}' "
        f"({format_major_amount(price, currency)} each) to your cart. "
        f"You now have {total_qty}x of this item in your cart. "
        f"Current cart total: {cart_total_text}."
    )


@tool
def view_cart() -> str:
    """
    View all items currently in the user's shopping cart, including quantities, unit prices,
    subtotals, and overall total.
    """
    items = get_cart_items()
    if not items:
        return "Your shopping cart is currently empty."

    currency = _shared_known_currency(items)
    total_price = (
        sum(item["subtotal"] for item in items)
        if currency is not None
        else None
    )
    summary = ["🛒 Your Shopping Cart:"]
    for idx, item in enumerate(items, 1):
        summary.append(
            f"{idx}. {item['name']} (ID:{item['product_id']}) — "
            f"{item['quantity']}x @ {format_major_amount(item['price'], item['currency'])} = "
            f"{format_major_amount(item['subtotal'], item['currency'])}"
        )
    summary.append(
        "\nTotal: "
        + (
            format_major_amount(total_price, currency)
            if total_price is not None
            else "unavailable because cart currency is unverified or mixed"
        )
    )
    return "\n".join(summary)


@tool
def remove_from_cart(product_id: int) -> str:
    """
    Remove an item completely from the user's shopping cart by product ID.
    """
    cart = _get_active_cart()
    if product_id not in cart:
        return f"Item with ID {product_id} is not in your cart."

    del cart[product_id]
    return f"Product with ID {product_id} has been removed from your cart."


@tool
def checkout_cart() -> str:
    """
    Place an order for all items currently in the shopping cart.
    Saves each item to the database orders table, clears the cart, and returns
    a confirmation message with the order details.
    """
    items = get_cart_items()
    if not items:
        return "Your cart is empty. There are no items to checkout."
    owner_id = _get_active_order_owner()

    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        _require_order_ownership(cursor)

        order_ids = []
        total_amount = 0.0
        total_currency = _shared_known_currency(items)
        order_has_currency = "currency" in _table_columns(cursor, "orders")

        try:
            for item in items:
                pid = item["product_id"]
                name = item["name"]
                unit_price = item["price"]
                qty = item["quantity"]
                item_currency = item["currency"]
                if item_currency is not None:
                    item_currency = validate_currency_code(item_currency)

                for _ in range(qty):
                    if order_has_currency:
                        cursor.execute(
                            "INSERT INTO orders "
                            "(product_id, product_name, price, owner_id, status, currency) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                pid,
                                name,
                                unit_price,
                                owner_id,
                                INITIAL_ORDER_STATUS,
                                item_currency,
                            ),
                        )
                    else:
                        cursor.execute(
                            "INSERT INTO orders "
                            "(product_id, product_name, price, owner_id, status) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (pid, name, unit_price, owner_id, INITIAL_ORDER_STATUS),
                        )
                    order_ids.append(cursor.lastrowid)
                    if total_currency is not None:
                        total_amount += unit_price

            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()

    # Cart is cleared only after all database writes committed successfully.
    clear_cart_items()

    order_id_str = ", #".join(str(oid) for oid in order_ids)
    total_text = (
        format_major_amount(total_amount, total_currency)
        if total_currency is not None
        else "currency is unverified or mixed; no combined total is available"
    )
    return (
        f"🎉 Order confirmed! (Order ID(s): #{order_id_str}). "
        f"Successfully purchased {len(order_ids)} item(s); total: {total_text}. "
        f"Your order will arrive in 3-5 business days. Thank you for shopping with us!"
    )


@tool
def checkout(product_id: int) -> str:
    """
    Place an order for the given product ID. Saves the order to the database and returns
    a confirmation message with the order ID, product name, and price.
    """
    owner_id = _get_active_order_owner()
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        _require_order_ownership(cursor)
        product_currency_column = (
            "currency" if "currency" in _table_columns(cursor, "products") else "NULL"
        )
        cursor.execute(
            f"SELECT name, price, {product_currency_column} "
            "FROM products WHERE id = ?",
            (product_id,),
        )
        row = cursor.fetchone()

        if not row:
            return f"Error: product with ID {product_id} not found."

        name, price, currency = row
        if currency is not None:
            currency = validate_currency_code(currency)
        try:
            if "currency" in _table_columns(cursor, "orders"):
                cursor.execute(
                    "INSERT INTO orders "
                    "(product_id, product_name, price, owner_id, status, currency) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        product_id,
                        name,
                        price,
                        owner_id,
                        INITIAL_ORDER_STATUS,
                        currency,
                    ),
                )
            else:
                cursor.execute(
                    "INSERT INTO orders "
                    "(product_id, product_name, price, owner_id, status) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (product_id, name, price, owner_id, INITIAL_ORDER_STATUS),
                )
            order_id = cursor.lastrowid
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()

    return (
        f"Order #{order_id} confirmed! '{name}' has been successfully ordered for "
        f"{format_major_amount(price, currency)}. "
        f"Your order will arrive in 3-5 business days. Thank you for shopping with us!"
    )


def analyze_product_image(image_path: str) -> dict:
    if not image_path or not os.path.isfile(image_path):
        raise FileNotFoundError("Image file does not exist.")
    if os.path.getsize(image_path) > MAX_UPLOAD_SIZE_BYTES:
        raise ValueError("Image must be no larger than 10 MB.")

    try:
        with open(image_path, "rb") as image_file:
            image_data = image_file.read()
    except OSError:
        raise OSError("Unable to read the uploaded image.") from None

    normalized_image = normalize_image_bytes(
        image_data,
        filename=os.path.basename(image_path),
    )
    return vision_analyzer.analyze(normalized_image).to_legacy_response()


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

agent = create_agent(
    tools=[
        search_products,
        get_rating,
        get_batch_ratings,
        checkout,
        get_order_history,
        get_order_status,
        list_categories,
        add_to_cart,
        view_cart,
        remove_from_cart,
        checkout_cart,
    ],
    model=llm,
    system_prompt=(
        "You are a helpful shopping assistant. Follow these rules strictly.\n"
        "When calling tools, always invoke the exact tool name in lowercase snake_case (e.g. search_products, get_batch_ratings, get_rating, checkout, etc.). Never change tool names to title case or add spaces.\n\n"
        "CURRENCY — product and order currency is unverified when the currency field is null. "
        "Never infer USD, INR, or another currency from the amount, a symbol, a provider, or a user's location. "
        "Apply price filters only when the user explicitly names a supported ISO currency code. "
        "If currency is unverified or mixed, do not compare or combine those amounts; say currency is unverified.\n\n"
        "STORE CATEGORIES — when the user asks what you sell, what categories are available, or wants to explore:\n"
        "1. Call list_categories to fetch all available product categories and counts.\n"
        "2. Present the categories clearly to the user.\n\n"
        "BROWSING — when the user describes what they want to buy:\n"
        "1. Call search_products to find matching items. Apply a price filter only with the user's explicit currency code.\n"
        "2. Call get_batch_ratings with the candidate product IDs (or get_rating individually) to retrieve their average ratings.\n"
        "3. Filter by the user's minimum rating if specified.\n"
        "4. Present qualifying products as a numbered list. For each item use this exact format "
        "   (plain text, no backticks, no code blocks, no bold, no italic):\n\n"
        "   #<number>. <name> (ID:<product_id>) — <price> <currency or 'currency unverified'> "
        "★<rating> — <organic or non-organic>\n\n"
        "   Add a blank line between each product entry for readability. "
        "   Always include (ID:X) so you can reference it later.\n"
        "5. If only one product qualifies, still show it in the list and ask: "
        "   'Would you like to order it or add it to your cart?'\n"
        "6. Do NOT call checkout or add_to_cart at this stage.\n\n"
        "CART MANAGEMENT — when the user wants to add items to their cart, view cart, or checkout their cart:\n"
        "1. To add an item (e.g. 'add to cart', 'add 2 of #1', 'put the second one in my cart'):\n"
        "   - Identify product_id from (ID:X) in your previous message.\n"
        "   - Call add_to_cart with product_id and specified quantity (default is 1).\n"
        "   - Confirm the addition and mention the cart total.\n"
        "2. To view the cart (e.g. 'view cart', 'what is in my cart', 'show cart'):\n"
        "   - Call view_cart and display the cart contents.\n"
        "3. To remove an item (e.g. 'remove #1 from cart'):\n"
        "   - Call remove_from_cart with the product_id.\n"
        "4. To checkout the cart (e.g. 'checkout cart', 'buy everything in my cart', 'order my cart'):\n"
        "   - Call checkout_cart.\n"
        "   - Confirm the final order to the user in plain text.\n\n"
        "ORDER TRACKING & HISTORY:\n"
        "1. When the user asks about recent orders or order history: Call get_order_history.\n"
        "2. When the user asks about a specific order status (e.g. 'status of order #8'): Call get_order_status with the order_id.\n"
        "3. Present the order details and delivery status clearly.\n\n"
        "ORDERING SINGLE ITEMS — when the user confirms they want to buy an individual item immediately "
        "(e.g. 'yes', 'sure', 'buy #1', 'order number 2', 'the first one', 'get me #3'):\n"
        "1. Look at your previous message to find the (ID:X) for the chosen product "
        "   (if only one was listed and the user says 'yes', use that product's ID).\n"
        "2. Call checkout with that product_id (the number from (ID:X)).\n"
        "3. Confirm the order to the user in plain text.\n\n"
        "Never place an order or checkout unless the user explicitly confirms. "
        "Never guess a product_id — always take it from the (ID:X) in your own previous message."
    ),
)

if __name__ == "__main__":
    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "I want to buy organic honey with 4.5+ rating and less than $20 price."
                    ),
                }
            ]
        }
    )
    print(result["messages"][-1].content)
