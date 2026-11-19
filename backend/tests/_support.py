import importlib
import io
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import patch


_SAFE_MAIN: Any = None
_TEMPORARY_ROOT = Path(tempfile.gettempdir()).resolve()


def _require_temporary_path(path):
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(_TEMPORARY_ROOT):
        raise RuntimeError("Test databases must be inside the system temp directory.")
    return resolved


def _safe_import_main():
    secrets_path = (
        Path(__file__).resolve().parents[2]
        / ".streamlit"
        / "secrets.toml"
    ).resolve()
    original_is_file = Path.is_file
    original_open = Path.open

    def is_file_without_real_secrets(path: Path) -> bool:
        if path.resolve() == secrets_path:
            return True
        return original_is_file(path)

    def open_without_real_secrets(path, *args, **kwargs):
        if path.resolve() == secrets_path:
            return io.BytesIO(
                b'[auth]\n'
                b'cookie_secret = "test-cookie-secret"\n'
                b'client_id = "test-client-id"\n'
                b'client_secret = "test-client-secret"\n'
                b'server_metadata_url = "https://accounts.example.test/.well-known"\n'
            )
        return original_open(path, *args, **kwargs)

    oauth_environment_variables = (
        "DEALSYNC_ENV",
        "DEALSYNC_GOOGLE_CLIENT_ID",
        "DEALSYNC_GOOGLE_CLIENT_SECRET",
        "DEALSYNC_OAUTH_COOKIE_SECRET",
        "DEALSYNC_GOOGLE_SERVER_METADATA_URL",
        "DEALSYNC_GOOGLE_REDIRECT_URI",
        "DEALSYNC_FRONTEND_ORIGIN",
    )
    with patch.dict(os.environ):
        for variable in oauth_environment_variables:
            os.environ.pop(variable, None)
        with patch.object(
            Path,
            "is_file",
            new=is_file_without_real_secrets,
        ), patch.object(
            Path,
            "open",
            new=open_without_real_secrets,
        ):
            return importlib.import_module("backend.main")


def import_main_with_temporary_auth_db():
    global _SAFE_MAIN
    if _SAFE_MAIN is not None:
        return _SAFE_MAIN
    if "backend.main" in sys.modules:
        raise RuntimeError(
            "backend.main was imported before its configuration was isolated."
        )

    _SAFE_MAIN = _safe_import_main()
    original_create_engines = _SAFE_MAIN.create_engines

    def create_temporary_engines():
        return original_create_engines(
            test_databases={
                "business": _require_temporary_path(_SAFE_MAIN.STORE_DB_PATH),
                "authentication": _require_temporary_path(
                    _SAFE_MAIN.AUTH_DB_PATH
                ),
            },
            environ={"DEALSYNC_ENV": "test"},
        )

    _SAFE_MAIN.create_engines = create_temporary_engines
    return _SAFE_MAIN


def create_auth_schema(path):
    database_path = _require_temporary_path(path)
    with closing(sqlite3.connect(database_path)) as connection:
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
            ON auth_sessions(expires_at)
        """)
        connection.commit()


def create_cart_schema(path):
    database_path = _require_temporary_path(path)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS cart_items (
                owner_id TEXT NOT NULL,
                product_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                PRIMARY KEY (owner_id, product_id),
                FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
            )
        """)
        connection.commit()


def create_confirmation_schema(path):
    database_path = _require_temporary_path(path)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS cart_confirmation_outcomes (
                owner_id TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                references_json TEXT NOT NULL,
                outcome_json TEXT NOT NULL,
                expires_at REAL NOT NULL,
                PRIMARY KEY (owner_id, token_hash)
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_cart_confirmation_outcomes_expiry
            ON cart_confirmation_outcomes (expires_at)
        """)
        connection.commit()


def prepare_test_schemas(business_path, authentication_path):
    create_cart_schema(business_path)
    create_confirmation_schema(business_path)
    create_auth_schema(authentication_path)
