"""The one money implementation: Decimal, half-up cents, one balance and tolerance rule."""

from __future__ import annotations

import random
import re
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from worker import quality as Q
from worker.money import balance_status, cents, net_to_float, to_decimal, to_float, total


def test_floats_read_through_their_repr_and_round_half_up() -> None:
    assert to_decimal(0.1) + to_decimal(0.2) == Decimal("0.3")  # binary floats give 0.30000000000000004
    assert cents(2.675) == Decimal("2.68")  # round(2.675, 2) is 2.67: the float sits just below the half
    assert cents(1234.565) == Decimal("1234.57")
    assert cents(-2.675) == Decimal("-2.68")  # half-up, away from zero
    assert to_float(0.004) == 0.0 and str(cents(-0.004)) == "0.00"  # never -0.00


def test_the_streaming_shortcut_gives_the_decimal_answer() -> None:
    rng = random.Random(7)
    cases: list[tuple[float, float]] = [(2.675, 0.0), (0.0, 1234.565), (0.285, 0.0), (1.005, 0.0), (0.1, 0.2)]
    cases += [(round(rng.uniform(0, 1e7), rng.choice([0, 1, 2, 3, 4])), round(rng.uniform(0, 1e7), 2))
              for _ in range(20000)]
    cases += [(rng.randint(0, 10**6), 0) for _ in range(100)]
    for dr, cr in cases:
        assert net_to_float(dr, cr) == to_float(to_decimal(dr) - to_decimal(cr)), (dr, cr)
    assert net_to_float("1,250.005", "(0.00)") == 1250.01  # text goes through the Decimal path


def test_cells_with_commas_parentheses_and_blanks() -> None:
    assert to_decimal("1,234.50") == Decimal("1234.50")
    assert to_decimal("(1,234.50)") == Decimal("-1234.50")
    assert to_decimal(None) == to_decimal("") == to_decimal("  ") == 0
    for bad in ("abc", "nan", float("inf"), True):
        with pytest.raises(ValueError):
            to_decimal(bad)


def test_total_sums_exactly_then_rounds_once() -> None:
    assert total([0.1] * 10) == Decimal("1.00")
    assert total([1.005, 1.005]) == Decimal("2.01")  # per-item rounding first would give 2.00 or 2.02


@pytest.mark.parametrize(("debits", "credits", "tol_pass", "tol_warn", "status", "diff"), [
    (100, 100, 0, 0, "ok", "0.00"),
    (100.10, 100.09, 0, 0, "err", "0.01"),
    (100.10, 100.09, 0.01, 0.01, "ok", "0.01"),
    (100.10, 100.05, 0.01, 0.05, "warn", "0.05"),
    (100.10, 100.04, 0.01, 0.05, "err", "0.06"),
    (100, 100.004, 0, 0, "ok", "0.00"),  # a sub-cent difference is not a cent
    (50, 60, 0, 0, "err", "-10.00"),
])
def test_balance_status(debits: float, credits: float, tol_pass: float, tol_warn: float, status: str, diff: str) -> None:
    got_status, got_diff = balance_status(debits, credits, tol_pass, tol_warn)
    assert (got_status, str(got_diff)) == (status, diff)


def test_the_data_quality_balance_rule_uses_the_same_status(tmp_path: Path) -> None:
    """quality.scan finds the journals that money.balance_status calls errors, and honours the rule's tolerance."""
    path = tmp_path / "gl.csv"
    path.write_text("Journal,Debit,Credit\nJ1,100.10,0\nJ1,0,100.10\nJ2,50.00,0\nJ2,0,49.99\nJ3,0.1,0\nJ3,0.2,0\n"
                    "J3,0,0.3\n", encoding="utf-8")
    rules = Q.load_rules()
    cols = {"Journal": "Journal", "Debit": "Debit", "Credit": "Credit"}

    def unbalanced(rs: list[Q.Rule]) -> set[str]:
        rep = Q.scan(path, rs, columns=cols, record_type="GL")
        cause = next((c for c in rep["causes"] if c["rule"] == "journal_unbalanced"), None)
        words = [] if cause is None else cause["locator"]["filter"].replace(",", " ").split()
        return {w for w in words if re.fullmatch(r"J\d", w)}

    assert unbalanced(rules) == {"J2"}  # J3: 0.1 + 0.2 = 0.3 exactly
    loose = [replace(r, options={**r.options, "tol_pass": 0.01}) if r.op == "balance" else r for r in rules]
    assert unbalanced(loose) == set()  # one cent is inside the tolerance the rule declares
