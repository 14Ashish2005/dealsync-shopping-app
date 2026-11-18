import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from backend.migrations import v004_product_specifications as migration
from backend.product_specifications_schema import (
    PRODUCT_SPECIFICATIONS_TABLE,
    TRUSTED_SPECIFICATION_SOURCES_TABLE,
    ensure_product_specifications_schema,
    validate_verified_specification_evidence,
)
from setup_db import create_database


_EVIDENCE_COLUMNS = (
    "value",
    "unit",
    "verification_status",
    "source_reference",
    "source_key",
    "verification_source",
    "observed_at",
    "verified_at",
)


class ProductSpecificationsMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "products.sqlite"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_legacy_database(self, path: Path | None = None) -> Path:
        database_path = path or self.db_path
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(
                """
                CREATE TABLE products (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT,
                    price REAL,
                    description TEXT,
                    is_organic INTEGER DEFAULT 0
                );
                CREATE TABLE reviews (
                    id INTEGER PRIMARY KEY,
                    product_id INTEGER,
                    rating REAL,
                    reviewer_name TEXT,
                    review_text TEXT,
                    FOREIGN KEY (product_id) REFERENCES products(id)
                );
                CREATE TABLE orders (
                    id INTEGER PRIMARY KEY,
                    product_id INTEGER NOT NULL,
                    product_name TEXT NOT NULL,
                    price REAL NOT NULL,
                    FOREIGN KEY (product_id) REFERENCES products(id)
                );
                CREATE TABLE cart_items (
                    owner_id TEXT NOT NULL,
                    product_id INTEGER NOT NULL,
                    quantity INTEGER NOT NULL,
                    PRIMARY KEY (owner_id, product_id),
                    FOREIGN KEY (product_id)
                        REFERENCES products(id) ON DELETE CASCADE
                );
                INSERT INTO products
                    (id, name, category, price, description, is_organic)
                VALUES
                    (1, 'Legacy item', 'test', 12.5, 'Existing description', 0);
                INSERT INTO reviews
                    (id, product_id, rating, reviewer_name, review_text)
                VALUES (1, 1, 4.5, 'Reviewer', 'Existing review');
                INSERT INTO orders (id, product_id, product_name, price)
                VALUES (1, 1, 'Legacy item', 12.5);
                INSERT INTO cart_items (owner_id, product_id, quantity)
                VALUES ('owner-a', 1, 2);
                """
            )
        return database_path

    @staticmethod
    def _snapshot(connection: sqlite3.Connection) -> dict[str, tuple]:
        tables = ("products", "reviews", "orders", "cart_items")
        return {
            table: tuple(
                connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            )
            for table in tables
        }

    def _insert_trusted_sources(
        self,
        connection: sqlite3.Connection,
        *,
        enabled: bool = True,
    ) -> None:
        value = 1 if enabled else 0
        connection.executemany(
            f"""
            INSERT INTO {TRUSTED_SPECIFICATION_SOURCES_TABLE} (source_key, enabled)
            VALUES (?, ?)
            """,
            (
                ("approved-catalog", value),
                ("approved-verifier", value),
            ),
        )

    @staticmethod
    def _insert_evidence(
        connection: sqlite3.Connection,
        *,
        value: str = "8",
        unit: str = "GB",
        attribute_name: str = "ram",
        verification_status: str = "verified",
        source_reference: str | None = "catalog-record-1",
        source_key: str | None = "approved-catalog",
        verification_source: str | None = "approved-verifier",
        observed_at: str | None = "2026-01-02T03:04:05Z",
        verified_at: str | None = "2026-01-03T03:04:05Z",
    ) -> int:
        cursor = connection.execute(
            f"""
            INSERT INTO {PRODUCT_SPECIFICATIONS_TABLE} (
                product_id, attribute_name, value, unit, verification_status,
                source_reference, source_key, verification_source, observed_at,
                verified_at
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attribute_name,
                value,
                unit,
                verification_status,
                source_reference,
                source_key,
                verification_source,
                observed_at,
                verified_at,
            ),
        )
        return cursor.lastrowid

    def test_fresh_initializer_and_migration_use_the_same_schema(self):
        create_database(self.db_path)

        with closing(sqlite3.connect(self.db_path)) as connection:
            created = ensure_product_specifications_schema(connection)
            self.assertEqual(created, ())
            self.assertEqual(
                connection.execute(
                    f"SELECT COUNT(*) FROM {PRODUCT_SPECIFICATIONS_TABLE}"
                ).fetchone()[0],
                0,
            )
            first_schema = connection.execute(
                """
                SELECT sql FROM sqlite_master
                WHERE type = 'table' AND name IN (?, ?)
                ORDER BY name
                """,
                (
                    PRODUCT_SPECIFICATIONS_TABLE,
                    TRUSTED_SPECIFICATION_SOURCES_TABLE,
                ),
            ).fetchall()

        result = migration.migrate(self.db_path)
        self.assertEqual(result.created_tables, ())
        self.assertEqual(result.product_count, 32)
        self.assertEqual(result.evidence_count, 0)

        with closing(sqlite3.connect(self.db_path)) as connection:
            second_schema = connection.execute(
                """
                SELECT sql FROM sqlite_master
                WHERE type = 'table' AND name IN (?, ?)
                ORDER BY name
                """,
                (
                    PRODUCT_SPECIFICATIONS_TABLE,
                    TRUSTED_SPECIFICATION_SOURCES_TABLE,
                ),
            ).fetchall()
            self.assertEqual(second_schema, first_schema)
            self.assertEqual(
                connection.execute(
                    f"SELECT COUNT(*) FROM {PRODUCT_SPECIFICATIONS_TABLE}"
                ).fetchone()[0],
                0,
            )

    def test_legacy_migration_preserves_products_cart_and_related_rows(self):
        self._create_legacy_database()
        with closing(sqlite3.connect(self.db_path)) as connection:
            before = self._snapshot(connection)

        result = migration.migrate(self.db_path)

        self.assertEqual(
            result.created_tables,
            (
                TRUSTED_SPECIFICATION_SOURCES_TABLE,
                PRODUCT_SPECIFICATIONS_TABLE,
            ),
        )
        self.assertEqual(result.product_count, 1)
        self.assertEqual(result.evidence_count, 0)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(self._snapshot(connection), before)
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(),
                [],
            )
            self.assertEqual(
                connection.execute(
                    f"SELECT COUNT(*) FROM {PRODUCT_SPECIFICATIONS_TABLE}"
                ).fetchone()[0],
                0,
            )

    def test_specification_fields_default_to_null(self):
        self._create_legacy_database()
        migration.migrate(self.db_path)

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                f"""
                INSERT INTO {PRODUCT_SPECIFICATIONS_TABLE}
                    (product_id, attribute_name)
                VALUES (1, 'ram')
                """
            )
            row = connection.execute(
                f"""
                SELECT {", ".join(_EVIDENCE_COLUMNS)}
                FROM {PRODUCT_SPECIFICATIONS_TABLE}
                """
            ).fetchone()
            self.assertEqual(row, (None,) * len(_EVIDENCE_COLUMNS))

    def test_valid_structured_evidence_is_retained_and_verifiable(self):
        self._create_legacy_database()
        migration.migrate(self.db_path)

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            self._insert_trusted_sources(connection)
            self._insert_evidence(connection)
            evidence = dict(
                zip(
                    (
                        "value",
                        "unit",
                        "verification_status",
                        "source_reference",
                        "source_key",
                        "verification_source",
                        "observed_at",
                        "verified_at",
                        "attribute_name",
                    ),
                    connection.execute(
                        f"""
                        SELECT {", ".join(_EVIDENCE_COLUMNS)}, attribute_name
                        FROM {PRODUCT_SPECIFICATIONS_TABLE}
                        """
                    ).fetchone(),
                )
            )

            self.assertEqual(
                validate_verified_specification_evidence(
                    connection,
                    evidence,
                    now=datetime(2026, 2, 1, tzinfo=timezone.utc),
                ),
                Decimal("8"),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    f"""
                    DELETE FROM {TRUSTED_SPECIFICATION_SOURCES_TABLE}
                    WHERE source_key = 'approved-catalog'
                    """
                )

    def test_multiple_claims_for_one_attribute_are_preserved(self):
        self._create_legacy_database()
        migration.migrate(self.db_path)

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            self._insert_trusted_sources(connection)
            first_id = self._insert_evidence(connection, value="8")
            second_id = self._insert_evidence(
                connection,
                value="16",
                source_reference="catalog-record-2",
                observed_at="2026-01-04T03:04:05Z",
                verified_at="2026-01-05T03:04:05Z",
            )
            claims = connection.execute(
                f"""
                SELECT evidence_id, value
                FROM {PRODUCT_SPECIFICATIONS_TABLE}
                WHERE product_id = 1 AND attribute_name = 'ram'
                ORDER BY evidence_id
                """
            ).fetchall()
            self.assertEqual(claims, [(first_id, "8"), (second_id, "16")])

    def test_nonempty_verification_source_is_not_sufficient_for_trust(self):
        self._create_legacy_database()
        migration.migrate(self.db_path)

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            self._insert_trusted_sources(connection, enabled=False)
            self._insert_evidence(connection)
            row = connection.execute(
                f"""
                SELECT {", ".join(_EVIDENCE_COLUMNS)}, attribute_name
                FROM {PRODUCT_SPECIFICATIONS_TABLE}
                """
            ).fetchone()
            evidence = dict(
                zip((*_EVIDENCE_COLUMNS, "attribute_name"), row)
            )

            with self.assertRaisesRegex(ValueError, "not trusted"):
                validate_verified_specification_evidence(
                    connection,
                    evidence,
                    now=datetime(2026, 2, 1, tzinfo=timezone.utc),
                )

    def test_invalid_attributes_units_and_verification_metadata_are_rejected(self):
        self._create_legacy_database()
        migration.migrate(self.db_path)

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            self._insert_trusted_sources(connection)
            invalid_rows = (
                {"attribute_name": "processor", "value": "8", "unit": "GB"},
                {"attribute_name": "ram", "value": "8", "unit": "mAh"},
                {"attribute_name": "ram", "value": "8GB", "unit": "GB"},
                {
                    "attribute_name": "ram",
                    "value": "8",
                    "unit": "GB",
                    "verification_status": "verified",
                    "source_reference": "catalog-record",
                    "source_key": "approved-catalog",
                    "verification_source": "approved-verifier",
                    "observed_at": "not-a-timestamp",
                    "verified_at": "2026-01-03T03:04:05Z",
                },
            )
            for invalid in invalid_rows:
                values = {
                    "attribute_name": "ram",
                    "value": "8",
                    "unit": "GB",
                    "verification_status": "unverified",
                    "source_reference": "catalog-record",
                    "source_key": "approved-catalog",
                    "verification_source": None,
                    "observed_at": "2026-01-02T03:04:05Z",
                    "verified_at": None,
                }
                values.update(invalid)
                with self.subTest(invalid=invalid):
                    with self.assertRaises(sqlite3.IntegrityError):
                        self._insert_evidence(connection, **values)

            evidence = {
                "attribute_name": "ram",
                "value": "8",
                "unit": "GB",
                "verification_status": "verified",
                "source_reference": "catalog-record",
                "source_key": "approved-catalog",
                "verification_source": "approved-verifier",
                "observed_at": "2026-01-02T03:04:05Z",
                "verified_at": "2026-02-31T03:04:05Z",
            }
            with self.assertRaisesRegex(ValueError, "valid UTC timestamp"):
                validate_verified_specification_evidence(
                    connection,
                    evidence,
                    now=datetime(2026, 3, 1, tzinfo=timezone.utc),
                )

    def test_compatible_repeated_migration_is_safe(self):
        self._create_legacy_database()

        first = migration.migrate(self.db_path)
        second = migration.migrate(self.db_path)

        self.assertEqual(
            first.created_tables,
            (
                TRUSTED_SPECIFICATION_SOURCES_TABLE,
                PRODUCT_SPECIFICATIONS_TABLE,
            ),
        )
        self.assertEqual(second.created_tables, ())
        self.assertEqual(second.evidence_count, 0)

    def test_incompatible_existing_schema_fails_without_changes(self):
        self._create_legacy_database()
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                """
                CREATE TABLE product_specifications (
                    evidence_id INTEGER PRIMARY KEY,
                    product_id TEXT NOT NULL,
                    attribute_name TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO product_specifications VALUES (1, 'legacy', 'ram')"
            )
            before = self._snapshot(connection)
            schema_before = connection.execute(
                """
                SELECT sql FROM sqlite_master
                WHERE type = 'table' AND name = 'product_specifications'
                """
            ).fetchone()[0]

        with self.assertRaisesRegex(RuntimeError, "incompatible"):
            migration.migrate(self.db_path)

        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(self._snapshot(connection), before)
            self.assertEqual(
                connection.execute(
                    """
                    SELECT sql FROM sqlite_master
                    WHERE type = 'table' AND name = 'product_specifications'
                    """
                ).fetchone()[0],
                schema_before,
            )
            self.assertNotIn(
                TRUSTED_SPECIFICATION_SOURCES_TABLE,
                {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                },
            )

    def test_rollback_removes_empty_schema(self):
        self._create_legacy_database()
        migration.migrate(self.db_path)

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                f"""
                INSERT INTO {PRODUCT_SPECIFICATIONS_TABLE}
                    (product_id, attribute_name)
                VALUES (1, 'ram')
                """
            )
            connection.commit()

        removed = migration.rollback(self.db_path)

        self.assertEqual(
            removed,
            (
                PRODUCT_SPECIFICATIONS_TABLE,
                TRUSTED_SPECIFICATION_SOURCES_TABLE,
            ),
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            remaining = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertNotIn(PRODUCT_SPECIFICATIONS_TABLE, remaining)
            self.assertNotIn(TRUSTED_SPECIFICATION_SOURCES_TABLE, remaining)

    def test_rollback_refuses_to_drop_evidence_or_trust_sources(self):
        self._create_legacy_database()
        migration.migrate(self.db_path)

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            self._insert_trusted_sources(connection, enabled=False)
            self._insert_evidence(
                connection,
                verification_status="unverified",
                verification_source=None,
                verified_at=None,
            )
            connection.commit()

        with self.assertRaisesRegex(RuntimeError, "containing evidence"):
            migration.rollback(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    f"SELECT COUNT(*) FROM {PRODUCT_SPECIFICATIONS_TABLE}"
                ).fetchone()[0],
                1,
            )

    def test_rollback_refuses_to_drop_a_populated_trust_registry(self):
        self._create_legacy_database()
        migration.migrate(self.db_path)

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                f"""
                INSERT INTO {TRUSTED_SPECIFICATION_SOURCES_TABLE} (source_key)
                VALUES ('approved-catalog')
                """
            )
            connection.commit()

        with self.assertRaisesRegex(RuntimeError, "registry"):
            migration.rollback(self.db_path)

    def test_product_deletion_cascades_to_specs_and_existing_cart(self):
        self._create_legacy_database()
        migration.migrate(self.db_path)

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                f"""
                INSERT INTO {PRODUCT_SPECIFICATIONS_TABLE}
                    (product_id, attribute_name)
                VALUES (1, 'ram')
                """
            )
            connection.execute("DELETE FROM reviews WHERE product_id = 1")
            connection.execute("DELETE FROM orders WHERE product_id = 1")
            connection.execute("DELETE FROM products WHERE id = 1")
            self.assertEqual(
                connection.execute(
                    f"SELECT COUNT(*) FROM {PRODUCT_SPECIFICATIONS_TABLE}"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM cart_items").fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
