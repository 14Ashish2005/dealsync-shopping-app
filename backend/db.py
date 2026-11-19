"""Database configuration and SQLAlchemy engine helpers for DealSync."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Connection, Engine, URL, make_url
from sqlalchemy.exc import ArgumentError

from backend.database_paths import (
    AUTHENTICATION_SQLITE_PATH,
    BUSINESS_SQLITE_PATH,
    sqlite_database_path,
)


DatabaseName = Literal["business", "authentication"]
EnvironmentName = Literal["development", "production", "test"]
ProductionDatabaseBackend = Literal["postgresql", "sqlite"]
TestDatabaseSource = str | Path | URL

_PRODUCTION_URL_VARIABLES: dict[DatabaseName, str] = {
    "business": "DATABASE_URL",
    "authentication": "AUTH_DATABASE_URL",
}
_PRODUCTION_DATABASE_BACKEND_VARIABLE = "DEALSYNC_DATABASE_BACKEND"
_DEFAULT_PRODUCTION_DATABASE_BACKEND = "postgresql"
_DATABASE_PATHS: dict[DatabaseName, Path] = {
    "business": BUSINESS_SQLITE_PATH,
    "authentication": AUTHENTICATION_SQLITE_PATH,
}
_DATABASE_PATH_VARIABLES: dict[DatabaseName, str] = {
    "business": "DEALSYNC_STORE_DB_PATH",
    "authentication": "DEALSYNC_AUTH_DB_PATH",
}
_DATABASE_NAMES = frozenset({"business", "authentication"})


class DatabaseConfigurationError(ValueError):
    """Raised when a database configuration is missing or unsafe."""


@dataclass(frozen=True)
class DatabaseConfig:
    environment: EnvironmentName
    database: DatabaseName
    url: URL = field(repr=False)


@dataclass(slots=True)
class DatabaseEngines:
    business: Engine = field(repr=False)
    authentication: Engine = field(repr=False)

    def dispose(self) -> None:
        """Dispose both pools, even if disposing the first one raises."""
        try:
            self.business.dispose()
        finally:
            self.authentication.dispose()


def _environment_name(environ: Mapping[str, str]) -> EnvironmentName:
    if "DEALSYNC_ENV" not in environ:
        return "development"

    value = environ["DEALSYNC_ENV"]
    if value == "development":
        return "development"
    if value == "production":
        return "production"
    if value == "test":
        return "test"
    raise DatabaseConfigurationError(
        "DEALSYNC_ENV must be development, production, or test."
    )


def _production_url(variable_name: str, raw_url: str | None) -> URL:
    if raw_url is None or not raw_url.strip():
        raise DatabaseConfigurationError(
            f"{variable_name} is required in production."
        )

    try:
        url = make_url(raw_url.strip())
    except (ArgumentError, TypeError, ValueError):
        raise DatabaseConfigurationError(
            f"{variable_name} must be a valid PostgreSQL URL."
        ) from None

    if url.drivername not in {"postgresql", "postgresql+psycopg"}:
        raise DatabaseConfigurationError(
            f"{variable_name} must use PostgreSQL with Psycopg 3."
        )

    try:
        host = url.host
        database = url.database
        port = url.port
    except (ArgumentError, TypeError, ValueError):
        raise DatabaseConfigurationError(
            f"{variable_name} must be a valid PostgreSQL URL."
        ) from None

    if not host or not database:
        raise DatabaseConfigurationError(
            f"{variable_name} must include a host and database name."
        )
    if port is not None and not 1 <= port <= 65535:
        raise DatabaseConfigurationError(
            f"{variable_name} must use a valid PostgreSQL port."
        )

    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    return url


def _postgresql_target(url: URL) -> tuple[str, int, str]:
    host = url.host or ""
    port = url.port
    database = url.database or ""
    return host.rstrip(".").casefold(), 5432 if port is None else port, database


def _development_sqlite_path(
    database: DatabaseName,
    environ: Mapping[str, str],
) -> Path:
    if _DATABASE_PATH_VARIABLES[database] in environ:
        return sqlite_database_path(database, environ=environ)
    return Path(_DATABASE_PATHS[database]).expanduser().resolve()


def _production_urls(environ: Mapping[str, str]) -> dict[DatabaseName, URL]:
    urls = {
        database: _production_url(variable_name, environ.get(variable_name))
        for database, variable_name in _PRODUCTION_URL_VARIABLES.items()
    }

    if _postgresql_target(urls["business"]) == _postgresql_target(
        urls["authentication"]
    ):
        raise DatabaseConfigurationError(
            "DATABASE_URL and AUTH_DATABASE_URL must identify different PostgreSQL databases."
        )
    return urls


def _production_database_backend(
    environ: Mapping[str, str],
) -> ProductionDatabaseBackend:
    backend = environ.get(
        _PRODUCTION_DATABASE_BACKEND_VARIABLE,
        _DEFAULT_PRODUCTION_DATABASE_BACKEND,
    )
    if backend == "postgresql":
        return "postgresql"
    if backend == "sqlite":
        return "sqlite"
    raise DatabaseConfigurationError(
        f"{_PRODUCTION_DATABASE_BACKEND_VARIABLE} must be postgresql or sqlite."
    )


def _production_sqlite_paths(
    environ: Mapping[str, str],
) -> dict[DatabaseName, Path]:
    paths: dict[DatabaseName, Path] = {}
    for database, variable_name in _DATABASE_PATH_VARIABLES.items():
        configured_path = environ.get(variable_name)
        if configured_path is None or not configured_path.strip():
            raise DatabaseConfigurationError(
                f"{variable_name} is required when "
                f"{_PRODUCTION_DATABASE_BACKEND_VARIABLE}=sqlite."
            )
        if not Path(configured_path).expanduser().is_absolute():
            raise DatabaseConfigurationError(
                f"{variable_name} must be an absolute path in production."
            )

        path = sqlite_database_path(database, environ=environ)
        if not path.is_file():
            raise DatabaseConfigurationError(
                f"The {database} SQLite database file does not exist at {path}."
            )
        paths[database] = path

    if paths["business"].samefile(paths["authentication"]):
        raise DatabaseConfigurationError(
            "Business and authentication SQLite paths must differ."
        )
    return paths


def _temporary_sqlite_url(source: TestDatabaseSource | None) -> URL:
    if source is None:
        raise DatabaseConfigurationError(
            "Test mode requires an explicitly supplied temporary SQLite database."
        )

    if isinstance(source, URL):
        url = source
    elif isinstance(source, str) and "://" in source:
        try:
            url = make_url(source)
        except (ArgumentError, TypeError, ValueError):
            raise DatabaseConfigurationError(
                "The test database URL is invalid."
            ) from None
    else:
        path = Path(source).expanduser().resolve()
        url = URL.create("sqlite+pysqlite", database=str(path))

    if url.drivername not in {"sqlite", "sqlite+pysqlite"}:
        raise DatabaseConfigurationError(
            "Test mode accepts SQLite databases only."
        )
    url = url.set(drivername="sqlite+pysqlite")

    database = url.database
    if database == ":memory:":
        if url.query:
            raise DatabaseConfigurationError(
                "Test in-memory SQLite URLs cannot include query options."
            )
        return url

    if not database or url.query:
        raise DatabaseConfigurationError(
            "Test mode requires a temporary SQLite file path or in-memory URL."
        )

    database_path = Path(database).expanduser().resolve()
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if (
        database_path == temporary_root
        or not database_path.is_relative_to(temporary_root)
    ):
        raise DatabaseConfigurationError(
            "Test database files must be inside the system temporary directory."
        )
    return URL.create("sqlite+pysqlite", database=str(database_path))


def get_database_config(
    database: DatabaseName,
    *,
    test_database: TestDatabaseSource | None = None,
    environ: Mapping[str, str] | None = None,
) -> DatabaseConfig:
    """Resolve one logical database without opening a connection.

    Test mode reads only DEALSYNC_ENV and requires an explicit temporary
    SQLite database source. It never looks up either production URL variable.
    """
    if database not in _DATABASE_NAMES:
        raise DatabaseConfigurationError("Unknown DealSync database.")

    environment = os.environ if environ is None else environ
    mode = _environment_name(environment)

    if mode == "test":
        return DatabaseConfig(
            environment=mode,
            database=database,
            url=_temporary_sqlite_url(test_database),
        )

    if test_database is not None:
        raise DatabaseConfigurationError(
            "A test database source can only be supplied in test mode."
        )

    if mode == "development":
        database_path = _development_sqlite_path(database, environment)
        other_database: DatabaseName = (
            "authentication" if database == "business" else "business"
        )
        if database_path == _development_sqlite_path(other_database, environment):
            raise DatabaseConfigurationError(
                "Business and authentication SQLite paths must differ."
            )
        return DatabaseConfig(
            environment=mode,
            database=database,
            url=URL.create(
                "sqlite+pysqlite",
                database=str(database_path),
            ),
        )

    production_backend = _production_database_backend(environment)
    if production_backend == "sqlite":
        paths = _production_sqlite_paths(environment)
        url = URL.create(
            "sqlite+pysqlite",
            database=str(paths[database]),
        )
    else:
        url = _production_urls(environment)[database]
    return DatabaseConfig(
        environment=mode,
        database=database,
        url=url,
    )


def _existing_sqlite_connection(config: DatabaseConfig) -> sqlite3.Connection:
    database_path = Path(config.url.database or "").resolve()
    if not database_path.is_file():
        raise DatabaseConfigurationError(
            f"The {config.database} SQLite database file does not exist."
        )

    sqlite_uri = f"{database_path.as_uri()}?mode=rw"
    try:
        return sqlite3.connect(
            sqlite_uri,
            uri=True,
            check_same_thread=False,
            timeout=5,
        )
    except sqlite3.OperationalError:
        if not database_path.is_file():
            raise DatabaseConfigurationError(
                f"The {config.database} SQLite database file does not exist."
            ) from None
        raise


def _enable_sqlite_foreign_keys(
    dbapi_connection: sqlite3.Connection,
    _connection_record: object,
) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys = ON")
        enabled = cursor.execute("PRAGMA foreign_keys").fetchone()
        if enabled is None or enabled[0] != 1:
            raise RuntimeError("SQLite foreign-key enforcement could not be enabled.")
    finally:
        cursor.close()


def _add_sqlite_foreign_key_hook(engine: Engine) -> Engine:
    event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def _create_existing_sqlite_engine(config: DatabaseConfig) -> Engine:
    return _add_sqlite_foreign_key_hook(
        create_engine(
            config.url,
            creator=lambda: _existing_sqlite_connection(config),
            hide_parameters=True,
        )
    )


def _create_engine(config: DatabaseConfig) -> Engine:
    if config.environment == "production":
        if config.url.get_backend_name() == "sqlite":
            return _create_existing_sqlite_engine(config)
        return create_engine(
            config.url,
            pool_pre_ping=True,
            pool_size=2,
            max_overflow=1,
            pool_timeout=15,
            hide_parameters=True,
        )

    if config.environment == "development":
        return _create_existing_sqlite_engine(config)

    return _add_sqlite_foreign_key_hook(
        create_engine(
            config.url,
            connect_args={"check_same_thread": False, "timeout": 5},
            hide_parameters=True,
        )
    )


def create_business_engine(
    *,
    test_database: TestDatabaseSource | None = None,
    environ: Mapping[str, str] | None = None,
) -> Engine:
    """Create the business engine; this does not connect or create tables."""
    config = get_database_config(
        "business",
        test_database=test_database,
        environ=environ,
    )
    return _create_engine(config)


def create_authentication_engine(
    *,
    test_database: TestDatabaseSource | None = None,
    environ: Mapping[str, str] | None = None,
) -> Engine:
    """Create the authentication engine; this does not connect or create tables."""
    config = get_database_config(
        "authentication",
        test_database=test_database,
        environ=environ,
    )
    return _create_engine(config)


def create_engines(
    *,
    test_databases: Mapping[DatabaseName, TestDatabaseSource] | None = None,
    environ: Mapping[str, str] | None = None,
) -> DatabaseEngines:
    """Create both engines and clean up the first if the second fails."""
    business_test_database = (
        None if test_databases is None else test_databases.get("business")
    )
    authentication_test_database = (
        None if test_databases is None else test_databases.get("authentication")
    )

    business_config = get_database_config(
        "business",
        test_database=business_test_database,
        environ=environ,
    )
    authentication_config = get_database_config(
        "authentication",
        test_database=authentication_test_database,
        environ=environ,
    )

    business = _create_engine(business_config)
    try:
        authentication = _create_engine(authentication_config)
    except Exception:
        business.dispose()
        raise

    return DatabaseEngines(
        business=business,
        authentication=authentication,
    )


@contextmanager
def connection(engine: Engine) -> Iterator[Connection]:
    """Yield a connection and always return it to its pool."""
    with engine.connect() as database_connection:
        yield database_connection


@contextmanager
def transaction(engine: Engine) -> Iterator[Connection]:
    """Commit on success, roll back on failure, and close the connection."""
    with engine.begin() as database_connection:
        yield database_connection
