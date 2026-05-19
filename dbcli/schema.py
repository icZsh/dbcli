"""Schema type parsing and validation helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from dbcli.errors import DbcliError, Diagnostic, ExitCode


SchemaKind = Literal[
    "BOOLEAN",
    "TINYINT",
    "SMALLINT",
    "INT",
    "BIGINT",
    "DECIMAL",
    "FLOAT",
    "DOUBLE",
    "VARCHAR",
    "TEXT",
    "DATE",
    "DATETIME",
]

SIMPLE_TYPES: frozenset[SchemaKind] = frozenset(
    {
        "BOOLEAN",
        "TINYINT",
        "SMALLINT",
        "INT",
        "BIGINT",
        "FLOAT",
        "DOUBLE",
        "TEXT",
        "DATE",
        "DATETIME",
    }
)
TYPE_RE = re.compile(r"^(?P<name>[A-Za-z]+)(?:\((?P<params>[^)]*)\))?$")


@dataclass(frozen=True)
class MysqlType:
    raw: str
    kind: SchemaKind
    length: int | None = None
    precision: int | None = None
    scale: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "kind": self.kind,
            "length": self.length,
            "precision": self.precision,
            "scale": self.scale,
        }


@dataclass(frozen=True)
class SchemaColumn:
    name: str
    mysql_type: MysqlType
    nullable: bool = True

    @property
    def type(self) -> str:
        return self.mysql_type.raw

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.mysql_type.raw,
            "nullable": self.nullable,
        }


def parse_mysql_type(value: object, *, path: str | None = None) -> MysqlType:
    if not isinstance(value, str) or not value.strip():
        raise _schema_error(
            "schema.unsupported_type",
            "Schema type must be a non-empty string.",
            path,
            {"type": value},
        )

    raw = value.strip().upper()
    match = TYPE_RE.fullmatch(raw)
    if not match:
        raise _schema_error("schema.unsupported_type", f"Unsupported MySQL type `{value}`.", path, {"type": value})

    name = match.group("name").upper()
    params = match.group("params")

    if name in SIMPLE_TYPES and params is None:
        return MysqlType(raw=name, kind=name)  # type: ignore[arg-type]

    if name == "VARCHAR":
        length = _parse_positive_int(params, "VARCHAR length", value, path)
        return MysqlType(raw=f"VARCHAR({length})", kind="VARCHAR", length=length)

    if name == "DECIMAL":
        parts = _parse_decimal_params(params, value, path)
        precision, scale = parts
        return MysqlType(raw=f"DECIMAL({precision},{scale})", kind="DECIMAL", precision=precision, scale=scale)

    raise _schema_error("schema.unsupported_type", f"Unsupported MySQL type `{value}`.", path, {"type": value})


def parse_schema_columns(schema: object, *, path: str = "schema") -> list[SchemaColumn]:
    if not isinstance(schema, list) or not schema:
        raise _schema_error("schema.invalid", "Recipe schema must be a non-empty list.", path, {})

    columns: list[SchemaColumn] = []
    seen: set[str] = set()
    for index, item in enumerate(schema):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            raise _schema_error("schema.invalid", "Each schema entry must be a mapping.", item_path, {})

        unknown = sorted(set(item) - {"name", "type", "nullable"})
        if unknown:
            raise _schema_error(
                "recipe.unknown_key",
                f"Unknown key in schema entry: {unknown[0]}.",
                f"{item_path}.{unknown[0]}",
                {"key": unknown[0]},
            )

        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise _schema_error("schema.invalid", "Schema column name must be a non-empty string.", f"{item_path}.name", {})
        if name in seen:
            raise _schema_error(
                "schema.duplicate_column",
                f"Duplicate schema column `{name}`.",
                f"{item_path}.name",
                {"column": name},
            )
        seen.add(name)

        nullable = item.get("nullable", True)
        if not isinstance(nullable, bool):
            raise _schema_error(
                "schema.invalid",
                f"Schema column `{name}` nullable value must be true or false.",
                f"{item_path}.nullable",
                {"column": name},
            )

        columns.append(
            SchemaColumn(
                name=name,
                mysql_type=parse_mysql_type(item.get("type"), path=f"{item_path}.type"),
                nullable=nullable,
            )
        )

    return columns


def schema_to_dict(columns: list[SchemaColumn]) -> list[dict[str, Any]]:
    return [column.to_dict() for column in columns]


def _parse_positive_int(params: str | None, label: str, original: str, path: str | None) -> int:
    if params is None:
        raise _schema_error("schema.unsupported_type", f"{label} is required in `{original}`.", path, {"type": original})
    try:
        value = int(params.strip())
    except ValueError as exc:
        raise _schema_error("schema.unsupported_type", f"{label} must be an integer.", path, {"type": original}) from exc
    if value <= 0:
        raise _schema_error("schema.unsupported_type", f"{label} must be positive.", path, {"type": original})
    return value


def _parse_decimal_params(params: str | None, original: str, path: str | None) -> tuple[int, int]:
    if params is None:
        raise _schema_error(
            "schema.unsupported_type",
            f"DECIMAL precision and scale are required in `{original}`.",
            path,
            {"type": original},
        )
    parts = [part.strip() for part in params.split(",")]
    if len(parts) != 2:
        raise _schema_error(
            "schema.unsupported_type",
            f"DECIMAL type `{original}` must use DECIMAL(p,s).",
            path,
            {"type": original},
        )
    try:
        precision = int(parts[0])
        scale = int(parts[1])
    except ValueError as exc:
        raise _schema_error(
            "schema.unsupported_type",
            f"DECIMAL type `{original}` must use integer precision and scale.",
            path,
            {"type": original},
        ) from exc
    if precision <= 0 or scale < 0 or scale > precision:
        raise _schema_error(
            "schema.unsupported_type",
            f"DECIMAL type `{original}` has invalid precision or scale.",
            path,
            {"type": original},
        )
    return precision, scale


def _schema_error(code: str, message: str, path: str | None, details: dict[str, Any]) -> DbcliError:
    return DbcliError(
        Diagnostic(
            code=code,
            message=message,
            path=path,
            details=details,
        ),
        ExitCode.VALIDATION_ERROR,
    )
