"""Declarative edit pipeline operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import polars as pl

from dbcli.errors import DbcliError, Diagnostic, ExitCode
from dbcli.rejects import RejectRecord
from dbcli.schema import SchemaColumn


INTERNAL_ROW_INDEX = "__dbcli_internal_row_index"
CAST_TYPES = frozenset({"string", "boolean", "int", "float", "decimal", "date", "datetime"})
TRUE_VALUES = frozenset({"true", "1", "yes", "y"})
FALSE_VALUES = frozenset({"false", "0", "no", "n"})
CAST_COMPATIBILITY = {
    "string": {"VARCHAR", "TEXT"},
    "boolean": {"BOOLEAN"},
    "int": {"TINYINT", "SMALLINT", "INT", "BIGINT"},
    "float": {"FLOAT", "DOUBLE"},
    "decimal": {"DECIMAL"},
    "date": {"DATE"},
    "datetime": {"DATETIME"},
}


@dataclass(frozen=True)
class EditResult:
    dataframe: pl.DataFrame
    rejects: list[RejectRecord]
    original_columns: list[str]
    row_indices: list[int]
    original_rows_by_index: dict[int, dict[str, Any]]

    @property
    def rejected_row_indices(self) -> set[int]:
        return {reject.row_index for reject in self.rejects}


def apply_edit_pipeline(
    dataframe: pl.DataFrame,
    edits: list[dict[str, Any]],
    *,
    schema: list[SchemaColumn] | None = None,
) -> EditResult:
    _ensure_no_internal_conflict(dataframe)
    original_columns = list(dataframe.columns)
    working = dataframe.with_row_index(INTERNAL_ROW_INDEX, offset=1)
    original_by_index = {
        int(row[INTERNAL_ROW_INDEX]): {column: row.get(column) for column in original_columns}
        for row in working.to_dicts()
    }
    schema_by_name = {column.name: column for column in schema or []}
    rejects: list[RejectRecord] = []

    for edit in edits:
        op, spec = _single_edit(edit)
        if op == "rename":
            working = _apply_rename(working, spec)
        elif op == "drop":
            working = _apply_drop(working, spec)
        elif op == "trim":
            working = _apply_trim(working, spec)
        elif op == "parse_null":
            working = _apply_parse_null(working, spec)
        elif op == "cast":
            working, cast_rejects = _apply_cast(working, spec, schema_by_name, original_by_index)
            rejects.extend(cast_rejects)
        elif op == "fill_null":
            working = _apply_fill_null(working, spec)
        else:
            raise _edit_error("edit.unknown_op", f"Unknown edit operation `{op}`.", {"op": op})

    return EditResult(
        dataframe=working.drop(INTERNAL_ROW_INDEX),
        rejects=rejects,
        original_columns=original_columns,
        row_indices=[int(value) for value in working[INTERNAL_ROW_INDEX].to_list()],
        original_rows_by_index={index: dict(row) for index, row in original_by_index.items()},
    )


def _apply_rename(dataframe: pl.DataFrame, spec: object) -> pl.DataFrame:
    mapping = _string_mapping(spec, "rename")
    for source in mapping:
        _require_column(dataframe, source, "rename")
    if INTERNAL_ROW_INDEX in mapping.values():
        raise _edit_error(
            "edit.reserved_column",
            f"Column `{INTERNAL_ROW_INDEX}` is reserved for dbcli internals.",
            {"column": INTERNAL_ROW_INDEX},
        )
    if len(set(mapping.values())) != len(mapping):
        raise _edit_error("edit.rename_collision", "Rename operation has duplicate target columns.", {})

    final_columns = [mapping.get(column, column) for column in dataframe.columns]
    duplicates = _duplicates(final_columns)
    user_duplicates = [column for column in duplicates if column != INTERNAL_ROW_INDEX]
    if user_duplicates:
        raise _edit_error(
            "edit.rename_collision",
            f"Rename operation would create duplicate column `{user_duplicates[0]}`.",
            {"column": user_duplicates[0]},
        )
    return dataframe.rename(mapping)


def _apply_drop(dataframe: pl.DataFrame, spec: object) -> pl.DataFrame:
    columns = _string_list(spec, "drop")
    for column in columns:
        _require_column(dataframe, column, "drop")
    return dataframe.drop(columns)


def _apply_trim(dataframe: pl.DataFrame, spec: object) -> pl.DataFrame:
    columns = _string_list(spec, "trim")
    expressions: list[pl.Expr] = []
    for column in columns:
        _require_column(dataframe, column, "trim")
        if dataframe.schema[column] != pl.String:
            raise _edit_error(
                "edit.type_mismatch",
                f"Column `{column}` must be string-like for trim.",
                {"column": column, "op": "trim"},
            )
        expressions.append(pl.col(column).str.strip_chars().alias(column))
    return dataframe.with_columns(expressions)


def _apply_parse_null(dataframe: pl.DataFrame, spec: object) -> pl.DataFrame:
    if not isinstance(spec, dict):
        raise _edit_error("edit.invalid", "parse_null spec must be a mapping.", {"op": "parse_null"})
    unknown = sorted(set(spec) - {"columns", "values"})
    if unknown:
        raise _edit_error("edit.unknown_key", f"Unknown parse_null key `{unknown[0]}`.", {"key": unknown[0]})
    columns = _string_list(spec.get("columns"), "parse_null.columns")
    values = _list(spec.get("values"), "parse_null.values")
    for value in values:
        if not isinstance(value, str):
            raise _edit_error("edit.invalid", "parse_null values must be strings.", {"op": "parse_null"})

    expressions: list[pl.Expr] = []
    for column in columns:
        _require_column(dataframe, column, "parse_null")
        expressions.append(
            pl.when(pl.col(column).is_in(values))
            .then(pl.lit(None))
            .otherwise(pl.col(column))
            .alias(column)
        )
    return dataframe.with_columns(expressions)


def _apply_cast(
    dataframe: pl.DataFrame,
    spec: object,
    schema_by_name: Mapping[str, SchemaColumn],
    original_by_index: Mapping[int, Mapping[str, Any]],
) -> tuple[pl.DataFrame, list[RejectRecord]]:
    if not isinstance(spec, dict) or not spec:
        raise _edit_error("edit.invalid", "cast spec must be a non-empty mapping.", {"op": "cast"})

    expressions: list[pl.Expr] = []
    bad_rows_by_column: dict[str, set[int]] = {}
    cast_type_by_column: dict[str, str] = {}
    reject_records: list[RejectRecord] = []

    for column, cast_spec in spec.items():
        if not isinstance(column, str):
            raise _edit_error("edit.invalid", "cast column names must be strings.", {"op": "cast"})
        _require_column(dataframe, column, "cast")
        normalized = _normalize_cast_spec(column, cast_spec, schema_by_name)
        cast_type_by_column[column] = normalized["type"]
        casted = dataframe.select(_cast_expression(column, normalized).alias(column))[column]
        bad_mask = dataframe[column].is_not_null() & casted.is_null()
        bad_indices = set(dataframe.filter(bad_mask)[INTERNAL_ROW_INDEX].to_list())
        bad_rows_by_column[column] = {int(index) for index in bad_indices}
        expressions.append(casted.alias(column))

    casted_dataframe = dataframe.with_columns(expressions)
    rejected_indices = set().union(*bad_rows_by_column.values()) if bad_rows_by_column else set()
    for column, row_indices in bad_rows_by_column.items():
        for row_index in sorted(row_indices):
            row = dataframe.filter(pl.col(INTERNAL_ROW_INDEX) == row_index).row(0, named=True)
            reject_records.append(
                RejectRecord(
                    original_row=original_by_index[int(row_index)],
                    stage="edit",
                    row_index=int(row_index),
                    column=column,
                    check="cast_failed",
                    value=row.get(column),
                    reason=f"Could not cast value to {cast_type_by_column[column]}.",
                )
            )

    if rejected_indices:
        casted_dataframe = casted_dataframe.filter(~pl.col(INTERNAL_ROW_INDEX).is_in(list(rejected_indices)))
    return casted_dataframe, reject_records


def _apply_fill_null(dataframe: pl.DataFrame, spec: object) -> pl.DataFrame:
    if not isinstance(spec, dict):
        raise _edit_error("edit.invalid", "fill_null spec must be a mapping.", {"op": "fill_null"})
    expressions: list[pl.Expr] = []
    for column, value in spec.items():
        if not isinstance(column, str):
            raise _edit_error("edit.invalid", "fill_null column names must be strings.", {"op": "fill_null"})
        _require_column(dataframe, column, "fill_null")
        expressions.append(pl.col(column).fill_null(value).alias(column))
    return dataframe.with_columns(expressions)


def _cast_expression(column: str, spec: dict[str, Any]) -> pl.Expr:
    cast_type = spec["type"]
    if cast_type == "string":
        return pl.col(column).cast(pl.String, strict=False)
    if cast_type == "int":
        return pl.col(column).cast(pl.Int64, strict=False)
    if cast_type == "float":
        return pl.col(column).cast(pl.Float64, strict=False)
    if cast_type == "decimal":
        return pl.col(column).cast(pl.Decimal(precision=spec["precision"], scale=spec["scale"]), strict=False)
    if cast_type == "date":
        if spec.get("format"):
            return pl.col(column).str.strptime(pl.Date, format=spec["format"], strict=False)
        return pl.col(column).cast(pl.Date, strict=False)
    if cast_type == "datetime":
        if spec.get("format"):
            return pl.col(column).str.strptime(pl.Datetime, format=spec["format"], strict=False)
        return pl.col(column).cast(pl.Datetime, strict=False)
    if cast_type == "boolean":
        return pl.col(column).map_elements(_parse_boolean, return_dtype=pl.Boolean)
    raise _edit_error("edit.invalid", f"Unsupported cast type `{cast_type}`.", {"type": cast_type})


def _normalize_cast_spec(
    column: str,
    spec: object,
    schema_by_name: Mapping[str, SchemaColumn],
) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise _edit_error("edit.invalid", f"Cast spec for `{column}` must be a mapping.", {"column": column})
    unknown = sorted(set(spec) - {"type", "format", "precision", "scale"})
    if unknown:
        raise _edit_error("edit.unknown_key", f"Unknown cast key `{unknown[0]}`.", {"column": column, "key": unknown[0]})
    cast_type = spec.get("type")
    if not isinstance(cast_type, str) or cast_type not in CAST_TYPES:
        raise _edit_error("edit.invalid", f"Cast type for `{column}` is invalid.", {"column": column})

    normalized = dict(spec)
    normalized["type"] = cast_type
    schema_column = schema_by_name.get(column)
    if schema_column is not None:
        compatible = CAST_COMPATIBILITY[cast_type]
        schema_kind = schema_column.mysql_type.kind
        if schema_kind not in compatible:
            raise _edit_error(
                "schema.type_mismatch",
                f"Cast `{cast_type}` for `{column}` is not compatible with schema type {schema_column.type}.",
                {"column": column, "cast_type": cast_type, "schema_type": schema_column.type},
            )
        if cast_type == "decimal":
            normalized.setdefault("precision", schema_column.mysql_type.precision)
            normalized.setdefault("scale", schema_column.mysql_type.scale)

    if cast_type == "decimal":
        normalized["precision"] = _positive_int(normalized.get("precision"), "precision", column)
        normalized["scale"] = _scale_int(normalized.get("scale"), normalized["precision"], column)
    if cast_type in {"date", "datetime"} and "format" in normalized and not isinstance(normalized["format"], str):
        raise _edit_error("edit.invalid", f"Cast format for `{column}` must be a string.", {"column": column})
    return normalized


def _parse_boolean(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    return None


def _single_edit(edit: dict[str, Any]) -> tuple[str, Any]:
    if not isinstance(edit, dict) or len(edit) != 1:
        raise _edit_error("edit.invalid", "Each edit must be a single-operation mapping.", {})
    op = next(iter(edit))
    return op, edit[op]


def _string_mapping(value: object, op: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise _edit_error("edit.invalid", f"{op} spec must be a non-empty mapping.", {"op": op})
    mapping: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str) or not key or not item:
            raise _edit_error("edit.invalid", f"{op} mappings must use non-empty string keys and values.", {"op": op})
        mapping[key] = item
    return mapping


def _string_list(value: object, op: str) -> list[str]:
    items = _list(value, op)
    if not all(isinstance(item, str) and item for item in items):
        raise _edit_error("edit.invalid", f"{op} must be a list of non-empty strings.", {"op": op})
    return list(items)


def _list(value: object, op: str) -> list[Any]:
    if not isinstance(value, list):
        raise _edit_error("edit.invalid", f"{op} must be a list.", {"op": op})
    return list(value)


def _require_column(dataframe: pl.DataFrame, column: str, op: str) -> None:
    if column == INTERNAL_ROW_INDEX or column not in dataframe.columns:
        raise _edit_error("edit.missing_column", f"Column `{column}` does not exist for {op}.", {"column": column, "op": op})


def _ensure_no_internal_conflict(dataframe: pl.DataFrame) -> None:
    if INTERNAL_ROW_INDEX in dataframe.columns:
        raise _edit_error(
            "edit.reserved_column",
            f"Column `{INTERNAL_ROW_INDEX}` is reserved for dbcli internals.",
            {"column": INTERNAL_ROW_INDEX},
        )


def _duplicates(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return duplicates


def _positive_int(value: object, key: str, column: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise _edit_error("edit.invalid", f"Decimal cast `{key}` for `{column}` must be positive.", {"column": column})
    return value


def _scale_int(value: object, precision: int, column: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > precision:
        raise _edit_error(
            "edit.invalid",
            f"Decimal cast scale for `{column}` must be between 0 and precision.",
            {"column": column},
        )
    return value


def _edit_error(code: str, message: str, details: dict[str, Any]) -> DbcliError:
    return DbcliError(
        Diagnostic(
            code=code,
            message=message,
            details=details,
        ),
        ExitCode.VALIDATION_ERROR,
    )
