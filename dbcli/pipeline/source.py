"""CSV/XLSX source inspection and loading."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import polars as pl
from python_calamine import CalamineError, WorksheetNotFound, load_workbook

from dbcli.core.errors import DbcliError, Diagnostic, ExitCode


SUPPORTED_CSV_SUFFIXES = {".csv"}
SUPPORTED_XLSX_SUFFIXES = {".xlsx"}
CSV_DETECTION_DELIMITERS = [",", "\t", ";", "|"]
PREVIEW_ROWS = 5


@dataclass(frozen=True)
class SourceConfig:
    path: Path | str
    sheet: str | None = None
    encoding: str | None = None
    delimiter: str | None = None
    header_row: int = 1
    skip_rows: int = 0


@dataclass(frozen=True)
class LoadedSource:
    dataframe: pl.DataFrame
    path: Path
    file_type: Literal["csv", "xlsx"]
    rows_read: int
    columns: list[str]
    encoding: str | None = None
    delimiter: str | None = None
    sheet: str | None = None
    sheets: list[str] | None = None


@dataclass(frozen=True)
class SourceInspection:
    path: str
    file_type: Literal["csv", "xlsx"]
    encoding: str | None
    delimiter: str | None
    sheets: list[str]
    selected_sheet: str | None
    columns: list[str]
    row_count: int
    column_count: int
    dtypes: dict[str, str]
    preview: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "file_type": self.file_type,
            "encoding": self.encoding,
            "delimiter": self.delimiter,
            "sheets": self.sheets,
            "selected_sheet": self.selected_sheet,
            "columns": self.columns,
            "row_count": self.row_count,
            "column_count": self.column_count,
            "dtypes": self.dtypes,
            "preview": self.preview,
        }


def load_source(config: SourceConfig) -> LoadedSource:
    path = _resolve_path(config.path)
    file_type = detect_source_type(path)
    _validate_row_options(config)

    if file_type == "csv":
        return _load_csv(config, path)
    return _load_xlsx(config, path)


def inspect_source(
    path: Path | str,
    *,
    sheet: str | None = None,
    encoding: str | None = None,
    delimiter: str | None = None,
) -> SourceInspection:
    resolved_path = _resolve_path(path)
    file_type = detect_source_type(resolved_path)

    if file_type == "csv":
        resolved_encoding = encoding or detect_csv_encoding(resolved_path)
        resolved_delimiter = delimiter or detect_csv_delimiter(resolved_path, resolved_encoding)
        loaded = load_source(
            SourceConfig(
                path=resolved_path,
                encoding=resolved_encoding,
                delimiter=resolved_delimiter,
            )
        )
    else:
        loaded = load_source(SourceConfig(path=resolved_path, sheet=sheet))

    return SourceInspection(
        path=str(loaded.path),
        file_type=loaded.file_type,
        encoding=loaded.encoding,
        delimiter=loaded.delimiter,
        sheets=loaded.sheets or [],
        selected_sheet=loaded.sheet,
        columns=loaded.columns,
        row_count=loaded.rows_read,
        column_count=len(loaded.columns),
        dtypes={name: str(dtype) for name, dtype in zip(loaded.dataframe.columns, loaded.dataframe.dtypes, strict=True)},
        preview=_preview(loaded.dataframe),
    )


def list_xlsx_sheets(path: Path | str) -> list[str]:
    resolved_path = _resolve_path(path)
    file_type = detect_source_type(resolved_path)
    if file_type != "xlsx":
        raise DbcliError(
            Diagnostic(
                code="source.unsupported_type",
                message="Only XLSX source files have sheets.",
                path=str(resolved_path),
                details={"suffix": resolved_path.suffix.lower()},
            ),
            ExitCode.VALIDATION_ERROR,
        )

    try:
        workbook = load_workbook(resolved_path)
    except CalamineError as exc:
        raise _source_error("source.read_failed", f"Could not read XLSX source: {exc}", resolved_path) from exc

    sheets = list(workbook.sheet_names)
    if not sheets:
        raise _source_error("source.empty", "Workbook does not contain any sheets.", resolved_path)
    return sheets


def detect_source_type(path: Path) -> Literal["csv", "xlsx"]:
    suffix = path.suffix.lower()
    if suffix in SUPPORTED_CSV_SUFFIXES:
        return "csv"
    if suffix in SUPPORTED_XLSX_SUFFIXES:
        return "xlsx"
    raise DbcliError(
        Diagnostic(
            code="source.unsupported_type",
            message="Only CSV and XLSX source files are supported in v1.",
            path=str(path),
            details={"suffix": suffix},
        ),
        ExitCode.VALIDATION_ERROR,
    )


def detect_csv_encoding(path: Path) -> str:
    raw = _read_bytes(path)
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return "latin-1"
    return "utf-8"


def detect_csv_delimiter(path: Path, encoding: str, *, require_confident: bool = False) -> str:
    sample = _read_text(path, encoding)[:8192]
    if not sample:
        raise _source_error("source.empty", "Source file is empty.", path)

    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=CSV_DETECTION_DELIMITERS)
    except csv.Error:
        if require_confident:
            raise _source_error(
                "source.csv_detection_failed",
                "Could not confidently detect CSV delimiter.",
                path,
            )
        return _fallback_delimiter(sample)
    return dialect.delimiter


def format_inspection(inspection: SourceInspection) -> str:
    lines = [
        f"path: {inspection.path}",
        f"type: {inspection.file_type}",
        f"rows: {inspection.row_count}",
        f"columns: {inspection.column_count}",
    ]
    if inspection.encoding:
        lines.append(f"encoding: {inspection.encoding}")
    if inspection.delimiter:
        lines.append(f"delimiter: {inspection.delimiter}")
    if inspection.sheets:
        lines.append(f"sheets: {', '.join(inspection.sheets)}")
    if inspection.selected_sheet:
        lines.append(f"selected_sheet: {inspection.selected_sheet}")
    if inspection.columns:
        lines.append(f"headers: {', '.join(inspection.columns)}")
    if inspection.preview:
        lines.append("preview:")
        headers = inspection.columns
        lines.append("  " + " | ".join(headers))
        for row in inspection.preview:
            lines.append("  " + " | ".join("" if row.get(column) is None else str(row.get(column)) for column in headers))
    return "\n".join(lines)


def _load_csv(config: SourceConfig, path: Path) -> LoadedSource:
    if not config.encoding or not config.delimiter:
        raise DbcliError(
            Diagnostic(
                code="source.csv_settings_required",
                message="CSV recipes must declare both source.encoding and source.delimiter.",
                path=str(path),
                details={
                    "missing": [
                        key
                        for key, value in {"encoding": config.encoding, "delimiter": config.delimiter}.items()
                        if not value
                    ]
                },
            ),
            ExitCode.VALIDATION_ERROR,
        )
    if len(config.delimiter) != 1:
        raise DbcliError(
            Diagnostic(
                code="source.invalid_delimiter",
                message="CSV delimiter must be exactly one character.",
                path=str(path),
                details={},
            ),
            ExitCode.VALIDATION_ERROR,
        )

    rows = _read_csv_rows(path, config.encoding, config.delimiter)
    header = _extract_header(rows, config, path)

    try:
        dataframe = pl.read_csv(
            path,
            has_header=True,
            skip_rows=config.skip_rows + config.header_row - 1,
            separator=config.delimiter,
            encoding=config.encoding,
            infer_schema=False,
            missing_utf8_is_empty_string=True,
            try_parse_dates=False,
        )
    except Exception as exc:
        raise _source_error("source.read_failed", f"Could not read CSV source: {exc}", path) from exc

    return LoadedSource(
        dataframe=dataframe,
        path=path,
        file_type="csv",
        rows_read=dataframe.height,
        columns=header,
        encoding=config.encoding,
        delimiter=config.delimiter,
    )


def _load_xlsx(config: SourceConfig, path: Path) -> LoadedSource:
    try:
        workbook = load_workbook(path)
    except CalamineError as exc:
        raise _source_error("source.read_failed", f"Could not read XLSX source: {exc}", path) from exc

    sheets = list(workbook.sheet_names)
    if not sheets:
        raise _source_error("source.empty", "Workbook does not contain any sheets.", path)

    if config.sheet is None:
        if len(sheets) > 1:
            raise DbcliError(
                Diagnostic(
                    code="source.sheet_required",
                    message="XLSX files with multiple sheets require --sheet.",
                    path=str(path),
                    details={"sheets": sheets},
                ),
                ExitCode.VALIDATION_ERROR,
            )
        sheet_name = sheets[0]
    else:
        sheet_name = config.sheet

    try:
        sheet = workbook.get_sheet_by_name(sheet_name)
    except WorksheetNotFound as exc:
        raise DbcliError(
            Diagnostic(
                code="source.sheet_not_found",
                message=f"Sheet `{sheet_name}` was not found.",
                path=str(path),
                details={"sheet": sheet_name, "sheets": sheets},
            ),
            ExitCode.VALIDATION_ERROR,
        ) from exc

    rows = sheet.to_python()
    header = _extract_header(rows, config, path)
    data_rows = _normalize_data_rows(rows[config.skip_rows + config.header_row :], len(header))
    dataframe = _dataframe_from_rows(data_rows, header)

    return LoadedSource(
        dataframe=dataframe,
        path=path,
        file_type="xlsx",
        rows_read=dataframe.height,
        columns=header,
        sheet=sheet_name,
        sheets=sheets,
    )


def _dataframe_from_rows(rows: list[list[Any]], columns: list[str]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema={column: pl.String for column in columns})
    return pl.DataFrame(rows, schema=columns, orient="row", strict=False)


def _extract_header(rows: list[list[Any]], config: SourceConfig, path: Path) -> list[str]:
    if not rows:
        raise _source_error("source.empty", "Source file is empty.", path)

    header_index = config.skip_rows + config.header_row - 1
    if header_index >= len(rows):
        raise _source_error("source.empty", "Source file does not contain a readable header.", path)

    header = [_header_cell(value) for value in rows[header_index]]
    if not header:
        raise _source_error("source.empty_header", "Source header row is empty.", path)

    empty_headers = [index + 1 for index, value in enumerate(header) if not value.strip()]
    if empty_headers:
        raise DbcliError(
            Diagnostic(
                code="source.empty_header",
                message="Source header contains an empty column name.",
                path=str(path),
                details={"columns": empty_headers},
            ),
            ExitCode.VALIDATION_ERROR,
        )

    seen: set[str] = set()
    duplicates: list[str] = []
    for column in header:
        if column in seen and column not in duplicates:
            duplicates.append(column)
        seen.add(column)
    if duplicates:
        raise DbcliError(
            Diagnostic(
                code="source.duplicate_header",
                message="Source header contains duplicate column names.",
                path=str(path),
                details={"columns": duplicates},
            ),
            ExitCode.VALIDATION_ERROR,
        )
    return header


def _read_csv_rows(path: Path, encoding: str, delimiter: str) -> list[list[str]]:
    try:
        with path.open(newline="", encoding=encoding) as handle:
            return [row for row in csv.reader(handle, delimiter=delimiter)]
    except UnicodeError as exc:
        raise _source_error("source.encoding_failed", f"Could not decode CSV source with {encoding}.", path) from exc
    except OSError as exc:
        raise _source_error("source.read_failed", f"Could not read source file: {exc}", path) from exc


def _normalize_data_rows(rows: list[list[Any]], width: int) -> list[list[Any]]:
    normalized: list[list[Any]] = []
    for row in rows:
        padded = list(row[:width])
        if len(padded) < width:
            padded.extend([None] * (width - len(padded)))
        normalized.append(padded)
    return normalized


def _preview(dataframe: pl.DataFrame) -> list[dict[str, Any]]:
    return [_json_safe(row) for row in dataframe.head(PREVIEW_ROWS).to_dicts()]


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _header_cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _validate_row_options(config: SourceConfig) -> None:
    if config.header_row < 1:
        raise DbcliError(
            Diagnostic(
                code="source.invalid_header_row",
                message="source.header_row must be 1 or greater.",
                details={"header_row": config.header_row},
            ),
            ExitCode.VALIDATION_ERROR,
        )
    if config.skip_rows < 0:
        raise DbcliError(
            Diagnostic(
                code="source.invalid_skip_rows",
                message="source.skip_rows must be 0 or greater.",
                details={"skip_rows": config.skip_rows},
            ),
            ExitCode.VALIDATION_ERROR,
        )


def _resolve_path(path: Path | str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise DbcliError(
            Diagnostic(
                code="source.not_found",
                message="Source file does not exist.",
                path=str(resolved),
                details={"path": str(resolved)},
            ),
            ExitCode.VALIDATION_ERROR,
        )
    if not resolved.is_file():
        raise DbcliError(
            Diagnostic(
                code="source.not_file",
                message="Source path is not a file.",
                path=str(resolved),
                details={"path": str(resolved)},
            ),
            ExitCode.VALIDATION_ERROR,
        )
    return resolved


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise _source_error("source.read_failed", f"Could not read source file: {exc}", path) from exc


def _read_text(path: Path, encoding: str) -> str:
    try:
        return path.read_text(encoding=encoding)
    except UnicodeError as exc:
        raise _source_error("source.encoding_failed", f"Could not decode CSV source with {encoding}.", path) from exc
    except OSError as exc:
        raise _source_error("source.read_failed", f"Could not read source file: {exc}", path) from exc


def _fallback_delimiter(sample: str) -> str:
    lines = [line for line in sample.splitlines() if line.strip()]
    if not lines:
        return ","

    counts = {
        delimiter: sum(line.count(delimiter) for line in lines[:5])
        for delimiter in CSV_DETECTION_DELIMITERS
    }
    delimiter, count = max(counts.items(), key=lambda item: item[1])
    return delimiter if count > 0 else ","


def _source_error(code: str, message: str, path: Path) -> DbcliError:
    return DbcliError(
        Diagnostic(
            code=code,
            message=message,
            path=str(path),
            details={},
        ),
        ExitCode.VALIDATION_ERROR,
    )
