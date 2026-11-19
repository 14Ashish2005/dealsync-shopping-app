"""Explicit bootstrap for genuinely fresh DealSync deployment directories only.

If publication is interrupted, stop the application and do not rerun against or
delete either final database. Preserve and inspect any existing file read-only.
Only after confirming it contains no user data may an operator move it to a
unique quarantine name (never delete it) and retry with both target paths absent.
If it may contain user data, restore or reconcile it from a verified backup, or
choose new target paths; never initialize over it.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from contextlib import closing
from pathlib import Path

from backend.migrations import (
    v001_product_offers,
    v002_external_cart_quotes,
    v002_product_images,
    v003_currency_metadata,
    v004_product_specifications,
    v005_demo_orders,
)
from setup_db import create_database


MIGRATION_ORDER = (
    "v001_product_offers",
    "v002_external_cart_quotes",
    "v002_product_images",
    "v003_currency_metadata",
    "v004_product_specifications",
    "v005_demo_orders",
)

_BUSINESS_COLUMNS = {
    "products": {
        "id", "name", "category", "price", "description", "is_organic", "currency",
    },
    "reviews": {"id", "product_id", "rating", "reviewer_name", "review_text"},
    "orders": {
        "id", "product_id", "product_name", "price", "ordered_at", "owner_id",
        "status", "currency", "order_reference", "quantity", "total_minor",
        "order_currency",
    },
    "cart_items": {"owner_id", "product_id", "quantity"},
    "cart_confirmation_outcomes": {
        "owner_id", "token_hash", "references_json", "outcome_json", "expires_at",
    },
    "trusted_specification_sources": {"source_key", "enabled"},
    "product_specifications": {
        "evidence_id", "product_id", "attribute_name", "value", "unit",
        "verification_status", "source_reference", "source_key",
        "verification_source", "observed_at", "verified_at",
    },
    "product_providers": {"provider_key", "display_name", "terms_url", "enabled"},
    "canonical_products": {
        "canonical_product_id", "legacy_product_id", "name", "brand", "model",
        "gtin", "created_at", "updated_at",
    },
    "product_offers": {
        "offer_id", "provider_key", "provider_offer_key", "provider_product_id",
        "canonical_product_id", "product_name", "brand", "model", "price_minor",
        "currency", "product_url", "image_url", "availability", "last_checked_at",
        "expires_at", "created_at", "updated_at",
    },
    "cart_offer_items": {
        "owner_id", "offer_id", "quantity", "selected_product_name",
        "selected_provider_key", "selected_provider_product_id",
        "selected_price_minor", "selected_currency", "selected_product_url",
        "selected_availability", "added_at", "selected_provider_offer_key",
        "selected_verified_at", "quote_status", "pending_price_minor",
        "pending_currency", "pending_verified_at", "pending_product_name",
        "pending_product_url", "pending_availability",
    },
    "product_images": {"product_id", "image_url", "local_image_path"},
}
_AUTHENTICATION_COLUMNS = {
    "auth_sessions": {"session_hash", "owner_id", "expires_at", "created_at"},
}
_BUSINESS_FOREIGN_KEYS = {
    "reviews": {("product_id", "products", "id", "NO ACTION")},
    "orders": {("product_id", "products", "id", "NO ACTION")},
    "cart_items": {("product_id", "products", "id", "CASCADE")},
    "canonical_products": {
        ("legacy_product_id", "products", "id", "RESTRICT"),
    },
    "product_offers": {
        ("provider_key", "product_providers", "provider_key", "RESTRICT"),
        (
            "canonical_product_id",
            "canonical_products",
            "canonical_product_id",
            "RESTRICT",
        ),
    },
    "cart_offer_items": {
        ("offer_id", "product_offers", "offer_id", "RESTRICT"),
    },
    "product_images": {("product_id", "products", "id", "RESTRICT")},
    "product_specifications": {
        ("product_id", "products", "id", "CASCADE"),
        (
            "source_key",
            "trusted_specification_sources",
            "source_key",
            "RESTRICT",
        ),
        (
            "verification_source",
            "trusted_specification_sources",
            "source_key",
            "RESTRICT",
        ),
    },
}


class SQLiteInitializationError(RuntimeError):
    pass


class PartialSQLitePublicationError(FileExistsError):
    """Raised when exactly one final database target exists."""


@dataclass(frozen=True)
class SQLiteInitializationResult:
    business_database: Path
    authentication_database: Path
    migrations_applied: tuple[str, ...]


def _resolve_fresh_targets(
    business_database: str | Path,
    authentication_database: str | Path,
) -> tuple[Path, Path]:
    raw_paths = (
        Path(business_database).expanduser(),
        Path(authentication_database).expanduser(),
    )

    target_exists = tuple(path.exists() or path.is_symlink() for path in raw_paths)
    if target_exists[0] != target_exists[1]:
        existing_path = raw_paths[0] if target_exists[0] else raw_paths[1]
        missing_path = raw_paths[1] if target_exists[0] else raw_paths[0]
        raise PartialSQLitePublicationError(
            "Only one fresh-deployment database target exists "
            f"({existing_path}); refusing to treat this partial pair as initialized "
            f"or to modify either target. Stop the application and preserve the "
            f"existing file. Inspect it read-only and use the recovery procedure "
            f"documented in backend/initialize_sqlite.py. Do not delete it. Only "
            f"if an operator confirms it contains no user data, move it to a unique "
            f"quarantine path, then retry with both targets absent "
            f"(missing target: {missing_path})."
        )

    for path, exists in zip(raw_paths, target_exists):
        if exists:
            raise FileExistsError(
                f"Refusing to initialize an existing database target: {path}"
            )
        if not path.parent.is_dir():
            raise FileNotFoundError(
                f"Database parent directory does not exist: {path.parent}"
            )

    resolved_paths = tuple(path.resolve() for path in raw_paths)
    if resolved_paths[0] == resolved_paths[1]:
        raise ValueError("Business and authentication database paths must differ.")
    return resolved_paths[0], resolved_paths[1]


def _create_cart_items(database_path: Path) -> None:
    with closing(sqlite3.connect(database_path, timeout=30)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("SQLite foreign-key enforcement could not be enabled.")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""
            CREATE TABLE cart_items (
                owner_id TEXT NOT NULL,
                product_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                PRIMARY KEY (owner_id, product_id),
                FOREIGN KEY (product_id)
                    REFERENCES products(id) ON DELETE CASCADE
            )
        """)
        connection.commit()


def _create_confirmation_outcomes(database_path: Path) -> None:
    with closing(sqlite3.connect(database_path, timeout=30)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""
            CREATE TABLE cart_confirmation_outcomes (
                owner_id TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                references_json TEXT NOT NULL,
                outcome_json TEXT NOT NULL,
                expires_at REAL NOT NULL,
                PRIMARY KEY (owner_id, token_hash)
            )
        """)
        connection.execute("""
            CREATE INDEX idx_cart_confirmation_outcomes_expiry
            ON cart_confirmation_outcomes (expires_at)
        """)
        connection.commit()


def _create_authentication_schema(database_path: Path) -> None:
    with closing(sqlite3.connect(database_path, timeout=30)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""
            CREATE TABLE auth_sessions (
                session_hash TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                expires_at REAL NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        connection.execute("""
            CREATE INDEX idx_auth_sessions_expiry
            ON auth_sessions (expires_at)
        """)
        connection.commit()


def _verify_database(
    database_path: Path,
    required_columns: dict[str, set[str]],
    required_indexes: set[str],
    required_foreign_keys: dict[
        str, set[tuple[str, str, str, str]]
    ] | None = None,
) -> None:
    with closing(sqlite3.connect(database_path, timeout=30)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing_tables = set(required_columns) - tables
        if missing_tables:
            raise SQLiteInitializationError(
                f"Database is missing required tables: {', '.join(sorted(missing_tables))}."
            )

        for table, expected_columns in required_columns.items():
            actual_columns = {
                row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
            }
            missing_columns = expected_columns - actual_columns
            if missing_columns:
                raise SQLiteInitializationError(
                    f"Table {table!r} is missing required columns: "
                    f"{', '.join(sorted(missing_columns))}."
                )

        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        missing_indexes = required_indexes - indexes
        if missing_indexes:
            raise SQLiteInitializationError(
                f"Database is missing required indexes: {', '.join(sorted(missing_indexes))}."
            )

        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise SQLiteInitializationError("Database contains foreign-key violations.")

        for table, expected_foreign_keys in (required_foreign_keys or {}).items():
            actual_foreign_keys = {
                (row[3], row[2], row[4], row[6].upper())
                for row in connection.execute(
                    f'PRAGMA foreign_key_list("{table}")'
                )
            }
            if actual_foreign_keys != expected_foreign_keys:
                raise SQLiteInitializationError(
                    f"Table {table!r} has unexpected foreign keys."
                )


def _create_staging_file(parent: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=".dealsync-init-",
        suffix=".sqlite",
        dir=parent,
    )
    os.close(descriptor)
    return Path(name)


def _remove_staging_files(paths: tuple[Path, ...]) -> None:
    for path in paths:
        for candidate in (
            path,
            Path(f"{path}-journal"),
            Path(f"{path}-wal"),
            Path(f"{path}-shm"),
        ):
            if candidate.exists():
                candidate.unlink()


def initialize_fresh_databases(
    business_database: str | Path,
    authentication_database: str | Path,
) -> SQLiteInitializationResult:
    """Initialize new SQLite databases; this must only target a fresh deployment directory."""
    business_target, authentication_target = _resolve_fresh_targets(
        business_database,
        authentication_database,
    )
    staging_files: list[Path] = []
    published: list[tuple[Path, Path]] = []

    try:
        business_stage = _create_staging_file(business_target.parent)
        staging_files.append(business_stage)
        authentication_stage = _create_staging_file(authentication_target.parent)
        staging_files.append(authentication_stage)

        create_database(business_stage)
        _create_cart_items(business_stage)
        v001_product_offers.migrate(business_stage)
        v002_external_cart_quotes.migrate(business_stage)
        v002_product_images.migrate_database(business_stage)
        v003_currency_metadata.migrate(business_stage)
        v004_product_specifications.migrate(business_stage)
        v005_demo_orders.migrate(business_stage)
        _create_confirmation_outcomes(business_stage)
        _create_authentication_schema(authentication_stage)

        _verify_database(
            business_stage,
            _BUSINESS_COLUMNS,
            {
                "idx_cart_confirmation_outcomes_expiry",
                "idx_orders_owner_reference",
            },
            _BUSINESS_FOREIGN_KEYS,
        )
        _verify_database(
            authentication_stage,
            _AUTHENTICATION_COLUMNS,
            {"idx_auth_sessions_expiry"},
        )

        for source, target in (
            (business_stage, business_target),
            (authentication_stage, authentication_target),
        ):
            os.link(source, target)
            published.append((source, target))

        return SQLiteInitializationResult(
            business_database=business_target,
            authentication_database=authentication_target,
            migrations_applied=MIGRATION_ORDER,
        )
    except BaseException:
        for source, target in reversed(published):
            try:
                if os.path.samefile(source, target):
                    target.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        _remove_staging_files(tuple(staging_files))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Initialize fresh DealSync SQLite databases. Use only in a genuinely "
            "fresh deployment directory; existing database files are never replaced."
        )
    )
    parser.add_argument(
        "--business-database",
        required=True,
        type=Path,
        help="New business/catalog SQLite database path.",
    )
    parser.add_argument(
        "--authentication-database",
        required=True,
        type=Path,
        help="New authentication-session SQLite database path.",
    )
    arguments = parser.parse_args()
    result = initialize_fresh_databases(
        arguments.business_database,
        arguments.authentication_database,
    )
    print(result)


if __name__ == "__main__":
    main()
