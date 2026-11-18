from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from backend.product_specifications_schema import (
    PRODUCT_SPECIFICATIONS_TABLE,
    TRUSTED_SPECIFICATION_SOURCES_TABLE,
    ProductSpecificationsSchemaError,
    ensure_product_specifications_schema,
    validate_existing_product_specifications_schema,
)


_PRESERVED_TABLES = ("products", "reviews", "orders", "cart_items")


@dataclass(frozen=True)
class ProductSpecificationsMigrationResult:
    created_tables: tuple[str, ...]
    product_count: int
    evidence_count: int


def _snapshot_preserved_tables(
    connection: sqlite3.Connection,
) -> dict[str, tuple[tuple[str, ...], tuple[tuple[object, ...], ...]]]:
    existing_tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    snapshots = {}
    for table in _PRESERVED_TABLES:
        if table not in existing_tables:
            continue
        columns = tuple(
            row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
        )
        rows = tuple(
            connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
        )
        snapshots[table] = (columns, rows)
    return snapshots


def _require_products_table(connection: sqlite3.Connection) -> int:
    rows = connection.execute("PRAGMA table_info(products)").fetchall()
    id_column = next((row for row in rows if row[1] == "id"), None)
    if id_column is None or id_column[2].upper() != "INTEGER" or id_column[5] != 1:
        raise ProductSpecificationsSchemaError(
            "The existing products table must have an INTEGER PRIMARY KEY id."
        )
    return connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]


def migrate(db_path: str | Path) -> ProductSpecificationsMigrationResult:
    """Apply v004 to an explicitly supplied database file."""
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"Store database does not exist: {path}")

    with closing(sqlite3.connect(path, timeout=30)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("SQLite foreign-key enforcement could not be enabled.")

        connection.execute("BEGIN IMMEDIATE")
        try:
            product_count = _require_products_table(connection)
            preserved_before = _snapshot_preserved_tables(connection)
            created_tables = ensure_product_specifications_schema(connection)

            preserved_after = _snapshot_preserved_tables(connection)
            if preserved_after != preserved_before:
                raise RuntimeError(
                    "The product specifications migration changed existing product, "
                    "cart, or related records."
                )

            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(
                    f"Foreign-key check found {len(violations)} violation(s)."
                )

            evidence_count = connection.execute(
                f"SELECT COUNT(*) FROM {PRODUCT_SPECIFICATIONS_TABLE}"
            ).fetchone()[0]
            connection.commit()
            return ProductSpecificationsMigrationResult(
                created_tables=created_tables,
                product_count=product_count,
                evidence_count=evidence_count,
            )
        except BaseException:
            connection.rollback()
            raise


def rollback(db_path: str | Path) -> tuple[str, ...]:
    """Remove only empty v004 tables from an explicitly supplied database file."""
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"Store database does not exist: {path}")

    with closing(sqlite3.connect(path, timeout=30)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("SQLite foreign-key enforcement could not be enabled.")

        connection.execute("BEGIN IMMEDIATE")
        try:
            validate_existing_product_specifications_schema(connection)
            evidence_count = connection.execute(
                f"""
                SELECT COUNT(*)
                FROM {PRODUCT_SPECIFICATIONS_TABLE}
                WHERE value IS NOT NULL
                   OR unit IS NOT NULL
                   OR verification_status IS NOT NULL
                   OR source_reference IS NOT NULL
                   OR source_key IS NOT NULL
                   OR verification_source IS NOT NULL
                   OR observed_at IS NOT NULL
                   OR verified_at IS NOT NULL
                """
            ).fetchone()[0]
            trusted_source_count = connection.execute(
                f"SELECT COUNT(*) FROM {TRUSTED_SPECIFICATION_SOURCES_TABLE}"
            ).fetchone()[0]
            if evidence_count:
                raise RuntimeError(
                    "Cannot roll back product specifications containing evidence."
                )
            if trusted_source_count:
                raise RuntimeError(
                    "Cannot roll back the populated trusted specification source registry."
                )

            connection.execute(f"DROP TABLE {PRODUCT_SPECIFICATIONS_TABLE}")
            connection.execute(f"DROP TABLE {TRUSTED_SPECIFICATION_SOURCES_TABLE}")
            connection.commit()
            return (
                PRODUCT_SPECIFICATIONS_TABLE,
                TRUSTED_SPECIFICATION_SOURCES_TABLE,
            )
        except BaseException:
            connection.rollback()
            raise


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply or roll back v004 on an explicit SQLite database."
    )
    parser.add_argument(
        "--database",
        required=True,
        type=Path,
        help="Database file to migrate.",
    )
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="Remove v004 tables only when they contain no evidence or trusted sources.",
    )
    arguments = parser.parse_args()
    if arguments.rollback:
        print(rollback(arguments.database))
    else:
        print(migrate(arguments.database))


if __name__ == "__main__":
    main()
