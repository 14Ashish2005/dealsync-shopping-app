from __future__ import annotations

import sqlite3
from pathlib import Path

from backend.database_paths import BUSINESS_SQLITE_PATH

DEFAULT_STORE_DB_PATH = BUSINESS_SQLITE_PATH


def migrate(db_path: str | Path = DEFAULT_STORE_DB_PATH) -> tuple[str, ...]:
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"Store database does not exist: {path}")

    connection = sqlite3.connect(path, timeout=30)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'orders'"
            ).fetchone()
            if table_exists is None:
                connection.execute(
                    """
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
                        order_reference TEXT,
                        quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),
                        total_minor INTEGER CHECK (
                            total_minor IS NULL OR total_minor >= 0
                        ),
                        order_currency TEXT CHECK (
                            order_currency IS NULL OR
                            order_currency GLOB '[A-Z][A-Z][A-Z]'
                        ),
                        FOREIGN KEY (product_id) REFERENCES products(id)
                    )
                    """
                )

            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(orders)")
            }
            required_base_columns = {"id", "product_id", "product_name", "price"}
            missing_base_columns = required_base_columns - columns
            if missing_base_columns:
                raise RuntimeError(
                    "The orders table is missing required columns: "
                    f"{', '.join(sorted(missing_base_columns))}."
                )

            additions = (
                ("ordered_at", "TEXT"),
                ("owner_id", "TEXT"),
                ("status", "TEXT"),
                (
                    "currency",
                    "TEXT CHECK (currency IS NULL OR currency GLOB '[A-Z][A-Z][A-Z]')",
                ),
                ("order_reference", "TEXT"),
                ("quantity", "INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0)"),
                (
                    "total_minor",
                    "INTEGER CHECK (total_minor IS NULL OR total_minor >= 0)",
                ),
                (
                    "order_currency",
                    "TEXT CHECK (order_currency IS NULL OR "
                    "order_currency GLOB '[A-Z][A-Z][A-Z]')",
                ),
            )
            columns_added = []
            for name, definition in additions:
                if name not in columns:
                    connection.execute(
                        f'ALTER TABLE orders ADD COLUMN "{name}" {definition}'
                    )
                    columns_added.append(name)

            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_owner_id_id "
                "ON orders(owner_id, id DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_owner_reference "
                "ON orders(owner_id, order_reference)"
            )
            connection.commit()
            return tuple(columns_added)
        except BaseException:
            connection.rollback()
            raise
    finally:
        connection.close()
