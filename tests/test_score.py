from __future__ import annotations

import csv
import json
from collections.abc import Callable
from pathlib import Path

import openpyxl
import pytest

from worker.score import main, score

SET7_KEY = Path(__file__).parent / "fixtures" / "set7" / "answer_key"
TEMPLATES = ("Investors", "Commitments", "GL")
Rows = list[list[str]]


def _copy_key(dest: Path, mutate: dict[str, Callable[[Rows], Rows]] | None = None) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for t in TEMPLATES:
        with open(SET7_KEY / f"{t}.csv", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
        if mutate and t in mutate:
            rows = mutate[t](rows)
        book = openpyxl.Workbook()
        ws = book.active
        for r in rows:
            ws.append([v if v != "" else None for v in r])
        book.save(dest / f"{t}_Upload.xlsx")


def _dup_first(field: str, value: str) -> Callable[[Rows], Rows]:
    def fn(rows: Rows) -> Rows:
        new = list(rows[1])
        new[rows[0].index(field)] = value
        return [*rows, new]

    return fn


def test_perfect_copy_gets_full_marks(tmp_path: Path) -> None:
    _copy_key(tmp_path)
    res = score(tmp_path, SET7_KEY)
    for t in TEMPLATES:
        r = res["templates"][t]
        assert r["rows_expected"] == r["rows_got"] == r["matched"] > 0
        assert r["missing"] == 0 and r["extra"] == 0 and r["field_agreement"] == 1.0
    assert res["issues"]["total"] == 3 and res["issues"]["mentioned"] == 0


def test_dropped_row_changed_amount_and_extra_row(tmp_path: Path) -> None:
    def drop(rows: Rows) -> Rows:
        return [rows[0], *rows[2:]]

    def change(rows: Rows) -> Rows:
        rows[1][rows[0].index("Commitment_Amount")] = "999"
        return rows

    _copy_key(
        tmp_path,
        {"Investors": drop, "Commitments": change, "GL": _dup_first("Event_ID", "GP-001-J99-999-00001")},
    )
    res = score(tmp_path, SET7_KEY)["templates"]
    inv, com, gl = res["Investors"], res["Commitments"], res["GL"]
    assert (inv["missing"], inv["extra"], inv["matched"]) == (1, 0, inv["rows_expected"] - 1)
    n = com["rows_expected"]
    assert com["matched"] == n and com["missing"] == 0 and com["extra"] == 0
    assert com["fields"]["commitmentamount"] == {"agree": n - 1, "of": n}
    assert com["field_agreement"] < 1.0
    assert (gl["extra"], gl["missing"]) == (1, 0)
    assert gl["rows_got"] == gl["rows_expected"] + 1


def test_extra_investor_row_is_counted(tmp_path: Path) -> None:
    _copy_key(tmp_path, {"Investors": _dup_first("Investor_ID", "I-999")})
    inv = score(tmp_path, SET7_KEY)["templates"]["Investors"]
    assert inv["extra"] == 1 and inv["missing"] == 0 and inv["matched"] == inv["rows_expected"]


def test_issue_mentions_and_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _copy_key(tmp_path)
    audit = {"seq": 1, "rationale": "Journal j02-003 is out of balance by 50; X-005 not on roster"}
    (tmp_path / "audit.jsonl").write_text(json.dumps(audit) + "\n", encoding="utf-8")
    res = score(tmp_path, SET7_KEY)
    assert [i["mentioned"] for i in res["issues"]["items"]] == [True, True, False]
    assert main([str(tmp_path), str(SET7_KEY)]) == 0
    assert "Investors" in capsys.readouterr().out


def test_missing_upload_counts_all_rows_missing(tmp_path: Path) -> None:
    res = score(tmp_path, SET7_KEY)["templates"]["Investors"]
    assert res["rows_got"] == 0 and res["missing"] == res["rows_expected"] and not res["file_found"]
