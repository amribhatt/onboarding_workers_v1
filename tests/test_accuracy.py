"""Accuracy checks: set-based proofs that block a write on failure."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

import test_engine_golden as EG
from worker import accuracy as AC
from worker import columnar
from worker import engine as E
from worker.setops import RowTable

SRC = "_src"


def test_conservation_balances_and_names_what_is_held() -> None:
    ok = AC.conservation(100, 95, {"excluded": 3, "blocked": 2})
    assert ok.ok and "100 rows read = 95 written + 5 held" in ok.detail
    lp = AC.conservation(10, 12, {"excluded": 0}, generated=2)
    assert lp.ok and "2 generated" in lp.detail
    lost = AC.conservation(100, 90, {"excluded": 3})
    assert not lost.ok and "7 row(s) unexplained" in lost.detail
    with pytest.raises(AC.AccuracyError):
        AC.require([ok, lost])
    AC.require([ok, lp])


def test_control_totals_are_exact_to_the_last_digit() -> None:
    a = {"rows": 2, "net": Decimal("0.30")}
    assert AC.control_totals(a, {"rows": 2, "net": Decimal("0.3000")}).ok
    off = AC.control_totals(a, {"rows": 2, "net": Decimal("0.31")})
    assert not off.ok and "net should be 0.30 and is 0.31" in off.detail
    assert not AC.control_totals({"rows": 3}, {"rows": 2}).ok


def test_paired_values_find_a_changed_cell_and_a_row_without_a_source() -> None:
    src = RowTable.from_rows([{SRC: "S!2", "acct": "1000"}, {SRC: "S!3", "acct": "2000"}])
    good = RowTable.from_rows([{SRC: "S!2", "Account_Name": " 1000 "}, {SRC: "S!3", "Account_Name": "2000"}])
    assert AC.paired_values(src, good, [("acct", "Account_Name")]).ok
    bad = RowTable.from_rows([{SRC: "S!2", "Account_Name": "1000"}, {SRC: "S!3", "Account_Name": "2001"},
                              {SRC: "S!9", "Account_Name": "1"}])
    got = AC.paired_values(src, bad, [("acct", "Account_Name")])
    assert not got.ok and "1 row(s) differ" in got.detail and "no source row" in got.detail


def test_cell_accounting_flags_a_column_with_data_nobody_owns() -> None:
    cols = {"Amount": 10, "Notes": 4, "Empty": 0}
    ok = AC.cell_accounting(cols, {"Amount": "Commitment_Amount"}, {"Notes": "not a field of this upload"})
    assert ok.ok and ok.facts["columns"]["Empty"]["what"] == "empty"
    loose = AC.cell_accounting(cols, {"Amount": "Commitment_Amount"}, {})
    assert not loose.ok and loose.facts["unaccounted"] == ["Notes"]


def test_column_fill_counts_cells_without_reading_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_CACHE_DIR", str(tmp_path / "cache"))
    p = tmp_path / "t.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Id", "Amount", "Note"])
    ws.append(["a", 1.5, None])
    ws.append(["b", None, "x"])
    ws.append(["c", 3, " "])
    wb.save(p)
    assert columnar.column_fill(p) == (["Id", "Amount", "Note"], [3, 2, 1])
    c = tmp_path / "t.csv"
    c.write_text("Id,Amount,Note\na,1.5,\nb,,x\nc,3, \n", encoding="utf-8")
    assert columnar.column_fill(c) == (["Id", "Amount", "Note"], [3, 2, 1])


@pytest.mark.slow
def test_the_write_is_refused_when_the_file_does_not_tie_to_the_check(tmp_path: Path,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    c, _j, src, picks = EG._gl_lines(tmp_path, "GL Fund", True)
    spec, ports, out = EG._gl_engine(tmp_path, c, src, "GL Fund", picks)
    first = E.evaluate(spec, ports)
    assert first.outcome == "complete"
    real = AC.control_totals
    monkeypatch.setattr(AC, "control_totals", lambda want, got, what="": real({**want, "net": Decimal(1)}, got,
                                                                               what))
    with pytest.raises(E.Refused) as err:
        E.evaluate(spec, ports, approval=EG.approval_for(first), stop_after="write")
    assert err.value.code == "accuracy_failed"
    assert not list(out.glob("GL_Upload*.xlsx"))  # nothing was written
