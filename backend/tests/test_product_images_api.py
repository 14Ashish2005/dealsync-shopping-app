from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.migrations.v002_product_images import migrate_database
from backend.product_images import (
    category_fallback,
    enrich_product_images,
    validate_image_url,
    validate_local_image_path,
)
from backend.tests._support import (
    import_main_with_temporary_auth_db,
    prepare_test_schemas,
)


main = import_main_with_temporary_auth_db()


class ProductImagesTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "store.db"
        self.auth_path = self.root / "auth_sessions.db"
        self.public_root = self.root / "public" / "images" / "products"
        self.public_root.mkdir(parents=True)
        self._create_database()
        self.original_store_path = main.STORE_DB_PATH
        self.original_auth_path = main.AUTH_DB_PATH
        main.STORE_DB_PATH = self.database_path
        main.AUTH_DB_PATH = self.auth_path
        prepare_test_schemas(self.database_path, self.auth_path)

    @contextmanager
    def _open_database(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def tearDown(self):
        main.STORE_DB_PATH = self.original_store_path
        main.AUTH_DB_PATH = self.original_auth_path
        self.temp_dir.cleanup()

    def _create_database(self):
        with self._open_database() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("""
                CREATE TABLE products (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT,
                    price REAL,
                    description TEXT,
                    is_organic INTEGER DEFAULT 0
                )
            """)
            connection.execute("""
                CREATE TABLE cart_items (
                    owner_id TEXT NOT NULL,
                    product_id INTEGER NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    PRIMARY KEY (owner_id, product_id),
                    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
                )
            """)
            connection.executemany(
                "INSERT INTO products VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (1, "Organic Coffee", "coffee", 5.25, "Ground coffee", 1),
                    (2, "Unlisted product", "unlisted", 3.00, "Catalog item", 0),
                ],
            )
            connection.execute(
                "INSERT INTO cart_items VALUES ('owner-a', 1, 2)"
            )

    def test_api_preserves_product_fields_and_adds_category_fallbacks(self):
        with patch.object(
            main,
            "create_vision_analyzer",
            side_effect=ValueError("vision is not used by this test"),
        ):
            with TestClient(main.app) as client:
                response = client.get("/api/products")

        self.assertEqual(response.status_code, 200)
        result = response.json()["products"]
        self.assertEqual(len(result), 2)
        products_by_id = {product["id"]: product for product in result}
        self.assertEqual(products_by_id[1]["name"], "Organic Coffee")
        self.assertEqual(products_by_id[1]["category"], "coffee")
        self.assertEqual(products_by_id[1]["price"], 5.25)
        self.assertEqual(products_by_id[1]["description"], "Ground coffee")
        self.assertTrue(products_by_id[1]["is_organic"])
        self.assertIsNone(products_by_id[1]["currency"])
        self.assertIsNone(products_by_id[1]["image_url"])
        self.assertIsNone(products_by_id[1]["local_image_url"])
        self.assertEqual(
            products_by_id[1]["fallback_image"],
            "/images/products/categories/coffee.svg",
        )
        self.assertEqual(products_by_id[2]["name"], "Unlisted product")
        self.assertEqual(
            products_by_id[2]["fallback_image"],
            "/images/products/categories/default.svg",
        )

    def test_api_reads_optional_image_metadata_without_losing_search_results(self):
        migrate_database(self.database_path)
        local_image = self.public_root / "catalog" / "coffee.svg"
        local_image.parent.mkdir()
        local_image.write_text("<svg/>", encoding="utf-8")
        with self._open_database() as connection:
            connection.execute(
                "INSERT INTO product_images (product_id, image_url, local_image_path) "
                "VALUES (?, ?, ?)",
                (
                    1,
                    "https://cdn.example.test/coffee.jpg",
                    "/images/products/catalog/coffee.svg",
                ),
            )

        with patch.object(
            main,
            "create_vision_analyzer",
            side_effect=ValueError("vision is not used by this test"),
        ):
            with patch("backend.product_images.PRODUCTS_PUBLIC_DIR", self.public_root):
                with TestClient(main.app) as client:
                    response = client.get("/api/products", params={"query": "coffee"})

        self.assertEqual(response.status_code, 200)
        result = response.json()["products"]
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Organic Coffee")
        self.assertEqual(result[0]["image_url"], "https://cdn.example.test/coffee.jpg")
        self.assertEqual(
            result[0]["local_image_url"],
            "/images/products/catalog/coffee.svg",
        )

    def test_https_is_accepted_and_unsafe_url_schemes_are_rejected(self):
        self.assertEqual(
            validate_image_url("https://cdn.example.test/image.jpg"),
            "https://cdn.example.test/image.jpg",
        )
        for value in (
            "http://cdn.example.test/image.jpg",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "https://user:password@cdn.example.test/image.jpg",
            "not a URL",
        ):
            with self.subTest(value=value):
                self.assertIsNone(validate_image_url(value))

    def test_local_image_paths_must_resolve_inside_the_image_directory(self):
        image_path = self.public_root / "catalog" / "product.svg"
        image_path.parent.mkdir()
        image_path.write_text("<svg/>", encoding="utf-8")
        with patch("backend.product_images.PRODUCTS_PUBLIC_DIR", self.public_root):
            self.assertEqual(
                validate_local_image_path(
                    "/images/products/catalog/product.svg",
                    self.public_root,
                ),
                "/images/products/catalog/product.svg",
            )
            self.assertIsNone(
                validate_local_image_path("/images/products/missing.svg", self.public_root)
            )
            self.assertIsNone(
                validate_local_image_path("/images/products/../outside.svg", self.public_root)
            )
            self.assertIsNone(
                validate_local_image_path("C:/private/image.svg", self.public_root)
            )
            self.assertIsNone(
                validate_local_image_path(
                    "/images/products/catalog/bad name.svg",
                    self.public_root,
                )
            )

    def test_unknown_categories_receive_generic_fallback(self):
        self.assertEqual(
            category_fallback("new-category"),
            "/images/products/categories/default.svg",
        )
        self.assertEqual(
            category_fallback(None),
            "/images/products/categories/default.svg",
        )

    def test_migration_is_idempotent_and_preserves_products_and_cart(self):
        with self._open_database() as connection:
            products_before = connection.execute(
                "SELECT id, name, category, price FROM products ORDER BY id"
            ).fetchall()
            cart_before = connection.execute(
                "SELECT owner_id, product_id, quantity FROM cart_items ORDER BY owner_id, product_id"
            ).fetchall()

        first_result = migrate_database(self.database_path)
        second_result = migrate_database(self.database_path)
        self.assertEqual(first_result, second_result)
        self.assertEqual(first_result.product_count, 2)
        self.assertEqual(first_result.cart_item_count, 1)
        self.assertEqual(first_result.product_image_count, 0)
        self.assertEqual(first_result.foreign_key_violations, 0)

        with self._open_database() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT id, name, category, price FROM products ORDER BY id"
                ).fetchall(),
                products_before,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT owner_id, product_id, quantity FROM cart_items ORDER BY owner_id, product_id"
                ).fetchall(),
                cart_before,
            )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_migration_image_table_enforces_product_foreign_key(self):
        migrate_database(self.database_path)
        with self._open_database() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO product_images (product_id, image_url) VALUES (999, ?)",
                    ("https://cdn.example.test/orphan.jpg",),
                )
            connection.execute(
                "INSERT INTO product_images (product_id, image_url) VALUES (1, ?)",
                ("https://cdn.example.test/coffee.jpg",),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM products WHERE id = 1")

    def test_invalid_database_image_records_are_returned_as_null(self):
        migrate_database(self.database_path)
        with self._open_database() as connection:
            connection.execute(
                "INSERT INTO product_images (product_id, image_url, local_image_path) "
                "VALUES (?, ?, ?)",
                (1, "http://insecure.example/image.jpg", "/images/products/../bad.svg"),
            )

        with patch("backend.product_images.PRODUCTS_PUBLIC_DIR", self.public_root):
            result = enrich_product_images(
                [{"id": 1, "category": "coffee"}],
                self.database_path,
            )
        self.assertIsNone(result[0]["image_url"])
        self.assertIsNone(result[0]["local_image_url"])


if __name__ == "__main__":
    unittest.main()
