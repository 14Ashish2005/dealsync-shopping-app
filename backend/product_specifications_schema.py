from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


SPECIFICATION_UNITS = {
    "ram": frozenset({"GB", "TB"}),
    "storage_capacity": frozenset({"GB", "TB"}),
    "weight": frozenset({"mg", "g", "kg", "oz", "lb"}),
    "battery_capacity": frozenset({"mAh"}),
    "display_size": frozenset({"inch", "mm", "cm"}),
}
_DECIMAL_VALUE_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z")
_UTC_TIMESTAMP_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z"
)

TRUSTED_SPECIFICATION_SOURCES_TABLE = "trusted_specification_sources"
PRODUCT_SPECIFICATIONS_TABLE = "product_specifications"

_TRUSTED_SOURCES_SQL = """
    CREATE TABLE IF NOT EXISTS trusted_specification_sources (
        source_key TEXT PRIMARY KEY NOT NULL
            CHECK (length(trim(source_key)) BETWEEN 1 AND 128),
        enabled INTEGER NOT NULL DEFAULT 0
            CHECK (enabled IN (0, 1))
    )
"""

_PRODUCT_SPECIFICATIONS_SQL = """
    CREATE TABLE IF NOT EXISTS product_specifications (
        evidence_id INTEGER PRIMARY KEY,
        product_id INTEGER NOT NULL,
        attribute_name TEXT NOT NULL
            CHECK (attribute_name IN (
                'ram',
                'storage_capacity',
                'weight',
                'battery_capacity',
                'display_size'
            )),
        value TEXT DEFAULT NULL,
        unit TEXT DEFAULT NULL,
        verification_status TEXT DEFAULT NULL
            CHECK (
                verification_status IS NULL
                OR verification_status IN ('verified', 'unverified')
            ),
        source_reference TEXT DEFAULT NULL
            CHECK (
                source_reference IS NULL
                OR length(trim(source_reference)) BETWEEN 1 AND 2048
            ),
        source_key TEXT DEFAULT NULL
            REFERENCES trusted_specification_sources(source_key)
            ON DELETE RESTRICT,
        verification_source TEXT DEFAULT NULL
            REFERENCES trusted_specification_sources(source_key)
            ON DELETE RESTRICT,
        observed_at TEXT DEFAULT NULL,
        verified_at TEXT DEFAULT NULL,
        FOREIGN KEY (product_id)
            REFERENCES products(id) ON DELETE CASCADE,
        CHECK (
            value IS NULL
            OR (
                typeof(value) = 'text'
                AND length(value) > 0
                AND value NOT GLOB '*[^0-9.]*'
                AND value GLOB '*[1-9]*'
                AND length(value) - length(replace(value, '.', '')) <= 1
                AND substr(value, 1, 1) GLOB '[0-9]'
                AND substr(value, -1, 1) GLOB '[0-9]'
            )
        ),
        CHECK (
            (value IS NULL AND unit IS NULL)
            OR (value IS NOT NULL AND unit IS NOT NULL)
        ),
        CHECK (
            unit IS NULL
            OR (
                attribute_name IN ('ram', 'storage_capacity')
                AND unit IN ('GB', 'TB')
            )
            OR (attribute_name = 'weight' AND unit IN ('mg', 'g', 'kg', 'oz', 'lb'))
            OR (attribute_name = 'battery_capacity' AND unit = 'mAh')
            OR (attribute_name = 'display_size' AND unit IN ('inch', 'mm', 'cm'))
        ),
        CHECK (
            observed_at IS NULL
            OR (
                length(observed_at) = 20
                AND observed_at GLOB
                    '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
            )
        ),
        CHECK (
            verified_at IS NULL
            OR (
                length(verified_at) = 20
                AND verified_at GLOB
                    '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
            )
        ),
        CHECK (
            (
                value IS NULL
                AND unit IS NULL
                AND verification_status IS NULL
                AND source_reference IS NULL
                AND source_key IS NULL
                AND verification_source IS NULL
                AND observed_at IS NULL
                AND verified_at IS NULL
            )
            OR (
                value IS NOT NULL
                AND unit IS NOT NULL
                AND verification_status = 'unverified'
                AND source_reference IS NOT NULL
                AND source_key IS NOT NULL
                AND verification_source IS NULL
                AND observed_at IS NOT NULL
                AND verified_at IS NULL
            )
            OR (
                value IS NOT NULL
                AND unit IS NOT NULL
                AND verification_status = 'verified'
                AND source_reference IS NOT NULL
                AND source_key IS NOT NULL
                AND verification_source IS NOT NULL
                AND observed_at IS NOT NULL
                AND verified_at IS NOT NULL
            )
        )
    )
"""


class ProductSpecificationsSchemaError(RuntimeError):
    pass


def _normalized_sql(connection: sqlite3.Connection, table: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if row is None or not isinstance(row[0], str):
        raise ProductSpecificationsSchemaError(
            f"Required table {table!r} is missing."
        )
    return " ".join(row[0].replace('"', "").split()).casefold()


def _validate_columns(
    connection: sqlite3.Connection,
    table: str,
    expected: tuple[tuple[str, str, int, str | None, int], ...],
) -> None:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    actual = tuple(
        (row[1], row[2].upper(), row[3], row[4], row[5])
        for row in rows
    )
    if actual != expected:
        raise ProductSpecificationsSchemaError(
            f"Existing table {table!r} has an incompatible column definition."
        )


def _foreign_keys(
    connection: sqlite3.Connection,
    table: str,
) -> set[tuple[str, str, str, str]]:
    return {
        (row[3], row[2], row[4], row[6].upper())
        for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
    }


def _validate_trusted_sources_schema(connection: sqlite3.Connection) -> None:
    if _foreign_keys(connection, TRUSTED_SPECIFICATION_SOURCES_TABLE):
        raise ProductSpecificationsSchemaError(
            "The trusted specification source registry has unexpected foreign keys."
        )
    _validate_columns(
        connection,
        TRUSTED_SPECIFICATION_SOURCES_TABLE,
        (
            ("source_key", "TEXT", 1, None, 1),
            ("enabled", "INTEGER", 1, "0", 0),
        ),
    )
    sql = _normalized_sql(connection, TRUSTED_SPECIFICATION_SOURCES_TABLE)
    if (
        "check (length(trim(source_key)) between 1 and 128)" not in sql
        or "check (enabled in (0, 1))" not in sql
    ):
        raise ProductSpecificationsSchemaError(
            "The trusted specification source registry is missing required constraints."
        )


def _validate_product_specifications_schema(
    connection: sqlite3.Connection,
) -> None:
    _validate_columns(
        connection,
        PRODUCT_SPECIFICATIONS_TABLE,
        (
            ("evidence_id", "INTEGER", 0, None, 1),
            ("product_id", "INTEGER", 1, None, 0),
            ("attribute_name", "TEXT", 1, None, 0),
            ("value", "TEXT", 0, "NULL", 0),
            ("unit", "TEXT", 0, "NULL", 0),
            ("verification_status", "TEXT", 0, "NULL", 0),
            ("source_reference", "TEXT", 0, "NULL", 0),
            ("source_key", "TEXT", 0, "NULL", 0),
            ("verification_source", "TEXT", 0, "NULL", 0),
            ("observed_at", "TEXT", 0, "NULL", 0),
            ("verified_at", "TEXT", 0, "NULL", 0),
        ),
    )

    expected_foreign_keys = {
        ("product_id", "products", "id", "CASCADE"),
        (
            "source_key",
            TRUSTED_SPECIFICATION_SOURCES_TABLE,
            "source_key",
            "RESTRICT",
        ),
        (
            "verification_source",
            TRUSTED_SPECIFICATION_SOURCES_TABLE,
            "source_key",
            "RESTRICT",
        ),
    }
    if _foreign_keys(connection, PRODUCT_SPECIFICATIONS_TABLE) != expected_foreign_keys:
        raise ProductSpecificationsSchemaError(
            "The product specifications table has incompatible foreign keys."
        )

    sql = _normalized_sql(connection, PRODUCT_SPECIFICATIONS_TABLE)
    required_constraints = (
        "'ram', 'storage_capacity', 'weight', 'battery_capacity', 'display_size'",
        "unit in ('gb', 'tb')",
        "unit in ('mg', 'g', 'kg', 'oz', 'lb')",
        "unit = 'mah'",
        "unit in ('inch', 'mm', 'cm')",
        "value is null and unit is null",
        "value is not null and unit is not null",
        "verification_status = 'verified'",
        "verification_status = 'unverified'",
    )
    if any(fragment not in sql for fragment in required_constraints):
        raise ProductSpecificationsSchemaError(
            "The product specifications table is missing required constraints."
        )
    unique_sets = set()
    for row in connection.execute(
        f'PRAGMA index_list("{PRODUCT_SPECIFICATIONS_TABLE}")'
    ):
        if row[2]:
            columns = connection.execute(
                f'PRAGMA index_info("{row[1]}")'
            ).fetchall()
            unique_sets.add(tuple(column[2] for column in columns))
    if ("product_id", "attribute_name") in unique_sets:
        raise ProductSpecificationsSchemaError(
            "The product specifications table must permit multiple evidence records."
        )


def validate_existing_product_specifications_schema(
    connection: sqlite3.Connection,
) -> None:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if not {
        TRUSTED_SPECIFICATION_SOURCES_TABLE,
        PRODUCT_SPECIFICATIONS_TABLE,
    }.issubset(tables):
        raise ProductSpecificationsSchemaError(
            "The product specifications schema is incomplete."
        )
    _validate_trusted_sources_schema(connection)
    _validate_product_specifications_schema(connection)


def ensure_product_specifications_schema(
    connection: sqlite3.Connection,
) -> tuple[str, ...]:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "products" not in tables:
        raise ProductSpecificationsSchemaError(
            "The products table must exist before product specifications."
        )
    product_columns = connection.execute("PRAGMA table_info(products)").fetchall()
    if not any(
        row[1] == "id" and row[2].upper() == "INTEGER" and row[5] == 1
        for row in product_columns
    ):
        raise ProductSpecificationsSchemaError(
            "The products table must have an integer primary-key ID."
        )

    if TRUSTED_SPECIFICATION_SOURCES_TABLE in tables:
        _validate_trusted_sources_schema(connection)
    if PRODUCT_SPECIFICATIONS_TABLE in tables:
        _validate_product_specifications_schema(connection)

    created: list[str] = []
    if TRUSTED_SPECIFICATION_SOURCES_TABLE not in tables:
        connection.execute(_TRUSTED_SOURCES_SQL)
        created.append(TRUSTED_SPECIFICATION_SOURCES_TABLE)
    if PRODUCT_SPECIFICATIONS_TABLE not in tables:
        connection.execute(_PRODUCT_SPECIFICATIONS_SQL)
        created.append(PRODUCT_SPECIFICATIONS_TABLE)

    validate_existing_product_specifications_schema(connection)
    return tuple(created)


def _parse_utc_timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not _UTC_TIMESTAMP_PATTERN.fullmatch(value):
        raise ValueError(f"{name} must be a valid UTC timestamp.")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid UTC timestamp.") from exc
    return parsed


def validate_verified_specification_evidence(
    connection: sqlite3.Connection,
    evidence: Mapping[str, object],
    *,
    now: datetime | None = None,
) -> Decimal:
    """Validate evidence for future trusted filtering; a source name alone is insufficient."""
    attribute_name = evidence.get("attribute_name")
    if not isinstance(attribute_name, str) or attribute_name not in SPECIFICATION_UNITS:
        raise ValueError("Specification attribute is not supported.")

    unit = evidence.get("unit")
    if not isinstance(unit, str) or unit not in SPECIFICATION_UNITS[attribute_name]:
        raise ValueError("Specification unit is not supported for this attribute.")

    raw_value = evidence.get("value")
    if not isinstance(raw_value, str) or not _DECIMAL_VALUE_PATTERN.fullmatch(raw_value):
        raise ValueError("Specification value must be a positive decimal.")
    try:
        value = Decimal(raw_value)
    except InvalidOperation as exc:
        raise ValueError("Specification value must be a positive decimal.") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("Specification value must be a positive decimal.")

    if evidence.get("verification_status") != "verified":
        raise ValueError("Specification evidence is not verified.")

    source_reference = evidence.get("source_reference")
    if not isinstance(source_reference, str) or not source_reference.strip():
        raise ValueError("Specification evidence requires a source reference.")

    source_key = evidence.get("source_key")
    verification_source = evidence.get("verification_source")
    for name, source in (
        ("data source", source_key),
        ("verification source", verification_source),
    ):
        if not isinstance(source, str) or not source.strip():
            raise ValueError(f"Specification evidence requires a trusted {name}.")
        row = connection.execute(
            f"""
            SELECT enabled
            FROM {TRUSTED_SPECIFICATION_SOURCES_TABLE}
            WHERE source_key = ?
            """,
            (source,),
        ).fetchone()
        if row is None or row[0] != 1:
            raise ValueError(f"Specification {name} is not trusted.")

    observed_at = _parse_utc_timestamp(evidence.get("observed_at"), "observed_at")
    verified_at = _parse_utc_timestamp(evidence.get("verified_at"), "verified_at")
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("Validation time must include a timezone.")
    if observed_at > verified_at or verified_at > current_time.astimezone(timezone.utc):
        raise ValueError("Specification verification timestamps are inconsistent.")

    return value
