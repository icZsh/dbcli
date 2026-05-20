from __future__ import annotations

import json
import html
import os
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from typer.testing import CliRunner

from dbcli.cli import app
from dbcli.core.errors import DbcliError
from dbcli.project import init_project
from dbcli.recipes import load_recipe, parse_recipe
from dbcli.recipes.schema import parse_mysql_type


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


def test_scan_renames_unsafe_source_headers_to_mysql_safe_schema_names() -> None:
    with runner.isolated_filesystem():
        assert runner.invoke(app, ["init"]).exit_code == 0
        Path("companies.csv").write_text(
            "Company ID,Company Name,1st Value\nC001,Galaxy Tech,42\n",
            encoding="utf-8",
        )

        result = runner.invoke(
            app,
            [
                "scan",
                "companies.csv",
                "--table",
                "dim_companies",
                "--profile",
                "dev",
                "--json",
                "--ci",
            ],
        )

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["recipe_data"]["edits"] == [
            {
                "rename": {
                    "Company ID": "company_id",
                    "Company Name": "company_name",
                    "1st Value": "col_1st_value",
                }
            }
        ]
        assert [column["name"] for column in payload["recipe_data"]["schema"]] == [
            "company_id",
            "company_name",
            "col_1st_value",
        ]

        validate_result = runner.invoke(app, ["validate", "dim_companies", "--json", "--ci"])
        assert validate_result.exit_code == 0


def test_scan_dir_writes_recipes_for_supported_files_and_xlsx_sheets() -> None:
    with runner.isolated_filesystem():
        assert runner.invoke(app, ["init"]).exit_code == 0
        Path("data").mkdir()
        Path("data/companies.csv").write_text("Company ID,Name\nC001,Galaxy Tech\n", encoding="utf-8")
        _write_xlsx(Path("data/companies.xlsx"), {"Companies": [["Company ID", "Name"], ["C002", "Cedar Data"]]})
        _write_xlsx(
            Path("data/workbook.xlsx"),
            {
                "Companies": [["Company ID", "Name"], ["C003", "Northbridge"]],
                "Employees": [["Employee ID", "Company ID"], ["E1001", "C003"]],
            },
        )
        Path("data/notes.txt").write_text("ignored\n", encoding="utf-8")

        result = runner.invoke(
            app,
            [
                "scan-dir",
                "data",
                "--profile",
                "dev",
                "--encoding",
                "utf-8",
                "--delimiter",
                ",",
                "--json",
                "--ci",
            ],
        )

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["files_found"] == 3
        assert payload["recipes_created"] == 4
        assert payload["failed"] == 0
        assert [entry["recipe_name"] for entry in payload["entries"]] == [
            "companies_csv",
            "companies_xlsx",
            "workbook_companies",
            "workbook_employees",
        ]
        assert payload["entries"][2]["sheet"] == "Companies"
        assert payload["entries"][3]["sheet"] == "Employees"

        list_result = runner.invoke(app, ["recipes", "list", "--json", "--ci"])
        assert list_result.exit_code == 0
        list_payload = json.loads(list_result.stdout)
        assert [recipe["name"] for recipe in list_payload["recipes"]] == [
            "companies_csv",
            "companies_xlsx",
            "workbook_companies",
            "workbook_employees",
        ]

        validate_result = runner.invoke(app, ["validate", "workbook_employees", "--json", "--ci"])
        assert validate_result.exit_code == 0


def test_scan_directory_argument_scans_current_directory() -> None:
    with runner.isolated_filesystem():
        assert runner.invoke(app, ["init"]).exit_code == 0
        Path("sellers.csv").write_text("Seller ID,Tier\n001,VIP\n", encoding="utf-8")
        Path("orders.csv").write_text("Order ID,Amount\nA001,42\n", encoding="utf-8")

        result = runner.invoke(
            app,
            [
                "scan",
                ".",
                "--profile",
                "dev",
                "--encoding",
                "utf-8",
                "--delimiter",
                ",",
                "--json",
                "--ci",
            ],
        )

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["command"] == "scan"
        assert payload["directory"] == "."
        assert payload["files_found"] == 2
        assert payload["recipes_created"] == 2
        assert [entry["recipe_name"] for entry in payload["entries"]] == ["orders", "sellers"]

        validate_result = runner.invoke(app, ["validate", "sellers", "--json", "--ci"])
        assert validate_result.exit_code == 0


def test_scan_directory_argument_resolves_from_current_subdirectory() -> None:
    with runner.isolated_filesystem():
        assert runner.invoke(app, ["init"]).exit_code == 0
        Path("data").mkdir()
        Path("data/sellers.csv").write_text("Seller ID,Tier\n001,VIP\n", encoding="utf-8")
        original_cwd = Path.cwd()
        try:
            os.chdir("data")
            result = runner.invoke(
                app,
                [
                    "scan",
                    ".",
                    "--profile",
                    "dev",
                    "--encoding",
                    "utf-8",
                    "--delimiter",
                    ",",
                    "--json",
                    "--ci",
                ],
            )
        finally:
            os.chdir(original_cwd)

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["directory"] == "data"
        assert payload["entries"][0]["source_path"] == "data/sellers.csv"
        assert payload["entries"][0]["recipe_name"] == "sellers"


def test_scan_directory_argument_rejects_single_file_options() -> None:
    with runner.isolated_filesystem():
        assert runner.invoke(app, ["init"]).exit_code == 0

        result = runner.invoke(app, ["scan", ".", "--table", "one_table", "--json", "--ci"])

        assert result.exit_code == 2
        payload = json.loads(result.stdout)
        assert payload["diagnostics"][0]["code"] == "scan.directory_option_conflict"


def test_scan_dir_rejects_directories_outside_project_scope() -> None:
    with runner.isolated_filesystem():
        assert runner.invoke(app, ["init"]).exit_code == 0
        Path("../outside").mkdir(exist_ok=True)

        result = runner.invoke(app, ["scan-dir", "../outside", "--json", "--ci"])

        assert result.exit_code == 10
        payload = json.loads(result.stdout)
        assert payload["diagnostics"][0]["code"] == "scan.directory_out_of_scope"


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


def _write_xlsx(path: Path, sheets: dict[str, list[list[object]]]) -> None:
    with ZipFile(path, "w", ZIP_DEFLATED) as workbook:
        workbook.writestr("[Content_Types].xml", _content_types(len(sheets)))
        workbook.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        )
        workbook.writestr("xl/workbook.xml", _workbook_xml(list(sheets)))
        workbook.writestr("xl/_rels/workbook.xml.rels", _workbook_rels(len(sheets)))
        for index, rows in enumerate(sheets.values(), start=1):
            workbook.writestr(f"xl/worksheets/sheet{index}.xml", _sheet_xml(rows))


def _content_types(sheet_count: int) -> str:
    sheet_overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        f"{sheet_overrides}</Types>"
    )


def _workbook_xml(sheet_names: list[str]) -> str:
    sheets = "".join(
        f'<sheet name="{html.escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, name in enumerate(sheet_names, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{sheets}</sheets></workbook>"
    )


def _workbook_rels(sheet_count: int) -> str:
    rels = "".join(
        f'<Relationship Id="rId{index}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{rels}</Relationships>"
    )


def _sheet_xml(rows: list[list[object]]) -> str:
    sheet_rows: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        cells: list[str] = []
        for column_index, value in enumerate(row, start=1):
            if value is None:
                continue
            ref = f"{_column_name(column_index)}{row_index}"
            if isinstance(value, int | float):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{html.escape(str(value))}</t></is></c>')
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    )


def _column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name
