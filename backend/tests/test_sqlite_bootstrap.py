from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from backend.bootstrap_sqlite import (
    SQLiteBootstrapError,
    bootstrap_sqlite_databases,
)


class SQLiteBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.business_path = root / "store.db"
        self.authentication_path = root / "auth_sessions.db"
        self.environment = {
            "DEALSYNC_ENV": "production",
            "DEALSYNC_DATABASE_BACKEND": "sqlite",
            "DEALSYNC_STORE_DB_PATH": str(self.business_path),
            "DEALSYNC_AUTH_DB_PATH": str(self.authentication_path),
        }

    def _bootstrap(self) -> tuple[Path, Path]:
        return bootstrap_sqlite_databases(environ=self.environment)

    def test_both_missing_initializes_both_databases(self) -> None:
        paths = self._bootstrap()

        self.assertEqual(paths, (self.business_path, self.authentication_path))
        self.assertTrue(self.business_path.is_file())
        self.assertTrue(self.authentication_path.is_file())
        with closing(sqlite3.connect(self.business_path)) as connection:
            product_count = connection.execute(
                "SELECT COUNT(*) FROM products"
            ).fetchone()[0]
            self.assertGreater(product_count, 0)
        with closing(sqlite3.connect(self.authentication_path)) as connection:
            table = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'auth_sessions'"
            ).fetchone()
            self.assertIsNotNone(table)

    def test_both_existing_does_not_initialize(self) -> None:
        self.business_path.write_bytes(b"existing store")
        self.authentication_path.write_bytes(b"existing authentication")
        existing_contents = (
            self.business_path.read_bytes(),
            self.authentication_path.read_bytes(),
        )

        with patch(
            "backend.bootstrap_sqlite.initialize_fresh_databases"
        ) as initialize:
            self._bootstrap()

        initialize.assert_not_called()
        self.assertEqual(
            (
                self.business_path.read_bytes(),
                self.authentication_path.read_bytes(),
            ),
            existing_contents,
        )

    def test_only_store_database_exists_fails_without_initializing(self) -> None:
        self.business_path.touch()

        with patch(
            "backend.bootstrap_sqlite.initialize_fresh_databases"
        ) as initialize:
            with self.assertRaisesRegex(
                SQLiteBootstrapError,
                "Only one configured SQLite database file exists",
            ):
                self._bootstrap()

        initialize.assert_not_called()
        self.assertTrue(self.business_path.is_file())
        self.assertFalse(self.authentication_path.exists())

    def test_only_auth_database_exists_fails_without_initializing(self) -> None:
        self.authentication_path.touch()

        with patch(
            "backend.bootstrap_sqlite.initialize_fresh_databases"
        ) as initialize:
            with self.assertRaisesRegex(
                SQLiteBootstrapError,
                "Only one configured SQLite database file exists",
            ):
                self._bootstrap()

        initialize.assert_not_called()
        self.assertFalse(self.business_path.exists())
        self.assertTrue(self.authentication_path.is_file())

    def test_initialization_failure_is_reported_clearly(self) -> None:
        with patch(
            "backend.bootstrap_sqlite.initialize_fresh_databases",
            side_effect=RuntimeError("test initialization failure"),
        ) as initialize:
            with self.assertRaisesRegex(
                SQLiteBootstrapError,
                "Fresh SQLite database initialization failed",
            ):
                self._bootstrap()

        initialize.assert_called_once_with(
            self.business_path,
            self.authentication_path,
        )
        self.assertFalse(self.business_path.exists())
        self.assertFalse(self.authentication_path.exists())

    def test_initialization_must_create_both_database_files(self) -> None:
        with patch(
            "backend.bootstrap_sqlite.initialize_fresh_databases"
        ) as initialize:
            with self.assertRaisesRegex(
                SQLiteBootstrapError,
                "without creating both database files",
            ):
                self._bootstrap()

        initialize.assert_called_once_with(
            self.business_path,
            self.authentication_path,
        )


if __name__ == "__main__":
    unittest.main()
