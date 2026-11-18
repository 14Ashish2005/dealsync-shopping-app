import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

from backend.migrations import v001_product_offers as migration


CORE_TABLES = ("products", "reviews", "orders", "cart_items")


class ProductOffersMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "legacy-fixture.sqlite"
        self._create_legacy_database()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_legacy_database(self):
        with closing(sqlite3.connect(self.db_path, timeout=30)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript("""
                CREATE TABLE products (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT,
                    price REAL,
                    description TEXT,
                    is_organic INTEGER DEFAULT 0,
                    currency TEXT CHECK (
                        currency IS NULL OR currency GLOB '[A-Z][A-Z][A-Z]'
                    )
                );
                CREATE TABLE reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id INTEGER,
                    rating REAL,
                    reviewer_name TEXT,
                    review_text TEXT,
                    FOREIGN KEY (product_id) REFERENCES products(id)
                );
                CREATE TABLE orders (
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
                );
                CREATE TABLE cart_items (
                    owner_id TEXT NOT NULL,
                    product_id INTEGER NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    PRIMARY KEY (owner_id, product_id),
                    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
                );
            """)
            products = [
                (
                    product_id,
                    f"Synthetic legacy product {product_id}",
                    "test",
                    float(product_id),
                    "Synthetic migration fixture product.",
                    0,
                    None,
                )
                for product_id in range(1, 33)
            ]
            connection.executemany(
                """
                INSERT INTO products
                    (id, name, category, price, description, is_organic, currency)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                products,
            )
            connection.execute(
                """
                INSERT INTO reviews
                    (product_id, rating, reviewer_name, review_text)
                VALUES (?, ?, ?, ?)
                """,
                (
                    1,
                    4.5,
                    "Synthetic fixture reviewer",
                    "Synthetic migration fixture review.",
                ),
            )
            connection.execute(
                """
                INSERT INTO orders
                    (product_id, product_name, price, ordered_at, owner_id, status, currency)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    2,
                    "Synthetic legacy product 2",
                    2.0,
                    "2026-01-01T00:00:00Z",
                    "synthetic-order-owner",
                    "test",
                    None,
                ),
            )
            connection.execute(
                "INSERT INTO cart_items (owner_id, product_id, quantity) VALUES (?, ?, ?)",
                ("synthetic-cart-owner", 1, 2),
            )
            connection.commit()

    @contextmanager
    def _open_db(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    def _product_and_offer_ids(self):
        with self._open_db() as connection:
            return (
                connection.execute(
                    """
                    SELECT legacy_product_id, canonical_product_id
                    FROM canonical_products ORDER BY legacy_product_id
                    """
                ).fetchall(),
                connection.execute(
                    """
                    SELECT provider_offer_key, offer_id
                    FROM product_offers
                    WHERE provider_key = ?
                    ORDER BY provider_offer_key
                    """,
                    (migration.LOCAL_PROVIDER_KEY,),
                ).fetchall(),
            )

    def test_fresh_migration_maps_products_and_preserves_existing_tables(self):
        with self._open_db() as connection:
            before = {
                table: connection.execute(
                    f'SELECT * FROM "{table}" ORDER BY rowid'
                ).fetchall()
                for table in CORE_TABLES
            }
        self.assertTrue(all(before[table] for table in CORE_TABLES))

        result = migration.migrate(self.db_path)

        self.assertEqual(result.provider_key, "local_catalog")
        self.assertEqual(result.canonical_products, 32)
        self.assertEqual(result.local_offers, 32)
        self.assertEqual(result.canonical_products_created, 32)
        self.assertEqual(result.local_offers_created, 32)
        self.assertEqual(result.prices_verified, 0)
        self.assertEqual(result.foreign_key_violations, 0)

        with self._open_db() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT display_name, enabled FROM product_providers WHERE provider_key = ?",
                    ("local_catalog",),
                ).fetchone(),
                ("Local catalog", 1),
            )
            after = {
                table: connection.execute(
                    f'SELECT * FROM "{table}" ORDER BY rowid'
                ).fetchall()
                for table in CORE_TABLES
            }
            self.assertEqual(after, before)

            products = connection.execute(
                """
                SELECT p.id, p.name, p.price, c.legacy_product_id,
                       c.canonical_product_id, o.provider_offer_key,
                       o.provider_product_id, o.canonical_product_id,
                       o.product_name, o.price_minor, o.currency,
                       o.brand, o.model, o.product_url, o.image_url,
                       o.availability, o.last_checked_at, o.expires_at
                FROM products AS p
                JOIN canonical_products AS c ON c.legacy_product_id = p.id
                JOIN product_offers AS o
                    ON o.canonical_product_id = c.canonical_product_id
                WHERE o.provider_key = ?
                ORDER BY p.id
                """,
                ("local_catalog",),
            ).fetchall()
            self.assertEqual(len(products), 32)
            for row in products:
                (
                    product_id, name, price, legacy_id, canonical_id,
                    offer_key, provider_product_id, offer_canonical_id,
                    offer_name, price_minor, currency, brand, model,
                    product_url, image_url, availability, checked_at, expires_at,
                ) = row
                self.assertEqual(legacy_id, product_id)
                self.assertEqual(offer_key, f"local-product:{product_id}")
                self.assertEqual(provider_product_id, str(product_id))
                self.assertEqual(offer_canonical_id, canonical_id)
                self.assertEqual(offer_name, name)
                self.assertIsNone(price_minor)
                self.assertIsNone(currency)
                self.assertIsNone(brand)
                self.assertIsNone(model)
                self.assertIsNone(product_url)
                self.assertIsNone(image_url)
                self.assertEqual(availability, "unknown")
                self.assertIsNotNone(checked_at)
                self.assertIsNone(expires_at)

            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(),
                [],
            )

    def test_rerun_preserves_canonical_and_offer_uuids(self):
        first = migration.migrate(self.db_path)
        identifiers_before = self._product_and_offer_ids()
        with self._open_db() as connection:
            offer_data_before = connection.execute(
                """
                SELECT offer_id, provider_product_id, canonical_product_id,
                       product_name, brand, model, price_minor, currency,
                       product_url, image_url, availability, last_checked_at,
                       expires_at, created_at, updated_at
                FROM product_offers ORDER BY offer_id
                """
            ).fetchall()
            canonical_data_before = connection.execute(
                """
                SELECT canonical_product_id, legacy_product_id, name, brand,
                       model, gtin, created_at, updated_at
                FROM canonical_products ORDER BY canonical_product_id
                """
            ).fetchall()
        second = migration.migrate(self.db_path)
        identifiers_after = self._product_and_offer_ids()

        self.assertEqual(identifiers_after, identifiers_before)
        with self._open_db() as connection:
            self.assertEqual(
                connection.execute(
                    """
                    SELECT offer_id, provider_product_id, canonical_product_id,
                           product_name, brand, model, price_minor, currency,
                           product_url, image_url, availability, last_checked_at,
                           expires_at, created_at, updated_at
                    FROM product_offers ORDER BY offer_id
                    """
                ).fetchall(),
                offer_data_before,
            )
            self.assertEqual(
                connection.execute(
                    """
                    SELECT canonical_product_id, legacy_product_id, name, brand,
                           model, gtin, created_at, updated_at
                    FROM canonical_products ORDER BY canonical_product_id
                    """
                ).fetchall(),
                canonical_data_before,
            )
        self.assertEqual(first.canonical_products_created, 32)
        self.assertEqual(first.local_offers_created, 32)
        self.assertEqual(second.canonical_products_created, 0)
        self.assertEqual(second.local_offers_created, 0)
        self.assertEqual(second.canonical_products, 32)
        self.assertEqual(second.local_offers, 32)

    def test_conflicting_local_provider_configuration_is_preserved_and_rejected(self):
        migration.migrate(self.db_path)
        with self._open_db() as connection:
            connection.execute(
                """
                UPDATE product_providers
                SET display_name = ?, terms_url = ?, enabled = 0
                WHERE provider_key = ?
                """,
                (
                    "Managed catalog",
                    "https://example.test/terms",
                    migration.LOCAL_PROVIDER_KEY,
                ),
            )
            connection.commit()
            provider_before = connection.execute(
                """
                SELECT display_name, terms_url, enabled
                FROM product_providers WHERE provider_key = ?
                """,
                (migration.LOCAL_PROVIDER_KEY,),
            ).fetchone()

        with self.assertRaisesRegex(RuntimeError, "Provider configuration"):
            migration.migrate(self.db_path)

        with self._open_db() as connection:
            self.assertEqual(
                connection.execute(
                    """
                    SELECT display_name, terms_url, enabled
                    FROM product_providers WHERE provider_key = ?
                    """,
                    (migration.LOCAL_PROVIDER_KEY,),
                ).fetchone(),
                provider_before,
            )

    def test_conflicting_canonical_product_mapping_fails_without_replacing_it(self):
        migration.migrate(self.db_path)
        with self._open_db() as connection:
            canonical_two = connection.execute(
                """
                SELECT canonical_product_id
                FROM canonical_products WHERE legacy_product_id = 2
                """
            ).fetchone()[0]
            connection.execute(
                """
                UPDATE product_offers SET canonical_product_id = ?
                WHERE provider_key = ? AND provider_offer_key = ?
                """,
                (
                    canonical_two,
                    migration.LOCAL_PROVIDER_KEY,
                    "local-product:1",
                ),
            )
            connection.commit()
            offer_before = connection.execute(
                """
                SELECT offer_id, provider_product_id, canonical_product_id,
                       product_name, price_minor, product_url, image_url
                FROM product_offers
                WHERE provider_key = ? AND provider_offer_key = ?
                """,
                (migration.LOCAL_PROVIDER_KEY, "local-product:1"),
            ).fetchone()

        with self.assertRaisesRegex(RuntimeError, "conflicts"):
            migration.migrate(self.db_path)

        with self._open_db() as connection:
            self.assertEqual(
                connection.execute(
                    """
                    SELECT offer_id, provider_product_id, canonical_product_id,
                           product_name, price_minor, product_url, image_url
                    FROM product_offers
                    WHERE provider_key = ? AND provider_offer_key = ?
                    """,
                    (migration.LOCAL_PROVIDER_KEY, "local-product:1"),
                ).fetchone(),
                offer_before,
            )

    def test_conflict_rolls_back_rows_created_earlier_in_the_transaction(self):
        migration.migrate(self.db_path)
        with self._open_db() as connection:
            canonical_three = connection.execute(
                """
                SELECT canonical_product_id
                FROM canonical_products WHERE legacy_product_id = 3
                """
            ).fetchone()[0]
            connection.execute(
                """
                UPDATE product_offers SET canonical_product_id = ?
                WHERE provider_key = ? AND provider_offer_key = ?
                """,
                (
                    canonical_three,
                    migration.LOCAL_PROVIDER_KEY,
                    "local-product:2",
                ),
            )
            connection.execute(
                """
                DELETE FROM product_offers
                WHERE provider_key = ? AND provider_offer_key = ?
                """,
                (migration.LOCAL_PROVIDER_KEY, "local-product:1"),
            )
            connection.execute(
                "DELETE FROM canonical_products WHERE legacy_product_id = 1"
            )
            connection.commit()

        with self.assertRaisesRegex(RuntimeError, "conflicts"):
            migration.migrate(self.db_path)

        with self._open_db() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM canonical_products WHERE legacy_product_id = 1"
                ).fetchone()
            )
            self.assertIsNone(
                connection.execute(
                    """
                    SELECT 1 FROM product_offers
                    WHERE provider_key = ? AND provider_offer_key = ?
                    """,
                    (migration.LOCAL_PROVIDER_KEY, "local-product:1"),
                ).fetchone()
            )
            self.assertEqual(
                connection.execute(
                    """
                    SELECT canonical_product_id FROM product_offers
                    WHERE provider_key = ? AND provider_offer_key = ?
                    """,
                    (migration.LOCAL_PROVIDER_KEY, "local-product:2"),
                ).fetchone()[0],
                connection.execute(
                    """
                    SELECT canonical_product_id FROM canonical_products
                    WHERE legacy_product_id = 3
                    """
                ).fetchone()[0],
            )

    def test_existing_offer_and_canonical_data_are_preserved_on_rerun(self):
        migration.migrate(self.db_path)
        with self._open_db() as connection:
            connection.execute(
                """
                UPDATE canonical_products
                SET name = ?, brand = ?, model = ?, gtin = ?
                WHERE legacy_product_id = 1
                """,
                (
                    "Curated product name",
                    "Curated brand",
                    "Model X",
                    "1234567890123",
                ),
            )
            connection.execute(
                """
                UPDATE product_offers
                SET product_name = ?, brand = ?, model = ?, price_minor = ?,
                    product_url = ?, image_url = ?, availability = ?,
                    last_checked_at = ?, expires_at = ?, updated_at = ?, currency = ?
                WHERE provider_key = ? AND provider_offer_key = ?
                """,
                (
                    "Manually maintained offer",
                    "Offer brand",
                    "Offer model",
                    99999,
                    "https://shop.example.test/product",
                    "https://cdn.example.test/product.jpg",
                    "in_stock",
                    "2026-01-01T00:00:00Z",
                    "2027-01-01T00:00:00Z",
                    "2026-01-02T00:00:00Z",
                    "INR",
                    migration.LOCAL_PROVIDER_KEY,
                    "local-product:1",
                ),
            )
            connection.commit()
            canonical_before = connection.execute(
                """
                SELECT canonical_product_id, legacy_product_id, name, brand,
                       model, gtin, created_at, updated_at
                FROM canonical_products WHERE legacy_product_id = 1
                """
            ).fetchone()
            offer_before = connection.execute(
                """
                SELECT offer_id, provider_key, provider_offer_key,
                       provider_product_id, canonical_product_id, product_name,
                       brand, model, price_minor, currency, product_url, image_url,
                       availability, last_checked_at, expires_at, created_at,
                       updated_at
                FROM product_offers
                WHERE provider_key = ? AND provider_offer_key = ?
                """,
                (migration.LOCAL_PROVIDER_KEY, "local-product:1"),
            ).fetchone()

        result = migration.migrate(self.db_path)

        with self._open_db() as connection:
            self.assertEqual(
                connection.execute(
                    """
                    SELECT canonical_product_id, legacy_product_id, name, brand,
                           model, gtin, created_at, updated_at
                    FROM canonical_products WHERE legacy_product_id = 1
                    """
                ).fetchone(),
                canonical_before,
            )
            self.assertEqual(
                connection.execute(
                    """
                    SELECT offer_id, provider_key, provider_offer_key,
                           provider_product_id, canonical_product_id, product_name,
                           brand, model, price_minor, currency, product_url, image_url,
                           availability, last_checked_at, expires_at, created_at,
                           updated_at
                    FROM product_offers
                    WHERE provider_key = ? AND provider_offer_key = ?
                    """,
                    (migration.LOCAL_PROVIDER_KEY, "local-product:1"),
                ).fetchone(),
                offer_before,
            )
        self.assertEqual(result.canonical_products_created, 0)
        self.assertEqual(result.local_offers_created, 0)

    def test_prices_convert_to_integer_paise_and_invalid_precision_is_rejected(self):
        migration.migrate(self.db_path)
        with self._open_db() as connection:
            products = connection.execute(
                """
                SELECT p.id, p.price, o.price_minor
                FROM products AS p
                JOIN canonical_products AS c ON c.legacy_product_id = p.id
                JOIN product_offers AS o ON o.canonical_product_id = c.canonical_product_id
                WHERE o.provider_key = ?
                ORDER BY p.id
                """,
                ("local_catalog",),
            ).fetchall()
        self.assertEqual(len(products), 32)
        for _, _, stored_minor in products:
            self.assertIsNone(stored_minor)

        self.assertEqual(migration.inr_to_paise(12.34, 1), 1234)
        self.assertEqual(migration.inr_to_paise("0.01", 1), 1)
        self.assertEqual(migration.to_minor_units(12.34, "USD"), 1234)
        for invalid_price in (None, -1, float("nan"), "0.001", "Infinity"):
            with self.subTest(price=invalid_price):
                with self.assertRaises(ValueError):
                    migration.inr_to_paise(invalid_price, 1)

    def test_unique_constraints_and_cart_offer_delete_protection(self):
        migration.migrate(self.db_path)
        with self._open_db() as connection:
            offer = connection.execute(
                """
                SELECT offer_id, provider_key, provider_product_id, product_name,
                       price_minor, currency, availability
                FROM product_offers
                WHERE provider_key = ? AND provider_offer_key = ?
                """,
                (migration.LOCAL_PROVIDER_KEY, "local-product:1"),
            ).fetchone()
            offer_id, provider, provider_product_id, name, price, currency, availability = offer

            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO canonical_products (
                        canonical_product_id, legacy_product_id, name,
                        created_at, updated_at
                    ) VALUES ('duplicate-canonical', 1, 'Duplicate', 'now', 'now')
                    """
                )
            connection.rollback()

            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO product_offers (
                        offer_id, provider_key, provider_offer_key,
                        provider_product_id, product_name, price_minor, currency,
                        availability, created_at, updated_at
                    ) VALUES (
                        'duplicate-offer', ?, 'local-product:1', '1',
                        'Duplicate', 1, 'INR', 'unknown', 'now', 'now'
                    )
                    """,
                    (migration.LOCAL_PROVIDER_KEY,),
                )
            connection.rollback()

            snapshot = (
                "test-owner",
                offer_id,
                1,
                name,
                provider,
                provider_product_id,
                1234,
                "INR",
                None,
                availability,
                "2026-01-01T00:00:00Z",
            )
            insert_cart_offer = """
                INSERT INTO cart_offer_items (
                    owner_id, offer_id, quantity, selected_product_name,
                    selected_provider_key, selected_provider_product_id,
                    selected_price_minor, selected_currency, selected_product_url,
                    selected_availability, added_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            connection.execute(insert_cart_offer, snapshot)
            connection.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(insert_cart_offer, snapshot)
            connection.rollback()
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM cart_offer_items WHERE owner_id = ? AND offer_id = ?",
                    ("test-owner", offer_id),
                ).fetchone()[0],
                1,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM product_offers WHERE offer_id = ?",
                    (offer_id,),
                )
            connection.rollback()
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM cart_offer_items WHERE owner_id = ? AND offer_id = ?",
                    ("test-owner", offer_id),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(),
                [],
            )

    def test_migration_preserves_nonempty_legacy_cart_rows(self):
        with self._open_db() as connection:
            connection.execute(
                "INSERT INTO cart_items (owner_id, product_id, quantity) VALUES (?, ?, ?)",
                ("additional-synthetic-owner", 1, 3),
            )
            connection.commit()
            products_before = connection.execute(
                "SELECT * FROM products ORDER BY id"
            ).fetchall()
            before = connection.execute(
                "SELECT owner_id, product_id, quantity FROM cart_items ORDER BY owner_id, product_id"
            ).fetchall()

        result = migration.migrate(self.db_path)

        with self._open_db() as connection:
            products_after = connection.execute(
                "SELECT * FROM products ORDER BY id"
            ).fetchall()
            after = connection.execute(
                "SELECT owner_id, product_id, quantity FROM cart_items ORDER BY owner_id, product_id"
            ).fetchall()
        self.assertEqual(products_after, products_before)
        self.assertEqual(after, before)
        self.assertEqual(result.preserved_row_counts["cart_items"], len(before))

    def test_provider_conflict_rolls_back_new_schema_objects(self):
        with self._open_db() as connection:
            connection.execute("""
                CREATE TABLE product_providers (
                    provider_key TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
                    terms_url TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
                )
            """)
            connection.execute(
                """
                INSERT INTO product_providers (provider_key, display_name, terms_url, enabled)
                VALUES (?, ?, ?, ?)
                """,
                (migration.LOCAL_PROVIDER_KEY, "Managed catalog", None, 0),
            )
            connection.commit()
            tables_before = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()

        with self.assertRaisesRegex(RuntimeError, "Provider configuration"):
            migration.migrate(self.db_path)

        with self._open_db() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                ).fetchall(),
                tables_before,
            )
            self.assertEqual(
                connection.execute(
                    """
                    SELECT display_name, terms_url, enabled
                    FROM product_providers WHERE provider_key = ?
                    """,
                    (migration.LOCAL_PROVIDER_KEY,),
                ).fetchone(),
                ("Managed catalog", None, 0),
            )

    def test_failed_migration_rolls_back_all_new_schema_and_data(self):
        with self._open_db() as connection:
            before_names = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
            before_products = connection.execute(
                "SELECT * FROM products ORDER BY id"
            ).fetchall()

        with patch.object(
            migration,
            "_upsert_local_offer",
            side_effect=RuntimeError("simulated failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated failure"):
                migration.migrate(self.db_path)

        with self._open_db() as connection:
            after_names = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
            after_products = connection.execute(
                "SELECT * FROM products ORDER BY id"
            ).fetchall()
        self.assertEqual(after_names, before_names)
        self.assertEqual(after_products, before_products)


if __name__ == "__main__":
    unittest.main()
