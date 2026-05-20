from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dbcli.cli import app
from dbcli.core.errors import DbcliError
from dbcli.project.history import append_run_record, current_git_sha, get_run_record, read_run_records
from dbcli.project import init_project


runner = CliRunner()


def test_run_records_append_and_read_newest_first(tmp_path: Path) -> None:
    paths, _ = init_project(tmp_path)
    append_run_record(_record("r_1", table="dim_sellers", exit_code=0), paths=paths)
    append_run_record(_record("r_2", table="dim_orders", exit_code=20), paths=paths)

    assert [record["run_id"] for record in read_run_records(paths=paths)] == ["r_2", "r_1"]
    assert [record["run_id"] for record in read_run_records(table="dim_sellers", paths=paths)] == ["r_1"]
    assert [record["run_id"] for record in read_run_records(limit=1, paths=paths)] == ["r_2"]
    assert get_run_record("r_1", paths=paths)["table"] == "dim_sellers"


def test_current_git_sha_returns_none_outside_git_repo(tmp_path: Path) -> None:
    assert current_git_sha(tmp_path) is None


def test_history_and_show_cli_emit_json() -> None:
    with runner.isolated_filesystem():
        assert runner.invoke(app, ["init"]).exit_code == 0
        paths, _ = init_project(Path.cwd())
        append_run_record(_record("r_1", table="dim_sellers", exit_code=0), paths=paths)
        append_run_record(_record("r_2", table="dim_orders", exit_code=20), paths=paths)

        history_result = runner.invoke(app, ["history", "--table", "dim_orders", "--json", "--ci"])
        assert history_result.exit_code == 0
        history_payload = json.loads(history_result.stdout)
        assert history_payload["records"] == [_record("r_2", table="dim_orders", exit_code=20)]

        show_result = runner.invoke(app, ["show", "r_1", "--json", "--ci"])
        assert show_result.exit_code == 0
        show_payload = json.loads(show_result.stdout)
        assert show_payload["run_id"] == "r_1"
        assert show_payload["record"]["table"] == "dim_sellers"


def test_show_missing_run_is_structured_error() -> None:
    with runner.isolated_filesystem():
        assert runner.invoke(app, ["init"]).exit_code == 0

        result = runner.invoke(app, ["show", "missing", "--json", "--ci"])

        assert result.exit_code == 2
        payload = json.loads(result.stdout)
        assert payload["diagnostics"][0]["code"] == "history.not_found"


def test_invalid_history_jsonl_is_diagnostic(tmp_path: Path) -> None:
    paths, _ = init_project(tmp_path)
    paths.runs.write_text("{bad json}\n", encoding="utf-8")

    with pytest.raises(DbcliError) as exc_info:
        read_run_records(paths=paths)

    assert int(exc_info.value.exit_code) == 10
    assert exc_info.value.diagnostic.code == "history.invalid_jsonl"


def _record(run_id: str, *, table: str, exit_code: int) -> dict[str, object]:
    return {
        "run_id": run_id,
        "recipe": "sellers",
        "profile": "dev",
        "table": table,
        "mode": "append",
        "started_at": "2026-05-19T08:00:00Z",
        "finished_at": "2026-05-19T08:00:01Z",
        "duration_ms": 1000,
        "rows": {
            "read": 1,
            "edited": 1,
            "validated": 1,
            "loaded": 1 if exit_code == 0 else None,
            "rejected_edit": 0,
            "rejected_validate": 0,
        },
        "rejects": None,
        "diagnostics": [],
        "exit_code": exit_code,
        "git_sha": None,
    }
