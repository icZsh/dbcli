from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import polars as pl
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

from dbcli.cli import app
from dbcli.core.errors import DbcliError
from dbcli.project.history import read_run_records
from dbcli.db.load import run_load
from dbcli.db.mysql import schema_drift_error
from dbcli.project.profiles import add_profile
from dbcli.project import ProjectPaths, init_project


runner = CliRunner()


def test_run_load_append_success_writes_history(tmp_path: Path, monkeypatch) -> None:
    paths = _project_with_recipe(tmp_path, monkeypatch)
    adapter = _FakeAppendAdapter()

    result = run_load("sellers", paths=paths, adapter=adapter)

    assert result.exit_code == 0
    assert result.rows["loaded"] == 2
    assert adapter.calls == [("dim_sellers", ["seller_id", "tier"], 2, 2)]
    assert adapter.rows == [{"seller_id": 1, "tier": "A"}, {"seller_id": 2, "tier": "B"}]

    records = read_run_records(paths=paths)
    assert len(records) == 1
    assert records[0]["run_id"] == result.run_id
    assert records[0]["exit_code"] == 0
    assert records[0]["rows"]["loaded"] == 2


def test_run_load_validation_failure_writes_history(tmp_path: Path, monkeypatch) -> None:
    paths = _project_with_recipe(
        tmp_path,
        monkeypatch,
        schema="""
schema:
  - {name: missing_col, type: BIGINT, nullable: false}
""",
    )
    adapter = _FakeAppendAdapter()

    result = run_load("sellers", paths=paths, adapter=adapter)

    assert result.exit_code == 10
    assert adapter.calls == []
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        "schema.extra_source_column",
        "schema.extra_source_column",
        "schema.missing_source_column",
    ]
    assert read_run_records(paths=paths)[0]["exit_code"] == 10


def test_run_load_append_drift_exits_2_and_records_diff(tmp_path: Path, monkeypatch) -> None:
    paths = _project_with_recipe(tmp_path, monkeypatch)
    adapter = _FakeAppendAdapter(drift=[{"check": "type", "column": "seller_id", "expected": "BIGINT", "actual": "INT"}])

    result = run_load("sellers", paths=paths, adapter=adapter)

    assert result.exit_code == 2
    assert result.diagnostics[0].code == "mysql.schema_drift"
    assert result.diagnostics[0].details["diffs"][0]["check"] == "type"
    assert read_run_records(paths=paths)[0]["exit_code"] == 2


def test_run_load_append_sql_error_is_profile_error_and_does_not_commit_rows(tmp_path: Path, monkeypatch) -> None:
    paths = _project_with_recipe(tmp_path, monkeypatch)
    adapter = _FakeAppendAdapter(error=OperationalError("INSERT", {}, Exception("boom")))

    result = run_load("sellers", paths=paths, adapter=adapter)

    assert result.exit_code == 30
    assert result.rows["loaded"] is None
    assert result.diagnostics[0].code == "mysql.load_failed"
    assert adapter.rows == []
    record = read_run_records(paths=paths)[0]
    assert record["exit_code"] == 30
    assert record["rows"]["loaded"] is None


def test_run_load_replace_uses_replace_adapter_and_writes_loaded_rows(tmp_path: Path, monkeypatch) -> None:
    paths = _project_with_recipe(tmp_path, monkeypatch, mode="replace")
    adapter = _FakeReplaceAdapter()

    result = run_load("sellers", paths=paths, adapter=adapter)

    assert result.exit_code == 0
    assert result.rows["loaded"] == 2
    assert adapter.calls == [("dim_sellers", ["seller_id", "tier"], 2, result.run_id)]
    record = read_run_records(paths=paths)[0]
    assert record["mode"] == "replace"
    assert record["rows"]["loaded"] == 2


def test_load_cli_emits_json_and_writes_history(monkeypatch) -> None:
    adapter = _FakeAppendAdapter()

    def fake_run_load(reference: str) -> Any:
        assert reference == "sellers"
        paths, _ = init_project(Path.cwd())
        result = run_load("sellers", paths=paths, adapter=adapter)
        return result

    monkeypatch.setattr("dbcli.cli.run_load", fake_run_load)
    with runner.isolated_filesystem():
        _write_project_files(Path.cwd(), schema=_default_schema())
        add_profile(
            "dev",
            host="localhost",
            port=3306,
            user="dbcli",
            database="ecom_dev",
            password_env="DBCLI_DEV_PW",
            paths=init_project(Path.cwd())[0],
        )
        result = runner.invoke(app, ["load", "sellers", "--json", "--ci"], env={"DBCLI_DEV_PW": "pw"})

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["command"] == "load"
    assert payload["rows"]["loaded"] == 2


def _project_with_recipe(
    root: Path,
    monkeypatch: Any,
    *,
    schema: str | None = None,
    mode: str = "append",
) -> ProjectPaths:
    paths, _ = init_project(root)
    _write_project_files(root, schema=schema or _default_schema(), mode=mode)
    add_profile(
        "dev",
        host="localhost",
        port=3306,
        user="dbcli",
        database="ecom_dev",
        password_env="DBCLI_DEV_PW",
        paths=paths,
    )
    monkeypatch.setenv("DBCLI_DEV_PW", "pw")
    return paths


def _write_project_files(root: Path, *, schema: str, mode: str = "append") -> None:
    paths, _ = init_project(root)
    data_dir = root / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "sellers.csv").write_text("seller_id,tier\n001,A\n002,B\n", encoding="utf-8")
    (paths.recipes_dir / "sellers.yaml").write_text(
        f"""name: sellers
source:
  path: data/sellers.csv
  encoding: utf-8
  delimiter: ","
target:
  profile: dev
  table: dim_sellers
  mode: {mode}
{schema}edits:
  - cast: {{seller_id: {{type: int}}}}
options:
  reject_threshold: 1.0
  batch_size: 2
""",
        encoding="utf-8",
    )


def _default_schema() -> str:
    return """schema:
  - {name: seller_id, type: BIGINT, nullable: false}
  - {name: tier, type: VARCHAR(4), nullable: false}
"""


class _FakeAppendAdapter:
    def __init__(self, *, drift: list[dict[str, Any]] | None = None, error: OperationalError | None = None) -> None:
        self.drift = drift
        self.error = error
        self.calls: list[tuple[str, list[str], int, int]] = []
        self.rows: list[dict[str, Any]] = []

    def append_rows(self, table: str, schema: list[Any], dataframe: pl.DataFrame, settings: Any) -> int:
        self.calls.append((table, [column.name for column in schema], dataframe.height, settings.batch_size))
        if self.drift:
            raise schema_drift_error(table, self.drift)
        if self.error:
            raise self.error
        self.rows.extend(dataframe.to_dicts())
        return dataframe.height


class _FakeReplaceAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], int, str]] = []

    def replace_rows(
        self,
        table: str,
        schema: list[Any],
        dataframe: pl.DataFrame,
        settings: Any,
        *,
        run_id: str,
    ) -> int:
        self.calls.append((table, [column.name for column in schema], dataframe.height, run_id))
        assert settings.batch_size == 2
        return dataframe.height
