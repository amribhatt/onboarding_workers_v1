"""The upload writer emits true Excel dates for date fields (user decision), not ISO text.

A `*_Date` field holding an ISO date (`YYYY-MM-DD`) is written as a date cell with the number format `yyyy-mm-dd`;
anything else in the column stays text, blanks stay blank, and CSV output is unchanged. Reading the file back gives
the same ISO date, so replay and parity do not move.
"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path
from typing import Any

import openpyxl

from worker.engine_console import cell_text
from worker.staging import write_rows

FIELDS = ("Commitment_ID", "Commitment_Date", "Commitment_Amount")
ROWS = [
    {"Commitment_ID": "C1", "Commitment_Date": "2025-04-03", "Commitment_Amount": "100.5"},
    {"Commitment_ID": "C2", "Commitment_Date": "", "Commitment_Amount": "7"},
    {"Commitment_ID": "C3", "Commitment_Date": "not a date", "Commitment_Amount": "9"},
]


def _cells(path: Path) -> list[list[Any]]:
    book = openpyxl.load_workbook(path)
    return [list(r) for r in book.worksheets[0].iter_rows(min_row=2)]


def test_an_iso_date_is_a_real_date_cell_with_a_format(tmp_path: Path) -> None:
    path, n = write_rows("Commitments", FIELDS, ROWS, tmp_path)
    assert n == 3
    cell = _cells(path)[0][1]
    assert cell.data_type == "d" and cell.is_date
    assert cell.number_format == "yyyy-mm-dd"
    assert isinstance(cell.value, dt.datetime) and cell.value.date() == dt.date(2025, 4, 3)
    assert not isinstance(cell.value, str)


def test_blank_and_non_date_text_stay_as_they_were(tmp_path: Path) -> None:
    path, _ = write_rows("Commitments", FIELDS, ROWS, tmp_path)
    rows = _cells(path)
    assert rows[1][1].value is None
    assert rows[2][1].value == "not a date" and rows[2][1].data_type == "s"
    assert rows[0][0].value == "C1" and rows[0][2].value == "100.5"  # other fields are untouched


def test_reading_the_file_back_gives_the_same_iso_date(tmp_path: Path) -> None:
    path, _ = write_rows("Commitments", FIELDS, ROWS, tmp_path)
    assert cell_text(_cells(path)[0][1].value) == "2025-04-03"


def test_csv_is_unchanged(tmp_path: Path) -> None:
    path, _ = write_rows("Commitments", FIELDS, ROWS, tmp_path, output="csv")
    with path.open(encoding="utf-8-sig", newline="") as fh:
        got = list(csv.reader(fh))
    assert got[1][1] == "2025-04-03"
