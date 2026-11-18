from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from backend.database_paths import BUSINESS_SQLITE_PATH

DEFAULT_STORE_DB_PATH = BUSINESS_SQLITE_PATH
PRICE_TABLES = ("products", "orders")
_CURRENCY_CHECK = "currency GLOB '[A-Z][A-Z][A-Z]'"


@dataclass(frozen=True)
class CurrencyMetadataMigrationResult:
    columns_added: tuple[str, ...]
    unknown_currency_rows: dict[str, int]


def _table_columns(connection: sqlite3.Connection, table: str) -> dict[str, tuple]:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    if not rows:
        raise RuntimeError(f"Required table {table!r} is missing.")
    return {row[1]: row for row in rows}


def _validate_currency_values(
    connection: sqlite3.Connection,
    table: str,
) -> int:
    values = connection.execute(
        f'SELECT currency FROM "{table}" WHERE currency IS NOT NULL'
    ).fetchall()
    for (currency,) in values:
        if not isinstance(currency, str) or re.fullmatch(r"[A-Z]{3}", currency) is None:
            raise RuntimeError(
                f"Table {table!r} contains a malformed currency code."
            )
    return connection.execute(
        f'SELECT COUNT(*) FROM "{table}" WHERE currency IS NULL'
    ).fetchone()[0]


def migrate(
    db_path: str | Path = DEFAULT_STORE_DB_PATH,
) -> CurrencyMetadataMigrationResult:
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"Store database does not exist: {path}")

    connection = sqlite3.connect(path, timeout=30)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            columns_added = []
            for table in PRICE_TABLES:
                columns = _table_columns(connection, table)
                if "currency" not in columns:
                    connection.execute(
                        f"""
                        ALTER TABLE "{table}"
                        ADD COLUMN currency TEXT
                            CHECK (currency IS NULL OR {_CURRENCY_CHECK})
                        """
                    )
                    columns_added.append(table)
                else:
                    currency_column = columns["currency"]
                    if currency_column[2].upper() != "TEXT" or currency_column[3] != 0:
                        raise RuntimeError(
                            f"Table {table!r} has an incompatible currency column."
                        )

            unknown_currency_rows = {
                table: _validate_currency_values(connection, table)
                for table in PRICE_TABLES
            }
            connection.commit()
            return CurrencyMetadataMigrationResult(
                columns_added=tuple(columns_added),
                unknown_currency_rows=unknown_currency_rows,
            )
        except BaseException:
            connection.rollback()
            raise
    finally:
        connection.close()


def rollback(
    db_path: str | Path = DEFAULT_STORE_DB_PATH,
) -> tuple[str, ...]:
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"Store database does not exist: {path}")
    if sqlite3.sqlite_version_info < (3, 35, 0):
        raise RuntimeError("SQLite does not support safe DROP COLUMN rollback.")

    connection = sqlite3.connect(path, timeout=30)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            columns_to_drop = []
            for table in PRICE_TABLES:
                columns = _table_columns(connection, table)
                if "currency" in columns:
                    if connection.execute(
                        f'SELECT 1 FROM "{table}" WHERE currency IS NOT NULL LIMIT 1'
                    ).fetchone():
                        raise RuntimeError(
                            f"Cannot roll back {table!r} currency metadata containing values."
                        )
                    columns_to_drop.append(table)

            for table in columns_to_drop:
                connection.execute(
                    f'ALTER TABLE "{table}" DROP COLUMN currency'
                )
            connection.commit()
            return tuple(columns_to_drop)
        except BaseException:
            connection.rollback()
            raise
    finally:
        connection.close()
