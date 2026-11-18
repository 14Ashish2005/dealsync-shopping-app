import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from backend.currency import (
    format_major_amount,
    is_unverified_seed_currency,
    to_minor_units,
    validate_currency_code,
    validate_new_product_currency,
)
from backend.migrations import v003_currency_metadata as migration
from backend.providers.local_catalog import LocalCatalogAdapter
from setup_db import insert_product


class CurrencyMetadataMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "currency.sqlite"
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                """
                CREATE TABLE products (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT,
                    price REAL,
                    description TEXT,
                    is_organic INTEGER DEFAULT 0
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id INTEGER NOT NULL,
                    product_name TEXT NOT NULL,
                    price REAL NOT NULL,
                    ordered_at TEXT NOT NULL DEFAULT (datetime('now')),
                    owner_id TEXT,
                    status TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO products (id, name, category, price, description, is_organic)
                VALUES (1, 'Seed item', 'honey', 14.99, 'Delicious seed honey', 1)
                """
            )
            connection.execute(
                """
                INSERT INTO orders (id, product_id, product_name, price, owner_id, status)
                VALUES (9, 1, 'Seed item', 12.99, 'test-owner', 'Delivered')
                """
            )
            connection.commit()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_migration_preserves_amounts_and_leaves_existing_currency_unknown(self):
        result = migration.migrate(self.db_path)

        self.assertEqual(result.columns_added, ("products", "orders"))
        self.assertEqual(result.unknown_currency_rows, {"products": 1, "orders": 1})
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT id, name, price, currency FROM products"
                ).fetchall(),
                [(1, "Seed item", 14.99, None)],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT id, product_id, price, currency FROM orders"
                ).fetchall(),
                [(9, 1, 12.99, None)],
            )

    def test_migration_rerun_is_idempotent(self):
        migration.migrate(self.db_path)
        result = migration.migrate(self.db_path)

        self.assertEqual(result.columns_added, ())
        self.assertEqual(result.unknown_currency_rows, {"products": 1, "orders": 1})

    def test_migration_rolls_back_partial_schema_change_on_conflict(self):
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "ALTER TABLE products ADD COLUMN currency TEXT"
            )
            connection.execute(
                "UPDATE products SET currency = 'bad' WHERE id = 1"
            )
            connection.commit()

        with self.assertRaisesRegex(RuntimeError, "malformed currency"):
            migration.migrate(self.db_path)

        with closing(sqlite3.connect(self.db_path)) as connection:
            product_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(products)")
            }
            order_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(orders)")
            }
            self.assertIn("currency", product_columns)
            self.assertNotIn("currency", order_columns)
            self.assertEqual(
                connection.execute("SELECT price, currency FROM products").fetchone(),
                (14.99, "bad"),
            )

    @unittest.skipIf(
        sqlite3.sqlite_version_info < (3, 35, 0),
        "SQLite does not support DROP COLUMN.",
    )
    def test_rollback_drops_only_empty_currency_metadata(self):
        migration.migrate(self.db_path)

        self.assertEqual(
            migration.rollback(self.db_path),
            ("products", "orders"),
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            for table in ("products", "orders"):
                self.assertNotIn(
                    "currency",
                    {
                        row[1]
                        for row in connection.execute(f'PRAGMA table_info("{table}")')
                    },
                )
            self.assertEqual(
                connection.execute("SELECT price FROM products").fetchone()[0],
                14.99,
            )

    def test_rollback_refuses_to_drop_recorded_currency(self):
        migration.migrate(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("UPDATE products SET currency = 'USD' WHERE id = 1")
            connection.commit()

        with self.assertRaisesRegex(RuntimeError, "containing values"):
            migration.rollback(self.db_path)

        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT price, currency FROM products WHERE id = 1"
                ).fetchone(),
                (14.99, "USD"),
            )
            self.assertIn(
                "currency",
                {
                    row[1] for row in connection.execute("PRAGMA table_info(orders)")
                },
            )

    def test_new_price_normalization_requires_supported_explicit_currency(self):
        self.assertEqual(validate_currency_code("INR"), "INR")
        self.assertEqual(validate_currency_code("USD"), "USD")
        self.assertEqual(to_minor_units("14.99", "USD"), 1499)
        self.assertEqual(to_minor_units("14.99", "INR"), 1499)

        for invalid_currency in (None, "", "inr", "XYZ"):
            with self.subTest(currency=invalid_currency):
                with self.assertRaises(ValueError):
                    validate_currency_code(invalid_currency)

    def test_option1_policy_enforcement_and_new_product_writes(self):
        migration.migrate(self.db_path)
        self.assertTrue(is_unverified_seed_currency(None))
        self.assertFalse(is_unverified_seed_currency("USD"))
        self.assertFalse(is_unverified_seed_currency("INR"))

        with self.assertRaisesRegex(ValueError, "require an explicit valid ISO 4217"):
            validate_new_product_currency(None)

        with self.assertRaises(ValueError):
            validate_new_product_currency("invalid")

        self.assertEqual(validate_new_product_currency("USD"), "USD")
        self.assertEqual(validate_new_product_currency("INR"), "INR")

        with closing(sqlite3.connect(self.db_path)) as connection:
            with self.assertRaises(ValueError):
                insert_product(
                    connection,
                    name="Invalid New Product",
                    category="snacks",
                    price=5.99,
                    description="No currency",
                    currency=None,
                )

            new_id = insert_product(
                connection,
                name="Valid USD Item",
                category="snacks",
                price=6.99,
                description="Valid USD",
                currency="USD",
            )
            row = connection.execute(
                "SELECT id, name, price, currency FROM products WHERE id = ?",
                (new_id,),
            ).fetchone()
            self.assertEqual(row, (new_id, "Valid USD Item", 6.99, "USD"))

            # Verify existing seed item remains untouched with NULL currency
            seed_row = connection.execute(
                "SELECT id, name, price, currency FROM products WHERE id = 1"
            ).fetchone()
            self.assertEqual(seed_row, (1, "Seed item", 14.99, None))

    def test_provider_normalization_with_null_and_explicit_currencies(self):
        migration.migrate(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                """
                INSERT INTO products (id, name, category, price, description, is_organic, currency)
                VALUES (2, 'USD Product', 'honey', 14.99, 'USD Honey', 0, 'USD'),
                       (3, 'INR Product', 'honey', 14.99, 'INR Honey', 0, 'INR')
                """
            )
            connection.commit()

        adapter = LocalCatalogAdapter(self.db_path)

        # Normalization of seed product (NULL currency)
        null_offer = adapter.normalize_offer({"id": 1, "name": "Seed item", "price": 14.99, "currency": None})
        self.assertIsNone(null_offer.currency)
        self.assertIsNone(null_offer.price_minor)

        # Normalization of USD product
        usd_offer = adapter.normalize_offer({"id": 2, "name": "USD Product", "price": 14.99, "currency": "USD"})
        self.assertEqual(usd_offer.currency, "USD")
        self.assertEqual(usd_offer.price_minor, 1499)

        # Normalization of INR product
        inr_offer = adapter.normalize_offer({"id": 3, "name": "INR Product", "price": 14.99, "currency": "INR"})
        self.assertEqual(inr_offer.currency, "INR")
        self.assertEqual(inr_offer.price_minor, 1499)

        # Search without price filter returns all 3 offers
        all_offers = adapter.search()
        self.assertEqual(len(all_offers), 3)

        # Search with INR price filter must exclude NULL currency and USD items
        inr_filtered = adapter.search(max_price=20.0, currency="INR")
        self.assertEqual(len(inr_filtered), 1)
        self.assertEqual(inr_filtered[0].provider_product_id, "3")
        self.assertEqual(inr_filtered[0].currency, "INR")

        # Search with USD price filter must exclude NULL currency and INR items
        usd_filtered = adapter.search(max_price=20.0, currency="USD")
        self.assertEqual(len(usd_filtered), 1)
        self.assertEqual(usd_filtered[0].provider_product_id, "2")
        self.assertEqual(usd_filtered[0].currency, "USD")

    def test_cart_calculations_with_null_inr_usd_and_mixed(self):
        from backend.tests._support import import_main_with_temporary_auth_db

        _cart_response = import_main_with_temporary_auth_db()._cart_response

        migration.migrate(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cart_items (
                    owner_id TEXT NOT NULL,
                    product_id INTEGER NOT NULL,
                    quantity INTEGER NOT NULL,
                    PRIMARY KEY (owner_id, product_id)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO products (id, name, category, price, description, is_organic, currency)
                VALUES (2, 'USD Product', 'honey', 10.00, 'USD Honey', 0, 'USD'),
                       (3, 'INR Product', 'honey', 50.00, 'INR Honey', 0, 'INR')
                """
            )
            connection.commit()

            # 1. Cart with NULL currency only (Seed item 1)
            connection.execute("INSERT INTO cart_items VALUES ('user-null', 1, 2)")
            connection.commit()
            res_null = _cart_response(connection, "user-null")
            self.assertIsNone(res_null["currency"])
            self.assertIsNone(res_null["subtotal"])
            self.assertEqual(res_null["items"][0]["price"], 14.99)
            self.assertIsNone(res_null["items"][0]["currency"])

            # 2. Cart with homogeneous USD only (Product 2)
            connection.execute("INSERT INTO cart_items VALUES ('user-usd', 2, 3)")
            connection.commit()
            res_usd = _cart_response(connection, "user-usd")
            self.assertEqual(res_usd["currency"], "USD")
            self.assertEqual(res_usd["subtotal"], 30.00)

            # 3. Cart with homogeneous INR only (Product 3)
            connection.execute("INSERT INTO cart_items VALUES ('user-inr', 3, 2)")
            connection.commit()
            res_inr = _cart_response(connection, "user-inr")
            self.assertEqual(res_inr["currency"], "INR")
            self.assertEqual(res_inr["subtotal"], 100.00)

            # 4. Cart with mixed currencies (USD + INR)
            connection.execute("INSERT INTO cart_items VALUES ('user-mixed', 2, 1), ('user-mixed', 3, 1)")
            connection.commit()
            res_mixed = _cart_response(connection, "user-mixed")
            self.assertIsNone(res_mixed["currency"])
            self.assertIsNone(res_mixed["subtotal"])

            # 5. Cart with mixed currencies (USD + NULL)
            connection.execute("INSERT INTO cart_items VALUES ('user-mixed-null', 1, 1), ('user-mixed-null', 2, 1)")
            connection.commit()
            res_mixed_null = _cart_response(connection, "user-mixed-null")
            self.assertIsNone(res_mixed_null["currency"])
            self.assertIsNone(res_mixed_null["subtotal"])

    def test_shopping_agent_tools_with_currency_defensiveness(self):
        import shopping_agent

        migration.migrate(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                """
                INSERT INTO products (id, name, price, currency, category, description, is_organic)
                VALUES (2, 'Organic Tea USD', 10.00, 'USD', 'tea', 'Nice tea', 1),
                       (3, 'Chai INR', 50.00, 'INR', 'tea', 'Indian tea', 0)
                """
            )
            connection.commit()

        original_db_path = shopping_agent.DB_PATH
        shopping_agent.DB_PATH = str(self.db_path)
        try:
            # 1. search_products without price filter returns all items with their respective currency
            results = json.loads(shopping_agent.search_products.invoke({"query": "tea"}))
            self.assertEqual(len(results), 2)
            currencies = {p["name"]: p["currency"] for p in results}
            self.assertEqual(currencies["Organic Tea USD"], "USD")
            self.assertEqual(currencies["Chai INR"], "INR")

            # 2. search_products with max_price but no currency raises ValueError
            with self.assertRaisesRegex(ValueError, "A currency code is required"):
                shopping_agent.search_products.invoke({"query": "tea", "max_price": 20.0})

            # 3. search_products with max_price and explicit USD currency filters only USD items
            usd_results = json.loads(shopping_agent.search_products.invoke({
                "query": "tea",
                "max_price": 20.0,
                "currency": "USD",
            }))
            self.assertEqual(len(usd_results), 1)
            self.assertEqual(usd_results[0]["name"], "Organic Tea USD")

            # 4. view_cart with unverified seed item reports cart total as unverified
            with (
                shopping_agent.use_order_owner("test-owner"),
                shopping_agent.use_cart({1: 1}),
            ):
                cart_view = shopping_agent.view_cart.invoke({})
                self.assertIn("currency is unverified or mixed", cart_view)

            # 5. checkout_cart snapshots the currency into orders table
            with (
                shopping_agent.use_order_owner("test-owner"),
                shopping_agent.use_cart({1: 1, 2: 1}),
            ):
                confirmation = shopping_agent.checkout_cart.invoke({})
                self.assertIn("Order confirmed", confirmation)

            with closing(sqlite3.connect(self.db_path)) as connection:
                orders = connection.execute(
                    "SELECT product_id, price, currency FROM orders ORDER BY id"
                ).fetchall()
                # Order 9 was in setUp, orders for product 1 and 2 were added
                self.assertIn((1, 14.99, None), orders)
                self.assertIn((2, 10.0, "USD"), orders)
        finally:
            shopping_agent.DB_PATH = original_db_path


if __name__ == "__main__":
    unittest.main()
