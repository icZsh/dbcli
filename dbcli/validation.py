"""End-to-end recipe validation without database access."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import polars as pl

from dbcli.edits import EditResult, apply_edit_pipeline
from dbcli.errors import Diagnostic, ExitCode
from dbcli.project import ProjectPaths, find_project, load_project_config, resolve_settings
from dbcli.recipes import Recipe, load_recipe
from dbcli.rejects import RejectRecord, write_rejects_csv
from dbcli.schema import SchemaColumn
from dbcli.source import SourceConfig, load_source


INTEGER_RANGES = {
    "TINYINT": (-128, 127),
    "SMALLINT": (-32768, 32767),
    "INT": (-2147483648, 2147483647),
    "BIGINT": (-9223372036854775808, 9223372036854775807),
}


@dataclass(frozen=True)
class ValidationResult:
    status: str
    command: str
    run_id: str
    recipe: str
    profile: str | None
    table: str | None
    mode: str | None
    rows: dict[str, int | None]
    rejects: str | None
    diagnostics: list[Diagnostic]
    duration_ms: int
    exit_code: ExitCode
    dataframe: pl.DataFrame | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "command": self.command,
            "run_id": self.run_id,
            "recipe": self.recipe,
            "profile": self.profile,
            "table": self.table,
            "mode": self.mode,
            "rows": self.rows,
            "rejects": self.rejects,
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
            "duration_ms": self.duration_ms,
            "exit_code": int(self.exit_code),
        }


def run_validate(reference: str | Path, *, paths: ProjectPaths | None = None) -> ValidationResult:
    paths = paths or find_project()
    started = datetime.now(UTC)
    run_id = mint_run_id(started)
    recipe = load_recipe(reference, paths=paths)
    config = load_project_config(paths)
    settings = resolve_settings(config, recipe_target=recipe.target, recipe_options=recipe.options)
    loaded = load_source(_source_config(recipe, paths))
    edit_result = apply_edit_pipeline(loaded.dataframe, recipe.edits, schema=recipe.schema)

    diagnostics = _schema_diagnostics(edit_result.dataframe, recipe.schema)
    validate_rejects: list[RejectRecord] = []
    final_dataframe: pl.DataFrame | None = None
    if not diagnostics:
        validate_rejects = _validate_rows(edit_result, recipe.schema)
        rejected_validate_indices = {reject.row_index for reject in validate_rejects}
        keep_mask = [row_index not in rejected_validate_indices for row_index in edit_result.row_indices]
        final_dataframe = edit_result.dataframe.filter(pl.Series(keep_mask)).select([column.name for column in recipe.schema])

    all_rejects = [*edit_result.rejects, *validate_rejects]
    rejects_path = None
    if all_rejects:
        written_path = write_rejects_csv(run_id, all_rejects, edit_result.original_columns, paths=paths)
        rejects_path = _relative(paths.root, written_path)

    unique_rejects = {reject.row_index for reject in all_rejects}
    ratio = len(unique_rejects) / loaded.rows_read if loaded.rows_read else 0.0
    exit_code = ExitCode.SUCCESS
    if diagnostics:
        exit_code = ExitCode.VALIDATION_ERROR
    elif ratio > settings.reject_threshold:
        exit_code = ExitCode.REJECT_THRESHOLD

    rows = {
        "read": loaded.rows_read,
        "edited": loaded.rows_read - len({reject.row_index for reject in edit_result.rejects}),
        "validated": None if diagnostics else (final_dataframe.height if final_dataframe is not None else 0),
        "loaded": None,
        "rejected_edit": len({reject.row_index for reject in edit_result.rejects}),
        "rejected_validate": len({reject.row_index for reject in validate_rejects}),
    }
    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    return ValidationResult(
        status="success" if exit_code == ExitCode.SUCCESS else "failed",
        command="validate",
        run_id=run_id,
        recipe=recipe.name,
        profile=settings.profile,
        table=recipe.target.get("table"),
        mode=recipe.target.get("mode"),
        rows=rows,
        rejects=rejects_path,
        diagnostics=diagnostics,
        duration_ms=duration_ms,
        exit_code=exit_code,
        dataframe=final_dataframe,
    )


def format_validation_report(result: ValidationResult) -> str:
    heading = f"{result.recipe}"
    if result.profile and result.table and result.mode:
        heading = f"{result.recipe} @ {result.profile}.{result.table} ({result.mode})"
    rows = result.rows
    lines = [
        heading,
        f"  read:       {rows['read']}",
        f"  edited:     {rows['edited']}   ({rows['rejected_edit']} edit rejects)",
        f"  validated:  {rows['validated'] if rows['validated'] is not None else 'n/a'}   ({rows['rejected_validate']} validate rejects)",
    ]
    if result.rejects:
        lines.append(f"  rejects:    {result.rejects}")
    if result.diagnostics:
        lines.append(f"  diagnostics:{len(result.diagnostics)}")
    lines.append(f"  duration:   {result.duration_ms}ms")
    return "\n".join(lines)


def mint_run_id(started: datetime | None = None) -> str:
    timestamp = (started or datetime.now(UTC)).strftime("%Y_%m_%dT%H_%M_%SZ")
    return f"r_{timestamp}_{uuid4().hex[:4]}"


def _source_config(recipe: Recipe, paths: ProjectPaths) -> SourceConfig:
    source = dict(recipe.source)
    source_path = Path(source["path"])
    if not source_path.is_absolute():
        source_path = paths.root / source_path
    return SourceConfig(
        path=source_path,
        sheet=source.get("sheet"),
        encoding=source.get("encoding"),
        delimiter=source.get("delimiter"),
        header_row=source.get("header_row", 1),
        skip_rows=source.get("skip_rows", 0),
    )


def _schema_diagnostics(dataframe: pl.DataFrame, schema: list[SchemaColumn]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    dataframe_columns = set(dataframe.columns)
    schema_columns = [column.name for column in schema]
    schema_column_set = set(schema_columns)
    for column in sorted(dataframe_columns - schema_column_set):
        diagnostics.append(
            Diagnostic(
                code="schema.extra_source_column",
                message=f"Final dataframe contains extra column `{column}`.",
                path="schema",
                details={"column": column},
            )
        )
    for column in schema:
        if column.name not in dataframe_columns:
            diagnostics.append(
                Diagnostic(
                    code="schema.missing_source_column",
                    message=f"Final dataframe is missing schema column `{column.name}`.",
                    path="schema",
                    details={"column": column.name},
                )
            )
    if diagnostics:
        return diagnostics

    dtype_by_name = dict(zip(dataframe.columns, dataframe.dtypes, strict=True))
    for column in schema:
        dtype = dtype_by_name[column.name]
        if not _dtype_compatible(dtype, column.mysql_type.kind):
            diagnostics.append(
                Diagnostic(
                    code="schema.type_mismatch",
                    message=f"Column `{column.name}` is declared {column.type} but data is {dtype}.",
                    path="schema",
                    details={"column": column.name, "schema_type": column.type, "dtype": str(dtype)},
                )
            )
    return diagnostics


def _validate_rows(edit_result: EditResult, schema: list[SchemaColumn]) -> list[RejectRecord]:
    rejects: list[RejectRecord] = []
    dataframe = edit_result.dataframe
    row_indices = edit_result.row_indices
    for column in schema:
        series = dataframe[column.name]
        if not column.nullable:
            for offset in _true_offsets(series.is_null()):
                rejects.append(
                    _validation_reject(edit_result, row_indices[offset], column.name, "not_null", series[offset], "Value is required.")
                )

        if column.mysql_type.kind == "VARCHAR" and column.mysql_type.length is not None:
            too_long = series.is_not_null() & (series.cast(pl.String).str.len_chars() > column.mysql_type.length)
            for offset in _true_offsets(too_long):
                rejects.append(
                    _validation_reject(
                        edit_result,
                        row_indices[offset],
                        column.name,
                        "varchar_length",
                        series[offset],
                        f"Value exceeds VARCHAR({column.mysql_type.length}).",
                    )
                )

        if column.mysql_type.kind in INTEGER_RANGES:
            min_value, max_value = INTEGER_RANGES[column.mysql_type.kind]
            out_of_range = series.is_not_null() & ((series < min_value) | (series > max_value))
            for offset in _true_offsets(out_of_range):
                rejects.append(
                    _validation_reject(
                        edit_result,
                        row_indices[offset],
                        column.name,
                        "numeric_range",
                        series[offset],
                        f"Value is outside {column.mysql_type.kind} range.",
                    )
                )
    return rejects


def _validation_reject(
    edit_result: EditResult,
    row_index: int,
    column: str,
    check: str,
    value: Any,
    reason: str,
) -> RejectRecord:
    return RejectRecord(
        original_row=edit_result.original_rows_by_index[row_index],
        stage="validate",
        row_index=row_index,
        column=column,
        check=check,
        value=value,
        reason=reason,
    )


def _true_offsets(mask: pl.Series) -> list[int]:
    return [index for index, value in enumerate(mask.to_list()) if value]


def _dtype_compatible(dtype: pl.DataType, kind: str) -> bool:
    if kind in {"VARCHAR", "TEXT"}:
        return dtype == pl.String
    if kind == "BOOLEAN":
        return dtype == pl.Boolean
    if kind in INTEGER_RANGES:
        return dtype.is_integer()
    if kind in {"FLOAT", "DOUBLE"}:
        return dtype.is_float()
    if kind == "DECIMAL":
        return isinstance(dtype, pl.Decimal)
    if kind == "DATE":
        return dtype == pl.Date
    if kind == "DATETIME":
        return isinstance(dtype, pl.Datetime)
    return False


def _relative(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
