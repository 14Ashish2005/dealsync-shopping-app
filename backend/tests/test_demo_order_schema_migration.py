import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend.migrations.v005_demo_orders import migrate


class DemoOrderSchemaMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store_path = Path(self.temp_dir.name) / "store.db"

    def test_existing_order_rows_are_preserved_and_migration_is_idempotent(self):
        with closing(sqlite3.connect(self.store_path)) as connection:
            connection.execute(
                "CREATE TABLE products (id INTEGER PRIMARY KEY)"
            )
            connection.execute(
                """
                CREATE TABLE orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id INTEGER NOT NULL,
                    product_name TEXT NOT NULL,
                    price REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO orders (product_id, product_name, price)
                VALUES (1, 'Legacy product', 12.5)
                """
            )
            connection.commit()

        added = migrate(self.store_path)
        self.assertEqual(
            added,
            (
                "ordered_at",
                "owner_id",
                "status",
                "currency",
                "order_reference",
                "quantity",
                "total_minor",
                "order_currency",
            ),
        )
        self.assertEqual(migrate(self.store_path), ())

        with closing(sqlite3.connect(self.store_path)) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(orders)")
            }
            row = connection.execute(
                """
                SELECT product_id, product_name, price, quantity,
                       order_reference, total_minor, order_currency
                FROM orders
                """
            ).fetchone()
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }

        self.assertTrue(
            {
                "owner_id",
                "status",
                "currency",
                "ordered_at",
                "order_reference",
                "quantity",
                "total_minor",
                "order_currency",
            }.issubset(columns)
        )
        self.assertEqual(
            row,
            (1, "Legacy product", 12.5, 1, None, None, None),
        )
        self.assertIn("idx_orders_owner_reference", indexes)

    def test_creates_order_table_when_missing(self):
        with closing(sqlite3.connect(self.store_path)) as connection:
            connection.execute("CREATE TABLE products (id INTEGER PRIMARY KEY)")
            connection.commit()

        migrate(self.store_path)

        with closing(sqlite3.connect(self.store_path)) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(orders)")
            }
            foreign_keys = connection.execute(
                'PRAGMA foreign_key_list("orders")'
            ).fetchall()

        self.assertTrue(
            {"order_reference", "quantity", "total_minor", "order_currency"}
            .issubset(columns)
        )
        self.assertTrue(
            any(row[2:5] == ("products", "product_id", "id") for row in foreign_keys)
        )


if __name__ == "__main__":
    unittest.main()
