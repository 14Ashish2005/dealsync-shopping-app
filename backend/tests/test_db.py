from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator, Mapping
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError

from backend import db


class GuardedEnvironment(Mapping[str, str]):
    def __init__(
        self,
        values: Mapping[str, str],
        forbidden_keys: set[str],
    ) -> None:
        self._values = dict(values)
        self._forbidden_keys = forbidden_keys

    def __getitem__(self, key: str) -> str:
        if key in self._forbidden_keys:
            raise AssertionError(f"Forbidden environment lookup: {key}")
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __contains__(self, key: object) -> bool:
        if key in self._forbidden_keys:
            raise AssertionError(f"Forbidden environment lookup: {key}")
        return key in self._values

    def get(self, key: str, default: str | None = None) -> str | None:
        if key in self._forbidden_keys:
            raise AssertionError(f"Forbidden environment lookup: {key}")
        return self._values.get(key, default)


class DatabaseFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name)

    def _test_environment(self) -> dict[str, str]:
        return {"DEALSYNC_ENV": "test"}

    def _temporary_database(self, name: str) -> Path:
        return self.temporary_path / name

    def test_missing_environment_defaults_to_development(self) -> None:
        config = db.get_database_config("business", environ={})
        self.assertEqual(config.environment, "development")

    def test_existing_sqlite_paths_are_selected_without_opening_them(self) -> None:
        business = db.get_database_config("business", environ={})
        authentication = db.get_database_config("authentication", environ={})

        self.assertEqual(
            Path(business.url.database),
            db.BUSINESS_SQLITE_PATH,
        )
        self.assertEqual(
            Path(authentication.url.database),
            db.AUTHENTICATION_SQLITE_PATH,
        )

        business_engine = db.create_business_engine(environ={})
        authentication_engine = db.create_authentication_engine(environ={})
        self.addCleanup(business_engine.dispose)
        self.addCleanup(authentication_engine.dispose)

        self.assertIsNot(business_engine, authentication_engine)
        self.assertEqual(
            Path(business_engine.url.database),
            db.BUSINESS_SQLITE_PATH,
        )
        self.assertEqual(
            Path(authentication_engine.url.database),
            db.AUTHENTICATION_SQLITE_PATH,
        )
        self.assertEqual(business_engine.pool.checkedout(), 0)
        self.assertEqual(authentication_engine.pool.checkedout(), 0)

    def test_unsupported_environment_is_rejected(self) -> None:
        with self.assertRaises(db.DatabaseConfigurationError):
            db.get_database_config(
                "business",
                environ={"DEALSYNC_ENV": "staging"},
            )

    def test_different_production_database_names_on_same_server_are_accepted(
        self,
    ) -> None:
        environment = {
            "DEALSYNC_ENV": "production",
            "DATABASE_URL": "postgresql://user:secret@db.example.test/store",
            "AUTH_DATABASE_URL": "postgresql+psycopg://user:secret@db.example.test/auth",
        }

        business = db.get_database_config("business", environ=environment)
        authentication = db.get_database_config(
            "authentication",
            environ=environment,
        )

        self.assertEqual(business.url.drivername, "postgresql+psycopg")
        self.assertEqual(authentication.url.drivername, "postgresql+psycopg")
        self.assertEqual(business.url.database, "store")
        self.assertEqual(authentication.url.database, "auth")

    def test_identical_production_targets_with_different_credentials_are_rejected(
        self,
    ) -> None:
        business_url = (
            "postgresql://business-user:business-password@DB.example.test:5432/shared"
        )
        authentication_url = (
            "postgresql://auth-user:auth-password@db.example.test./shared"
        )

        with self.assertRaises(db.DatabaseConfigurationError) as context:
            db.get_database_config(
                "business",
                environ={
                    "DEALSYNC_ENV": "production",
                    "DATABASE_URL": business_url,
                    "AUTH_DATABASE_URL": authentication_url,
                },
            )

        error = str(context.exception)
        for secret in (
            "business-user",
            "business-password",
            "auth-user",
            "auth-password",
            business_url,
            authentication_url,
        ):
            self.assertNotIn(secret, error)

    def test_different_production_hosts_are_accepted(self) -> None:
        environment = {
            "DEALSYNC_ENV": "production",
            "DATABASE_URL": "postgresql://user:secret@business.example.test/store",
            "AUTH_DATABASE_URL": (
                "postgresql://user:secret@authentication.example.test/store"
            ),
        }

        self.assertEqual(
            db.get_database_config("business", environ=environment).url.database,
            "store",
        )

    def test_missing_production_urls_are_rejected(self) -> None:
        with self.assertRaisesRegex(
            db.DatabaseConfigurationError,
            "DATABASE_URL is required",
        ):
            db.get_database_config(
                "business",
                environ={"DEALSYNC_ENV": "production"},
            )

        with self.assertRaisesRegex(
            db.DatabaseConfigurationError,
            "AUTH_DATABASE_URL is required",
        ):
            db.get_database_config(
                "authentication",
                environ={
                    "DEALSYNC_ENV": "production",
                    "DATABASE_URL": "postgresql://user:secret@db.example.test/store",
                },
            )

    def test_invalid_production_urls_are_rejected_without_exposing_values(
        self,
    ) -> None:
        credential_url = (
            "postgresql://fake-user-sentinel:fake-password-sentinel@"
            "db.example.test:not-a-port/store"
        )
        invalid_urls = (
            "not a URL",
            "sqlite:///not-allowed.db",
            "postgresql://",
            credential_url,
        )

        for invalid_url in invalid_urls:
            with self.subTest(invalid_url=invalid_url):
                with self.assertRaises(db.DatabaseConfigurationError) as context:
                    db.get_database_config(
                        "business",
                        environ={
                            "DEALSYNC_ENV": "production",
                            "DATABASE_URL": invalid_url,
                        },
                    )
                error = str(context.exception)
                self.assertNotIn("fake-user-sentinel", error)
                self.assertNotIn("fake-password-sentinel", error)
                self.assertNotIn(credential_url, error)

    def test_production_engine_uses_conservative_pool_without_connecting(
        self,
    ) -> None:
        environment = {
            "DEALSYNC_ENV": "production",
            "DATABASE_URL": "postgresql://user:secret@db.example.test/store",
            "AUTH_DATABASE_URL": (
                "postgresql://user:secret@db.example.test/authentication"
            ),
        }
        with patch.object(db, "create_engine") as create_engine:
            engine = db.create_business_engine(environ=environment)

        self.assertIs(engine, create_engine.return_value)
        create_engine.assert_called_once()
        kwargs = create_engine.call_args.kwargs
        self.assertTrue(kwargs["pool_pre_ping"])
        self.assertEqual(kwargs["pool_size"], 2)
        self.assertEqual(kwargs["max_overflow"], 1)
        self.assertEqual(kwargs["pool_timeout"], 15)
        self.assertTrue(kwargs["hide_parameters"])

    def test_test_mode_requires_explicit_temporary_databases(self) -> None:
        with self.assertRaises(db.DatabaseConfigurationError):
            db.get_database_config(
                "business",
                environ=self._test_environment(),
            )

        with self.assertRaises(db.DatabaseConfigurationError):
            db.get_database_config(
                "business",
                test_database=db.BUSINESS_SQLITE_PATH,
                environ=self._test_environment(),
            )

    def test_test_mode_never_reads_database_url(self) -> None:
        environment = GuardedEnvironment(
            self._test_environment(),
            {"DATABASE_URL"},
        )
        config = db.get_database_config(
            "business",
            test_database=self._temporary_database("business.db"),
            environ=environment,
        )
        self.assertEqual(config.url.get_backend_name(), "sqlite")

    def test_test_mode_never_reads_auth_database_url(self) -> None:
        environment = GuardedEnvironment(
            self._test_environment(),
            {"AUTH_DATABASE_URL"},
        )
        config = db.get_database_config(
            "authentication",
            test_database=self._temporary_database("authentication.db"),
            environ=environment,
        )
        self.assertEqual(config.url.get_backend_name(), "sqlite")

    def test_test_mode_accepts_temporary_sqlite_urls(self) -> None:
        database_path = self._temporary_database("business-url.db")
        database_url = str(
            URL.create("sqlite+pysqlite", database=str(database_path))
        )
        config = db.get_database_config(
            "business",
            test_database=database_url,
            environ=self._test_environment(),
        )
        self.assertEqual(Path(config.url.database), database_path)

    def test_test_mode_rejects_postgresql_urls(self) -> None:
        with self.assertRaises(db.DatabaseConfigurationError):
            db.get_database_config(
                "business",
                test_database="postgresql://user:secret@db.example.test/store",
                environ=self._test_environment(),
            )

    def test_test_engines_use_separate_temporary_sqlite_files(self) -> None:
        business_path = self._temporary_database("business.db")
        authentication_path = self._temporary_database("authentication.db")

        engines = db.create_engines(
            test_databases={
                "business": business_path,
                "authentication": authentication_path,
            },
            environ=self._test_environment(),
        )
        self.addCleanup(engines.dispose)

        self.assertIsNot(engines.business, engines.authentication)
        self.assertEqual(Path(engines.business.url.database), business_path)
        self.assertEqual(
            Path(engines.authentication.url.database),
            authentication_path,
        )

    def test_transaction_commits_successful_work(self) -> None:
        engine = db.create_business_engine(
            test_database=self._temporary_database("commit.db"),
            environ=self._test_environment(),
        )
        self.addCleanup(engine.dispose)

        with db.transaction(engine) as connection:
            connection.execute(
                text("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)")
            )
            connection.execute(
                text("INSERT INTO records (value) VALUES (:value)"),
                {"value": "committed"},
            )

        with db.connection(engine) as connection:
            value = connection.execute(
                text("SELECT value FROM records")
            ).scalar_one()
        self.assertEqual(value, "committed")

    def test_transaction_rolls_back_failed_work(self) -> None:
        engine = db.create_business_engine(
            test_database=self._temporary_database("rollback.db"),
            environ=self._test_environment(),
        )
        self.addCleanup(engine.dispose)

        with db.transaction(engine) as connection:
            connection.execute(
                text("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)")
            )

        with self.assertRaisesRegex(RuntimeError, "force rollback"):
            with db.transaction(engine) as connection:
                connection.execute(
                    text("INSERT INTO records (value) VALUES (:value)"),
                    {"value": "rolled back"},
                )
                raise RuntimeError("force rollback")

        with db.connection(engine) as connection:
            count = connection.execute(
                text("SELECT COUNT(*) FROM records")
            ).scalar_one()
        self.assertEqual(count, 0)

    def test_connection_context_returns_connection_to_pool(self) -> None:
        engine = db.create_business_engine(
            test_database=self._temporary_database("connection.db"),
            environ=self._test_environment(),
        )
        self.addCleanup(engine.dispose)

        with db.connection(engine) as connection:
            connection.execute(text("SELECT 1"))
            self.assertEqual(engine.pool.checkedout(), 1)

        self.assertEqual(engine.pool.checkedout(), 0)

    def test_engine_bundle_disposes_both_engines(self) -> None:
        engines = db.create_engines(
            test_databases={
                "business": self._temporary_database("dispose-business.db"),
                "authentication": self._temporary_database("dispose-auth.db"),
            },
            environ=self._test_environment(),
        )
        business_pool = engines.business.pool
        authentication_pool = engines.authentication.pool

        engines.dispose()

        self.assertIsNot(engines.business.pool, business_pool)
        self.assertIsNot(engines.authentication.pool, authentication_pool)

    def test_sqlite_engines_enforce_foreign_keys(self) -> None:
        test_engines = db.create_engines(
            test_databases={
                "business": self._temporary_database("test-business-fk.db"),
                "authentication": self._temporary_database("test-auth-fk.db"),
            },
            environ=self._test_environment(),
        )
        self.addCleanup(test_engines.dispose)

        development_paths = {
            "business": self._temporary_database("development-business-fk.db"),
            "authentication": self._temporary_database("development-auth-fk.db"),
        }
        for path in development_paths.values():
            sqlite3.connect(path).close()

        with patch.dict(db._DATABASE_PATHS, development_paths):
            development_engines = db.create_engines(environ={})
        self.addCleanup(development_engines.dispose)

        engines = (
            test_engines.business,
            test_engines.authentication,
            development_engines.business,
            development_engines.authentication,
        )
        for index, engine in enumerate(engines):
            with self.subTest(engine=index):
                with db.connection(engine) as connection:
                    self.assertEqual(
                        connection.execute(text("PRAGMA foreign_keys")).scalar_one(),
                        1,
                    )

                with db.transaction(engine) as connection:
                    connection.execute(
                        text(
                            "CREATE TABLE parents "
                            "(id INTEGER PRIMARY KEY)"
                        )
                    )
                    connection.execute(
                        text(
                            "CREATE TABLE children "
                            "(parent_id INTEGER REFERENCES parents(id))"
                        )
                    )

                with self.assertRaises(IntegrityError):
                    with db.transaction(engine) as connection:
                        connection.execute(
                            text(
                                "INSERT INTO children (parent_id) VALUES (999)"
                            )
                        )

    def test_engine_creation_does_not_open_sqlite_database(self) -> None:
        database_path = self._temporary_database("not-opened.db")
        with patch.object(sqlite3, "connect", wraps=sqlite3.connect) as connect:
            engine = db.create_business_engine(
                test_database=database_path,
                environ=self._test_environment(),
            )
        self.addCleanup(engine.dispose)

        connect.assert_not_called()
        self.assertFalse(database_path.exists())

    def test_development_connection_does_not_create_a_missing_database_file(
        self,
    ) -> None:
        database_path = self._temporary_database("missing-development.db")
        with patch.dict(db._DATABASE_PATHS, {"business": database_path}):
            engine = db.create_business_engine(environ={})
            self.addCleanup(engine.dispose)

            with self.assertRaisesRegex(
                db.DatabaseConfigurationError,
                "business SQLite database file does not exist",
            ):
                with db.connection(engine):
                    pass

        self.assertFalse(database_path.exists())

    def test_development_connection_keeps_existing_database_writable(self) -> None:
        database_path = self._temporary_database("existing-development.db")
        sqlite3.connect(database_path).close()

        with patch.dict(db._DATABASE_PATHS, {"business": database_path}):
            engine = db.create_business_engine(environ={})
            self.addCleanup(engine.dispose)

            with db.transaction(engine) as connection:
                connection.execute(
                    text("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)")
                )
                connection.execute(
                    text("INSERT INTO records (value) VALUES (:value)"),
                    {"value": "writable"},
                )

            with db.connection(engine) as connection:
                value = connection.execute(
                    text("SELECT value FROM records")
                ).scalar_one()

        self.assertEqual(value, "writable")

    def test_cold_import_is_lazy_and_does_not_read_production_urls(self) -> None:
        script = r"""
import os
import sqlite3
from unittest.mock import patch

import sqlalchemy
from sqlalchemy.engine import Engine


class GuardedEnvironment(dict):
    def __getitem__(self, key):
        if key in {"DATABASE_URL", "AUTH_DATABASE_URL"}:
            raise AssertionError(f"Forbidden environment lookup: {key}")
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key in {"DATABASE_URL", "AUTH_DATABASE_URL"}:
            raise AssertionError(f"Forbidden environment lookup: {key}")
        return super().get(key, default)

    def __contains__(self, key):
        if key in {"DATABASE_URL", "AUTH_DATABASE_URL"}:
            raise AssertionError(f"Forbidden environment lookup: {key}")
        return super().__contains__(key)


os.environ = GuardedEnvironment({
    "DEALSYNC_ENV": "test",
    "DATABASE_URL": "postgresql://must-not-be-read",
    "AUTH_DATABASE_URL": "postgresql://must-not-be-read",
})

with (
    patch("sqlalchemy.create_engine") as create_engine,
    patch.object(Engine, "connect", side_effect=AssertionError),
    patch.object(sqlite3, "connect", side_effect=AssertionError),
):
    import backend.db

assert not create_engine.called
"""
        child_environment = {
            name: os.environ[name]
            for name in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
            if name in os.environ
        }
        child_environment["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[2],
            env=child_environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
