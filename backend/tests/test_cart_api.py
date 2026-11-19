import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.tests._support import (
    import_main_with_temporary_auth_db,
    prepare_test_schemas,
)


main = import_main_with_temporary_auth_db()


class FakeAgent:
    def __init__(self, reply="Shopping assistant response"):
        self.reply = reply

    def invoke(self, _request):
        return {
            "messages": [
                SimpleNamespace(type="ai", content=self.reply),
            ]
        }


class CartApiFixture(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.temp_path = Path(self.temp_dir.name)
        self.store_path = self.temp_path / "store.db"
        self.auth_path = self.temp_path / "auth_sessions.db"
        self._create_store()

        self.original_store_path = main.STORE_DB_PATH
        self.original_auth_path = main.AUTH_DB_PATH
        main.STORE_DB_PATH = self.store_path
        self.addCleanup(setattr, main, "STORE_DB_PATH", self.original_store_path)
        main.AUTH_DB_PATH = self.auth_path
        self.addCleanup(setattr, main, "AUTH_DB_PATH", self.original_auth_path)
        prepare_test_schemas(self.store_path, self.auth_path)

        self.client = TestClient(main.app)
        with patch.object(
            main,
            "create_vision_analyzer",
            side_effect=ValueError("Vision search is disabled in this test."),
        ):
            self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        authentication_engine = main.app.state.database_engines.authentication
        self.owner_a = main.create_session("test-owner-a", authentication_engine)
        self.owner_b = main.create_session("test-owner-b", authentication_engine)

    def _create_store(self):
        connection = sqlite3.connect(self.store_path)
        try:
            connection.execute("""
                CREATE TABLE products (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT,
                    price REAL,
                    description TEXT,
                    is_organic INTEGER DEFAULT 0,
                    currency TEXT
                )
            """)
            connection.execute("""
                CREATE TABLE orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id INTEGER NOT NULL,
                    product_name TEXT NOT NULL,
                    price REAL NOT NULL,
                    ordered_at TEXT NOT NULL DEFAULT (datetime('now')),
                    owner_id TEXT,
                    status TEXT,
                    currency TEXT
                )
            """)
            connection.executemany(
                """
                INSERT INTO products
                    (id, name, category, price, description, is_organic, currency)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (1, "Organic Raw Honey", "Honey", 14.99, "Raw honey", 1, "USD"),
                    (2, "Wildflower Honey", "Honey", 12.99, "Wildflower honey", 0, "INR"),
                    (3, "Rolled Oats", "Grains", 5.49, "Whole grain oats", 0, None),
                ],
            )
            connection.commit()
        finally:
            connection.close()

    def _cookies(self, token):
        self.client.cookies.clear()
        self.client.cookies.set(main.SESSION_COOKIE_NAME, token)
        return None


class DemoOrderApiTests(CartApiFixture):
    def test_create_order_persists_server_cart_and_clears_it_after_commit(self):
        added = self.client.post(
            "/api/cart/items",
            json={"product_id": 1, "quantity": 2},
            cookies=self._cookies(self.owner_a),
        )
        self.assertEqual(added.status_code, 200)

        response = self.client.post(
            "/api/orders",
            cookies=self._cookies(self.owner_a),
        )
        self.assertEqual(response.status_code, 201)
        order = response.json()["order"]
        self.assertTrue(order["order_id"])
        self.assertEqual(order["status"], "Demo Order Placed")
        self.assertEqual(order["currency"], "USD")
        self.assertEqual(order["total_minor"], 2998)
        self.assertEqual(
            order["items"],
            [
                {
                    "product_id": 1,
                    "product_name": "Organic Raw Honey",
                    "quantity": 2,
                    "unit_price": 14.99,
                    "currency": "USD",
                }
            ],
        )
        self.assertEqual(
            self.client.get(
                "/api/cart",
                cookies=self._cookies(self.owner_a),
            ).json()["items"],
            [],
        )

        with closing(sqlite3.connect(self.store_path)) as connection:
            stored = connection.execute(
                """
                SELECT owner_id, status, order_reference, quantity,
                       total_minor, order_currency
                FROM orders
                """
            ).fetchone()
        self.assertEqual(
            stored,
            (self.owner_a, "Demo Order Placed", order["order_id"], 2, 2998, "USD"),
        )

    def test_order_history_is_authenticated_and_owner_scoped(self):
        unauthenticated = self.client.get("/api/orders")
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(self.client.post("/api/orders").status_code, 401)

        empty_history = self.client.get(
            "/api/orders",
            cookies=self._cookies(self.owner_b),
        )
        self.assertEqual(empty_history.status_code, 200)
        self.assertEqual(empty_history.json(), {"orders": []})

        self.client.post(
            "/api/cart/items",
            json={"product_id": 1, "quantity": 1},
            cookies=self._cookies(self.owner_a),
        )
        placed = self.client.post(
            "/api/orders",
            cookies=self._cookies(self.owner_a),
        )
        self.assertEqual(placed.status_code, 201)

        owner_a_history = self.client.get(
            "/api/orders",
            cookies=self._cookies(self.owner_a),
        )
        owner_b_history = self.client.get(
            "/api/orders",
            cookies=self._cookies(self.owner_b),
        )
        self.assertEqual(len(owner_a_history.json()["orders"]), 1)
        self.assertEqual(
            owner_a_history.json()["orders"][0]["order_id"],
            placed.json()["order"]["order_id"],
        )
        self.assertEqual(owner_b_history.json(), {"orders": []})

    def test_multiple_products_are_grouped_with_server_calculated_total(self):
        with closing(sqlite3.connect(self.store_path)) as connection:
            connection.execute(
                """
                INSERT INTO products
                    (id, name, category, price, description, is_organic, currency)
                VALUES (4, 'Green Tea', 'Tea', 3.25, 'Tea', 0, 'USD')
                """
            )
            connection.commit()

        self.client.post(
            "/api/cart/items",
            json={"product_id": 1, "quantity": 2},
            cookies=self._cookies(self.owner_a),
        )
        self.client.post(
            "/api/cart/items",
            json={"product_id": 4, "quantity": 3},
            cookies=self._cookies(self.owner_a),
        )
        placed = self.client.post(
            "/api/orders",
            cookies=self._cookies(self.owner_a),
        )
        self.assertEqual(placed.status_code, 201)
        order = placed.json()["order"]
        self.assertEqual(order["total_minor"], 3973)
        self.assertEqual(
            {
                item["product_name"]: item["quantity"]
                for item in order["items"]
            },
            {"Organic Raw Honey": 2, "Green Tea": 3},
        )

        history = self.client.get(
            "/api/orders",
            cookies=self._cookies(self.owner_a),
        ).json()["orders"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["total_minor"], 3973)
        self.assertEqual(
            {
                item["product_name"]: item["quantity"]
                for item in history[0]["items"]
            },
            {"Organic Raw Honey": 2, "Green Tea": 3},
        )

    def test_failed_order_write_keeps_cart_and_saves_no_order(self):
        self.client.post(
            "/api/cart/items",
            json={"product_id": 1, "quantity": 2},
            cookies=self._cookies(self.owner_a),
        )
        with closing(sqlite3.connect(self.store_path)) as connection:
            connection.execute(
                """
                CREATE TRIGGER reject_demo_order
                BEFORE INSERT ON orders
                WHEN NEW.order_reference IS NOT NULL
                BEGIN
                    SELECT RAISE(ABORT, 'injected order write failure');
                END
                """
            )
            connection.commit()

        response = self.client.post(
            "/api/orders",
            cookies=self._cookies(self.owner_a),
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            self.client.get(
                "/api/cart",
                cookies=self._cookies(self.owner_a),
            ).json()["items"][0]["quantity"],
            2,
        )
        with closing(sqlite3.connect(self.store_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM orders WHERE owner_id = ?",
                    (self.owner_a,),
                ).fetchone()[0],
                0,
            )

    def test_unknown_currency_is_not_invented_for_order_total(self):
        self.client.post(
            "/api/cart/items",
            json={"product_id": 3, "quantity": 1},
            cookies=self._cookies(self.owner_a),
        )
        response = self.client.post(
            "/api/orders",
            cookies=self._cookies(self.owner_a),
        )
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.json()["order"]["currency"])
        self.assertIsNone(response.json()["order"]["total_minor"])


class CartApiTests(CartApiFixture):

    def test_all_cart_endpoints_reject_unauthenticated_requests(self):
        responses = [
            self.client.get("/api/cart"),
            self.client.post(
                "/api/cart/items",
                json={"product_id": 1, "quantity": 1},
            ),
            self.client.patch("/api/cart/items/1", json={"quantity": 2}),
            self.client.delete("/api/cart/items/1"),
            self.client.delete("/api/cart"),
        ]
        self.assertEqual([response.status_code for response in responses], [401] * 5)

    def test_cart_mutation_methods_are_allowed_for_configured_frontend_origin(self):
        response = self.client.options(
            "/api/cart/items/1",
            headers={
                "Origin": "http://localhost:5174",
                "Access-Control-Request-Method": "PATCH",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("PATCH", response.headers["access-control-allow-methods"])

    def test_add_update_remove_and_clear_cart(self):
        added = self.client.post(
            "/api/cart/items",
            json={"product_id": 1, "quantity": 2},
            cookies=self._cookies(self.owner_a),
        )
        self.assertEqual(added.status_code, 200)
        self.assertEqual(added.json()["items"][0]["quantity"], 2)
        self.assertEqual(added.json()["items"][0]["subtotal"], 29.98)
        self.assertEqual(added.json()["items"][0]["currency"], "USD")
        self.assertEqual(added.json()["subtotal"], 29.98)
        self.assertEqual(added.json()["currency"], "USD")

        updated = self.client.patch(
            "/api/cart/items/1",
            json={"quantity": 3},
            cookies=self._cookies(self.owner_a),
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["items"][0]["quantity"], 3)
        self.assertEqual(updated.json()["subtotal"], 44.97)

        removed = self.client.delete(
            "/api/cart/items/1",
            cookies=self._cookies(self.owner_a),
        )
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(
            removed.json(),
            {"items": [], "subtotal": 0.0, "currency": None, "item_count": 0},
        )

        self.client.post(
            "/api/cart/items",
            json={"product_id": 1, "quantity": 1},
            cookies=self._cookies(self.owner_a),
        )
        cleared = self.client.delete(
            "/api/cart",
            cookies=self._cookies(self.owner_a),
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.json()["items"], [])

    def test_cart_is_persistent_and_isolated_by_owner(self):
        added = self.client.post(
            "/api/cart/items",
            json={"product_id": 2, "quantity": 2},
            cookies=self._cookies(self.owner_a),
        )
        self.assertEqual(added.status_code, 200)

        owner_a_cart = self.client.get(
            "/api/cart",
            cookies=self._cookies(self.owner_a),
        ).json()
        owner_b_cart = self.client.get(
            "/api/cart",
            cookies=self._cookies(self.owner_b),
        ).json()
        self.assertEqual(owner_a_cart["items"][0]["quantity"], 2)
        self.assertEqual(owner_a_cart["subtotal"], 25.98)
        self.assertEqual(
            owner_b_cart,
            {"items": [], "subtotal": 0.0, "currency": None, "item_count": 0},
        )

        denied_remove = self.client.delete(
            "/api/cart/items/2",
            cookies=self._cookies(self.owner_b),
        )
        self.assertEqual(denied_remove.status_code, 404)
        self.assertEqual(
            self.client.get(
                "/api/cart",
                cookies=self._cookies(self.owner_a),
            ).json()["items"][0]["quantity"],
            2,
        )
        self.assertEqual(
            self.client.delete(
                "/api/cart",
                cookies=self._cookies(self.owner_b),
            ).json()["items"],
            [],
        )
        self.assertEqual(
            self.client.get(
                "/api/cart",
                cookies=self._cookies(self.owner_a),
            ).json()["items"][0]["quantity"],
            2,
        )

    def test_concurrent_adds_do_not_overwrite_cart_quantities(self):
        headers = {"Cookie": f"{main.SESSION_COOKIE_NAME}={self.owner_a}"}

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(
                lambda _index: self.client.post(
                    "/api/cart/items",
                    json={"product_id": 1, "quantity": 1},
                    headers=headers,
                ),
                range(2),
            ))

        self.assertEqual([response.status_code for response in responses], [200, 200])
        cart = self.client.get(
            "/api/cart",
            headers=headers,
        ).json()
        self.assertEqual(cart["items"][0]["quantity"], 2)
        self.assertEqual(cart["subtotal"], 29.98)

    def test_invalid_requests_and_missing_products_are_rejected(self):
        for quantity in (0, -1, True, 1.5, "2"):
            response = self.client.post(
                "/api/cart/items",
                json={"product_id": 1, "quantity": quantity},
                cookies=self._cookies(self.owner_a),
            )
            self.assertEqual(response.status_code, 422, quantity)

        for product_id in (0, -1, 999):
            response = self.client.post(
                "/api/cart/items",
                json={"product_id": product_id, "quantity": 1},
                cookies=self._cookies(self.owner_a),
            )
            self.assertIn(response.status_code, (404, 422), product_id)

        for quantity in (0, -1, True):
            response = self.client.patch(
                "/api/cart/items/1",
                json={"quantity": quantity},
                cookies=self._cookies(self.owner_a),
            )
            self.assertEqual(response.status_code, 422, quantity)

        for product_id in (0, -1):
            response = self.client.delete(
                f"/api/cart/items/{product_id}",
                cookies=self._cookies(self.owner_a),
            )
            self.assertEqual(response.status_code, 422, product_id)

        extra_owner = self.client.post(
            "/api/cart/items",
            json={"product_id": 1, "quantity": 1, "owner_id": "test-owner-b"},
            cookies=self._cookies(self.owner_a),
        )
        self.assertEqual(extra_owner.status_code, 422)

    def test_existing_product_and_read_only_chat_routes_still_work(self):
        products = self.client.get("/api/products")
        categories = self.client.get("/api/categories")
        self.assertEqual(products.status_code, 200)
        self.assertEqual(products.json()["products"][0]["id"], 1)
        self.assertEqual(categories.status_code, 200)

        read_only_agent = FakeAgent()
        authenticated_agent = FakeAgent("Added Organic Raw Honey.")
        with (
            patch.object(main, "_get_read_only_chat_agent", return_value=read_only_agent) as read_only,
            patch.object(
                main,
                "_get_authenticated_cart_chat_agent",
                return_value=authenticated_agent,
            ) as cart_agent,
        ):
            unauthenticated_chat = self.client.post(
                "/api/chat",
                json={"messages": [{"role": "user", "content": "Add honey to my cart"}]},
            )
            authenticated_chat = self.client.post(
                "/api/chat",
                json={
                    "messages": [{
                        "role": "user",
                        "content": "Add Organic Raw Honey to my cart",
                    }]
                },
                cookies=self._cookies(self.owner_a),
            )

        self.assertEqual(unauthenticated_chat.status_code, 200)
        self.assertEqual(authenticated_chat.status_code, 200)
        read_only.assert_called_once()
        cart_agent.assert_called_once()
        self.assertEqual(cart_agent.call_args.args[0], "test-owner-a")

    def test_unknown_cart_currency_has_no_combined_subtotal(self):
        response = self.client.post(
            "/api/cart/items",
            json={"product_id": 3, "quantity": 2},
            cookies=self._cookies(self.owner_a),
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["currency"])
        self.assertIsNone(response.json()["subtotal"])
        self.assertIsNone(response.json()["items"][0]["currency"])

if __name__ == "__main__":
    unittest.main()
