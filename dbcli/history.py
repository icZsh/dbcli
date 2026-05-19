"""Run history JSONL handling."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from dbcli.errors import DbcliError, Diagnostic, ExitCode
from dbcli.project import ProjectPaths, find_project


def append_run_record(record: dict[str, Any], *, paths: ProjectPaths | None = None) -> None:
    paths = paths or find_project()
    paths.dbcli_dir.mkdir(parents=True, exist_ok=True)
    paths.runs.parent.mkdir(parents=True, exist_ok=True)
    with paths.runs.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, separators=(",", ":"), default=str))
        handle.write("\n")


def read_run_records(
    *,
    table: str | None = None,
    limit: int | None = None,
    paths: ProjectPaths | None = None,
) -> list[dict[str, Any]]:
    paths = paths or find_project()
    if not paths.runs.exists():
        return []
    records: list[dict[str, Any]] = []
    with paths.runs.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise DbcliError(
                    Diagnostic(
                        code="history.invalid_jsonl",
                        message=f"Could not parse run history line {line_number}.",
                        path=str(paths.runs),
                        details={"line": line_number},
                    ),
                    ExitCode.VALIDATION_ERROR,
                ) from exc
            if table is None or record.get("table") == table:
                records.append(record)
    records = list(reversed(records))
    if limit is not None:
        if limit < 0:
            raise DbcliError(
                Diagnostic(
                    code="history.invalid_limit",
                    message="History limit must be greater than or equal to 0.",
                    path=None,
                    details={"limit": limit},
                ),
                ExitCode.USAGE_OR_DRIFT,
            )
        records = records[:limit]
    return records


def get_run_record(run_id: str, *, paths: ProjectPaths | None = None) -> dict[str, Any]:
    records = read_run_records(paths=paths)
    for record in records:
        if record.get("run_id") == run_id:
            return record
    resolved_paths = paths or find_project()
    raise DbcliError(
        Diagnostic(
            code="history.not_found",
            message=f"Run `{run_id}` was not found.",
            path=str(resolved_paths.runs),
            details={"run_id": run_id},
        ),
        ExitCode.USAGE_OR_DRIFT,
    )


def current_git_sha(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    sha = result.stdout.strip()
    return sha or None


def format_history(records: list[dict[str, Any]]) -> str:
    if not records:
        return ""
    return "\n".join(
        "\t".join(
            [
                str(record.get("run_id", "")),
                str(record.get("recipe", "")),
                str(record.get("table", "")),
                str(record.get("mode", "")),
                str(record.get("exit_code", "")),
            ]
        )
        for record in records
    )


def format_run_record(record: dict[str, Any]) -> str:
    return json.dumps(record, indent=2, sort_keys=True)
