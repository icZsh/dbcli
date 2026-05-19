from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dbcli.cli import app
from dbcli.errors import DbcliError
from dbcli.project import init_project
from dbcli.recipes import load_recipe, parse_recipe
from dbcli.schema import parse_mysql_type


runner = CliRunner()


def test_parse_mysql_type_supports_v1_subset() -> None:
    assert parse_mysql_type("bigint").to_dict()["kind"] == "BIGINT"
    assert parse_mysql_type("varchar(16)").length == 16
    decimal_type = parse_mysql_type("decimal(12,2)")
    assert decimal_type.kind == "DECIMAL"
    assert decimal_type.precision == 12
    assert decimal_type.scale == 2


def test_parse_mysql_type_rejects_unsupported_type() -> None:
    with pytest.raises(DbcliError) as exc_info:
        parse_mysql_type("JSON")

    assert int(exc_info.value.exit_code) == 10
    assert exc_info.value.diagnostic.code == "schema.unsupported_type"


def test_invalid_yaml_has_structured_diagnostic() -> None:
    with pytest.raises(DbcliError) as exc_info:
        parse_recipe("name: [")

    assert int(exc_info.value.exit_code) == 10
    assert exc_info.value.diagnostic.code == "recipe.invalid_yaml"


def test_recipe_unknown_key_and_duplicate_schema_column_fail() -> None:
    with pytest.raises(DbcliError) as unknown_info:
        parse_recipe(_recipe_text(extra="unexpected: true\n"))

    assert unknown_info.value.diagnostic.code == "recipe.unknown_key"

    with pytest.raises(DbcliError) as duplicate_info:
        parse_recipe(
            _recipe_text(
                schema="""schema:
  - {name: seller_id, type: BIGINT, nullable: false}
  - {name: seller_id, type: VARCHAR(16), nullable: true}
"""
            )
        )

    assert duplicate_info.value.diagnostic.code == "schema.duplicate_column"


def test_scan_writes_recipe_that_round_trips_through_parser() -> None:
    with runner.isolated_filesystem():
        assert runner.invoke(app, ["init"]).exit_code == 0
        Path("sellers.csv").write_text("seller_id,tier\n00123,A\n04567,VIP\n", encoding="utf-8")

        result = runner.invoke(
            app,
            [
                "scan",
                "sellers.csv",
                "--table",
                "dim_sellers",
                "--profile",
                "dev",
                "--json",
                "--ci",
            ],
        )

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        recipe_path = Path(payload["recipe_path"])
        assert recipe_path.exists()
        assert recipe_path == Path.cwd() / ".dbcli" / "recipes" / "dim_sellers.yaml"
        assert payload["recipe_data"]["source"]["encoding"] == "utf-8"
        assert payload["recipe_data"]["source"]["delimiter"] == ","
        assert payload["recipe_data"]["edits"] == []

        recipe = load_recipe("dim_sellers")
        assert recipe.name == "dim_sellers"
        assert recipe.target["table"] == "dim_sellers"
        assert [column.name for column in recipe.schema] == ["seller_id", "tier"]


def test_scan_fails_when_csv_delimiter_detection_is_ambiguous() -> None:
    with runner.isolated_filesystem():
        assert runner.invoke(app, ["init"]).exit_code == 0
        Path("single_column.csv").write_text("seller_id\n00123\n04567\n", encoding="utf-8")

        result = runner.invoke(app, ["scan", "single_column.csv", "--json", "--ci"])

        assert result.exit_code == 10
        payload = json.loads(result.stdout)
        assert payload["diagnostics"][0]["code"] == "source.csv_detection_failed"


def test_recipes_list_and_show_emit_json() -> None:
    with runner.isolated_filesystem():
        assert runner.invoke(app, ["init"]).exit_code == 0
        recipe_dir = Path(".dbcli/recipes")
        recipe_dir.joinpath("sellers.yaml").write_text(_recipe_text(), encoding="utf-8")

        list_result = runner.invoke(app, ["recipes", "list", "--json", "--ci"])
        assert list_result.exit_code == 0
        list_payload = json.loads(list_result.stdout)
        assert list_payload["recipes"] == [
            {
                "name": "sellers",
                "path": ".dbcli/recipes/sellers.yaml",
                "table": "dim_sellers",
                "profile": "dev",
                "mode": "append",
            }
        ]

        show_result = runner.invoke(app, ["recipes", "show", "sellers", "--json", "--ci"])
        assert show_result.exit_code == 0
        show_payload = json.loads(show_result.stdout)
        assert show_payload["recipe_data"]["name"] == "sellers"
        assert show_payload["recipe_data"]["schema"][0]["type"] == "BIGINT"


def _recipe_text(*, extra: str = "", schema: str | None = None) -> str:
    schema_text = schema or """schema:
  - {name: seller_id, type: BIGINT, nullable: false}
  - {name: tier, type: VARCHAR(16), nullable: true}
"""
    return f"""name: sellers
source:
  path: data/sellers.csv
  encoding: utf-8
  delimiter: ","
target:
  profile: dev
  table: dim_sellers
  mode: append
{schema_text}edits: []
options:
  reject_threshold: 0.0
  batch_size: 5000
{extra}"""
