from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from backend.currency import MAX_SQLITE_INTEGER, to_minor_units, validate_currency_code
from backend.database_paths import BUSINESS_SQLITE_PATH


DEFAULT_STORE_DB_PATH: Final = BUSINESS_SQLITE_PATH
LOCAL_PROVIDER_KEY: Final = "local_catalog"
LOCAL_PROVIDER_NAME: Final = "Local catalog"
CORE_TABLES: Final = ("products", "reviews", "orders", "cart_items")
SQLITE_MAX_INTEGER: Final = MAX_SQLITE_INTEGER


@dataclass(frozen=True)
class MigrationResult:
    provider_key: str
    canonical_products: int
    local_offers: int
    canonical_products_created: int
    local_offers_created: int
    prices_verified: int
    preserved_row_counts: dict[str, int]
    foreign_key_violations: int


def inr_to_paise(value: object, product_id: int) -> int:
    try:
        return to_minor_units(value, "INR")
    except ValueError as exc:
        raise ValueError(f"Product {product_id} has no valid INR price.") from exc


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _create_tables(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS product_providers (
            provider_key TEXT PRIMARY KEY,
            display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
            terms_url TEXT,
            enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS canonical_products (
            canonical_product_id TEXT PRIMARY KEY,
            legacy_product_id INTEGER UNIQUE,
            name TEXT NOT NULL CHECK (length(trim(name)) > 0),
            brand TEXT,
            model TEXT,
            gtin TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (legacy_product_id)
                REFERENCES products(id) ON DELETE RESTRICT
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS product_offers (
            offer_id TEXT PRIMARY KEY,
            provider_key TEXT NOT NULL,
            provider_offer_key TEXT NOT NULL,
            provider_product_id TEXT NOT NULL,
            canonical_product_id TEXT,
            product_name TEXT NOT NULL CHECK (length(trim(product_name)) > 0),
            brand TEXT,
            model TEXT,
            price_minor INTEGER CHECK (price_minor IS NULL OR price_minor >= 0),
            currency TEXT CHECK (
                currency IS NULL OR currency GLOB '[A-Z][A-Z][A-Z]'
            ),
            product_url TEXT,
            image_url TEXT,
            availability TEXT NOT NULL CHECK (
                availability IN ('in_stock', 'out_of_stock', 'preorder', 'backorder', 'unknown')
            ),
            last_checked_at TEXT,
            expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (provider_key, provider_offer_key),
            CHECK (
                (price_minor IS NULL AND currency IS NULL) OR
                (
                    price_minor IS NOT NULL
                    AND currency GLOB '[A-Z][A-Z][A-Z]'
                )
            ),
            FOREIGN KEY (provider_key)
                REFERENCES product_providers(provider_key) ON DELETE RESTRICT,
            FOREIGN KEY (canonical_product_id)
                REFERENCES canonical_products(canonical_product_id) ON DELETE RESTRICT
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS cart_offer_items (
            owner_id TEXT NOT NULL,
            offer_id TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            selected_product_name TEXT NOT NULL,
            selected_provider_key TEXT NOT NULL,
            selected_provider_product_id TEXT NOT NULL,
            selected_price_minor INTEGER NOT NULL CHECK (selected_price_minor >= 0),
            selected_currency TEXT NOT NULL CHECK (
                selected_currency GLOB '[A-Z][A-Z][A-Z]'
            ),
            selected_product_url TEXT,
            selected_availability TEXT NOT NULL CHECK (
                selected_availability IN ('in_stock', 'out_of_stock', 'preorder', 'backorder', 'unknown')
            ),
            added_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, offer_id),
            FOREIGN KEY (offer_id)
                REFERENCES product_offers(offer_id) ON DELETE RESTRICT
        )
    """)


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[1]
        for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    }


def _unique_column_sets(
    connection: sqlite3.Connection,
    table: str,
) -> set[tuple[str, ...]]:
    unique_sets: set[tuple[str, ...]] = set()
    for index in connection.execute(f'PRAGMA index_list("{table}")').fetchall():
        if index[2]:
            columns = connection.execute(
                f'PRAGMA index_info("{index[1]}")'
            ).fetchall()
            unique_sets.add(tuple(column[2] for column in columns))
    return unique_sets


def _foreign_key_exists(
    connection: sqlite3.Connection,
    table: str,
    from_column: str,
    to_table: str,
    to_column: str,
    on_delete: str,
) -> bool:
    return any(
        row[2] == to_table
        and row[3] == from_column
        and row[4] == to_column
        and row[6].upper() == on_delete
        for row in connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
    )


def _normalized_table_sql(connection: sqlite3.Connection, table: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if row is None or not row[0]:
        raise RuntimeError(f"Required table {table!r} is missing.")
    return re.sub(r"\s+", " ", row[0]).strip().casefold()


def _verify_schema(connection: sqlite3.Connection) -> None:
    required_columns = {
        "product_providers": {
            "provider_key", "display_name", "terms_url", "enabled",
        },
        "canonical_products": {
            "canonical_product_id", "legacy_product_id", "name", "brand", "model",
            "gtin", "created_at", "updated_at",
        },
        "product_offers": {
            "offer_id", "provider_key", "provider_offer_key", "provider_product_id",
            "canonical_product_id", "product_name", "brand", "model", "price_minor",
            "currency", "product_url", "image_url", "availability",
            "last_checked_at", "expires_at", "created_at", "updated_at",
        },
        "cart_offer_items": {
            "owner_id", "offer_id", "quantity", "selected_product_name",
            "selected_provider_key", "selected_provider_product_id",
            "selected_price_minor", "selected_currency", "selected_product_url",
            "selected_availability", "added_at",
        },
    }
    for table, columns in required_columns.items():
        if not columns.issubset(_table_columns(connection, table)):
            raise RuntimeError(f"Table {table!r} does not match the migration schema.")

    primary_keys = {
        table: tuple(
            row[1]
            for row in sorted(
                (
                    row for row in connection.execute(
                        f'PRAGMA table_info("{table}")'
                    ).fetchall()
                    if row[5] > 0
                ),
                key=lambda row: row[5],
            )
        )
        for table in required_columns
    }
    expected_primary_keys = {
        "product_providers": ("provider_key",),
        "canonical_products": ("canonical_product_id",),
        "product_offers": ("offer_id",),
        "cart_offer_items": ("owner_id", "offer_id"),
    }
    if primary_keys != expected_primary_keys:
        raise RuntimeError("A product-offer table has an unexpected primary key.")

    unique_sets = _unique_column_sets(connection, "product_offers")
    if ("provider_key", "provider_offer_key") not in unique_sets:
        raise RuntimeError("Product offers lack their provider offer uniqueness constraint.")
    if ("legacy_product_id",) not in _unique_column_sets(
        connection, "canonical_products"
    ):
        raise RuntimeError("Canonical products lack unique legacy product mappings.")

    expected_foreign_keys = (
        ("canonical_products", "legacy_product_id", "products", "id", "RESTRICT"),
        (
            "product_offers", "provider_key", "product_providers", "provider_key",
            "RESTRICT",
        ),
        (
            "product_offers", "canonical_product_id", "canonical_products",
            "canonical_product_id", "RESTRICT",
        ),
        (
            "cart_offer_items", "offer_id", "product_offers", "offer_id",
            "RESTRICT",
        ),
    )
    for table, from_col, to_table, to_col, on_delete in expected_foreign_keys:
        if not _foreign_key_exists(
            connection, table, from_col, to_table, to_col, on_delete
        ):
            raise RuntimeError(
                f"Table {table!r} lacks the required {on_delete} foreign key."
            )

    checks = {
        "canonical_products": ("unique",),  # legacy_product_id is unique.
        "product_offers": (
            "check (price_minor is null or price_minor >= 0)",
            "currency glob '[a-z][a-z][a-z]'",
            "price_minor is null and currency is null",
            "price_minor is not null and currency glob",
            "check ( availability in",
        ),
        "cart_offer_items": (
            "check (quantity > 0)",
            "check (selected_price_minor >= 0)",
            "selected_currency glob '[a-z][a-z][a-z]'",
        ),
    }
    for table, fragments in checks.items():
        schema_sql = _normalized_table_sql(connection, table)
        if any(fragment not in schema_sql for fragment in fragments):
            raise RuntimeError(f"Table {table!r} is missing a required constraint.")


def _snapshot_core_tables(
    connection: sqlite3.Connection,
) -> dict[str, tuple[str, tuple[str, ...], list[tuple[object, ...]]]]:
    snapshot = {}
    for table in CORE_TABLES:
        ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if ddl is None:
            raise RuntimeError(f"Existing core table {table!r} is missing.")
        columns = tuple(
            row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
        )
        rows = connection.execute(
            f'SELECT * FROM "{table}" ORDER BY rowid'
        ).fetchall()
        snapshot[table] = (ddl[0], columns, rows)
    return snapshot


def _register_local_provider(connection: sqlite3.Connection) -> None:
    existing = connection.execute(
        """
        SELECT display_name, terms_url, enabled
        FROM product_providers
        WHERE provider_key = ?
        """,
        (LOCAL_PROVIDER_KEY,),
    ).fetchone()
    if existing is None:
        connection.execute(
            """
            INSERT INTO product_providers (
                provider_key, display_name, terms_url, enabled
            ) VALUES (?, ?, NULL, 1)
            """,
            (LOCAL_PROVIDER_KEY, LOCAL_PROVIDER_NAME),
        )
    elif existing != (LOCAL_PROVIDER_NAME, None, 1):
        raise RuntimeError(
            f"Provider configuration for {LOCAL_PROVIDER_KEY!r} conflicts "
            "with the local catalog migration."
        )


def _upsert_canonical_product(
    connection: sqlite3.Connection,
    product_id: int,
    name: str,
    timestamp: str,
) -> tuple[str, bool]:
    existing = connection.execute(
        """
        SELECT canonical_product_id
        FROM canonical_products
        WHERE legacy_product_id = ?
        """,
        (product_id,),
    ).fetchone()
    if existing is None:
        canonical_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO canonical_products (
                canonical_product_id, legacy_product_id, name, brand, model, gtin,
                created_at, updated_at
            ) VALUES (?, ?, ?, NULL, NULL, NULL, ?, ?)
            """,
            (canonical_id, product_id, name, timestamp, timestamp),
        )
        return canonical_id, True

    canonical_id = existing[0]
    try:
        uuid.UUID(canonical_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise RuntimeError(
            f"Canonical product for local product {product_id} has an invalid UUID."
        ) from exc
    return canonical_id, False


def _upsert_local_offer(
    connection: sqlite3.Connection,
    product_id: int,
    canonical_id: str,
    name: str,
    price_minor: int | None,
    currency: str | None,
    timestamp: str,
) -> bool:
    provider_offer_key = f"local-product:{product_id}"
    existing = connection.execute(
        """
        SELECT offer_id, provider_product_id, canonical_product_id
        FROM product_offers
        WHERE provider_key = ? AND provider_offer_key = ?
        """,
        (LOCAL_PROVIDER_KEY, provider_offer_key),
    ).fetchone()
    if existing is None:
        offer_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO product_offers (
                offer_id, provider_key, provider_offer_key, provider_product_id,
                canonical_product_id, product_name, brand, model, price_minor,
                currency, product_url, image_url, availability, last_checked_at,
                expires_at, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, NULL, NULL, 'unknown',
                ?, NULL, ?, ?
            )
            """,
            (
                offer_id,
                LOCAL_PROVIDER_KEY,
                provider_offer_key,
                str(product_id),
                canonical_id,
                name,
                price_minor,
                currency,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        return True

    offer_id, existing_product_id, existing_canonical_id = existing
    try:
        uuid.UUID(offer_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise RuntimeError(
            f"Local offer for product {product_id} has an invalid UUID."
        ) from exc
    if (
        existing_product_id != str(product_id)
        or existing_canonical_id != canonical_id
    ):
        raise RuntimeError(
            f"Local offer mapping for product {product_id} conflicts with "
            "the existing provider offer."
        )
    return False


def _verify_local_mappings(
    connection: sqlite3.Connection,
    products: list[tuple[int, str, object, str | None]],
) -> None:
    local_offers = connection.execute(
        """
        SELECT provider_offer_key
        FROM product_offers
        WHERE provider_key = ?
        """,
        (LOCAL_PROVIDER_KEY,),
    ).fetchall()
    expected_keys = {
        f"local-product:{product_id}" for product_id, _, _, _ in products
    }
    if not expected_keys.issubset({row[0] for row in local_offers}):
        raise RuntimeError("A local offer is missing for an existing product ID.")

    for product_id, _, _, _ in products:
        canonical = connection.execute(
            """
            SELECT canonical_product_id
            FROM canonical_products
            WHERE legacy_product_id = ?
            """,
            (product_id,),
        ).fetchone()
        if canonical is None:
            raise RuntimeError(
                f"Canonical mapping for local product {product_id} is incorrect."
            )

        offer = connection.execute(
            """
            SELECT offer_id, provider_product_id, canonical_product_id
            FROM product_offers
            WHERE provider_key = ? AND provider_offer_key = ?
            """,
            (LOCAL_PROVIDER_KEY, f"local-product:{product_id}"),
        ).fetchone()
        if (
            offer is None
            or offer[1] != str(product_id)
            or offer[2] != canonical[0]
        ):
            raise RuntimeError(
                f"Local offer mapping for product {product_id} is incorrect."
            )
        try:
            uuid.UUID(canonical[0])
            uuid.UUID(offer[0])
        except (TypeError, ValueError, AttributeError) as exc:
            raise RuntimeError(
                f"Product {product_id} is missing a valid persistent UUID."
            ) from exc


def migrate(
    db_path: str | Path = DEFAULT_STORE_DB_PATH,
) -> MigrationResult:
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"Store database does not exist: {path}")

    connection = sqlite3.connect(path, timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("SQLite foreign-key enforcement could not be enabled.")
        connection.execute("BEGIN IMMEDIATE")
        try:
            original_snapshot = _snapshot_core_tables(connection)
            _create_tables(connection)
            _verify_schema(connection)
            _register_local_provider(connection)

            product_columns = _table_columns(connection, "products")
            currency_column = "currency" if "currency" in product_columns else "NULL"
            products = connection.execute(
                f"SELECT id, name, price, {currency_column} FROM products ORDER BY id"
            ).fetchall()
            timestamp = _utc_timestamp()
            canonical_created = 0
            offers_created = 0
            prices_verified = 0

            for product_id, name, price, currency in products:
                if not isinstance(name, str) or not name.strip():
                    raise ValueError(
                        f"Product {product_id} has no valid name for canonical mapping."
                    )
                if currency is None:
                    price_minor = None
                    normalized_currency = None
                else:
                    try:
                        normalized_currency = validate_currency_code(currency)
                        price_minor = to_minor_units(price, normalized_currency)
                    except ValueError as exc:
                        raise ValueError(
                            f"Product {product_id} has invalid currency or price metadata."
                        ) from exc
                    prices_verified += 1

                canonical_id, canonical_was_created = _upsert_canonical_product(
                    connection,
                    product_id,
                    name,
                    timestamp,
                )
                canonical_created += int(canonical_was_created)
                offers_created += int(
                    _upsert_local_offer(
                        connection,
                        product_id,
                        canonical_id,
                        name,
                        price_minor,
                        normalized_currency,
                        timestamp,
                    )
                )

            _verify_local_mappings(connection, products)
            current_snapshot = _snapshot_core_tables(connection)
            if current_snapshot != original_snapshot:
                raise RuntimeError(
                    "Migration changed an existing core table or its records."
                )

            foreign_key_violations = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if foreign_key_violations:
                raise RuntimeError(
                    f"Foreign-key check failed: {len(foreign_key_violations)} violation(s)."
                )

            result = MigrationResult(
                provider_key=LOCAL_PROVIDER_KEY,
                canonical_products=len(products),
                local_offers=len(products),
                canonical_products_created=canonical_created,
                local_offers_created=offers_created,
                prices_verified=prices_verified,
                preserved_row_counts={
                    table: len(original_snapshot[table][2])
                    for table in CORE_TABLES
                },
                foreign_key_violations=0,
            )
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise
    finally:
        connection.close()


if __name__ == "__main__":
    print(json.dumps(asdict(migrate()), sort_keys=True))
