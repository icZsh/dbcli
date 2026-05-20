from __future__ import annotations

import json
import shlex
import sys
from dataclasses import dataclass
from typing import Any, TextIO


@dataclass(frozen=True)
class OutputConfig:
    json_output: bool = False
    no_progress: bool = False
    ci: bool = False


def detect_ci(stdin: TextIO | None = None) -> bool:
    stream = stdin if stdin is not None else sys.stdin
    return not stream.isatty()


def build_output_config(
    *,
    json_output: bool = False,
    no_progress: bool = False,
    ci: bool = False,
    stdin: TextIO | None = None,
) -> OutputConfig:
    resolved_ci = ci or detect_ci(stdin)
    return OutputConfig(
        json_output=json_output,
        no_progress=no_progress or resolved_ci,
        ci=resolved_ci,
    )


def merge_output_config(
    base: OutputConfig | None,
    *,
    json_output: bool = False,
    no_progress: bool = False,
    ci: bool = False,
    stdin: TextIO | None = None,
) -> OutputConfig:
    if base is None:
        base = build_output_config(stdin=stdin)
    return build_output_config(
        json_output=base.json_output or json_output,
        no_progress=base.no_progress or no_progress,
        ci=base.ci or ci,
        stdin=stdin,
    )


def result_envelope(
    *,
    status: str,
    command: str,
    exit_code: int,
    run_id: str | None = None,
    recipe: str | None = None,
    profile: str | None = None,
    table: str | None = None,
    mode: str | None = None,
    rows: dict[str, Any] | None = None,
    rejects: str | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "command": command,
        "run_id": run_id,
        "recipe": recipe,
        "profile": profile,
        "table": table,
        "mode": mode,
        "rows": rows,
        "rejects": rejects,
        "diagnostics": diagnostics or [],
        "duration_ms": duration_ms,
        "exit_code": exit_code,
    }


def emit_result(
    payload: dict[str, Any],
    config: OutputConfig,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> None:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    if config.json_output:
        out.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        out.write("\n")
        return

    diagnostics = payload.get("diagnostics") or []
    if diagnostics:
        for diagnostic in diagnostics:
            err.write(f"{diagnostic['code']}: {diagnostic['message']}\n")
        return

    status = payload.get("status", "success")
    command = payload.get("command", "unknown")
    err.write(f"{command}: {status}\n")


def log(
    level: str,
    message: str,
    config: OutputConfig,
    *,
    stderr: TextIO | None = None,
    **fields: Any,
) -> None:
    err = stderr if stderr is not None else sys.stderr
    if config.ci:
        parts = [f"level={_quote(level)}", f"message={_quote(message)}"]
        parts.extend(f"{key}={_quote(value)}" for key, value in fields.items())
        err.write(" ".join(parts) + "\n")
        return

    suffix = ""
    if fields:
        suffix = " " + " ".join(f"{key}={value}" for key, value in fields.items())
    err.write(f"{level}: {message}{suffix}\n")


def _quote(value: Any) -> str:
    text = str(value)
    if not text:
        return "''"
    if any(char.isspace() for char in text):
        return shlex.quote(text)
    return text
