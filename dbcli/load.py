"""Load orchestration for append and replace modes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import polars as pl
from sqlalchemy.exc import SQLAlchemyError

from dbcli.errors import DbcliError, Diagnostic, ExitCode
from dbcli.history import append_run_record, current_git_sha
from dbcli.mysql import SqlAlchemyMysqlAdapter
from dbcli.profiles import resolve_profile
from dbcli.project import ProjectPaths, find_project, load_project_config, resolve_settings
from dbcli.recipes import Recipe, load_recipe
from dbcli.schema import SchemaColumn
from dbcli.validation import ValidationResult, format_validation_report, mint_run_id, run_validate


class LoadAdapter(Protocol):
    def append_rows(
        self,
        table: str,
        schema: list[SchemaColumn],
        dataframe: pl.DataFrame,
        settings: Any,
    ) -> int:
        ...


@dataclass(frozen=True)
class LoadResult:
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
    started_at: str
    finished_at: str
    git_sha: str | None

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

    def to_run_record(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "recipe": self.recipe,
            "profile": self.profile,
            "table": self.table,
            "mode": self.mode,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "rows": self.rows,
            "rejects": self.rejects,
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
            "exit_code": int(self.exit_code),
            "git_sha": self.git_sha,
        }


def run_load(
    reference: str | Path,
    *,
    paths: ProjectPaths | None = None,
    adapter: LoadAdapter | None = None,
) -> LoadResult:
    paths = paths or find_project()
    started = datetime.now(UTC)
    run_id = mint_run_id(started)
    started_at = _isoformat(started)

    try:
        validation = run_validate(reference, paths=paths, run_id=run_id, command="load", started=started)
    except DbcliError as exc:
        result = _error_result(
            reference,
            paths=paths,
            started=started,
            run_id=run_id,
            started_at=started_at,
            diagnostic=exc.diagnostic,
            exit_code=exc.exit_code,
        )
        append_run_record(result.to_run_record(), paths=paths)
        return result

    if validation.exit_code != ExitCode.SUCCESS:
        result = _from_validation(validation, paths=paths, started=started, started_at=started_at)
        append_run_record(result.to_run_record(), paths=paths)
        return result

    try:
        recipe = load_recipe(reference, paths=paths)
        config = load_project_config(paths)
        settings = resolve_settings(config, recipe_target=recipe.target, recipe_options=recipe.options)
        resolved_profile = resolve_profile(settings.profile, paths=paths)
        owned_adapter = adapter is None
        adapter = adapter or SqlAlchemyMysqlAdapter(resolved_profile)
        loaded_rows = _dispatch_load(adapter, recipe, validation, settings)
        result = _success_result(
            validation,
            paths=paths,
            started=started,
            started_at=started_at,
            loaded_rows=loaded_rows,
        )
    except DbcliError as exc:
        result = _failure_result(
            validation,
            paths=paths,
            started=started,
            started_at=started_at,
            diagnostics=[exc.diagnostic],
            exit_code=exc.exit_code,
        )
    except SQLAlchemyError as exc:
        result = _failure_result(
            validation,
            paths=paths,
            started=started,
            started_at=started_at,
            diagnostics=[_load_failed_diagnostic(validation, exc)],
            exit_code=ExitCode.DB_OR_PROFILE_ERROR,
        )
    finally:
        if "owned_adapter" in locals() and owned_adapter and adapter is not None and hasattr(adapter, "close"):
            adapter.close()  # type: ignore[attr-defined]

    append_run_record(result.to_run_record(), paths=paths)
    return result


def format_load_report(result: LoadResult) -> str:
    lines = format_validation_report(_validation_view(result)).splitlines()
    rows = result.rows
    lines.append(f"  loaded:     {rows['loaded'] if rows['loaded'] is not None else 'n/a'}")
    return "\n".join(lines)


def _dispatch_load(adapter: LoadAdapter, recipe: Recipe, validation: ValidationResult, settings: Any) -> int:
    if validation.dataframe is None:
        return 0
    table = recipe.target["table"]
    mode = recipe.target["mode"]
    if mode == "append":
        return adapter.append_rows(table, recipe.schema, validation.dataframe, settings)
    raise DbcliError(
        Diagnostic(
            code="load.unsupported_mode",
            message=f"Load mode `{mode}` is not implemented yet.",
            path="target.mode",
            details={"mode": mode},
        ),
        ExitCode.USAGE_OR_DRIFT,
    )


def _success_result(
    validation: ValidationResult,
    *,
    paths: ProjectPaths,
    started: datetime,
    started_at: str,
    loaded_rows: int,
) -> LoadResult:
    rows = dict(validation.rows)
    rows["loaded"] = loaded_rows
    return _load_result(
        validation,
        paths=paths,
        started=started,
        started_at=started_at,
        rows=rows,
        diagnostics=[],
        exit_code=ExitCode.SUCCESS,
    )


def _from_validation(
    validation: ValidationResult,
    *,
    paths: ProjectPaths,
    started: datetime,
    started_at: str,
) -> LoadResult:
    return _load_result(
        validation,
        paths=paths,
        started=started,
        started_at=started_at,
        rows=dict(validation.rows),
        diagnostics=validation.diagnostics,
        exit_code=validation.exit_code,
    )


def _failure_result(
    validation: ValidationResult,
    *,
    paths: ProjectPaths,
    started: datetime,
    started_at: str,
    diagnostics: list[Diagnostic],
    exit_code: ExitCode,
) -> LoadResult:
    rows = dict(validation.rows)
    rows["loaded"] = None
    return _load_result(
        validation,
        paths=paths,
        started=started,
        started_at=started_at,
        rows=rows,
        diagnostics=diagnostics,
        exit_code=exit_code,
    )


def _load_result(
    validation: ValidationResult,
    *,
    paths: ProjectPaths,
    started: datetime,
    started_at: str,
    rows: dict[str, int | None],
    diagnostics: list[Diagnostic],
    exit_code: ExitCode,
) -> LoadResult:
    finished = datetime.now(UTC)
    return LoadResult(
        status="success" if exit_code == ExitCode.SUCCESS else "failed",
        command="load",
        run_id=validation.run_id,
        recipe=validation.recipe,
        profile=validation.profile,
        table=validation.table,
        mode=validation.mode,
        rows=rows,
        rejects=validation.rejects,
        diagnostics=diagnostics,
        duration_ms=int((finished - started).total_seconds() * 1000),
        exit_code=exit_code,
        started_at=started_at,
        finished_at=_isoformat(finished),
        git_sha=current_git_sha(paths.root),
    )


def _error_result(
    reference: str | Path,
    *,
    paths: ProjectPaths,
    started: datetime,
    run_id: str,
    started_at: str,
    diagnostic: Diagnostic,
    exit_code: ExitCode,
) -> LoadResult:
    finished = datetime.now(UTC)
    return LoadResult(
        status="failed",
        command="load",
        run_id=run_id,
        recipe=Path(reference).stem,
        profile=None,
        table=None,
        mode=None,
        rows={
            "read": None,
            "edited": None,
            "validated": None,
            "loaded": None,
            "rejected_edit": None,
            "rejected_validate": None,
        },
        rejects=None,
        diagnostics=[diagnostic],
        duration_ms=int((finished - started).total_seconds() * 1000),
        exit_code=exit_code,
        started_at=started_at,
        finished_at=_isoformat(finished),
        git_sha=current_git_sha(paths.root),
    )


def _load_failed_diagnostic(validation: ValidationResult, exc: BaseException) -> Diagnostic:
    return Diagnostic(
        code="mysql.load_failed",
        message=f"MySQL load failed for table `{validation.table}`.",
        path="target.table",
        details={
            "table": validation.table,
            "mode": validation.mode,
            "error": exc.__class__.__name__,
        },
    )


def _validation_view(result: LoadResult) -> ValidationResult:
    return ValidationResult(
        status=result.status,
        command=result.command,
        run_id=result.run_id,
        recipe=result.recipe,
        profile=result.profile,
        table=result.table,
        mode=result.mode,
        rows=result.rows,
        rejects=result.rejects,
        diagnostics=result.diagnostics,
        duration_ms=result.duration_ms,
        exit_code=result.exit_code,
        dataframe=None,
    )


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
