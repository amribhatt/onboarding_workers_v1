"""Money is exact end to end: a float never stands in for an amount in the set-based path."""

from __future__ import annotations

import ast
import csv
from decimal import Decimal
from pathlib import Path

import pytest

from worker.engine_console import line_of, read_gl_records
from worker.glio import read_gl_lines
from worker.money import exact_amount, net_to_float, to_decimal

SRC = Path(__file__).resolve().parents[1] / "src" / "worker"
BIG = "12345678901234567.89"  # seventeen significant digits: a float cannot hold it


def _gl(path: Path, debit: str, credit: str) -> Path:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["GL Date", "Journal", "Line", "GL Account", "Account Name", "Transaction Type", "Debit",
                    "Credit", "Description"])
        w.writerow(["2025-12-31", "J-1", 1, "1000", "Cash", "Expense", debit, "", "x"])
        w.writerow(["2025-12-31", "J-1", 2, "3000", "Equity", "Expense", "", credit, "y"])
    return path


def test_an_amount_a_float_would_round_stays_exact() -> None:
    got = exact_amount(BIG)
    assert isinstance(got, Decimal) and got == Decimal(BIG)
    assert to_decimal(float(BIG)) != Decimal(BIG)


@pytest.mark.parametrize("cell", ["1,250.00", "(40.00)", 12.5, 7, "0.1", "", None])
def test_ordinary_amounts_are_still_the_floats_every_consumer_takes(cell: object) -> None:
    got = exact_amount(cell)
    assert isinstance(got, float)
    if cell not in ("", None):
        assert to_decimal(got) == to_decimal(cell)


def test_the_bulk_gl_reader_keeps_exact_amounts(tmp_path: Path) -> None:
    src = _gl(tmp_path / "gl.csv", BIG, BIG)
    lines = [ln for _n, ln in read_gl_lines(src)]
    assert lines[0].dr == Decimal(BIG) and lines[1].cr == Decimal(BIG)
    # a balanced journal nets to nothing; as floats the two sides would still agree, so check the side itself
    assert net_to_float(lines[0].dr, lines[1].cr) == 0.0
    assert net_to_float(lines[0].dr, "0.01") == float(Decimal(BIG) - Decimal("0.01"))


def test_the_console_gl_reader_keeps_exact_amounts(tmp_path: Path) -> None:
    src = _gl(tmp_path / "gl.csv", BIG, BIG)
    _name, recs = read_gl_records(src, lambda _acct: False)
    assert recs[0]["dr"] == Decimal(BIG)
    assert line_of(recs[0]).dr == Decimal(BIG)


def test_no_float_in_the_set_based_modules() -> None:
    """The set-based modules read cells back as they were (`float(text)` of a float cell) and nowhere else."""
    allowed = {"_untag", "_untag_cell"}
    offences = []
    for name in ("setops.py", "setrecipe.py", "accuracy.py", "columnar.py"):
        tree = ast.parse((SRC / name).read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef) and fn.name not in allowed:
                for node in ast.walk(fn):
                    if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "float":
                        offences.append(f"{name}:{node.lineno} in {fn.name}")
    assert not offences, offences
