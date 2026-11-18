import sqlite3
import os

from backend.database_paths import BUSINESS_SQLITE_PATH
from backend.product_specifications_schema import ensure_product_specifications_schema

DB_PATH = BUSINESS_SQLITE_PATH


def create_database(db_path: str | os.PathLike[str] = DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.execute("BEGIN IMMEDIATE")
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT,
            price REAL,
            description TEXT,
            is_organic INTEGER DEFAULT 0,
            currency TEXT CHECK (
                currency IS NULL OR currency GLOB '[A-Z][A-Z][A-Z]'
            )
        )
    """)

    ensure_product_specifications_schema(conn)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER,
            rating REAL,
            reviewer_name TEXT,
            review_text TEXT,
            FOREIGN KEY (product_id) REFERENCES products(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            price REAL NOT NULL,
            ordered_at TEXT NOT NULL DEFAULT (datetime('now')),
            owner_id TEXT,
            status TEXT,
            currency TEXT CHECK (
                currency IS NULL OR currency GLOB '[A-Z][A-Z][A-Z]'
            ),
            FOREIGN KEY (product_id) REFERENCES products(id)
        )
    """)

    products = [
        # --- Honey (8) ---
        (1,  "Organic Raw Honey",            "honey",        14.99, "Pure organic raw honey, unfiltered and cold-pressed",                1),
        (2,  "Wildflower Honey",             "honey",        12.99, "Natural wildflower honey from local beekeepers",                     0),
        (3,  "Organic Manuka Honey",         "honey",        29.99, "Premium organic Manuka honey from New Zealand",                      1),
        (4,  "Clover Honey",                 "honey",         8.99, "Classic clover honey, smooth and sweet",                             0),
        (5,  "Organic Buckwheat Honey",      "honey",        18.99, "Dark and robust organic buckwheat honey, antioxidant-rich",          1),
        (6,  "Orange Blossom Honey",         "honey",        15.99, "Light and floral orange blossom honey",                              0),
        (7,  "Organic Acacia Honey",         "honey",        17.99, "Light and mild organic acacia honey, low glycemic index",            1),
        (8,  "Creamed Honey",                "honey",        11.99, "Smooth creamed honey with spreadable texture",                       0),
        # --- Oils (4) ---
        (9,  "Organic Extra Virgin Olive Oil","oil",         16.99, "Cold-pressed organic EVOO from Mediterranean olives",               1),
        (10, "Coconut Oil",                  "oil",          12.49, "Refined coconut oil, great for high-heat cooking",                   0),
        (11, "Organic Flaxseed Oil",         "oil",          14.99, "Cold-pressed organic flaxseed oil, rich in omega-3",                 1),
        (12, "Avocado Oil",                  "oil",          18.99, "Cold-pressed avocado oil, high smoke point",                         0),
        # --- Nuts & Seeds (4) ---
        (13, "Organic Almonds",              "nuts",         11.99, "Raw organic almonds, unsalted, non-GMO certified",                   1),
        (14, "Roasted Cashews",              "nuts",          9.99, "Lightly salted dry-roasted cashews",                                 0),
        (15, "Organic Chia Seeds",           "seeds",         8.49, "Organic black chia seeds, high in fiber and omega-3",                1),
        (16, "Mixed Nuts",                   "nuts",         13.99, "Premium mix of walnuts, pecans, almonds and Brazil nuts",            0),
        # --- Grains & Cereals (4) ---
        (17, "Organic Quinoa",               "grains",       10.99, "Organic white quinoa, complete protein, gluten-free",                1),
        (18, "Rolled Oats",                  "grains",        5.49, "Whole grain rolled oats, great for porridge and baking",             0),
        (19, "Organic Brown Rice",           "grains",        7.99, "Long-grain organic brown rice, naturally gluten-free",               1),
        (20, "Steel-Cut Oats",               "grains",        6.99, "Traditional steel-cut oats, low GI, hearty texture",                 0),
        # --- Tea & Coffee (4) ---
        (21, "Organic Green Tea",            "tea",          12.99, "Japanese organic sencha green tea, 50 bags",                         1),
        (22, "Chamomile Tea",                "tea",           8.99, "Dried chamomile flowers, caffeine-free, soothing",                   0),
        (23, "Organic Ethiopian Coffee",     "coffee",       16.99, "Single-origin organic Arabica, medium roast whole bean",             1),
        (24, "Dark Roast Espresso Blend",    "coffee",       14.49, "Bold dark roast espresso blend, ground",                             0),
        # --- Snacks (4) ---
        (25, "Organic Granola",              "snacks",        9.99, "Organic oat granola with honey, almonds and dried cranberries",      1),
        (26, "Rice Cakes",                   "snacks",        4.49, "Lightly salted brown rice cakes, low calorie",                       0),
        (27, "Organic Dried Mango",          "snacks",        7.99, "Unsweetened organic dried mango slices, no preservatives",           1),
        (28, "Trail Mix",                    "snacks",        8.49, "Classic trail mix with raisins, M&Ms, peanuts and sunflower seeds",  0),
        # --- Dairy Alternatives (4) ---
        (29, "Organic Almond Milk",          "dairy-alt",     4.99, "Unsweetened organic almond milk, fortified with calcium",            1),
        (30, "Oat Milk",                     "dairy-alt",     4.49, "Barista-style oat milk, great for coffee",                           0),
        (31, "Organic Coconut Milk",         "dairy-alt",     3.99, "Full-fat organic coconut milk, great for curries",                   1),
        (32, "Soy Milk",                     "dairy-alt",     3.49, "Unsweetened soy milk, high protein",                                 0),
    ]
    # These static legacy sample prices have no verified currency provenance.
    cursor.executemany(
        """
        INSERT INTO products
            (id, name, category, price, description, is_organic)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        products,
    )

    conn.commit()
    conn.close()
    print(f"Database created at: {db_path}")


def insert_product(
    conn: sqlite3.Connection,
    name: str,
    category: str,
    price: float,
    description: str,
    is_organic: int = 0,
    currency: str | None = None,
) -> int:
    """Insert a new product into the database.

    Under Currency Policy Option 1, all regular new product writes must
    supply a valid ISO 4217 currency code (cannot be NULL).
    """
    from backend.currency import validate_new_product_currency

    valid_currency = validate_new_product_currency(currency)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO products (name, category, price, description, is_organic, currency)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (name, category, price, description, is_organic, valid_currency),
    )
    conn.commit()
    return cursor.lastrowid


if __name__ == "__main__":
    create_database()
