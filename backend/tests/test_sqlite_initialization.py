from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import database_paths, db
from backend import initialize_sqlite as initializer
from backend.initialize_sqlite import PartialSQLitePublicationError
from backend.migrations import (
    v001_product_offers,
    v002_external_cart_quotes,
    v002_product_images,
    v003_currency_metadata,
    v004_product_specifications,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class SQLiteInitializationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.business_path = self.root / "store.db"
        self.authentication_path = self.root / "auth" / "auth_sessions.db"
        self.authentication_path.parent.mkdir()

    @staticmethod
    def _database_snapshot(path: Path) -> bytes:
        return path.read_bytes()

    def _initialize(self):
        return initializer.initialize_fresh_databases(
            self.business_path,
            self.authentication_path,
        )

    def _main_with_temporary_paths(self):
        from backend.tests._support import import_main_with_temporary_auth_db

        main = import_main_with_temporary_auth_db()
        original_business_path = main.STORE_DB_PATH
        original_authentication_path = main.AUTH_DB_PATH
        main.STORE_DB_PATH = self.business_path
        main.AUTH_DB_PATH = self.authentication_path
        self.addCleanup(setattr, main, "STORE_DB_PATH", original_business_path)
        self.addCleanup(setattr, main, "AUTH_DB_PATH", original_authentication_path)
        return main

    def _run_lifespan(self, main) -> None:
        application = SimpleNamespace(state=SimpleNamespace())

        async def run():
            async with main.lifespan(application):
                self.assertIsNotNone(application.state.discovery_engine)

        asyncio.run(run())

    def test_default_paths_remain_unchanged(self) -> None:
        self.assertEqual(
            database_paths.BUSINESS_SQLITE_PATH,
            PROJECT_ROOT / "store.db",
        )
        self.assertEqual(
            database_paths.AUTHENTICATION_SQLITE_PATH,
            PROJECT_ROOT / "backend" / "auth_sessions.db",
        )

    def test_custom_paths_are_shared_by_imported_runtime_modules(self) -> None:
        business = self.root / "configured" / "business.sqlite"
        authentication = self.root / "configured" / "authentication.sqlite"
        business.parent.mkdir()
        with closing(sqlite3.connect(business)) as connection:
            connection.execute(
                "CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
            )
            connection.commit()
        with closing(sqlite3.connect(authentication)):
            pass
        script = """
from pathlib import Path
from backend import db
from backend.main import STORE_DB_PATH, AUTH_DB_PATH
from backend.providers.local_catalog import DEFAULT_STORE_DB_PATH
from backend.providers.local_catalog import LocalCatalogAdapter
from backend.migrations.v001_product_offers import DEFAULT_STORE_DB_PATH as V001_PATH
from backend.migrations.v002_product_images import DEFAULT_STORE_DB_PATH as IMAGES_PATH
from backend.migrations.v003_currency_metadata import DEFAULT_STORE_DB_PATH as CURRENCY_PATH
from setup_db import DB_PATH
expected_business = Path(__import__('os').environ['DEALSYNC_STORE_DB_PATH']).resolve()
expected_auth = Path(__import__('os').environ['DEALSYNC_AUTH_DB_PATH']).resolve()
assert db.BUSINESS_SQLITE_PATH == expected_business
assert db.AUTHENTICATION_SQLITE_PATH == expected_auth
assert STORE_DB_PATH == expected_business
assert AUTH_DB_PATH == expected_auth
assert DEFAULT_STORE_DB_PATH == expected_business
assert V001_PATH == expected_business
assert IMAGES_PATH == expected_business
assert CURRENCY_PATH == expected_business
assert DB_PATH == expected_business
connection = __import__('backend.main', fromlist=['_connect_store'])._connect_store()
assert Path(connection.execute('PRAGMA database_list').fetchone()[2]).resolve() == expected_business
connection.close()
assert LocalCatalogAdapter().database_path == expected_business
assert LocalCatalogAdapter().search_catalog_products('') == []
"""
        environment = os.environ.copy()
        environment.update(
            {
                "DEALSYNC_STORE_DB_PATH": str(business),
                "DEALSYNC_AUTH_DB_PATH": str(authentication),
            }
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(business.exists())
        self.assertTrue(authentication.exists())

    def test_sqlalchemy_sqlite_engines_honor_custom_paths(self) -> None:
        business = self.root / "engine-store.db"
        authentication = self.root / "engine-auth.db"
        sqlite3.connect(business).close()
        sqlite3.connect(authentication).close()
        engines = db.create_engines(
            environ={
                "DEALSYNC_ENV": "development",
                "DEALSYNC_STORE_DB_PATH": str(business),
                "DEALSYNC_AUTH_DB_PATH": str(authentication),
            }
        )
        self.addCleanup(engines.dispose)

        self.assertEqual(Path(engines.business.url.database), business.resolve())
        self.assertEqual(
            Path(engines.authentication.url.database),
            authentication.resolve(),
        )
        with engines.business.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").scalar(), 1)
        with engines.authentication.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").scalar(), 1)

    def test_sqlite_database_roles_cannot_share_a_path(self) -> None:
        shared_path = self.root / "shared.sqlite"
        with self.assertRaisesRegex(db.DatabaseConfigurationError, "must differ"):
            db.create_engines(
                environ={
                    "DEALSYNC_ENV": "development",
                    "DEALSYNC_STORE_DB_PATH": str(shared_path),
                    "DEALSYNC_AUTH_DB_PATH": str(shared_path),
                }
            )
        self.assertFalse(shared_path.exists())

    def test_initializes_both_databases_in_required_migration_order(self) -> None:
        calls: list[str] = []
        migration_functions = (
            (v001_product_offers, "migrate", "v001_product_offers"),
            (v002_external_cart_quotes, "migrate", "v002_external_cart_quotes"),
            (v002_product_images, "migrate_database", "v002_product_images"),
            (v003_currency_metadata, "migrate", "v003_currency_metadata"),
            (v004_product_specifications, "migrate", "v004_product_specifications"),
        )
        patches = []
        for module, attribute, name in migration_functions:
            original = getattr(module, attribute)

            def invoke(path, *, _original=original, _name=name):
                calls.append(_name)
                return _original(path)

            patches.append(patch.object(module, attribute, side_effect=invoke))

        for migration_patch in patches:
            migration_patch.start()
            self.addCleanup(migration_patch.stop)

        result = self._initialize()
        self.assertEqual(tuple(calls), initializer.MIGRATION_ORDER)
        self.assertEqual(result.migrations_applied, initializer.MIGRATION_ORDER)
        self.assertTrue(self.business_path.is_file())
        self.assertTrue(self.authentication_path.is_file())

        with closing(sqlite3.connect(self.business_path)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertTrue(set(initializer._BUSINESS_COLUMNS).issubset(tables))
            for table, expected_columns in initializer._BUSINESS_COLUMNS.items():
                actual_columns = {
                    row[1]
                    for row in connection.execute(f'PRAGMA table_info("{table}")')
                }
                self.assertTrue(expected_columns.issubset(actual_columns), table)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM products"
                ).fetchone()[0],
                32,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM reviews").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT currency FROM products WHERE id = 1"
                ).fetchone()[0],
                None,
            )
            self.assertIn(
                "idx_cart_confirmation_outcomes_expiry",
                {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index'"
                    )
                },
            )
            self.assertIn(
                ("products", "product_id", "id", "CASCADE"),
                {
                    (row[2], row[3], row[4], row[6].upper())
                    for row in connection.execute(
                        'PRAGMA foreign_key_list("cart_items")'
                    )
                },
            )
            self.assertIn(
                ("products", "product_id", "id", "CASCADE"),
                {
                    (row[2], row[3], row[4], row[6].upper())
                    for row in connection.execute(
                        'PRAGMA foreign_key_list("product_specifications")'
                    )
                },
            )

        with closing(sqlite3.connect(self.authentication_path)) as connection:
            self.assertEqual(
                {
                    row[1]
                    for row in connection.execute(
                        'PRAGMA table_info("auth_sessions")'
                    )
                },
                initializer._AUTHENTICATION_COLUMNS["auth_sessions"],
            )
            self.assertIn(
                "idx_auth_sessions_expiry",
                {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index'"
                    )
                },
            )

    def test_initialized_schema_passes_existing_startup_validation(self) -> None:
        self._initialize()
        from backend.tests._support import import_main_with_temporary_auth_db

        main = import_main_with_temporary_auth_db()
        engines = db.create_engines(
            test_databases={
                "business": self.business_path,
                "authentication": self.authentication_path,
            },
            environ={"DEALSYNC_ENV": "test"},
        )
        self.addCleanup(engines.dispose)
        main._validate_database_schema(
            engines.business,
            "business",
            {
                "products": {"id", "name"},
                "cart_items": {"owner_id", "product_id", "quantity"},
                "cart_confirmation_outcomes": {
                    "owner_id", "token_hash", "references_json",
                    "outcome_json", "expires_at",
                },
            },
        )
        main._validate_database_schema(
            engines.authentication,
            "authentication",
            {"auth_sessions": {"session_hash", "owner_id", "expires_at", "created_at"}},
        )

    def test_startup_with_missing_business_database_does_not_create_it(self) -> None:
        initializer._create_authentication_schema(self.authentication_path)
        authentication_before = self._database_snapshot(self.authentication_path)
        main = self._main_with_temporary_paths()

        with self.assertRaisesRegex(RuntimeError, "business SQLite database does not exist"):
            self._run_lifespan(main)

        self.assertFalse(self.business_path.exists())
        self.assertEqual(
            self._database_snapshot(self.authentication_path),
            authentication_before,
        )

    def test_startup_with_missing_authentication_database_does_not_create_it(self) -> None:
        self._initialize()
        self.authentication_path.unlink()
        business_before = self._database_snapshot(self.business_path)
        main = self._main_with_temporary_paths()

        with self.assertRaisesRegex(
            RuntimeError,
            "authentication SQLite database does not exist",
        ):
            self._run_lifespan(main)

        self.assertFalse(self.authentication_path.exists())
        self.assertEqual(self._database_snapshot(self.business_path), business_before)

    def test_startup_with_incomplete_business_schema_does_not_modify_it(self) -> None:
        with closing(sqlite3.connect(self.business_path)) as connection:
            connection.execute(
                "CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
            )
            connection.commit()
        initializer._create_authentication_schema(self.authentication_path)
        business_before = self._database_snapshot(self.business_path)
        authentication_before = self._database_snapshot(self.authentication_path)
        main = self._main_with_temporary_paths()

        with self.assertRaisesRegex(
            RuntimeError,
            "explicit initializer before startup",
        ):
            self._run_lifespan(main)

        self.assertEqual(self._database_snapshot(self.business_path), business_before)
        self.assertEqual(
            self._database_snapshot(self.authentication_path),
            authentication_before,
        )

    def test_startup_with_initialized_databases_remains_functional(self) -> None:
        self._initialize()
        main = self._main_with_temporary_paths()
        with patch.object(main, "create_vision_analyzer", side_effect=ValueError):
            self._run_lifespan(main)

    def test_repeated_initialization_refuses_without_modifying_either_database(self):
        self._initialize()
        business_before = self._database_snapshot(self.business_path)
        authentication_before = self._database_snapshot(self.authentication_path)

        with self.assertRaises(FileExistsError):
            self._initialize()

        self.assertEqual(self._database_snapshot(self.business_path), business_before)
        self.assertEqual(
            self._database_snapshot(self.authentication_path),
            authentication_before,
        )

    def test_existing_business_database_is_not_modified(self) -> None:
        with closing(sqlite3.connect(self.business_path)) as connection:
            connection.execute("CREATE TABLE protected_data (value TEXT)")
            connection.execute("INSERT INTO protected_data VALUES ('preserve')")
            connection.commit()
        before = self._database_snapshot(self.business_path)

        with self.assertRaises(PartialSQLitePublicationError) as error:
            self._initialize()

        self.assertIn("recovery procedure", str(error.exception))
        self.assertEqual(self._database_snapshot(self.business_path), before)
        self.assertFalse(self.authentication_path.exists())

    def test_existing_authentication_database_is_not_modified(self) -> None:
        with closing(sqlite3.connect(self.authentication_path)) as connection:
            connection.execute("CREATE TABLE protected_data (value TEXT)")
            connection.execute("INSERT INTO protected_data VALUES ('preserve')")
            connection.commit()
        before = self._database_snapshot(self.authentication_path)

        with self.assertRaises(FileExistsError):
            self._initialize()

        self.assertEqual(self._database_snapshot(self.authentication_path), before)
        self.assertFalse(self.business_path.exists())

    def test_migration_failure_leaves_targets_absent_and_cleans_staging_files(self):
        with patch.object(
            v002_external_cart_quotes,
            "migrate",
            side_effect=RuntimeError("test migration failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "test migration failure"):
                self._initialize()

        self.assertFalse(self.business_path.exists())
        self.assertFalse(self.authentication_path.exists())
        self.assertEqual(list(self.root.rglob(".dealsync-init-*")), [])

    def test_second_target_publication_failure_preserves_racing_database(self) -> None:
        original_link = os.link
        competing_database: dict[str, bytes] = {}

        def link_with_authentication_collision(source, target):
            if Path(target) == self.authentication_path:
                with closing(sqlite3.connect(self.authentication_path)) as connection:
                    connection.execute(
                        "CREATE TABLE protected_data (value TEXT NOT NULL)"
                    )
                    connection.execute(
                        "INSERT INTO protected_data VALUES ('preserve')"
                    )
                    connection.commit()
                competing_database["snapshot"] = self._database_snapshot(
                    self.authentication_path
                )
            return original_link(source, target)

        with patch.object(initializer.os, "link", side_effect=link_with_authentication_collision):
            with self.assertRaises(FileExistsError):
                self._initialize()

        self.assertFalse(self.business_path.exists())
        self.assertEqual(
            self._database_snapshot(self.authentication_path),
            competing_database["snapshot"],
        )
        with closing(sqlite3.connect(self.authentication_path)) as connection:
            self.assertEqual(
                connection.execute("SELECT value FROM protected_data").fetchone()[0],
                "preserve",
            )
        self.assertEqual(list(self.root.rglob(".dealsync-init-*")), [])

    def test_existing_partial_publication_is_rejected_without_changes(self) -> None:
        with closing(sqlite3.connect(self.authentication_path)) as connection:
            connection.execute("CREATE TABLE protected_data (value TEXT)")
            connection.execute("INSERT INTO protected_data VALUES ('preserve')")
            connection.commit()
        before = self._database_snapshot(self.authentication_path)

        with self.assertRaises(PartialSQLitePublicationError):
            self._initialize()

        self.assertFalse(self.business_path.exists())
        self.assertEqual(self._database_snapshot(self.authentication_path), before)

    def test_identical_database_paths_are_rejected_before_creation(self) -> None:
        with self.assertRaisesRegex(ValueError, "must differ"):
            initializer.initialize_fresh_databases(
                self.business_path,
                self.business_path,
            )
        self.assertFalse(self.business_path.exists())


if __name__ == "__main__":
    unittest.main()
