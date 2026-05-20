"""Reject CSV record model and writer."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from dbcli.project import ProjectPaths, find_project


METADATA_COLUMNS = ["__stage", "__row_index", "__column", "__check", "__value", "__reason"]


@dataclass(frozen=True)
class RejectRecord:
    original_row: Mapping[str, Any]
    stage: str
    row_index: int
    column: str | None
    check: str
    value: Any
    reason: str

    def to_csv_row(self, original_columns: list[str]) -> dict[str, str]:
        row = {column: _csv_value(self.original_row.get(column)) for column in original_columns}
        row.update(
            {
                "__stage": self.stage,
                "__row_index": str(self.row_index),
                "__column": self.column or "",
                "__check": self.check,
                "__value": _csv_value(self.value),
                "__reason": self.reason,
            }
        )
        return row


def write_rejects_csv(
    run_id: str,
    rejects: list[RejectRecord],
    original_columns: list[str],
    *,
    paths: ProjectPaths | None = None,
) -> Path:
    paths = paths or find_project()
    paths.rejects_dir.mkdir(parents=True, exist_ok=True)
    reject_path = paths.rejects_dir / f"{run_id}.csv"
    fieldnames = [*original_columns, *METADATA_COLUMNS]

    with reject_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for reject in rejects:
            writer.writerow(reject.to_csv_row(original_columns))

    return reject_path


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)
