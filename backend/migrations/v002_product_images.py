from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from backend.database_paths import BUSINESS_SQLITE_PATH

DEFAULT_STORE_DB_PATH = BUSINESS_SQLITE_PATH


@dataclass(frozen=True)
class MigrationResult:
    product_count: int
    cart_item_count: int
    product_image_count: int
    foreign_key_violations: int


def _existing_rows(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
) -> tuple[tuple[object, ...], ...]:
    selected_columns = ", ".join(f'"{column}"' for column in columns)
    return tuple(connection.execute(
        f'SELECT {selected_columns} FROM "{table}" ORDER BY {selected_columns}'
    ).fetchall())


def migrate_database(
    database_path: str | Path = DEFAULT_STORE_DB_PATH,
) -> MigrationResult:
    path = Path(database_path)
    if not path.is_file():
        raise FileNotFoundError(f"Store database does not exist: {path}")

    with closing(sqlite3.connect(path, timeout=10)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("SQLite foreign-key enforcement could not be enabled.")

        try:
            connection.execute("BEGIN IMMEDIATE")
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if not {"products", "cart_items"}.issubset(tables):
                raise RuntimeError("The store database is missing required tables.")

            products_before = _existing_rows(
                connection,
                "products",
                ("id", "name", "category", "price", "description", "is_organic"),
            )
            cart_before = _existing_rows(
                connection,
                "cart_items",
                ("owner_id", "product_id", "quantity"),
            )

            connection.execute("""
                CREATE TABLE IF NOT EXISTS product_images (
                    product_id INTEGER PRIMARY KEY,
                    image_url TEXT,
                    local_image_path TEXT,
                    FOREIGN KEY (product_id)
                        REFERENCES products(id) ON DELETE RESTRICT
                )
            """)

            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(product_images)")
            }
            if not {"product_id", "image_url", "local_image_path"}.issubset(columns):
                raise RuntimeError("Existing product_images table has an incompatible schema.")
            if not any(
                row[2] == "products"
                and row[3] == "product_id"
                and row[4] == "id"
                and row[6].upper() == "RESTRICT"
                for row in connection.execute("PRAGMA foreign_key_list(product_images)")
            ):
                raise RuntimeError("product_images has an incompatible product foreign key.")

            products_after = _existing_rows(
                connection,
                "products",
                ("id", "name", "category", "price", "description", "is_organic"),
            )
            cart_after = _existing_rows(
                connection,
                "cart_items",
                ("owner_id", "product_id", "quantity"),
            )
            if products_before != products_after:
                raise RuntimeError("Product records changed during image migration.")
            if cart_before != cart_after:
                raise RuntimeError("Cart records changed during image migration.")

            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(
                    f"Foreign-key check found {len(violations)} violation(s)."
                )

            image_count = connection.execute(
                "SELECT COUNT(*) FROM product_images"
            ).fetchone()[0]
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise

    return MigrationResult(
        product_count=len(products_after),
        cart_item_count=len(cart_after),
        product_image_count=image_count,
        foreign_key_violations=0,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Add optional product image metadata.")
    parser.add_argument(
        "database",
        nargs="?",
        type=Path,
        default=DEFAULT_STORE_DB_PATH,
        help="SQLite store database path (defaults to the project store.db).",
    )
    arguments = parser.parse_args()
    print(migrate_database(arguments.database))
