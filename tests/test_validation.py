from __future__ import annotations

import csv
import json
from pathlib import Path

from typer.testing import CliRunner

from dbcli.cli import app
from dbcli.project import init_project
from dbcli.validation import run_validate


runner = CliRunner()


def test_validate_writes_edit_and_schema_rejects_under_threshold() -> None:
    with runner.isolated_filesystem():
        _write_project_with_recipe(reject_threshold=1.0)

        result = runner.invoke(app, ["validate", "sellers", "--json", "--ci"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["status"] == "success"
        assert payload["command"] == "validate"
        assert payload["rows"] == {
            "read": 3,
            "edited": 2,
            "validated": 1,
            "loaded": None,
            "rejected_edit": 1,
            "rejected_validate": 1,
        }
        rejects_path = Path(payload["rejects"])
        assert rejects_path.exists()
        with rejects_path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert [(row["__stage"], row["__row_index"], row["__column"], row["__check"]) for row in rows] == [
            ("edit", "2", "seller_id", "cast_failed"),
            ("validate", "3", "tier", "varchar_length"),
        ]
        assert rows[0]["seller_id"] == "bad"
        assert rows[1]["tier"] == "TOOLONG"
        assert not Path(".dbcli/runs.jsonl").read_text(encoding="utf-8")


def test_validate_exits_20_when_reject_threshold_exceeded() -> None:
    with runner.isolated_filesystem():
        _write_project_with_recipe(reject_threshold=0.1)

        result = runner.invoke(app, ["validate", "sellers", "--json", "--ci"])

        assert result.exit_code == 20
        payload = json.loads(result.stdout)
        assert payload["status"] == "failed"
        assert payload["exit_code"] == 20
        assert payload["diagnostics"] == []
        assert payload["rows"]["rejected_edit"] == 1
        assert payload["rows"]["rejected_validate"] == 1


def test_validate_records_not_null_and_numeric_range_rejects() -> None:
    with runner.isolated_filesystem():
        _write_project_with_recipe(
            reject_threshold=1.0,
            data="seller_id,tier,first_active_date\n200,,01/02/2024\n",
            edits="""edits:
  - parse_null: {columns: [tier], values: [""]}
  - cast:
      seller_id: {type: int}
      first_active_date: {type: date, format: "%m/%d/%Y"}
""",
            schema="""
schema:
  - {name: seller_id, type: TINYINT, nullable: false}
  - {name: tier, type: VARCHAR(3), nullable: false}
  - {name: first_active_date, type: DATE, nullable: true}
""",
        )

        result = runner.invoke(app, ["validate", "sellers", "--json", "--ci"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        with Path(payload["rejects"]).open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert [(row["__column"], row["__check"]) for row in rows] == [
            ("seller_id", "numeric_range"),
            ("tier", "not_null"),
        ]


def test_validate_schema_extra_and_missing_columns_are_diagnostics() -> None:
    with runner.isolated_filesystem():
        _write_project_with_recipe(
            reject_threshold=1.0,
            data="seller_id,tier,first_active_date\n001,OK,01/02/2024\n",
            schema="""
schema:
  - {name: seller_id, type: BIGINT, nullable: false}
  - {name: missing_col, type: VARCHAR(16), nullable: true}
""",
        )

        result = runner.invoke(app, ["validate", "sellers", "--json", "--ci"])

        assert result.exit_code == 10
        payload = json.loads(result.stdout)
        assert [diagnostic["code"] for diagnostic in payload["diagnostics"]] == [
            "schema.extra_source_column",
            "schema.extra_source_column",
            "schema.missing_source_column",
        ]
        assert payload["rejects"] is None


def test_validate_type_mismatch_is_schema_diagnostic() -> None:
    with runner.isolated_filesystem():
        _write_project_with_recipe(
            reject_threshold=1.0,
            edits="edits: []\n",
            schema="""
schema:
  - {name: seller_id, type: BIGINT, nullable: false}
  - {name: tier, type: VARCHAR(3), nullable: false}
  - {name: first_active_date, type: DATE, nullable: true}
""",
        )

        result = runner.invoke(app, ["validate", "sellers", "--json", "--ci"])

        assert result.exit_code == 10
        payload = json.loads(result.stdout)
        assert payload["diagnostics"][0]["code"] == "schema.type_mismatch"
        assert payload["diagnostics"][0]["details"]["column"] == "seller_id"


def test_run_validate_returns_schema_ordered_dataframe(tmp_path: Path) -> None:
    paths, _ = init_project(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "sellers.csv").write_text("seller_id,tier\n001,A\n", encoding="utf-8")
    (paths.recipes_dir / "sellers.yaml").write_text(
        """name: sellers
source:
  path: data/sellers.csv
  encoding: utf-8
  delimiter: ","
target:
  profile: dev
  table: dim_sellers
  mode: append
schema:
  - {name: tier, type: VARCHAR(3), nullable: false}
  - {name: seller_id, type: BIGINT, nullable: false}
edits:
  - cast: {seller_id: {type: int}}
options:
  reject_threshold: 1.0
  batch_size: 5000
""",
        encoding="utf-8",
    )

    result = run_validate("sellers", paths=paths)

    assert result.exit_code == 0
    assert result.dataframe is not None
    assert result.dataframe.columns == ["tier", "seller_id"]


def _write_project_with_recipe(
    *,
    reject_threshold: float,
    data: str | None = None,
    edits: str | None = None,
    schema: str | None = None,
) -> None:
    assert runner.invoke(app, ["init"]).exit_code == 0
    Path("data").mkdir()
    Path("data/sellers.csv").write_text(
        data
        or "seller_id,tier,first_active_date\n001,OK,01/02/2024\nbad,VIP,01/03/2024\n003,TOOLONG,01/04/2024\n",
        encoding="utf-8",
    )
    edits_text = edits or """edits:
  - cast:
      seller_id: {type: int}
      first_active_date: {type: date, format: "%m/%d/%Y"}
"""
    schema_text = schema or """
schema:
  - {name: seller_id, type: BIGINT, nullable: false}
  - {name: tier, type: VARCHAR(3), nullable: false}
  - {name: first_active_date, type: DATE, nullable: true}
"""
    Path(".dbcli/recipes/sellers.yaml").write_text(
        f"""name: sellers
source:
  path: data/sellers.csv
  encoding: utf-8
  delimiter: ","
target:
  profile: dev
  table: dim_sellers
  mode: append
{schema_text}{edits_text}options:
  reject_threshold: {reject_threshold}
  batch_size: 5000
""",
        encoding="utf-8",
    )
