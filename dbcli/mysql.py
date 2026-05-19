"""MySQL connectivity, identifier safety, and load helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

import polars as pl
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL
from sqlalchemy.exc import SQLAlchemyError

from dbcli.errors import DbcliError, Diagnostic, ExitCode
from dbcli.profiles import ResolvedProfile
from dbcli.project import ResolvedSettings
from dbcli.schema import SchemaColumn


IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SQL_OPTION_RE = re.compile(r"^[A-Za-z0-9_]+$")
MAX_IDENTIFIER_LENGTH = 64


@dataclass(frozen=True)
class QualifiedIdentifier:
    schema: str | None
    name: str

    def to_dict(self) -> dict[str, str | None]:
        return {"schema": self.schema, "name": self.name}


@dataclass(frozen=True)
class MysqlColumnInfo:
    name: str
    type: str
    nullable: bool
    charset: str | None = None
    collation: str | None = None
    ordinal_position: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "nullable": self.nullable,
            "charset": self.charset,
            "collation": self.collation,
            "ordinal_position": self.ordinal_position,
        }


@dataclass(frozen=True)
class MysqlTableInfo:
    schema: str
    name: str
    engine: str
    collation: str
    charset: str
    columns: list[MysqlColumnInfo]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "name": self.name,
            "engine": self.engine,
            "collation": self.collation,
            "charset": self.charset,
            "columns": [column.to_dict() for column in self.columns],
        }


def mysql_url(profile: ResolvedProfile) -> URL:
    return URL.create(
        "mysql+pymysql",
        username=profile.user,
        password=profile.password,
        host=profile.host,
        port=profile.port,
        database=profile.database,
        query={"charset": "utf8mb4"},
    )


def create_mysql_engine(profile: ResolvedProfile) -> Engine:
    return create_engine(mysql_url(profile), pool_pre_ping=True)


def check_profile_connection(profile: ResolvedProfile, *, engine: Engine | None = None) -> None:
    owned_engine = engine is None
    engine = engine or create_mysql_engine(profile)
    try:
        with engine.connect() as connection:
            result = connection.execute(text("SELECT 1"))
            result.scalar_one()
    except SQLAlchemyError as exc:
        raise DbcliError(
            Diagnostic(
                code="mysql.connection_failed",
                message=f"Could not connect to MySQL profile `{profile.name}`.",
                path=None,
                details={"profile": profile.name, "error": exc.__class__.__name__},
            ),
            ExitCode.DB_OR_PROFILE_ERROR,
        ) from exc
    finally:
        if owned_engine:
            engine.dispose()


def parse_identifier(value: str) -> QualifiedIdentifier:
    if not isinstance(value, str) or not value:
        raise _identifier_error(value)
    if "`" in value or ";" in value:
        raise _identifier_error(value)
    parts = value.split(".")
    if len(parts) > 2 or any(not part for part in parts):
        raise _identifier_error(value)
    for part in parts:
        if len(part) > MAX_IDENTIFIER_LENGTH or not IDENTIFIER_RE.fullmatch(part):
            raise _identifier_error(value)
    if len(parts) == 2:
        return QualifiedIdentifier(schema=parts[0], name=parts[1])
    return QualifiedIdentifier(schema=None, name=parts[0])


def quote_identifier(value: str) -> str:
    parsed = parse_identifier(value)
    if parsed.schema:
        return f"`{parsed.schema}`.`{parsed.name}`"
    return f"`{parsed.name}`"


def quote_name(value: str) -> str:
    parsed = parse_identifier(value)
    if parsed.schema is not None:
        raise _identifier_error(value)
    return f"`{parsed.name}`"


def create_table_sql(
    table: str,
    schema: list[SchemaColumn],
    *,
    charset: str,
    collation: str,
    engine: str,
) -> str:
    if not schema:
        raise DbcliError(
            Diagnostic(
                code="schema.invalid",
                message="Cannot create a table without schema columns.",
                path="schema",
                details={},
            ),
            ExitCode.VALIDATION_ERROR,
        )
    charset = _safe_sql_option(charset, "charset")
    collation = _safe_sql_option(collation, "collation")
    engine = _safe_sql_option(engine, "engine")
    columns = ",\n  ".join(_column_definition(column) for column in schema)
    return (
        f"CREATE TABLE {quote_identifier(table)} (\n"
        f"  {columns}\n"
        f") ENGINE={engine} DEFAULT CHARSET={charset} COLLATE={collation}"
    )


def insert_sql(table: str, columns: Iterable[str]) -> str:
    column_list = list(columns)
    if not column_list:
        raise DbcliError(
            Diagnostic(
                code="schema.invalid",
                message="Cannot insert rows without columns.",
                path="schema",
                details={},
            ),
            ExitCode.VALIDATION_ERROR,
        )
    quoted_columns = ", ".join(quote_name(column) for column in column_list)
    values = ", ".join(f":{column}" for column in column_list)
    return f"INSERT INTO {quote_identifier(table)} ({quoted_columns}) VALUES ({values})"


def compare_table_info(
    declared: list[SchemaColumn],
    live: MysqlTableInfo,
    *,
    charset: str,
    collation: str,
    engine: str,
) -> list[dict[str, Any]]:
    diffs: list[dict[str, Any]] = []
    expected_engine = _normalize_option(engine)
    if _normalize_option(live.engine) != expected_engine:
        diffs.append({"check": "engine", "expected": engine, "actual": live.engine})

    expected_charset = _normalize_option(charset)
    if _normalize_option(live.charset) != expected_charset:
        diffs.append({"check": "charset", "expected": charset, "actual": live.charset})

    expected_collation = _normalize_option(collation)
    if _normalize_option(live.collation) != expected_collation:
        diffs.append({"check": "collation", "expected": collation, "actual": live.collation})

    declared_names = [column.name for column in declared]
    live_names = [column.name for column in live.columns]
    live_by_name = {column.name: column for column in live.columns}
    declared_by_name = {column.name: column for column in declared}

    for column_name in declared_names:
        if column_name not in live_by_name:
            diffs.append({"check": "missing_column", "column": column_name})
    for column_name in live_names:
        if column_name not in declared_by_name:
            diffs.append({"check": "extra_column", "column": column_name})

    if set(declared_names) == set(live_names) and declared_names != live_names:
        diffs.append({"check": "column_order", "expected": declared_names, "actual": live_names})

    for declared_column in declared:
        live_column = live_by_name.get(declared_column.name)
        if live_column is None:
            continue
        expected_type = _normalize_declared_type(declared_column)
        actual_type = _normalize_live_type(live_column.type)
        if expected_type != actual_type:
            diffs.append(
                {
                    "check": "type",
                    "column": declared_column.name,
                    "expected": expected_type,
                    "actual": actual_type,
                }
            )
        if live_column.nullable != declared_column.nullable:
            diffs.append(
                {
                    "check": "nullability",
                    "column": declared_column.name,
                    "expected": declared_column.nullable,
                    "actual": live_column.nullable,
                }
            )
        if declared_column.mysql_type.kind in {"VARCHAR", "TEXT"}:
            if live_column.charset and _normalize_option(live_column.charset) != expected_charset:
                diffs.append(
                    {
                        "check": "column_charset",
                        "column": declared_column.name,
                        "expected": charset,
                        "actual": live_column.charset,
                    }
                )
            if live_column.collation and _normalize_option(live_column.collation) != expected_collation:
                diffs.append(
                    {
                        "check": "column_collation",
                        "column": declared_column.name,
                        "expected": collation,
                        "actual": live_column.collation,
                    }
                )
    return diffs


def schema_drift_error(table: str, diffs: list[dict[str, Any]]) -> DbcliError:
    return DbcliError(
        Diagnostic(
            code="mysql.schema_drift",
            message=f"Live table `{table}` differs from the recipe schema.",
            path="target.table",
            details={"table": table, "diffs": diffs},
        ),
        ExitCode.USAGE_OR_DRIFT,
    )


class SqlAlchemyMysqlAdapter:
    def __init__(self, profile: ResolvedProfile, *, engine: Engine | None = None) -> None:
        self.profile = profile
        self.engine = engine or create_mysql_engine(profile)
        self._owns_engine = engine is None

    def close(self) -> None:
        if self._owns_engine:
            self.engine.dispose()

    def append_rows(
        self,
        table: str,
        schema: list[SchemaColumn],
        dataframe: pl.DataFrame,
        settings: ResolvedSettings,
    ) -> int:
        with self.engine.begin() as connection:
            live = fetch_table_info(connection, table, default_schema=self.profile.database)
            if live is None:
                connection.execute(
                    text(
                        create_table_sql(
                            table,
                            schema,
                            charset=settings.charset,
                            collation=settings.collation,
                            engine=settings.engine,
                        )
                    )
                )
            else:
                diffs = compare_table_info(
                    schema,
                    live,
                    charset=settings.charset,
                    collation=settings.collation,
                    engine=settings.engine,
                )
                if diffs:
                    raise schema_drift_error(table, diffs)
            return insert_dataframe(connection, table, schema, dataframe, settings.batch_size)

    def replace_rows(
        self,
        table: str,
        schema: list[SchemaColumn],
        dataframe: pl.DataFrame,
        settings: ResolvedSettings,
        *,
        run_id: str,
    ) -> int:
        staging = companion_table_name(table, "staging", run_id)
        backup = companion_table_name(table, "backup", run_id)
        live_exists = False
        swapped = False
        try:
            with self.engine.begin() as connection:
                live_exists = fetch_table_info(connection, table, default_schema=self.profile.database) is not None
                connection.execute(
                    text(
                        create_table_sql(
                            staging,
                            schema,
                            charset=settings.charset,
                            collation=settings.collation,
                            engine=settings.engine,
                        )
                    )
                )
                loaded = insert_dataframe(connection, staging, schema, dataframe, settings.batch_size)

            with self.engine.begin() as connection:
                if live_exists:
                    connection.execute(text(rename_tables_sql([(table, backup), (staging, table)])))
                    swapped = True
                    connection.execute(text(drop_table_sql(backup)))
                else:
                    connection.execute(text(rename_tables_sql([(staging, table)])))
                    swapped = True
            return loaded
        except SQLAlchemyError:
            if not swapped:
                self._drop_table_quietly(staging)
            raise

    def _drop_table_quietly(self, table: str) -> None:
        try:
            with self.engine.begin() as connection:
                connection.execute(text(drop_table_sql(table)))
        except SQLAlchemyError:
            return


def fetch_table_info(connection: Any, table: str, *, default_schema: str) -> MysqlTableInfo | None:
    parsed = parse_identifier(table)
    table_schema = parsed.schema or default_schema
    table_result = connection.execute(
        text(
            """
            SELECT ENGINE, TABLE_COLLATION
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :table
            """
        ),
        {"schema": table_schema, "table": parsed.name},
    )
    table_row = table_result.mappings().first()
    if table_row is None:
        return None

    column_result = connection.execute(
        text(
            """
            SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, CHARACTER_SET_NAME, COLLATION_NAME, ORDINAL_POSITION
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :table
            ORDER BY ORDINAL_POSITION
            """
        ),
        {"schema": table_schema, "table": parsed.name},
    )
    columns = [
        MysqlColumnInfo(
            name=str(row["COLUMN_NAME"]),
            type=str(row["COLUMN_TYPE"]),
            nullable=str(row["IS_NULLABLE"]).upper() == "YES",
            charset=str(row["CHARACTER_SET_NAME"]) if row["CHARACTER_SET_NAME"] is not None else None,
            collation=str(row["COLLATION_NAME"]) if row["COLLATION_NAME"] is not None else None,
            ordinal_position=int(row["ORDINAL_POSITION"]) if row["ORDINAL_POSITION"] is not None else None,
        )
        for row in column_result.mappings()
    ]
    table_collation = str(table_row["TABLE_COLLATION"])
    return MysqlTableInfo(
        schema=table_schema,
        name=parsed.name,
        engine=str(table_row["ENGINE"]),
        collation=table_collation,
        charset=_charset_from_collation(table_collation),
        columns=columns,
    )


def insert_dataframe(
    connection: Any,
    table: str,
    schema: list[SchemaColumn],
    dataframe: pl.DataFrame,
    batch_size: int,
) -> int:
    columns = [column.name for column in schema]
    sql = text(insert_sql(table, columns))
    rows = dataframe.select(columns).to_dicts()
    for start in range(0, len(rows), batch_size):
        connection.execute(sql, rows[start : start + batch_size])
    return len(rows)


def companion_table_name(table: str, role: str, run_id: str) -> str:
    parsed = parse_identifier(table)
    if role not in {"staging", "backup"}:
        raise DbcliError(
            Diagnostic(
                code="mysql.invalid_companion_table",
                message="Companion table role must be staging or backup.",
                path=None,
                details={"role": role},
            ),
            ExitCode.USAGE_OR_DRIFT,
        )
    suffix = f"__dbcli_{role}__{run_id}"
    available = MAX_IDENTIFIER_LENGTH - len(suffix)
    if available <= 0:
        raise DbcliError(
            Diagnostic(
                code="mysql.invalid_companion_table",
                message="Run id leaves no room for a staging or backup table name.",
                path=None,
                details={"role": role},
            ),
            ExitCode.USAGE_OR_DRIFT,
        )
    base = parsed.name[:available]
    companion = f"{base}{suffix}"
    if parsed.schema:
        return f"{parsed.schema}.{companion}"
    return companion


def rename_tables_sql(pairs: list[tuple[str, str]]) -> str:
    if not pairs:
        raise DbcliError(
            Diagnostic(
                code="mysql.invalid_rename",
                message="At least one table rename pair is required.",
                path=None,
                details={},
            ),
            ExitCode.USAGE_OR_DRIFT,
        )
    fragments = [f"{quote_identifier(source)} TO {quote_identifier(target)}" for source, target in pairs]
    return "RENAME TABLE " + ", ".join(fragments)


def drop_table_sql(table: str) -> str:
    return f"DROP TABLE IF EXISTS {quote_identifier(table)}"


def _identifier_error(value: Any) -> DbcliError:
    return DbcliError(
        Diagnostic(
            code="mysql.invalid_identifier",
            message="Identifier must be a safe MySQL identifier or schema.table pair.",
            path=None,
            details={"identifier": str(value)},
        ),
        ExitCode.USAGE_OR_DRIFT,
    )


def _column_definition(column: SchemaColumn) -> str:
    nullability = "NULL" if column.nullable else "NOT NULL"
    return f"{quote_name(column.name)} {column.mysql_type.raw} {nullability}"


def _safe_sql_option(value: str, label: str) -> str:
    if not isinstance(value, str) or not SQL_OPTION_RE.fullmatch(value):
        raise DbcliError(
            Diagnostic(
                code="mysql.invalid_option",
                message=f"MySQL {label} contains unsafe characters.",
                path=f"target.{label}",
                details={"option": label},
            ),
            ExitCode.USAGE_OR_DRIFT,
        )
    return value


def _normalize_option(value: str | None) -> str:
    return "" if value is None else str(value).lower()


def _normalize_declared_type(column: SchemaColumn) -> str:
    if column.mysql_type.kind == "BOOLEAN":
        return "TINYINT(1)"
    return _normalize_live_type(column.mysql_type.raw)


def _normalize_live_type(value: str) -> str:
    normalized = re.sub(r"\s+", "", value.upper())
    if normalized in {"BOOL", "BOOLEAN"}:
        return "TINYINT(1)"
    integer_match = re.fullmatch(r"(TINYINT|SMALLINT|INT|BIGINT)(?:\((\d+)\))?", normalized)
    if integer_match:
        kind, width = integer_match.groups()
        if kind == "TINYINT" and width == "1":
            return "TINYINT(1)"
        return kind
    decimal_match = re.fullmatch(r"DECIMAL\((\d+),(\d+)\)", normalized)
    if decimal_match:
        precision, scale = decimal_match.groups()
        return f"DECIMAL({int(precision)},{int(scale)})"
    varchar_match = re.fullmatch(r"VARCHAR\((\d+)\)", normalized)
    if varchar_match:
        return f"VARCHAR({int(varchar_match.group(1))})"
    return normalized


def _charset_from_collation(collation: str) -> str:
    return collation.split("_", 1)[0]
