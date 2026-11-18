from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from backend.currency import MAX_SQLITE_INTEGER


MAX_CART_QUANTITY = 9_007_199_254_740_991
_QUOTE_STATUSES = (
    "'verified_current', 'stale_unverified', 'unavailable', "
    "'price_change_pending'"
)
_REQUIRED_V001_COLUMNS = {
    "products": {"id"},
    "product_providers": {
        "provider_key",
        "display_name",
        "terms_url",
        "enabled",
    },
    "canonical_products": {
        "canonical_product_id",
        "legacy_product_id",
        "name",
        "brand",
        "model",
        "gtin",
        "created_at",
        "updated_at",
    },
    "product_offers": {
        "offer_id",
        "provider_key",
        "provider_offer_key",
        "provider_product_id",
        "canonical_product_id",
        "price_minor",
        "currency",
        "product_name",
        "brand",
        "model",
        "product_url",
        "image_url",
        "availability",
        "last_checked_at",
        "expires_at",
        "created_at",
        "updated_at",
    },
    "cart_offer_items": {
        "owner_id",
        "offer_id",
        "quantity",
        "selected_product_name",
        "selected_provider_key",
        "selected_provider_product_id",
        "selected_price_minor",
        "selected_currency",
        "selected_product_url",
        "selected_availability",
        "added_at",
    },
}
_EXPECTED_PRIMARY_KEYS = {
    "product_providers": ("provider_key",),
    "canonical_products": ("canonical_product_id",),
    "product_offers": ("offer_id",),
    "cart_offer_items": ("owner_id", "offer_id"),
}
_EXPECTED_UNIQUE_COLUMN_SETS = {
    "canonical_products": {("legacy_product_id",)},
    "product_offers": {("provider_key", "provider_offer_key")},
}
_EXPECTED_FOREIGN_KEY_GROUPS = {
    "product_providers": (),
    "canonical_products": (
        (
            "products",
            (("legacy_product_id", "id"),),
            "NO ACTION",
            "RESTRICT",
            "NONE",
        ),
    ),
    "product_offers": (
        (
            "product_providers",
            (("provider_key", "provider_key"),),
            "NO ACTION",
            "RESTRICT",
            "NONE",
        ),
        (
            "canonical_products",
            (("canonical_product_id", "canonical_product_id"),),
            "NO ACTION",
            "RESTRICT",
            "NONE",
        ),
    ),
    "cart_offer_items": (
        (
            "product_offers",
            (("offer_id", "offer_id"),),
            "NO ACTION",
            "RESTRICT",
            "NONE",
        ),
    ),
}
_EXPECTED_CHECK_FRAGMENTS = {
    "product_offers": (
        "check (price_minor is null or price_minor >= 0)",
        "currency glob '[a-z][a-z][a-z]'",
        "price_minor is null and currency is null",
        "price_minor is not null and currency glob '[a-z][a-z][a-z]'",
        (
            "check ( availability in "
            "('in_stock', 'out_of_stock', 'preorder', 'backorder', 'unknown')"
        ),
    ),
    "cart_offer_items": (
        "check (quantity > 0)",
        "check (selected_price_minor >= 0)",
        "selected_currency glob '[a-z][a-z][a-z]'",
        (
            "check ( selected_availability in "
            "('in_stock', 'out_of_stock', 'preorder', 'backorder', 'unknown')"
        ),
    ),
}
_ADDITIONS = (
    (
        "selected_provider_offer_key",
        "TEXT",
    ),
    (
        "selected_verified_at",
        "TEXT",
    ),
    (
        "quote_status",
        "TEXT NOT NULL DEFAULT 'stale_unverified' "
        "CHECK (quote_status IN ("
        + _QUOTE_STATUSES
        + "))",
    ),
    (
        "pending_price_minor",
        "INTEGER CHECK (pending_price_minor IS NULL OR pending_price_minor >= 0)",
    ),
    (
        "pending_currency",
        "TEXT CHECK (pending_currency IS NULL OR "
        "pending_currency GLOB '[A-Z][A-Z][A-Z]')",
    ),
    (
        "pending_verified_at",
        "TEXT",
    ),
    (
        "pending_product_name",
        "TEXT",
    ),
    (
        "pending_product_url",
        "TEXT",
    ),
    (
        "pending_availability",
        "TEXT",
    ),
)


@dataclass(frozen=True)
class MigrationResult:
    added_columns: tuple[str, ...]
    backfilled_offer_keys: int
    preserved_cart_item_count: int
    foreign_key_violations: int


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[1]
        for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    }


def _primary_key_columns(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[str, ...]:
    return tuple(
        row[1]
        for row in sorted(
            (
                row
                for row in connection.execute(
                    f'PRAGMA table_info("{table}")'
                ).fetchall()
                if row[5] > 0
            ),
            key=lambda row: row[5],
        )
    )


def _unique_column_sets(
    connection: sqlite3.Connection,
    table: str,
) -> set[tuple[str, ...]]:
    unique_sets: set[tuple[str, ...]] = set()
    for index in connection.execute(f'PRAGMA index_list("{table}")').fetchall():
        if index[2] and not index[4]:
            columns = connection.execute(
                f'PRAGMA index_info("{index[1]}")'
            ).fetchall()
            unique_sets.add(tuple(column[2] for column in columns))
    return unique_sets


def _foreign_key_groups(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple[str, tuple[tuple[str, str], ...], str, str, str], ...]:
    grouped_rows: dict[int, list[tuple]] = {}
    for row in connection.execute(
        f'PRAGMA foreign_key_list("{table}")'
    ).fetchall():
        grouped_rows.setdefault(row[0], []).append(row)

    groups = []
    for foreign_key_id, rows in grouped_rows.items():
        ordered_rows = sorted(rows, key=lambda row: row[1])
        metadata = {
            (
                row[2],
                row[5].upper(),
                row[6].upper(),
                row[7].upper(),
            )
            for row in ordered_rows
        }
        if len(metadata) != 1:
            raise RuntimeError(
                f"Foreign key {foreign_key_id} on table {table!r} is inconsistent."
            )
        referenced_table, on_update, on_delete, match = metadata.pop()
        column_mappings = tuple(
            (row[3], row[4]) for row in ordered_rows
        )
        groups.append(
            (
                referenced_table,
                column_mappings,
                on_update,
                on_delete,
                match,
            )
        )
    return tuple(sorted(groups))


def _require_schema_checks(connection: sqlite3.Connection) -> None:
    for table, fragments in _EXPECTED_CHECK_FRAGMENTS.items():
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if row is None or not row[0]:
            raise RuntimeError(f"Required table {table!r} is missing.")
        normalized_sql = " ".join(row[0].split()).casefold()
        if any(fragment not in normalized_sql for fragment in fragments):
            raise RuntimeError(
                f"Table {table!r} is missing a required v001 constraint."
            )


def _require_v001_schema(connection: sqlite3.Connection) -> None:
    for table, required_columns in _REQUIRED_V001_COLUMNS.items():
        present = _table_columns(connection, table)
        if not required_columns.issubset(present):
            raise RuntimeError(
                "The v001 product-offer migration must be applied before "
                "the external-cart quote migration."
            )

    for table, expected_columns in _EXPECTED_PRIMARY_KEYS.items():
        if _primary_key_columns(connection, table) != expected_columns:
            raise RuntimeError(
                f"Table {table!r} does not have the expected v001 primary key."
            )

    for table, expected_sets in _EXPECTED_UNIQUE_COLUMN_SETS.items():
        if not expected_sets.issubset(_unique_column_sets(connection, table)):
            raise RuntimeError(
                f"Table {table!r} is missing a required v001 unique constraint."
            )

    for table, expected_groups in _EXPECTED_FOREIGN_KEY_GROUPS.items():
        if _foreign_key_groups(connection, table) != tuple(sorted(expected_groups)):
            raise RuntimeError(
                f"Table {table!r} has foreign keys that do not match v001."
            )

    _require_schema_checks(connection)


def _create_validation_triggers(connection: sqlite3.Connection) -> None:
    invalid_row = f"""
        NEW.quantity < 1
        OR NEW.quantity > {MAX_CART_QUANTITY}
        OR NEW.selected_provider_offer_key IS NULL
        OR trim(NEW.selected_provider_offer_key) = ''
        OR NEW.quote_status NOT IN ({_QUOTE_STATUSES})
        OR (
            NEW.quote_status = 'verified_current'
            AND (
                NEW.selected_verified_at IS NULL
                OR trim(NEW.selected_verified_at) = ''
            )
        )
        OR (
            NEW.quote_status = 'price_change_pending'
            AND (
                NEW.pending_price_minor IS NULL
                OR NEW.pending_currency IS NULL
                OR NEW.pending_verified_at IS NULL
                OR trim(NEW.pending_verified_at) = ''
                OR NEW.pending_product_name IS NULL
                OR trim(NEW.pending_product_name) = ''
                OR NEW.pending_availability IS NULL
                OR NEW.pending_availability NOT IN (
                    'in_stock', 'out_of_stock', 'preorder', 'backorder', 'unknown'
                )
            )
        )
        OR (
            NEW.quote_status != 'price_change_pending'
            AND (
                NEW.pending_price_minor IS NOT NULL
                OR NEW.pending_currency IS NOT NULL
                OR NEW.pending_verified_at IS NOT NULL
                OR NEW.pending_product_name IS NOT NULL
                OR NEW.pending_product_url IS NOT NULL
                OR NEW.pending_availability IS NOT NULL
            )
        )
        OR typeof(NEW.quantity) != 'integer'
        OR typeof(NEW.selected_price_minor) != 'integer'
        OR (
            NEW.pending_price_minor IS NOT NULL
            AND (
                typeof(NEW.pending_price_minor) != 'integer'
                OR NEW.pending_price_minor > {MAX_SQLITE_INTEGER}
            )
        )
        OR (
            NEW.pending_currency IS NOT NULL
            AND NEW.pending_currency NOT GLOB '[A-Z][A-Z][A-Z]'
        )
        OR (
            NEW.pending_product_url IS NOT NULL
            AND length(NEW.pending_product_url) > 2048
        )
        OR (
            NEW.pending_product_name IS NOT NULL
            AND length(NEW.pending_product_name) > 200
        )
        OR length(NEW.selected_provider_key) > 64
        OR length(NEW.selected_provider_offer_key) > 256
        OR length(NEW.selected_provider_product_id) > 128
        OR length(NEW.selected_product_name) > 200
        OR (
            NEW.selected_product_url IS NOT NULL
            AND (
                length(NEW.selected_product_url) > 2048
                OR lower(substr(NEW.selected_product_url, 1, 8)) != 'https://'
            )
        )
        OR (
            NEW.pending_product_url IS NOT NULL
            AND lower(substr(NEW.pending_product_url, 1, 8)) != 'https://'
        )
        OR NOT EXISTS (
            SELECT 1
            FROM product_offers
            WHERE offer_id = NEW.offer_id
              AND provider_key = NEW.selected_provider_key
              AND provider_offer_key = NEW.selected_provider_offer_key
              AND provider_product_id = NEW.selected_provider_product_id
        )
    """
    for operation in ("INSERT", "UPDATE"):
        trigger_name = f"cart_offer_items_validate_{operation.lower()}"
        connection.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {trigger_name}
            BEFORE {operation} ON cart_offer_items
            WHEN {invalid_row}
            BEGIN
                SELECT RAISE(ABORT, 'invalid external cart offer snapshot');
            END
            """
        )


def migrate(db_path: str | Path) -> MigrationResult:
    """Apply v002 to an explicitly supplied database file; there is no live-DB default."""
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"Database does not exist: {path}")

    connection = sqlite3.connect(path, timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("SQLite foreign-key enforcement could not be enabled.")

        connection.execute("BEGIN IMMEDIATE")
        try:
            _require_v001_schema(connection)
            original_count = connection.execute(
                "SELECT COUNT(*) FROM cart_offer_items"
            ).fetchone()[0]

            added_columns: list[str] = []
            columns = _table_columns(connection, "cart_offer_items")
            for name, declaration in _ADDITIONS:
                if name not in columns:
                    connection.execute(
                        f'ALTER TABLE cart_offer_items ADD COLUMN "{name}" {declaration}'
                    )
                    added_columns.append(name)
                    columns.add(name)

            backfill_cursor = connection.execute(
                """
                UPDATE cart_offer_items
                SET selected_provider_offer_key = (
                    SELECT product_offers.provider_offer_key
                    FROM product_offers
                    WHERE product_offers.offer_id = cart_offer_items.offer_id
                )
                WHERE selected_provider_offer_key IS NULL
                """
            )
            backfilled = backfill_cursor.rowcount

            invalid_identity_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM cart_offer_items AS cart
                WHERE cart.selected_provider_offer_key IS NULL
                   OR NOT EXISTS (
                       SELECT 1
                       FROM product_offers AS offer
                       WHERE offer.offer_id = cart.offer_id
                         AND offer.provider_key = cart.selected_provider_key
                         AND offer.provider_offer_key =
                             cart.selected_provider_offer_key
                         AND offer.provider_product_id =
                             cart.selected_provider_product_id
                   )
                """
            ).fetchone()[0]
            if invalid_identity_count:
                raise RuntimeError(
                    "Existing external-cart rows do not match their exact provider offer."
                )

            _create_validation_triggers(connection)

            current_count = connection.execute(
                "SELECT COUNT(*) FROM cart_offer_items"
            ).fetchone()[0]
            if current_count != original_count:
                raise RuntimeError("The migration changed the external-cart row count.")

            foreign_key_violations = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if foreign_key_violations:
                raise RuntimeError(
                    f"Foreign-key check failed: {len(foreign_key_violations)} violation(s)."
                )

            result = MigrationResult(
                added_columns=tuple(added_columns),
                backfilled_offer_keys=backfilled,
                preserved_cart_item_count=current_count,
                foreign_key_violations=0,
            )
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply the external-cart quote migration to an explicit database file."
    )
    parser.add_argument("--database", required=True, help="Database file to migrate.")
    arguments = parser.parse_args()
    result = migrate(arguments.database)
    print(
        "External-cart migration complete: "
        f"{len(result.added_columns)} column(s) added, "
        f"{result.backfilled_offer_keys} offer key(s) backfilled."
    )


if __name__ == "__main__":
    main()
