from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal


DatabaseName = Literal["business", "authentication"]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PATH_SETTINGS: dict[DatabaseName, tuple[str, Path]] = {
    "business": ("DEALSYNC_STORE_DB_PATH", PROJECT_ROOT / "store.db"),
    "authentication": (
        "DEALSYNC_AUTH_DB_PATH",
        Path(__file__).resolve().parent / "auth_sessions.db",
    ),
}


def sqlite_database_path(
    database: DatabaseName,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    try:
        variable_name, default_path = _PATH_SETTINGS[database]
    except KeyError as exc:
        raise ValueError("Unknown SQLite database.") from exc

    environment = os.environ if environ is None else environ
    configured_path = environment.get(variable_name)
    if configured_path is None:
        path = default_path
    elif not configured_path.strip():
        raise ValueError(f"{variable_name} must not be empty.")
    else:
        path = Path(configured_path).expanduser()
    return path.resolve()


BUSINESS_SQLITE_PATH = sqlite_database_path("business")
AUTHENTICATION_SQLITE_PATH = sqlite_database_path("authentication")
