from __future__ import annotations

import html
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import polars as pl
import pytest
from typer.testing import CliRunner

from dbcli.cli import app
from dbcli.core.errors import DbcliError
from dbcli.pipeline.source import SourceConfig, inspect_source, load_source


runner = CliRunner()


def test_csv_load_preserves_lossless_string_values(tmp_path: Path) -> None:
    source = tmp_path / "sellers.csv"
    source.write_text(
        'seller_id,sku,date_text,empty_value,quoted\n00123,000ABC,2024-01-02,,"hello, csv"\n',
        encoding="utf-8",
    )

    loaded = load_source(SourceConfig(path=source, encoding="utf-8", delimiter=","))

    assert loaded.dataframe.dtypes == [pl.String, pl.String, pl.String, pl.String, pl.String]
    assert loaded.rows_read == 1
    assert loaded.dataframe.to_dicts() == [
        {
            "seller_id": "00123",
            "sku": "000ABC",
            "date_text": "2024-01-02",
            "empty_value": "",
            "quoted": "hello, csv",
        }
    ]


def test_csv_load_requires_explicit_encoding_and_delimiter(tmp_path: Path) -> None:
    source = tmp_path / "sellers.csv"
    source.write_text("seller_id\n00123\n", encoding="utf-8")

    with pytest.raises(DbcliError) as exc_info:
        load_source(SourceConfig(path=source))

    assert int(exc_info.value.exit_code) == 10
    assert exc_info.value.diagnostic.code == "source.csv_settings_required"


def test_inspect_csv_detects_metadata_and_preview(tmp_path: Path) -> None:
    source = tmp_path / "sellers.csv"
    source.write_text("seller_id,sku\n00123,000ABC\n04567,2024-01-02\n", encoding="utf-8")

    inspection = inspect_source(source)

    assert inspection.file_type == "csv"
    assert inspection.encoding == "utf-8"
    assert inspection.delimiter == ","
    assert inspection.columns == ["seller_id", "sku"]
    assert inspection.row_count == 2
    assert inspection.preview == [
        {"seller_id": "00123", "sku": "000ABC"},
        {"seller_id": "04567", "sku": "2024-01-02"},
    ]


def test_inspect_cli_emits_json_payload_for_csv() -> None:
    with runner.isolated_filesystem():
        Path("sellers.csv").write_text("seller_id,sku\n00123,000ABC\n", encoding="utf-8")

        result = runner.invoke(app, ["inspect", "sellers.csv", "--json", "--ci"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["status"] == "success"
        assert payload["command"] == "inspect"
        assert payload["inspection"]["file_type"] == "csv"
        assert payload["inspection"]["preview"][0] == {"seller_id": "00123", "sku": "000ABC"}


def test_xlsx_with_multiple_sheets_requires_sheet(tmp_path: Path) -> None:
    source = tmp_path / "multi.xlsx"
    _write_xlsx(
        source,
        {
            "Sellers": [["SellerID", "Name"], ["00123", "Acme"]],
            "Orders": [["OrderID", "Amount"], ["A1", 12.5]],
        },
    )

    with pytest.raises(DbcliError) as exc_info:
        inspect_source(source)

    assert int(exc_info.value.exit_code) == 10
    assert exc_info.value.diagnostic.code == "source.sheet_required"
    assert exc_info.value.diagnostic.details["sheets"] == ["Sellers", "Orders"]


def test_inspect_xlsx_selected_sheet_preview(tmp_path: Path) -> None:
    source = tmp_path / "multi.xlsx"
    _write_xlsx(
        source,
        {
            "Sellers": [["SellerID", "Name"], ["00123", "Acme"]],
            "Orders": [["OrderID", "Amount"], ["A1", 12.5]],
        },
    )

    inspection = inspect_source(source, sheet="Orders")

    assert inspection.file_type == "xlsx"
    assert inspection.sheets == ["Sellers", "Orders"]
    assert inspection.selected_sheet == "Orders"
    assert inspection.columns == ["OrderID", "Amount"]
    assert inspection.preview == [{"OrderID": "A1", "Amount": 12.5}]


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
