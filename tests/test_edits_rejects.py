from __future__ import annotations

import csv
from pathlib import Path

import polars as pl
import pytest

from dbcli.pipeline.edits import apply_edit_pipeline
from dbcli.core.errors import DbcliError
from dbcli.project import init_project
from dbcli.pipeline.rejects import write_rejects_csv
from dbcli.recipes.schema import parse_schema_columns


def test_rename_drop_trim_parse_null_and_fill_null_pipeline() -> None:
    dataframe = pl.DataFrame(
        {
            "SellerID": ["001", "002"],
            "tier": [" A ", ""],
            "notes": ["drop", "drop"],
        }
    )
    result = apply_edit_pipeline(
        dataframe,
        [
            {"rename": {"SellerID": "seller_id"}},
            {"drop": ["notes"]},
            {"trim": ["tier"]},
            {"parse_null": {"columns": ["tier"], "values": [""]}},
            {"fill_null": {"tier": "UNKNOWN"}},
        ],
    )

    assert result.rejects == []
    assert result.dataframe.to_dicts() == [
        {"seller_id": "001", "tier": "A"},
        {"seller_id": "002", "tier": "UNKNOWN"},
    ]


def test_rename_collision_and_missing_drop_column_are_diagnostics() -> None:
    dataframe = pl.DataFrame({"a": ["1"], "b": ["2"]})

    with pytest.raises(DbcliError) as collision:
        apply_edit_pipeline(dataframe, [{"rename": {"a": "b"}}])
    assert collision.value.diagnostic.code == "edit.rename_collision"

    with pytest.raises(DbcliError) as missing:
        apply_edit_pipeline(dataframe, [{"drop": ["missing"]}])
    assert missing.value.diagnostic.code == "edit.missing_column"


def test_rename_cannot_target_internal_row_index() -> None:
    dataframe = pl.DataFrame({"a": ["1"]})

    with pytest.raises(DbcliError) as exc_info:
        apply_edit_pipeline(dataframe, [{"rename": {"a": "__dbcli_internal_row_index"}}])

    assert exc_info.value.diagnostic.code == "edit.reserved_column"


def test_cast_failures_produce_edit_rejects_and_remove_bad_rows() -> None:
    dataframe = pl.DataFrame(
        {
            "seller_id": ["001", "bad"],
            "first_active_date": ["01/02/2024", "not-a-date"],
            "active": ["yes", "maybe"],
        }
    )
    schema = parse_schema_columns(
        [
            {"name": "seller_id", "type": "BIGINT", "nullable": False},
            {"name": "first_active_date", "type": "DATE", "nullable": True},
            {"name": "active", "type": "BOOLEAN", "nullable": True},
        ]
    )

    result = apply_edit_pipeline(
        dataframe,
        [
            {
                "cast": {
                    "seller_id": {"type": "int"},
                    "first_active_date": {"type": "date", "format": "%m/%d/%Y"},
                    "active": {"type": "boolean"},
                }
            }
        ],
        schema=schema,
    )

    assert result.dataframe.to_dicts() == [
        {"seller_id": 1, "first_active_date": result.dataframe["first_active_date"][0], "active": True}
    ]
    assert str(result.dataframe["first_active_date"][0]) == "2024-01-02"
    assert result.rejected_row_indices == {2}
    assert [(reject.row_index, reject.column, reject.check, reject.value) for reject in result.rejects] == [
        (2, "seller_id", "cast_failed", "bad"),
        (2, "first_active_date", "cast_failed", "not-a-date"),
        (2, "active", "cast_failed", "maybe"),
    ]
    assert result.rejects[0].original_row == {
        "seller_id": "bad",
        "first_active_date": "not-a-date",
        "active": "maybe",
    }


def test_decimal_cast_uses_schema_precision_and_rejects_overflow() -> None:
    dataframe = pl.DataFrame({"amount": ["12.34", "1234.56"]})
    schema = parse_schema_columns([{"name": "amount", "type": "DECIMAL(5,2)"}])

    result = apply_edit_pipeline(dataframe, [{"cast": {"amount": {"type": "decimal"}}}], schema=schema)

    assert [str(value) for value in result.dataframe["amount"].to_list()] == ["12.34"]
    assert result.rejects[0].column == "amount"
    assert result.rejects[0].value == "1234.56"


def test_cast_schema_mismatch_is_structured_diagnostic() -> None:
    dataframe = pl.DataFrame({"seller_id": ["001"]})
    schema = parse_schema_columns([{"name": "seller_id", "type": "VARCHAR(16)"}])

    with pytest.raises(DbcliError) as exc_info:
        apply_edit_pipeline(dataframe, [{"cast": {"seller_id": {"type": "int"}}}], schema=schema)

    assert exc_info.value.diagnostic.code == "schema.type_mismatch"


def test_write_rejects_csv_includes_original_row_and_metadata(tmp_path: Path) -> None:
    paths, _ = init_project(tmp_path)
    dataframe = pl.DataFrame({"seller_id": ["bad"]})
    result = apply_edit_pipeline(dataframe, [{"cast": {"seller_id": {"type": "int"}}}])

    reject_path = write_rejects_csv("r_test", result.rejects, result.original_columns, paths=paths)

    with reject_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert reject_path == paths.rejects_dir / "r_test.csv"
    assert rows == [
        {
            "seller_id": "bad",
            "__stage": "edit",
            "__row_index": "1",
            "__column": "seller_id",
            "__check": "cast_failed",
            "__value": "bad",
            "__reason": "Could not cast value to int.",
        }
    ]
