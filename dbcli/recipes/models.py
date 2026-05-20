"""Recipe parsing, validation, and starter recipe generation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import polars as pl
import yaml

from dbcli.core.errors import DbcliError, Diagnostic, ExitCode
from dbcli.project import ProjectPaths, find_project, load_project_config, resolve_settings
from dbcli.recipes.schema import SchemaColumn, parse_schema_columns, schema_to_dict
from dbcli.pipeline.source import (
    SourceConfig,
    detect_csv_delimiter,
    detect_csv_encoding,
    detect_source_type,
    load_source,
)


TOP_LEVEL_KEYS = frozenset({"name", "source", "target", "schema", "edits", "options"})
SOURCE_KEYS = frozenset({"path", "sheet", "header_row", "skip_rows", "encoding", "delimiter"})
TARGET_KEYS = frozenset({"profile", "table", "mode", "charset", "collation", "engine"})
OPTIONS_KEYS = frozenset({"reject_threshold", "batch_size"})
EDIT_OPS = frozenset({"rename", "drop", "trim", "parse_null", "cast", "fill_null"})
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_]+")
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_IDENTIFIER_LENGTH = 64


@dataclass(frozen=True)
class Recipe:
    name: str
    source: dict[str, Any]
    target: dict[str, Any]
    schema: list[SchemaColumn]
    edits: list[dict[str, Any]]
    options: dict[str, Any]
    path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "target": self.target,
            "schema": schema_to_dict(self.schema),
            "edits": self.edits,
            "options": self.options,
        }


@dataclass(frozen=True)
class RecipeSummary:
    name: str
    path: str
    table: str | None
    profile: str | None
    mode: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "table": self.table,
            "profile": self.profile,
            "mode": self.mode,
        }


def load_recipe(reference: str | Path, *, paths: ProjectPaths | None = None) -> Recipe:
    paths = paths or find_project()
    recipe_path = resolve_recipe_path(reference, paths=paths)
    try:
        content = recipe_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _recipe_error("recipe.read_failed", f"Could not read recipe: {exc}", str(recipe_path), {}) from exc
    return parse_recipe(content, path=recipe_path)


def parse_recipe(content: str, *, path: Path | None = None) -> Recipe:
    try:
        raw = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise _recipe_error(
            "recipe.invalid_yaml",
            f"Could not parse recipe YAML: {exc}",
            str(path) if path else None,
            {},
        ) from exc

    if not isinstance(raw, dict):
        raise _recipe_error("recipe.invalid", "Recipe must be a YAML mapping.", str(path) if path else None, {})

    _reject_unknown(raw, TOP_LEVEL_KEYS, "recipe", path)
    for key in ("name", "source", "target", "schema"):
        if key not in raw:
            raise _recipe_error(
                "recipe.invalid",
                f"Recipe is missing required key `{key}`.",
                _path(path, key),
                {"key": key},
            )

    name = _required_string(raw["name"], "name", path)
    source = _parse_source(raw["source"], path)
    target = _parse_target(raw["target"], path)
    schema = parse_schema_columns(raw["schema"], path="schema")
    edits = _parse_edits(raw.get("edits", []), path)
    options = _parse_options(raw.get("options", {}), path)

    return Recipe(
        name=name,
        source=source,
        target=target,
        schema=schema,
        edits=edits,
        options=options,
        path=path,
    )


def resolve_recipe_path(reference: str | Path, *, paths: ProjectPaths | None = None) -> Path:
    paths = paths or find_project()
    candidate = Path(reference)
    if candidate.exists():
        return candidate.resolve()
    if candidate.suffix in {".yaml", ".yml"} and (paths.root / candidate).exists():
        return (paths.root / candidate).resolve()

    for suffix in (".yaml", ".yml"):
        recipe_path = paths.recipes_dir / f"{candidate.stem if candidate.suffix else str(reference)}{suffix}"
        if recipe_path.exists():
            return recipe_path.resolve()

    raise _recipe_error(
        "recipe.not_found",
        f"Recipe `{reference}` was not found.",
        str(paths.recipes_dir),
        {"recipe": str(reference)},
        exit_code=ExitCode.USAGE_OR_DRIFT,
    )


def list_recipe_files(*, paths: ProjectPaths | None = None) -> list[Path]:
    paths = paths or find_project()
    if not paths.recipes_dir.exists():
        return []
    return sorted([*paths.recipes_dir.glob("*.yaml"), *paths.recipes_dir.glob("*.yml")])


def list_recipe_summaries(*, paths: ProjectPaths | None = None) -> list[RecipeSummary]:
    paths = paths or find_project()
    summaries: list[RecipeSummary] = []
    for recipe_path in list_recipe_files(paths=paths):
        recipe = load_recipe(recipe_path, paths=paths)
        summaries.append(
            RecipeSummary(
                name=recipe.name,
                path=str(recipe_path.relative_to(paths.root)),
                table=recipe.target.get("table"),
                profile=recipe.target.get("profile"),
                mode=recipe.target.get("mode"),
            )
        )
    return summaries


def write_starter_recipe(
    source_file: str | Path,
    *,
    sheet: str | None = None,
    table: str | None = None,
    profile: str | None = None,
    encoding: str | None = None,
    delimiter: str | None = None,
    paths: ProjectPaths | None = None,
) -> tuple[Path, Recipe]:
    paths = paths or find_project()
    source_path = Path(source_file).expanduser().resolve()
    file_type = detect_source_type(source_path)
    if file_type == "csv":
        encoding = encoding or detect_csv_encoding(source_path)
        delimiter = delimiter or detect_csv_delimiter(source_path, encoding, require_confident=True)
        load_config = SourceConfig(path=source_path, encoding=encoding, delimiter=delimiter)
    else:
        load_config = SourceConfig(path=source_path, sheet=sheet)

    loaded = load_source(load_config)
    recipe_name = _recipe_name(table or source_path.stem)
    target_table = table or recipe_name
    config = load_project_config(paths)
    settings = resolve_settings(config, cli_values={"profile": profile})
    source_block: dict[str, Any] = {
        "path": _portable_source_path(source_path, paths),
        "header_row": 1,
        "skip_rows": 0,
    }
    if loaded.file_type == "xlsx" and loaded.sheet:
        source_block["sheet"] = loaded.sheet
    if loaded.file_type == "csv":
        source_block["encoding"] = loaded.encoding
        source_block["delimiter"] = loaded.delimiter

    schema_names = _safe_schema_names(loaded.dataframe.columns)
    rename_edit = {
        source: target
        for source, target in zip(loaded.dataframe.columns, schema_names, strict=True)
        if source != target
    }

    raw_recipe = {
        "name": recipe_name,
        "source": source_block,
        "target": {
            "profile": settings.profile,
            "table": target_table,
            "mode": "append",
            "charset": settings.charset,
            "collation": settings.collation,
            "engine": settings.engine,
        },
        "schema": _infer_schema(loaded.dataframe, column_names=schema_names),
        "edits": [{"rename": rename_edit}] if rename_edit else [],
        "options": {
            "reject_threshold": settings.reject_threshold,
            "batch_size": settings.batch_size,
        },
    }
    recipe = parse_recipe(dump_recipe_dict(raw_recipe))
    recipe_path = paths.recipes_dir / f"{recipe_name}.yaml"
    if recipe_path.exists():
        raise _recipe_error(
            "recipe.exists",
            f"Recipe `{recipe_name}` already exists.",
            str(recipe_path),
            {"recipe": recipe_name},
            exit_code=ExitCode.USAGE_OR_DRIFT,
        )

    paths.recipes_dir.mkdir(parents=True, exist_ok=True)
    recipe_path.write_text(dump_recipe_dict(recipe.to_dict()), encoding="utf-8")
    return recipe_path, load_recipe(recipe_path, paths=paths)


def dump_recipe_dict(recipe: Mapping[str, Any]) -> str:
    return yaml.safe_dump(dict(recipe), sort_keys=False, allow_unicode=False)


def format_recipe_summary(summaries: list[RecipeSummary]) -> str:
    if not summaries:
        return ""
    return "\n".join(
        f"{summary.name}\t{summary.table or ''}\t{summary.profile or ''}\t{summary.mode or ''}\t{summary.path}"
        for summary in summaries
    )


def _parse_source(value: object, path: Path | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _recipe_error("recipe.invalid", "Recipe source must be a mapping.", _path(path, "source"), {})
    _reject_unknown(value, SOURCE_KEYS, "source", path)
    source_path = _required_string(value.get("path"), "source.path", path)
    parsed: dict[str, Any] = {"path": source_path}
    for key in ("sheet", "encoding", "delimiter"):
        if key in value and value[key] is not None:
            parsed[key] = _required_string(value[key], f"source.{key}", path)
    parsed["header_row"] = _non_negative_int(value.get("header_row", 1), "source.header_row", path, minimum=1)
    parsed["skip_rows"] = _non_negative_int(value.get("skip_rows", 0), "source.skip_rows", path, minimum=0)
    return parsed


def _parse_target(value: object, path: Path | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _recipe_error("recipe.invalid", "Recipe target must be a mapping.", _path(path, "target"), {})
    _reject_unknown(value, TARGET_KEYS, "target", path)
    parsed = {
        "table": _required_string(value.get("table"), "target.table", path),
        "mode": _required_string(value.get("mode"), "target.mode", path),
    }
    if parsed["mode"] not in {"append", "replace"}:
        raise _recipe_error(
            "recipe.invalid",
            "target.mode must be one of append or replace.",
            _path(path, "target.mode"),
            {"mode": parsed["mode"]},
        )
    if "profile" in value and value["profile"] is not None:
        parsed["profile"] = _required_string(value["profile"], "target.profile", path)
    for key in ("charset", "collation", "engine"):
        if key in value and value[key] is not None:
            parsed[key] = _required_string(value[key], f"target.{key}", path)
    return parsed


def _parse_edits(value: object, path: Path | None) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _recipe_error("recipe.invalid", "Recipe edits must be a list.", _path(path, "edits"), {})
    parsed: list[dict[str, Any]] = []
    for index, edit in enumerate(value):
        if not isinstance(edit, dict) or len(edit) != 1:
            raise _recipe_error(
                "recipe.invalid",
                "Each edit must be a single-operation mapping.",
                _path(path, f"edits[{index}]"),
                {},
            )
        op = next(iter(edit))
        if op not in EDIT_OPS:
            raise _recipe_error(
                "recipe.unknown_edit_op",
                f"Unknown edit operation `{op}`.",
                _path(path, f"edits[{index}].{op}"),
                {"op": op},
            )
        parsed.append(dict(edit))
    return parsed


def _parse_options(value: object, path: Path | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _recipe_error("recipe.invalid", "Recipe options must be a mapping.", _path(path, "options"), {})
    _reject_unknown(value, OPTIONS_KEYS, "options", path)
    parsed: dict[str, Any] = {}
    if "reject_threshold" in value:
        threshold = value["reject_threshold"]
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not 0 <= float(threshold) <= 1:
            raise _recipe_error(
                "recipe.invalid",
                "options.reject_threshold must be a number from 0 to 1.",
                _path(path, "options.reject_threshold"),
                {},
            )
        parsed["reject_threshold"] = float(threshold)
    if "batch_size" in value:
        parsed["batch_size"] = _non_negative_int(value["batch_size"], "options.batch_size", path, minimum=1)
    return parsed


def _infer_schema(dataframe: pl.DataFrame, *, column_names: list[str] | None = None) -> list[dict[str, Any]]:
    schema: list[dict[str, Any]] = []
    names = column_names or dataframe.columns
    for source_column, schema_column, dtype in zip(dataframe.columns, names, dataframe.dtypes, strict=True):
        schema.append(
            {
                "name": schema_column,
                "type": _infer_mysql_type(dataframe[source_column], dtype),
                "nullable": True,
            }
        )
    return schema


def _infer_mysql_type(series: pl.Series, dtype: pl.DataType) -> str:
    if dtype == pl.Boolean:
        return "BOOLEAN"
    if dtype.is_integer():
        return "BIGINT"
    if dtype.is_float():
        return "DOUBLE"
    if dtype == pl.Date:
        return "DATE"
    if dtype == pl.Datetime:
        return "DATETIME"
    if dtype == pl.String:
        max_len = series.drop_nulls().str.len_chars().max()
        if max_len is None:
            return "VARCHAR(255)"
        if max_len <= 255:
            return f"VARCHAR({max(1, int(max_len))})"
    return "TEXT"


def _reject_unknown(value: Mapping[str, Any], allowed: frozenset[str], section: str, path: Path | None) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        key = unknown[0]
        raise _recipe_error(
            "recipe.unknown_key",
            f"Unknown key in {section}: {key}.",
            _path(path, f"{section}.{key}"),
            {"section": section, "key": key},
        )


def _required_string(value: object, key: str, path: Path | None) -> str:
    if not isinstance(value, str) or not value:
        raise _recipe_error(
            "recipe.invalid",
            f"{key} must be a non-empty string.",
            _path(path, key),
            {"key": key},
        )
    return value


def _non_negative_int(value: object, key: str, path: Path | None, *, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise _recipe_error(
            "recipe.invalid",
            f"{key} must be an integer greater than or equal to {minimum}.",
            _path(path, key),
            {"key": key},
        )
    return value


def _recipe_name(value: str) -> str:
    name = SAFE_NAME_RE.sub("_", value.strip().lower()).strip("_")
    return name or "recipe"


def _safe_schema_names(columns: list[str]) -> list[str]:
    used: set[str] = set()
    names: list[str] = []
    for column in columns:
        base = column if _is_safe_identifier(column) else _safe_identifier(column)
        name = _unique_identifier(base, used)
        names.append(name)
    return names


def _safe_identifier(value: str) -> str:
    name = SAFE_NAME_RE.sub("_", value.strip().lower()).strip("_")
    if not name:
        name = "column"
    if not re.match(r"^[A-Za-z_]", name):
        name = f"col_{name}"
    return _truncate_identifier(name)


def _unique_identifier(base: str, used: set[str]) -> str:
    base = _truncate_identifier(base) or "column"
    candidate = base
    suffix = 2
    while candidate.lower() in used:
        suffix_text = f"_{suffix}"
        candidate = f"{_truncate_identifier(base, MAX_IDENTIFIER_LENGTH - len(suffix_text))}{suffix_text}"
        suffix += 1
    used.add(candidate.lower())
    return candidate


def _truncate_identifier(value: str, max_length: int = MAX_IDENTIFIER_LENGTH) -> str:
    return value[:max_length].rstrip("_")


def _is_safe_identifier(value: str) -> bool:
    return len(value) <= MAX_IDENTIFIER_LENGTH and bool(IDENTIFIER_RE.fullmatch(value))


def _portable_source_path(source_path: Path, paths: ProjectPaths) -> str:
    try:
        return str(source_path.relative_to(paths.root))
    except ValueError:
        return str(source_path)


def _path(path: Path | None, key: str) -> str:
    return f"{path}:{key}" if path else key


def _recipe_error(
    code: str,
    message: str,
    path: str | None,
    details: dict[str, Any],
    *,
    exit_code: ExitCode = ExitCode.VALIDATION_ERROR,
) -> DbcliError:
    return DbcliError(
        Diagnostic(
            code=code,
            message=message,
            path=path,
            details=details,
        ),
        exit_code,
    )
