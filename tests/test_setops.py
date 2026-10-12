"""Set-based checks (`worker.setops`, `checks`, `review`) give the findings the row loops they replaced gave.

The `_ref_*` functions below are those loops, kept here as the definition. Random rows use the spaces, case and
Unicode the checks must treat as Python's `str.strip`, `str.split` and `str.lower` do.
"""

from __future__ import annotations

import random
from collections import Counter
from decimal import Context, Decimal, localcontext
from typing import Any

import pytest

from worker import checks as C
from worker import review as R
from worker.setops import PrecisionError, RowTable
from worker.templates import TEMPLATES

SRC = "_src"
SPACES = ["", " ", "  ", "\t", " ", "　", "\n"]
WORDS = ["a", "A", "b", "B", "x y", "X  Y", "x y", "GP-001", "gp-001", "GP-002", "ID-1", "id-1", "ID-2",
         "café", "CAFÉ", "9" * 25, "", "n/a", "007", "7"]


def _cell(rnd: random.Random) -> Any:
    v = rnd.choice(WORDS)
    pre, post = rnd.choice(SPACES), rnd.choice(SPACES)
    return pre + v + post


def _rows(seed: int, n: int, fields: list[str]) -> list[dict[str, Any]]:
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        row: dict[str, Any] = {SRC: f"Sheet!{i + 2}"}
        for f in fields:
            if rnd.random() < 0.9:
                row[f] = _cell(rnd)
        out.append(row)
    return out


def _ref_duplicates(rows: list[dict[str, Any]], key_fields: list[str], normalise: bool) -> list[dict[str, Any]]:
    def norm(v: Any) -> str:
        return " ".join(str(v).split()).lower()

    groups: dict[tuple[str, ...], list[str]] = {}
    for row in rows:
        parts = tuple((norm(row.get(f, "")) if normalise else str(row.get(f, "")).strip()) for f in key_fields)
        if not any(parts):
            continue
        groups.setdefault(parts, []).append(row[SRC])
    return [{"key": " | ".join(k), "rows": srcs} for k, srcs in groups.items() if len(srcs) > 1]


def _ref_output(template: str, rows: list[dict[str, Any]]) -> list[tuple[str, int, tuple[str, ...]]]:
    spec = TEMPLATES[template]
    out: list[tuple[str, int, tuple[str, ...]]] = []
    if spec.key:
        seen: Counter[str] = Counter()
        for r in rows:
            parts = [" ".join(str(r.get(f, "")).split()).lower() for f in spec.key]
            if all(parts):
                seen[" | ".join(parts)] += 1
        dups = {k for k, n in seen.items() if n > 1}
        if dups:
            bad = [str(r.get(SRC, "")) for r in rows
                   if " | ".join(" ".join(str(r.get(f, "")).split()).lower() for f in spec.key) in dups]
            out.append(("duplicate_id", len(bad), tuple(bad[:50])))
    for name in spec.required:
        blank = [str(r.get(SRC, "")) for r in rows if str(r.get(name, "")).strip() == ""]
        if blank:
            out.append(("required_blank", len(blank), tuple(blank[:50])))
    return out


def _ref_stage(template: str, rows: list[dict[str, Any]], gp_id: str) -> list[tuple[str, int, tuple[str, ...]]]:
    spec = TEMPLATES[template]
    out: list[tuple[str, int, tuple[str, ...]]] = []
    if "GP_ID" in spec.fields and gp_id:
        bad = [r for r in rows if str(r.get("GP_ID", "")).strip().lower() not in ("", gp_id.lower())]
        if bad:
            out.append(("gp_id_differs", len(bad), tuple(str(r.get(SRC, "")) for r in bad[:50])))
    if spec.id_max_len:
        for name in (f for f in spec.fields if f.endswith("_ID")):
            long = [r for r in rows if len(str(r.get(name, "")).strip()) > spec.id_max_len]
            if long:
                out.append(("id_too_long", len(long), tuple(str(r.get(SRC, "")) for r in long[:50])))
    fields = [f for f in spec.fields if f != SRC]
    for i, a in enumerate(fields):
        for b in fields[i + 1:]:
            pairs = [(str(r.get(a, "")).strip(), str(r.get(b, "")).strip()) for r in rows]
            filled = [(x, y) for x, y in pairs if x or y]
            if len(filled) >= 2 and all(x and x == y for x, y in filled) and len(filled) == len(rows):
                out.append(("identical_columns", len(rows), ()))
    return out


def _facts(found: list[dict[str, Any]]) -> list[tuple[str, int, tuple[str, ...]]]:
    return [(f["code"], f["count"], tuple(f["rows"])) for f in found]


@pytest.mark.parametrize("template", ["Investors", "Commitments", "Investments"])
@pytest.mark.parametrize("seed", range(6))
def test_findings_equal_the_row_loops(template: str, seed: int) -> None:
    spec = TEMPLATES[template]
    rows = _rows(seed, 40 + seed * 7, list(spec.fields))
    if seed % 3 == 0:  # a copied column: every row has the same text in two fields
        a, b = [f for f in spec.fields if f != SRC][:2]
        for r in rows:
            r[a] = r[b] = "same"
    got = C.stage_findings(template, rows, "GP-001") + C.output_findings(template, rows)
    want = _ref_stage(template, rows, "GP-001") + _ref_output(template, rows)
    assert _facts([f for f in got if f["code"] != "no_rows"]) == want


@pytest.mark.parametrize("normalise", [True, False])
@pytest.mark.parametrize("seed", range(5))
def test_duplicates_equal_the_row_loop(seed: int, normalise: bool) -> None:
    rows = _rows(seed, 80, ["k1", "k2", "v"])
    table = RowTable.from_rows(rows)
    assert table.duplicates(["k1", "k2"], normalise) == _ref_duplicates(rows, ["k1", "k2"], normalise)
    dropped = {s for g in _ref_duplicates(rows, ["k1"], True) for s in g["rows"][1:]}
    assert table.later_duplicates(["k1"]) == dropped
    assert len(table.without(dropped)) == len([r for r in rows if r[SRC] not in dropped])


def test_unknown_values_are_the_missing_dependencies() -> None:
    rows = _rows(11, 60, ["Parent"])
    known = {"a", "id-1", "x y"}
    used: dict[str, list[str]] = {}
    shown: dict[str, str] = {}
    for r in rows:
        v = str(r.get("Parent", "")).strip()
        if v:
            used.setdefault(v.lower(), []).append(r[SRC])
            shown.setdefault(v.lower(), v)
    missing = {v: s for v, s in used.items() if v not in known}
    groups, n, head, srcs = RowTable.from_rows(rows).unknown_values("Parent", known)
    assert groups == len(missing)
    assert n == sum(len(s) for s in missing.values())
    assert [h[0] for h in head] == [shown[v] for v in missing][:200]
    assert srcs == [s for v in missing for s in missing[v]][:50]


def test_exact_sum_keeps_every_digit_and_refuses_what_does_not_fit() -> None:
    rows = [{SRC: "r1", "Amt": "0.10"}, {SRC: "r2", "Amt": "0.20"}, {SRC: "r3", "Amt": "-0.30"},
            {SRC: "r4", "Amt": "12345678901234567890.123456789012"}, {SRC: "r5", "Amt": ""}]
    total, n = RowTable.from_rows(rows).exact_sum("Amt")
    assert total == Decimal("12345678901234567890.123456789012") and n == 4
    with pytest.raises(PrecisionError):
        RowTable.from_rows([{SRC: "r", "Amt": "0.1234567890123"}]).exact_sum("Amt")
    with pytest.raises(PrecisionError):
        RowTable.from_rows([{SRC: "r", "Amt": "1e3"}]).exact_sum("Amt")


def test_exact_sum_equals_python_decimal_on_random_money() -> None:
    """The scaled-integer sum is the exact `Decimal` sum: signs, 0 to 12 fraction digits, magnitudes up to 20 integer
    digits, repeated values (the sum counts each text once), padding spaces and blanks."""
    rnd = random.Random(7)
    texts: list[str] = []
    for _ in range(3000):
        digits = rnd.choice([1, 2, 5, 9, 15, 20])
        whole = str(rnd.randrange(10 ** (digits - 1), 10 ** digits)) if digits > 1 else str(rnd.randrange(10))
        frac = rnd.choice([0, 0, 2, 2, 3, 4, 12])
        text = whole + ("." + "".join(rnd.choice("0123456789") for _ in range(frac)) if frac else "")
        texts.append(rnd.choice(["", "-", "+"]) + text)
    texts += texts[:500]  # repeats
    rows = [{SRC: f"r{i}", "Amt": rnd.choice(["", " ", "	"]) + t + rnd.choice(["", " "])}
            for i, t in enumerate(texts)] + [{SRC: "blank", "Amt": " "}, {SRC: "gone"}]
    total, n = RowTable.from_rows(rows).exact_sum("Amt")
    with localcontext(Context(prec=80)):  # the reference sum must not round to 28 digits either
        want = sum((Decimal(t) for t in texts), Decimal(0))
    assert total == want and n == len(texts)
    assert total.as_tuple().exponent == -12  # always the 12-digit scale, as the fixed-scale sum gave
    assert RowTable.from_rows([{SRC: "r", "Amt": ""}]).exact_sum("Amt") == (Decimal("0.000000000000"), 0)


def test_exact_sum_refuses_a_total_that_does_not_fit() -> None:
    rows = [{SRC: f"r{i}", "Amt": "9" * 26} for i in range(3)]
    with pytest.raises(PrecisionError):
        RowTable.from_rows(rows).exact_sum("Amt")


@pytest.mark.parametrize("bad", ["1" * 30, ".5", "5.", "--1", "1,000", "0x10", "NaN", "1 2"])
def test_exact_sum_refuses_what_is_not_a_plain_exact_number(bad: str) -> None:
    with pytest.raises(PrecisionError):
        RowTable.from_rows([{SRC: "r1", "Amt": "1.00"}, {SRC: "r2", "Amt": bad}]).exact_sum("Amt")


@pytest.mark.parametrize("seed", range(3))
def test_duplicate_ids_equal_the_row_loop(seed: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Groups, rows in them and the first rows in row order, over several parts."""
    monkeypatch.setattr("worker.setops.BATCH_ROWS", 17)
    rows = _rows(seed, 120, ["k1", "k2"])
    parts = [[" ".join(str(r.get(f, "")).split()).lower() for f in ("k1", "k2")] for r in rows]
    keys = [" | ".join(p) if all(p) else None for p in parts]
    counts = Counter(k for k in keys if k)
    dup = [r[SRC] for r, k in zip(rows, keys, strict=True) if k and counts[k] > 1]
    groups, n, first = RowTable.from_rows(rows).duplicate_ids(["k1", "k2"], limit=5)
    assert (groups, n, first) == (sum(1 for c in counts.values() if c > 1), len(dup), dup[:5])


def test_review_table_matches_review_over_a_stage_for_dependencies() -> None:
    """Dependencies and reconciliation are the same whether the rows come from a Stage or from a table."""
    class Ctx:
        sponsor = "sponsor-a"
        gp_id = "GP-001"
        registry = None

    spec = TEMPLATES["Commitments"]
    depends = list(spec.depends)
    rows = _rows(3, 30, [f for f, _t in depends])
    table = RowTable.from_rows(rows)
    found = R.dependency_findings(Ctx(), "Commitments", table, {}, TEMPLATES, lambda _t: [])
    if depends:
        field, target = depends[0]
        groups, n, _head, _srcs = table.unknown_values(field, set())
        if groups:
            hit = next(f for f in found if f["field"] == field)
            assert hit["count"] == n and hit["record_type"] == target
