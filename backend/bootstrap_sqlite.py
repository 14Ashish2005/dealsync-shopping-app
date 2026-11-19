from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from backend.database_paths import sqlite_database_path
from backend.initialize_sqlite import initialize_fresh_databases


class SQLiteBootstrapError(RuntimeError):
    """Raised when the configured SQLite database pair is not safe to bootstrap."""


def bootstrap_sqlite_databases(
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, Path]:
    environment = os.environ if environ is None else environ
    if environment.get("DEALSYNC_ENV") != "production":
        raise SQLiteBootstrapError(
            "SQLite bootstrap requires DEALSYNC_ENV=production."
        )
    if environment.get("DEALSYNC_DATABASE_BACKEND") != "sqlite":
        raise SQLiteBootstrapError(
            "SQLite bootstrap requires DEALSYNC_DATABASE_BACKEND=sqlite."
        )

    path_variables = (
        ("business", "DEALSYNC_STORE_DB_PATH"),
        ("authentication", "DEALSYNC_AUTH_DB_PATH"),
    )
    for _, variable_name in path_variables:
        configured_path = environment.get(variable_name)
        if configured_path is None or not configured_path.strip():
            raise SQLiteBootstrapError(
                f"{variable_name} is required for production SQLite bootstrap."
            )
        if not Path(configured_path).expanduser().is_absolute():
            raise SQLiteBootstrapError(
                f"{variable_name} must be an absolute path."
            )

    business_path = sqlite_database_path("business", environ=environment)
    authentication_path = sqlite_database_path(
        "authentication",
        environ=environment,
    )
    paths = (business_path, authentication_path)
    present = tuple(path.exists() or path.is_symlink() for path in paths)

    if present == (True, True):
        if not all(path.is_file() for path in paths):
            raise SQLiteBootstrapError(
                "Both configured SQLite database paths must be existing files."
            )
        return paths

    if present != (False, False):
        raise SQLiteBootstrapError(
            "Only one configured SQLite database file exists; refusing to "
            "initialize, repair, or overwrite either database."
        )

    try:
        initialize_fresh_databases(business_path, authentication_path)
    except Exception as exc:
        raise SQLiteBootstrapError(
            "Fresh SQLite database initialization failed; refusing to continue."
        ) from exc

    if not all(path.is_file() for path in paths):
        raise SQLiteBootstrapError(
            "SQLite initialization completed without creating both database files."
        )

    return paths


def main() -> None:
    bootstrap_sqlite_databases()
    print("SQLite databases are ready.")


if __name__ == "__main__":
    main()
