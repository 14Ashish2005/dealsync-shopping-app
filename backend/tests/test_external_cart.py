import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

from backend.cart import (
    MAX_CART_QUANTITY,
    CartItemNotFoundError,
    ExternalOfferCartService,
    InvalidCartInputError,
    OfferNotEligibleError,
    OfferQuoteMismatchError,
    PriceConfirmationRequiredError,
    ResolvedOfferQuote,
)
from backend.migrations import v001_product_offers
from backend.migrations import v002_external_cart_quotes


LOCAL_TABLES = ("products", "reviews", "orders", "cart_items")


def _timestamp(seconds: int = 0) -> str:
    value = datetime.now(timezone.utc) - timedelta(seconds=60 - seconds)
    return value.replace(microsecond=0).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _timestamp_ago(minutes: int) -> str:
    value = datetime.now(timezone.utc) - timedelta(minutes=minutes, seconds=1)
    return value.replace(microsecond=0).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _create_schema_fixture(
    path: Path,
    *,
    columns_only: bool = False,
    product_offer_primary_key: bool = True,
    cart_primary_key: bool = True,
    product_offer_provider_fk: str | None = "valid",
    cart_offer_fk: str | None = "valid",
) -> None:
    if columns_only:
        columns = {
            "products": {"id"},
            "product_providers": {
                "provider_key", "display_name", "terms_url", "enabled",
            },
            "canonical_products": {
                "canonical_product_id", "legacy_product_id", "name", "brand",
                "model", "gtin", "created_at", "updated_at",
            },
            "product_offers": {
                "offer_id", "provider_key", "provider_offer_key",
                "provider_product_id", "canonical_product_id", "product_name",
                "brand", "model", "price_minor", "currency", "product_url",
                "image_url", "availability", "last_checked_at", "expires_at",
                "created_at", "updated_at",
            },
            "cart_offer_items": {
                "owner_id", "offer_id", "quantity", "selected_product_name",
                "selected_provider_key", "selected_provider_product_id",
                "selected_price_minor", "selected_currency",
                "selected_product_url", "selected_availability", "added_at",
            },
        }
        with closing(sqlite3.connect(path)) as connection:
            for table, names in columns.items():
                definitions = ", ".join(
                    f'"{name}" TEXT' for name in sorted(names)
                )
                connection.execute(f'CREATE TABLE "{table}" ({definitions})')
            connection.commit()
        return

    product_offer_constraints = [
        "UNIQUE (provider_key, provider_offer_key)",
        "CHECK (price_minor IS NULL OR price_minor >= 0)",
        "CHECK (currency IS NULL OR currency GLOB '[A-Z][A-Z][A-Z]')",
        (
            "CHECK ((price_minor IS NULL AND currency IS NULL) OR "
            "(price_minor IS NOT NULL AND currency GLOB '[A-Z][A-Z][A-Z]'))"
        ),
        (
            "CHECK (availability IN "
            "('in_stock', 'out_of_stock', 'preorder', 'backorder', 'unknown'))"
        ),
        (
            "FOREIGN KEY (canonical_product_id) "
            "REFERENCES canonical_products(canonical_product_id) ON DELETE RESTRICT"
        ),
    ]
    if product_offer_primary_key:
        product_offer_constraints.append("PRIMARY KEY (offer_id)")
    if product_offer_provider_fk == "valid":
        product_offer_constraints.append(
            "FOREIGN KEY (provider_key) "
            "REFERENCES product_providers(provider_key) ON DELETE RESTRICT"
        )
    elif product_offer_provider_fk == "incorrect":
        product_offer_constraints.append(
            "FOREIGN KEY (provider_key) "
            "REFERENCES product_providers(provider_key) ON DELETE CASCADE"
        )
    elif product_offer_provider_fk == "incorrect_update":
        product_offer_constraints.append(
            "FOREIGN KEY (provider_key) "
            "REFERENCES product_providers(provider_key) "
            "ON UPDATE CASCADE ON DELETE RESTRICT"
        )
    elif product_offer_provider_fk == "wrong_reference":
        product_offer_constraints.append(
            "FOREIGN KEY (provider_key) "
            "REFERENCES product_providers(display_name) ON DELETE RESTRICT"
        )
    elif product_offer_provider_fk == "composite":
        product_offer_constraints.append(
            "FOREIGN KEY (provider_key, provider_product_id) "
            "REFERENCES product_providers(provider_key, display_name) "
            "ON DELETE RESTRICT"
        )
    provider_extra_constraint = (
        ", UNIQUE (provider_key, display_name)"
        if product_offer_provider_fk == "composite"
        else ""
    )

    cart_constraints = [
        "CHECK (quantity > 0)",
        "CHECK (selected_price_minor >= 0)",
        "CHECK (selected_currency GLOB '[A-Z][A-Z][A-Z]')",
        (
            "CHECK (selected_availability IN "
            "('in_stock', 'out_of_stock', 'preorder', 'backorder', 'unknown'))"
        ),
    ]
    if cart_primary_key:
        cart_constraints.append("PRIMARY KEY (owner_id, offer_id)")
    if cart_offer_fk == "valid":
        cart_constraints.append(
            "FOREIGN KEY (offer_id) "
            "REFERENCES product_offers(offer_id) ON DELETE RESTRICT"
        )
    elif cart_offer_fk == "incorrect":
        cart_constraints.append(
            "FOREIGN KEY (offer_id) "
            "REFERENCES product_offers(offer_id) ON DELETE CASCADE"
        )

    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(f"""
            CREATE TABLE products (id INTEGER PRIMARY KEY);
            CREATE TABLE product_providers (
                provider_key TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                terms_url TEXT,
                enabled INTEGER NOT NULL
                {provider_extra_constraint}
            );
            CREATE TABLE canonical_products (
                canonical_product_id TEXT PRIMARY KEY,
                legacy_product_id INTEGER UNIQUE,
                name TEXT NOT NULL,
                brand TEXT,
                model TEXT,
                gtin TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (legacy_product_id)
                    REFERENCES products(id) ON DELETE RESTRICT
            );
            CREATE TABLE product_offers (
                offer_id TEXT NOT NULL,
                provider_key TEXT NOT NULL,
                provider_offer_key TEXT NOT NULL,
                provider_product_id TEXT NOT NULL,
                canonical_product_id TEXT,
                product_name TEXT NOT NULL,
                brand TEXT,
                model TEXT,
                price_minor INTEGER,
                currency TEXT,
                product_url TEXT,
                image_url TEXT,
                availability TEXT NOT NULL,
                last_checked_at TEXT,
                expires_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                {", ".join(product_offer_constraints)}
            );
            CREATE TABLE cart_offer_items (
                owner_id TEXT NOT NULL,
                offer_id TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                selected_product_name TEXT NOT NULL,
                selected_provider_key TEXT NOT NULL,
                selected_provider_product_id TEXT NOT NULL,
                selected_price_minor INTEGER NOT NULL,
                selected_currency TEXT NOT NULL,
                selected_product_url TEXT,
                selected_availability TEXT NOT NULL,
                added_at TEXT NOT NULL,
                {", ".join(cart_constraints)}
            );
        """)
        connection.commit()


def _create_v001_database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript("""
            CREATE TABLE products (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT,
                price REAL,
                description TEXT,
                is_organic INTEGER DEFAULT 0,
                currency TEXT
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
                currency TEXT,
                FOREIGN KEY (product_id) REFERENCES products(id)
            );
            CREATE TABLE cart_items (
                owner_id TEXT NOT NULL,
                product_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                PRIMARY KEY (owner_id, product_id),
                FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
            );
            INSERT INTO products
                (id, name, category, price, description, is_organic, currency)
            VALUES (1, 'Legacy local item', 'test', 3.25, 'Legacy data', 0, NULL);
            INSERT INTO reviews (product_id, rating, reviewer_name, review_text)
            VALUES (1, 4.0, 'Reviewer', 'Existing review');
            INSERT INTO orders (product_id, product_name, price, owner_id, status)
            VALUES (1, 'Legacy local item', 3.25, 'local-owner', 'placed');
            INSERT INTO cart_items (owner_id, product_id, quantity)
            VALUES ('local-owner', 1, 2);
        """)
        connection.commit()
    v001_product_offers.migrate(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO product_providers (provider_key, display_name, enabled)
            VALUES ('test_market', 'Test market fixture', 1)
            """
        )
        connection.commit()


def _insert_offer(
    path: Path,
    *,
    provider_offer_key: str,
    provider_product_id: str,
    price_minor: int | None,
    currency: str | None,
    availability: str = "in_stock",
    product_name: str = "Test external product",
    product_url: str | None = "https://shop.example/items/offer",
    checked_at: str | None = None,
) -> str:
    offer_id = str(uuid.uuid4())
    timestamp = checked_at or _timestamp()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO product_offers (
                offer_id, provider_key, provider_offer_key,
                provider_product_id, canonical_product_id, product_name,
                brand, model, price_minor, currency, product_url, image_url,
                availability, last_checked_at, expires_at, created_at, updated_at
            ) VALUES (?, 'test_market', ?, ?, NULL, ?, NULL, NULL, ?, ?, ?,
                      NULL, ?, ?, NULL, ?, ?)
            """,
            (
                offer_id,
                provider_offer_key,
                provider_product_id,
                product_name,
                price_minor,
                currency,
                product_url,
                availability,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        connection.commit()
    return offer_id


def _quote_for(path: Path, offer_id: str) -> ResolvedOfferQuote:
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute(
            """
            SELECT offer_id, provider_key, provider_offer_key,
                   provider_product_id, product_name, price_minor,
                   currency, product_url, availability, last_checked_at
            FROM product_offers WHERE offer_id = ?
            """,
            (offer_id,),
        ).fetchone()
    if row is None:
        raise AssertionError("The test offer was not inserted.")
    return ResolvedOfferQuote(
        offer_id=row[0],
        provider_key=row[1],
        provider_offer_key=row[2],
        provider_product_id=row[3],
        product_name=row[4],
        price_minor=row[5],
        currency=row[6],
        product_url=row[7],
        availability=row[8],
        verified_at=row[9],
    )


def _local_table_snapshot(path: Path) -> dict[str, list[tuple]]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            table: connection.execute(
                f'SELECT * FROM "{table}" ORDER BY rowid'
            ).fetchall()
            for table in LOCAL_TABLES
        }


class ExternalCartMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "migration-test.db"
        _create_v001_database(self.database_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _assert_rejected_before_change(self, path: Path) -> None:
        with self.assertRaises(RuntimeError):
            v002_external_cart_quotes.migrate(path)
        with closing(sqlite3.connect(path)) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(cart_offer_items)"
                ).fetchall()
            }
        self.assertTrue(
            all(
                name not in columns
                for name, _ in v002_external_cart_quotes._ADDITIONS
            )
        )

    def test_migration_rejects_columns_only_schema_before_altering(self):
        path = Path(self.temp_dir.name) / "columns-only.db"
        _create_schema_fixture(path, columns_only=True)

        self._assert_rejected_before_change(path)

    def test_migration_rejects_missing_primary_keys(self):
        invalid_schemas = (
            (
                "product-offer-primary-key",
                lambda path: _create_schema_fixture(
                    path, product_offer_primary_key=False
                ),
            ),
            (
                "cart-primary-key",
                lambda path: _create_schema_fixture(path, cart_primary_key=False),
            ),
        )
        for name, create_schema in invalid_schemas:
            with self.subTest(schema=name):
                path = Path(self.temp_dir.name) / f"{name}.db"
                create_schema(path)
                self._assert_rejected_before_change(path)

    def test_migration_rejects_missing_or_incorrect_foreign_keys(self):
        invalid_schemas = (
            (
                "missing-product-offer-provider-foreign-key",
                lambda path: _create_schema_fixture(
                    path, product_offer_provider_fk=None
                ),
            ),
            (
                "incorrect-product-offer-provider-foreign-key",
                lambda path: _create_schema_fixture(
                    path, product_offer_provider_fk="incorrect"
                ),
            ),
            (
                "incorrect-update-product-offer-provider-foreign-key",
                lambda path: _create_schema_fixture(
                    path, product_offer_provider_fk="incorrect_update"
                ),
            ),
            (
                "incorrect-reference-product-offer-provider-foreign-key",
                lambda path: _create_schema_fixture(
                    path, product_offer_provider_fk="wrong_reference"
                ),
            ),
            (
                "missing-cart-foreign-key",
                lambda path: _create_schema_fixture(path, cart_offer_fk=None),
            ),
            (
                "incorrect-cart-foreign-key",
                lambda path: _create_schema_fixture(
                    path, cart_offer_fk="incorrect"
                ),
            ),
        )
        for name, create_schema in invalid_schemas:
            with self.subTest(schema=name):
                path = Path(self.temp_dir.name) / f"{name}.db"
                create_schema(path)
                self._assert_rejected_before_change(path)

    def test_migration_rejects_composite_foreign_key_group(self):
        path = Path(self.temp_dir.name) / "composite-foreign-key.db"
        _create_schema_fixture(
            path,
            product_offer_provider_fk="composite",
        )

        self._assert_rejected_before_change(path)

    def test_migration_accepts_v001_single_column_foreign_key_groups(self):
        result = v002_external_cart_quotes.migrate(self.database_path)

        self.assertEqual(result.foreign_key_violations, 0)
        self.assertIn("quote_status", result.added_columns)

    def test_migration_is_repeatable_and_preserves_existing_rows(self):
        offer_id = _insert_offer(
            self.database_path,
            provider_offer_key="merchant-offer-1",
            provider_product_id="product-1",
            price_minor=2499,
            currency="USD",
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                """
                INSERT INTO cart_offer_items (
                    owner_id, offer_id, quantity, selected_product_name,
                    selected_provider_key, selected_provider_product_id,
                    selected_price_minor, selected_currency,
                    selected_product_url, selected_availability, added_at
                ) VALUES (
                    'owner-a', ?, 2, 'Saved product', 'test_market',
                    'product-1', 2499, 'USD',
                    'https://shop.example/items/offer', 'in_stock', ?
                )
                """,
                (offer_id, _timestamp()),
            )
            connection.commit()

        local_rows_before = _local_table_snapshot(self.database_path)
        first = v002_external_cart_quotes.migrate(self.database_path)

        self.assertIn("selected_provider_offer_key", first.added_columns)
        self.assertIn("selected_verified_at", first.added_columns)
        self.assertIn("quote_status", first.added_columns)
        self.assertEqual(first.backfilled_offer_keys, 1)
        self.assertEqual(first.preserved_cart_item_count, 1)
        self.assertEqual(first.foreign_key_violations, 0)

        with closing(sqlite3.connect(self.database_path)) as connection:
            row = connection.execute(
                """
                SELECT offer_id, quantity, selected_price_minor, selected_currency,
                       selected_provider_offer_key, selected_verified_at,
                       quote_status, pending_price_minor, pending_currency,
                       pending_verified_at
                FROM cart_offer_items WHERE owner_id = 'owner-a'
                """
            ).fetchone()
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(
            row,
            (
                offer_id,
                2,
                2499,
                "USD",
                "merchant-offer-1",
                None,
                "stale_unverified",
                None,
                None,
                None,
            ),
        )
        self.assertEqual(_local_table_snapshot(self.database_path), local_rows_before)

        second = v002_external_cart_quotes.migrate(self.database_path)
        self.assertEqual(second.added_columns, ())
        self.assertEqual(second.backfilled_offer_keys, 0)
        self.assertEqual(second.preserved_cart_item_count, 1)
        self.assertEqual(_local_table_snapshot(self.database_path), local_rows_before)

    def test_migration_rejects_database_without_v001_without_creating_tables(self):
        path = Path(self.temp_dir.name) / "not-migrated.db"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE products (id INTEGER PRIMARY KEY)")
            connection.commit()

        with self.assertRaisesRegex(RuntimeError, "v001"):
            v002_external_cart_quotes.migrate(path)

        with closing(sqlite3.connect(path)) as connection:
            self.assertIsNone(
                connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'cart_offer_items'
                    """
                ).fetchone()
            )

    def test_schema_trigger_rejects_mismatched_offer_identity(self):
        offer_id = _insert_offer(
            self.database_path,
            provider_offer_key="merchant-offer-2",
            provider_product_id="product-2",
            price_minor=500,
            currency="USD",
        )
        v002_external_cart_quotes.migrate(self.database_path)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO cart_offer_items (
                        owner_id, offer_id, quantity, selected_product_name,
                        selected_provider_key, selected_provider_offer_key,
                        selected_provider_product_id, selected_price_minor,
                        selected_currency, selected_product_url,
                        selected_availability, selected_verified_at, added_at,
                        quote_status
                    ) VALUES (
                        'owner-a', ?, 1, 'Test external product',
                        'test_market', 'wrong-offer-key', 'product-2', 500,
                        'USD', 'https://shop.example/items/offer', 'in_stock',
                        ?, ?, 'verified_current'
                    )
                    """,
                    (offer_id, _timestamp(), _timestamp()),
                )


class ExternalOfferCartServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "cart-test.db"
        _create_v001_database(self.database_path)
        v002_external_cart_quotes.migrate(self.database_path)
        self.usd_offer_id = _insert_offer(
            self.database_path,
            provider_offer_key="merchant-offer-usd",
            provider_product_id="shared-product",
            price_minor=1299,
            currency="USD",
        )
        self.inr_offer_id = _insert_offer(
            self.database_path,
            provider_offer_key="merchant-offer-inr",
            provider_product_id="shared-product",
            price_minor=750,
            currency="INR",
            product_url=None,
        )
        self.unknown_offer_id = _insert_offer(
            self.database_path,
            provider_offer_key="merchant-offer-unknown",
            provider_product_id="unknown-product",
            price_minor=None,
            currency=None,
        )
        self.service = ExternalOfferCartService(self.database_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _expire_selected_quote(self, owner_id: str, offer_id: str) -> str:
        expired_timestamp = _timestamp_ago(6)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                UPDATE cart_offer_items
                SET selected_verified_at = ?
                WHERE owner_id = ? AND offer_id = ?
                """,
                (expired_timestamp, owner_id, offer_id),
            )
            connection.commit()
        return expired_timestamp

    def test_add_and_duplicate_add_use_exact_offer_identity(self):
        quote = _quote_for(self.database_path, self.usd_offer_id)
        first = self.service.add_offer("owner-a", quote, 2)
        duplicate = self.service.add_offer("owner-a", quote, 3)

        self.assertEqual(first.offer_id, self.usd_offer_id)
        self.assertEqual(first.provider_offer_key, "merchant-offer-usd")
        self.assertEqual(duplicate.quantity, 5)
        self.assertEqual(duplicate.price_minor, 1299)
        self.assertEqual(duplicate.currency, "USD")
        self.assertEqual(duplicate.added_at, first.added_at)
        self.assertEqual(len(self.service.get_cart("owner-a").items), 1)

    def test_client_spoofed_quote_does_not_match_persisted_offer(self):
        quote = _quote_for(self.database_path, self.usd_offer_id)
        spoofed = replace(quote, price_minor=1)

        with self.assertRaises(OfferQuoteMismatchError):
            self.service.add_offer("owner-a", spoofed)
        self.assertEqual(self.service.get_cart("owner-a").items, ())

    def test_owner_isolation_and_exact_offer_reference(self):
        self.service.add_offer("owner-a", _quote_for(self.database_path, self.usd_offer_id))

        self.assertEqual(self.service.get_cart("owner-b").items, ())
        with self.assertRaises(CartItemNotFoundError):
            self.service.remove_item("owner-b", self.usd_offer_id)
        self.assertEqual(len(self.service.get_cart("owner-a").items), 1)

    def test_quantity_validation_and_maximum(self):
        quote = _quote_for(self.database_path, self.usd_offer_id)
        invalid_quantities: tuple[object, ...] = (
            0,
            -1,
            True,
            1.5,
            "2",
            MAX_CART_QUANTITY + 1,
        )
        for invalid in invalid_quantities:
            with self.subTest(quantity=invalid):
                with self.assertRaises(InvalidCartInputError):
                    self.service.add_offer(
                        "owner-a", quote, cast(int, invalid)
                    )

        item = self.service.add_offer("owner-a", quote)
        invalid_update_quantities: tuple[object, ...] = (
            0,
            -1,
            True,
            1.5,
            MAX_CART_QUANTITY + 1,
        )
        for invalid in invalid_update_quantities:
            with self.subTest(update_quantity=invalid):
                with self.assertRaises(InvalidCartInputError):
                    self.service.update_quantity(
                        "owner-a", item.offer_id, cast(int, invalid)
                    )
        self.assertEqual(
            self.service.update_quantity(
                "owner-a", item.offer_id, MAX_CART_QUANTITY
            ).quantity,
            MAX_CART_QUANTITY,
        )

    def test_unknown_price_or_currency_is_rejected(self):
        quote = _quote_for(self.database_path, self.unknown_offer_id)
        with self.assertRaisesRegex(OfferNotEligibleError, "verified price"):
            self.service.add_offer("owner-a", quote)

        with self.assertRaises(InvalidCartInputError):
            replace(
                _quote_for(self.database_path, self.usd_offer_id),
                currency=None,
            )
        with self.assertRaises(InvalidCartInputError):
            replace(quote, price_minor=100, currency="XXX")

    def test_unknown_and_out_of_stock_availability_are_not_purchase_ready(self):
        unknown_availability_id = _insert_offer(
            self.database_path,
            provider_offer_key="merchant-offer-availability-unknown",
            provider_product_id="availability-unknown",
            price_minor=100,
            currency="USD",
            availability="unknown",
        )
        out_of_stock_id = _insert_offer(
            self.database_path,
            provider_offer_key="merchant-offer-out-of-stock",
            provider_product_id="out-of-stock",
            price_minor=100,
            currency="USD",
            availability="out_of_stock",
        )
        for offer_id in (unknown_availability_id, out_of_stock_id):
            with self.subTest(offer_id=offer_id):
                with self.assertRaises(OfferNotEligibleError):
                    self.service.add_offer("owner-a", _quote_for(self.database_path, offer_id))

    def test_local_catalog_offer_cannot_enter_external_cart(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            local_offer_id = connection.execute(
                """
                SELECT offer_id FROM product_offers
                WHERE provider_key = 'local_catalog'
                """
            ).fetchone()[0]
        local_quote = _quote_for(self.database_path, local_offer_id)

        with self.assertRaisesRegex(OfferNotEligibleError, "existing local-product cart"):
            self.service.add_offer("owner-a", local_quote)

    def test_currency_totals_are_separate_and_have_no_combined_total(self):
        self.service.add_offer(
            "owner-a", _quote_for(self.database_path, self.usd_offer_id), 2
        )
        self.service.add_offer(
            "owner-a", _quote_for(self.database_path, self.inr_offer_id), 3
        )

        cart = self.service.get_cart("owner-a")
        totals = {
            total.currency: (total.snapshot_total_minor, total.item_count)
            for total in cart.currency_totals
        }
        self.assertEqual(totals, {"INR": (2250, 3), "USD": (2598, 2)})
        self.assertFalse(hasattr(cart, "total"))
        self.assertFalse(hasattr(cart, "subtotal"))

    def test_price_change_waits_for_explicit_confirmation(self):
        original_quote = _quote_for(self.database_path, self.usd_offer_id)
        original = self.service.add_offer("owner-a", original_quote, 2)
        changed_at = _timestamp(1)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                UPDATE product_offers
                SET price_minor = 1599, currency = 'INR',
                    product_name = 'Updated test product',
                    availability = 'preorder',
                    last_checked_at = ?, updated_at = ?
                WHERE offer_id = ?
                """,
                (changed_at, changed_at, self.usd_offer_id),
            )
            connection.commit()

        changed_quote = _quote_for(self.database_path, self.usd_offer_id)
        pending = self.service.add_offer("owner-a", changed_quote, 4)
        self.assertEqual(pending.quote_status, "price_change_pending")
        self.assertEqual(pending.price_minor, 1299)
        self.assertEqual(pending.quantity, 2)
        self.assertEqual(pending.pending_price_minor, 1599)
        self.assertEqual(pending.pending_currency, "INR")
        self.assertEqual(pending.pending_product_name, "Updated test product")
        self.assertEqual(pending.pending_availability, "preorder")
        self.assertEqual(pending.product_name, original.product_name)
        self.assertEqual(pending.availability, original.availability)
        self.assertEqual(pending.added_at, original.added_at)
        old_totals = self.service.get_cart("owner-a").currency_totals
        self.assertEqual([(total.currency, total.snapshot_total_minor) for total in old_totals], [("USD", 2598)])

        with self.assertRaises(PriceConfirmationRequiredError):
            self.service.add_offer("owner-a", changed_quote, 1)

        confirmed = self.service.confirm_price_change("owner-a", self.usd_offer_id)
        self.assertEqual(confirmed.quote_status, "verified_current")
        self.assertEqual(confirmed.price_minor, 1599)
        self.assertEqual(confirmed.currency, "INR")
        self.assertEqual(confirmed.verified_at, changed_at)
        self.assertEqual(confirmed.product_name, "Updated test product")
        self.assertEqual(confirmed.availability, "preorder")
        self.assertIsNone(confirmed.pending_price_minor)
        self.assertEqual(confirmed.quantity, 2)
        new_totals = self.service.get_cart("owner-a").currency_totals
        self.assertEqual([(total.currency, total.snapshot_total_minor) for total in new_totals], [("INR", 3198)])

    def test_stale_item_rejects_readding_identical_old_quote(self):
        original_quote = _quote_for(self.database_path, self.usd_offer_id)
        original = self.service.add_offer("owner-a", original_quote)
        self.service.mark_stale("owner-a", self.usd_offer_id)
        old_timestamp = _timestamp_ago(6)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                UPDATE product_offers
                SET last_checked_at = ?, updated_at = ?
                WHERE offer_id = ?
                """,
                (old_timestamp, old_timestamp, self.usd_offer_id),
            )
            connection.commit()

        with self.assertRaisesRegex(OfferNotEligibleError, "5-minute"):
            self.service.add_offer(
                "owner-a", _quote_for(self.database_path, self.usd_offer_id)
            )

        item = self.service.get_cart("owner-a").items[0]
        self.assertEqual(item.quote_status, "stale_unverified")
        self.assertEqual(item.verified_at, original.verified_at)
        self.assertEqual(item.quantity, original.quantity)

    def test_stale_item_rejects_old_pending_price_quote(self):
        original_quote = _quote_for(self.database_path, self.usd_offer_id)
        original = self.service.add_offer("owner-a", original_quote)
        self.service.mark_stale("owner-a", self.usd_offer_id)
        old_timestamp = _timestamp_ago(6)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                UPDATE product_offers
                SET price_minor = 1599, last_checked_at = ?, updated_at = ?
                WHERE offer_id = ?
                """,
                (old_timestamp, old_timestamp, self.usd_offer_id),
            )
            connection.commit()

        with self.assertRaisesRegex(OfferNotEligibleError, "5-minute"):
            self.service.stage_price_change(
                "owner-a", _quote_for(self.database_path, self.usd_offer_id)
            )

        item = self.service.get_cart("owner-a").items[0]
        self.assertEqual(item.quote_status, "stale_unverified")
        self.assertEqual(item.price_minor, original.price_minor)
        self.assertEqual(item.verified_at, original.verified_at)
        self.assertIsNone(item.pending_price_minor)

    def test_unavailable_item_rejects_readding_identical_old_quote(self):
        original_quote = _quote_for(self.database_path, self.usd_offer_id)
        original = self.service.add_offer("owner-a", original_quote)
        self.service.mark_unavailable("owner-a", self.usd_offer_id)
        old_timestamp = _timestamp_ago(6)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                UPDATE product_offers
                SET last_checked_at = ?, updated_at = ?
                WHERE offer_id = ?
                """,
                (old_timestamp, old_timestamp, self.usd_offer_id),
            )
            connection.commit()

        with self.assertRaisesRegex(OfferNotEligibleError, "5-minute"):
            self.service.add_offer(
                "owner-a", _quote_for(self.database_path, self.usd_offer_id)
            )

        item = self.service.get_cart("owner-a").items[0]
        self.assertEqual(item.quote_status, "unavailable")
        self.assertEqual(item.verified_at, original.verified_at)
        self.assertEqual(item.quantity, original.quantity)

    def test_fresh_validated_quote_restores_stale_item(self):
        original = self.service.add_offer(
            "owner-a", _quote_for(self.database_path, self.usd_offer_id)
        )
        self.service.mark_stale("owner-a", self.usd_offer_id)
        fresh_timestamp = _timestamp(30)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                UPDATE product_offers
                SET last_checked_at = ?, updated_at = ?
                WHERE offer_id = ?
                """,
                (fresh_timestamp, fresh_timestamp, self.usd_offer_id),
            )
            connection.commit()

        restored = self.service.add_offer(
            "owner-a", _quote_for(self.database_path, self.usd_offer_id)
        )
        self.assertEqual(restored.quote_status, "verified_current")
        self.assertEqual(restored.verified_at, fresh_timestamp)
        self.assertNotEqual(restored.verified_at, original.verified_at)

    def test_get_cart_downgrades_expired_quote_and_currency_total(self):
        self.service.add_offer(
            "owner-a", _quote_for(self.database_path, self.usd_offer_id)
        )
        expired_timestamp = self._expire_selected_quote(
            "owner-a", self.usd_offer_id
        )

        cart = self.service.get_cart("owner-a")

        self.assertEqual(cart.items[0].quote_status, "stale_unverified")
        self.assertEqual(cart.items[0].verified_at, expired_timestamp)
        self.assertFalse(cart.currency_totals[0].all_quotes_current)

    def test_update_quantity_does_not_restore_expired_quote(self):
        self.service.add_offer(
            "owner-a", _quote_for(self.database_path, self.usd_offer_id)
        )
        expired_timestamp = self._expire_selected_quote(
            "owner-a", self.usd_offer_id
        )

        updated = self.service.update_quantity(
            "owner-a", self.usd_offer_id, 3
        )

        self.assertEqual(updated.quantity, 3)
        self.assertEqual(updated.quote_status, "stale_unverified")
        self.assertEqual(updated.verified_at, expired_timestamp)

    def test_repeated_cart_reads_do_not_refresh_expired_verification_time(self):
        self.service.add_offer(
            "owner-a", _quote_for(self.database_path, self.usd_offer_id)
        )
        expired_timestamp = self._expire_selected_quote(
            "owner-a", self.usd_offer_id
        )

        first_read = self.service.get_cart("owner-a").items[0]
        second_read = self.service.get_cart("owner-a").items[0]
        with closing(sqlite3.connect(self.database_path)) as connection:
            stored_timestamp = connection.execute(
                """
                SELECT selected_verified_at
                FROM cart_offer_items
                WHERE owner_id = ? AND offer_id = ?
                """,
                ("owner-a", self.usd_offer_id),
            ).fetchone()[0]

        self.assertEqual(first_read.quote_status, "stale_unverified")
        self.assertEqual(second_read.quote_status, "stale_unverified")
        self.assertEqual(first_read.verified_at, expired_timestamp)
        self.assertEqual(second_read.verified_at, expired_timestamp)
        self.assertEqual(stored_timestamp, expired_timestamp)

    def test_confirmation_accepts_old_pending_price_without_refreshing_it(self):
        original = self.service.add_offer(
            "owner-a", _quote_for(self.database_path, self.usd_offer_id)
        )
        pending_timestamp = _timestamp(1)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                UPDATE product_offers
                SET price_minor = 1599, last_checked_at = ?, updated_at = ?
                WHERE offer_id = ?
                """,
                (pending_timestamp, pending_timestamp, self.usd_offer_id),
            )
            connection.commit()

        pending = self.service.add_offer(
            "owner-a", _quote_for(self.database_path, self.usd_offer_id)
        )
        self.assertEqual(pending.quote_status, "price_change_pending")
        self.assertEqual(pending.price_minor, original.price_minor)
        self.assertEqual(pending.verified_at, original.verified_at)
        self.assertEqual(pending.pending_verified_at, pending_timestamp)

        expired_timestamp = _timestamp_ago(6)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                UPDATE product_offers
                SET last_checked_at = ?, updated_at = ?
                WHERE offer_id = ?
                """,
                (expired_timestamp, expired_timestamp, self.usd_offer_id),
            )
            connection.execute(
                """
                UPDATE cart_offer_items
                SET pending_verified_at = ?
                WHERE owner_id = 'owner-a' AND offer_id = ?
                """,
                (expired_timestamp, self.usd_offer_id),
            )
            connection.commit()

        confirmed = self.service.confirm_price_change("owner-a", self.usd_offer_id)
        self.assertEqual(confirmed.quote_status, "stale_unverified")
        self.assertEqual(confirmed.price_minor, 1599)
        self.assertEqual(confirmed.verified_at, expired_timestamp)
        self.assertIsNone(confirmed.pending_price_minor)

    def test_stale_and_unavailable_states_preserve_saved_quote(self):
        original = self.service.add_offer(
            "owner-a", _quote_for(self.database_path, self.usd_offer_id)
        )
        stale = self.service.mark_stale("owner-a", self.usd_offer_id)
        self.assertEqual(stale.quote_status, "stale_unverified")
        self.assertEqual(stale.price_minor, original.price_minor)
        self.assertEqual(stale.verified_at, original.verified_at)

        unavailable = self.service.mark_unavailable("owner-a", self.usd_offer_id)
        self.assertEqual(unavailable.quote_status, "unavailable")
        self.assertEqual(unavailable.price_minor, original.price_minor)
        self.assertEqual(unavailable.currency, original.currency)
        self.assertFalse(self.service.get_cart("owner-a").currency_totals[0].all_quotes_current)

    def test_removal_and_clear_are_owner_scoped(self):
        self.service.add_offer(
            "owner-a", _quote_for(self.database_path, self.usd_offer_id)
        )
        self.service.add_offer(
            "owner-a", _quote_for(self.database_path, self.inr_offer_id)
        )
        self.service.add_offer(
            "owner-b", _quote_for(self.database_path, self.usd_offer_id)
        )

        self.service.remove_item("owner-a", self.usd_offer_id)
        self.assertEqual(
            [item.offer_id for item in self.service.get_cart("owner-a").items],
            [self.inr_offer_id],
        )
        self.assertEqual(self.service.clear_cart("owner-a"), 1)
        self.assertEqual(self.service.get_cart("owner-a").items, ())
        self.assertEqual(len(self.service.get_cart("owner-b").items), 1)

    def test_invalid_provider_url_is_rejected(self):
        quote = _quote_for(self.database_path, self.usd_offer_id)
        for url in ("http://shop.example/item", "javascript:alert(1)"):
            with self.subTest(url=url):
                with self.assertRaises(InvalidCartInputError):
                    replace(quote, product_url=url)

    def test_future_verification_timestamp_is_rejected(self):
        quote = _quote_for(self.database_path, self.usd_offer_id)
        future_timestamp = (
            datetime.now(timezone.utc) + timedelta(minutes=1)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.assertRaisesRegex(InvalidCartInputError, "future"):
            replace(quote, verified_at=future_timestamp)


if __name__ == "__main__":
    unittest.main()
