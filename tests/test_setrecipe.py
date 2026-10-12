"""Set-based recipes (`worker.setrecipe`) give, cell for cell, the outcome `transforms.apply_field` gives."""

from __future__ import annotations

import random
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pyarrow as pa
import pytest

from worker import setrecipe as SR
from worker import transforms as T

HEADERS = ("A", "B", "C")
TEXT = [
    "", " ", "abc", "  x  ", "N/A", "n/a", "-", "--", "TBD", "tbd ", "1,234.50", "(1,234.50)", "$1,234", "12%",
    "007", "A-B-C", "ID: 123", "ID:  45 x", "1.234,56", "-5", "5-", "1 000", "1'000", "1,23,456", "1,234,56",
    "abc,def", "a,,b", ",a", "a,", "x" * 150, "Yes", "yes", "NO", "Hello World", "Hello", "hello", "HELLO",
    "ACME-001", "acme-002", "  acme-003  ", "2025-03-31", "31/03/2025", "03/31/2025", "3/4/25", "1 Jan 2025",
    "Jan 5, 2025", "1.5", ".5", "5.", "1e5", "0", "0.00", "-0", "100", "USD 100", "100 USD", "€ 5", "café",
    "straße", "a\tb", "a\nb", "line1\nline2", "tab\t", "A  B   C", "NULL", "0.000001", "1234567.891",
    "12.3456789", "(5)", "-(5)", "$ 1,000.00", "50%", "-50%", "(50%)", "1,000%", "7 ", " 7", "+5",
    "9" * 30, "1" * 25 + ".5", "ref-1", "ref-2", "REF-3",
]
OTHER = [Decimal("1.50"), Decimal("-0.00"), Decimal(0), Decimal(1000), Decimal("2.5000"), 5, -3, 0, 1500.0,
         2.5, 1e20, 1e-7, 0.1, True, False, date(2025, 3, 31), datetime(2025, 3, 31, 0, 0),
         datetime(2025, 3, 31, 13, 30), None, None]
CORPUS: list[Any] = [*TEXT, *OTHER]

NUM = {"decimal": ".", "thousands": [","], "negative": ["minus", "brackets", "trailing_minus"],
       "currency": ["$", "USD", "€"], "percent": "reject"}
NUM_PCT = {"decimal": ".", "thousands": [","], "negative": ["minus"], "currency": [], "percent": "as_fraction"}
NUM_EU = {"decimal": ",", "thousands": [".", " "], "negative": ["minus", "brackets"], "currency": ["EUR"],
          "percent": "as_number"}

FIELDS: dict[str, dict[str, Any]] = {
    "trim": {"from": "A", "ops": [{"op": "trim"}]},
    "trim_collapse": {"from": "A", "ops": [{"op": "trim", "inner": "collapse"}]},
    "case_upper": {"from": "A", "ops": [{"op": "case", "to": "upper"}]},
    "case_lower": {"from": "A", "ops": [{"op": "trim"}, {"op": "case", "to": "lower"}]},
    "blank_ci": {"from": "A", "ops": [{"op": "treat_as_blank", "values": ["yes", "NO", "null"]}]},
    "blank_cs": {"from": "A", "ops": [{"op": "treat_as_blank", "values": ["Yes", "0"], "ci": False}]},
    "prefix": {"from": "A", "ops": [{"op": "strip_prefix", "text": "ID:"}]},
    "prefix_ci": {"from": "A", "ops": [{"op": "strip_prefix", "text": "acme-", "ci": True}, {"op": "trim"}]},
    "suffix": {"from": "A", "ops": [{"op": "strip_suffix", "text": " USD"}]},
    "split": {"from": "A", "ops": [{"op": "split", "sep": "-", "index": 2}]},
    "split_neg": {"from": "A", "ops": [{"op": "split", "sep": ",", "index": -1, "trim": False}]},
    "truncate": {"from": "A", "ops": [{"op": "truncate", "max": 5}]},
    "regex": {"from": "A", "ops": [{"op": "regex_extract", "pattern": "([0-9]+)"}]},
    "regex_missing": {"from": "A", "ops": [{"op": "regex_extract", "pattern": "ID: *([0-9]+)", "ci": True,
                                            "on_miss": "missing"}]},
    "number": {"from": "A", "ops": [{"op": "number_parse", **NUM}]},
    "number_pct": {"from": "A", "ops": [{"op": "number_parse", **NUM_PCT}]},
    "number_eu": {"from": "A", "ops": [{"op": "number_parse", **NUM_EU}]},
    "number_negate": {"from": "A", "ops": [{"op": "number_parse", **NUM}, {"op": "negate"}]},
    "date": {"from": "A", "ops": [{"op": "date_parse", "patterns": ["YYYY-MM-DD", "DD/MM/YYYY"]}]},
    "crosswalk": {"from": "A", "ops": [{"op": "crosswalk", "pairs": {"yes": "Y", "no": "N", "hello": "HI"},
                                        "on_miss": "missing"}]},
    "crosswalk_fail": {"from": "A", "ops": [{"op": "crosswalk", "pairs": {"yes": "Y", "No": "N"},
                                             "match": "exact"}]},
    "default_text": {"from": "A", "ops": [{"op": "treat_as_blank", "values": ["N/A"]},
                                          {"op": "default", "value": {"text": "none"}}]},
    "default_setup": {"from": "A", "ops": [{"op": "default", "value": {"setup": "fund_name"}}]},
    "coalesce": {"from": "A", "ops": [{"op": "coalesce", "cols": [{"col": "B"}, {"col": "C"}, {"text": "z"}]}]},
    "coalesce_num": {"from": "A", "ops": [{"op": "number_parse", **NUM},
                                          {"op": "coalesce", "cols": [{"col": "B"}]}]},
    "join": {"from": "A", "ops": [{"op": "join", "parts": [{"col": "A"}, {"text": "/"}, {"col": "B"},
                                                          {"col": "C"}], "sep": "-"}]},
    "join_keep_blank": {"from": "A", "ops": [{"op": "join", "parts": ["value", {"col": "B"}], "sep": "|",
                                              "skip_blank": False}]},
    "when_eq": {"from": "A", "ops": [{"op": "when", "test": {"left": "value", "cmp": "eq", "right": "Yes"},
                                      "then": {"text": "T"}, "else": "keep"}]},
    "when_col": {"from": "A", "ops": [{"op": "when", "test": {"left": {"col": "B"}, "cmp": "not_blank"},
                                       "then": {"col": "B"}, "else": "blank"}]},
    "when_in": {"from": "A", "ops": [{"op": "when", "test": {"left": "value", "cmp": "in",
                                                             "right": ["Yes", "yes", "NO"]},
                                      "then": "blank", "else": {"col": "C"}}]},
    "when_starts": {"from": "A", "ops": [{"op": "when", "test": {"left": "value", "cmp": "starts_with",
                                                                 "right": "Hel"},
                                          "then": {"text": "greeting"}, "else": "keep"}]},
    "when_ne": {"from": "A", "ops": [{"op": "when", "test": {"left": "value", "cmp": "ne", "right": "abc"},
                                      "then": "keep", "else": "blank"}]},
    "debit_credit": {"from": "A", "ops": [{"op": "debit_credit", "debit": {"col": "B"}, "credit": {"col": "C"},
                                           "number": NUM}]},
    "debit_credit_fail": {"from": "A", "ops": [{"op": "debit_credit", "debit": {"col": "A"},
                                                "credit": {"col": "B"}, "number": NUM_EU,
                                                "both_sides": "fail"}]},
    "missing_col": {"from": "Nope", "ops": [{"op": "trim"}]},
    "supply": {"supply": {"value": "FundX"}},
    "gone": {"missing": {"reason": "not in file"}},
    "chain": {"from": "A", "ops": [{"op": "trim", "inner": "collapse"}, {"op": "strip_prefix", "text": "ref-",
                                                                         "ci": True},
                                   {"op": "case", "to": "upper"}, {"op": "truncate", "max": 4}]},
}


def _rule(name: str) -> T.FieldRule:
    if name in ("number", "number_pct", "number_eu", "number_negate", "debit_credit", "debit_credit_fail",
                "coalesce_num"):
        return T.FieldRule(kind="amount")
    if name == "date":
        return T.FieldRule(kind="date")
    return T.FieldRule(kind="text")


def _same(a: Any, b: Any) -> bool:
    if type(a) is not type(b):
        return False
    return str(a) == str(b)


def _check(name: str, cells: list[tuple[Any, Any, Any]]) -> None:
    step = FIELDS[name]
    ctx = T.Context(setup={"fund_name": "Fund A"})
    cf = T.compile_field(name, step, _rule(name), ctx)
    cols = [[c[i] for c in cells] for i in range(3)]
    res = SR.apply_field(cf, HEADERS, cols, ctx)
    got_status, got_code, got_flags, got_value = res.status, res.code, res.flags, res.value
    tcols = T._columns(HEADERS)
    for n, row in enumerate(cells):
        want = T.apply_field(cf, T.Row(str(n), HEADERS, tuple(row)), ctx, tcols)
        assert got_status[n] == want.status, (name, row, got_status[n], want)
        assert got_code[n] == want.code, (name, row, got_code[n], want)
        assert got_flags[n] == want.flags, (name, row, got_flags[n], want)
        assert _same(got_value[n], want.value), (name, row, got_value[n], want.value)


def _cells(seed: int, count: int) -> list[tuple[Any, Any, Any]]:
    rnd = random.Random(seed)
    out: list[tuple[Any, Any, Any]] = [(a, None, None) for a in CORPUS]
    for _ in range(count):
        out.append((rnd.choice(CORPUS), rnd.choice(CORPUS), rnd.choice(CORPUS)))
    return out


@pytest.mark.parametrize("name", sorted(FIELDS))
def test_setrecipe_equals_the_reference(name: str) -> None:
    _check(name, _cells(sum(map(ord, name)), 400))


def _rule_reason(value: Any, rule: T.FieldRule, seen: set[str] | None) -> str | None:
    s = T._text(value)
    if rule.max_len is not None and len(s) > rule.max_len:
        return "id_too_long" if rule.is_id else "too_long"
    if rule.reference is not None and s not in rule.reference:
        return "not_in_reference"
    if seen is not None:
        if s in seen:
            return "duplicate_key"
        seen.add(s)
    return None


def _ref_verify(recipe: dict[str, Any], rows: Iterable[T.Row], target_rules: Mapping[str, T.FieldRule] | None = None,
           context: T.Context | None = None) -> dict[str, T.Verdict]:
    """Run every field of the recipe over all rows. Per field: conforming/total, MISSING, FAILED by code, flagged
    counts and up to 5 failing examples (copies of cells, first in source order). A MISSING row conforms on an
    optional field and not on a required one; a FAILED row never conforms."""
    compiled = T.compile_recipe(recipe, target_rules, context)
    verdicts = {n: T.Verdict(n, f.field_hash) for n, f in compiled.fields.items()}
    seen = {n: set() for n, f in compiled.fields.items() if f.rule.is_key}
    cache: dict[tuple[str, ...], dict[str, list[int]]] = {}
    for row in rows:
        cols = cache.get(row.headers)
        if cols is None:
            cols = cache[row.headers] = T._columns(row.headers)
        for name, cf in compiled.fields.items():
            v = verdicts[name]
            v.total += 1
            out = T.apply_field(cf, row, context, cols)
            for flag in sorted(out.flags):
                v.flagged[flag] = v.flagged.get(flag, 0) + 1
            reason: str | None = None
            if out.status == "failed":
                reason = out.code
                v.failed[out.code or ""] = v.failed.get(out.code or "", 0) + 1
            elif out.status == "missing":
                v.missing += 1
                reason = "required_blank" if cf.rule.required else None
            else:
                reason = _rule_reason(out.value, cf.rule, seen.get(name))
            if reason is None:
                v.conform += 1
            elif len(v.examples) < T.MAX_EXAMPLES:
                result = None if out.value is None else T._text(out.value)
                v.examples.append({"src": row.src, "source_value": T._cut(out.source), "result": result,
                                   "reason": reason})
    return verdicts


RULES = {
    "trim": T.FieldRule(kind="text", required=True, max_len=6),
    "case_upper": T.FieldRule(kind="text", is_id=True, is_key=True, max_len=5),
    "crosswalk": T.FieldRule(kind="text", reference=frozenset({"Y", "N"})),
    "number": T.FieldRule(kind="amount", required=True),
    "join": T.FieldRule(kind="text", is_key=True),
    "date": T.FieldRule(kind="date", required=True),
    "truncate": T.FieldRule(kind="text", max_len=3, required=True),
}


@pytest.mark.parametrize("name", sorted(RULES))
def test_verdicts_equal_the_reference(name: str) -> None:
    """Counts, failure codes in order of first appearance, flags and the first five examples match."""
    ctx = T.Context(setup={"fund_name": "Fund A"})
    rows = [T.Row(f"S!{n + 2}", HEADERS, tuple(c)) for n, c in enumerate(_cells(sum(map(ord, name)) + 1, 300))]
    recipe = {"fields": {name: FIELDS[name]}}
    want = _ref_verify(recipe, rows, {name: RULES[name]}, ctx)[name]
    got = SR.verify(recipe, rows, {name: RULES[name]}, ctx)[name]
    assert got == want
    assert list(got.failed) == list(want.failed) and list(got.flagged) == list(want.flagged)


def test_verify_handles_a_changing_header_layout() -> None:
    rows = [T.Row("S!2", ("A", "B"), ("x", "y")), T.Row("S!3", ("A", "B"), ("z", None)),
            T.Row("S!4", ("B", "A"), ("q", "w")), T.Row("S!5", ("Other",), ("v",))]
    recipe = {"fields": {"F": {"from": "A", "ops": [{"op": "case", "to": "upper"}]}}}
    assert SR.verify(recipe, rows)["F"] == _ref_verify(recipe, rows)["F"]


def test_arrow_columns_are_read_as_their_cells() -> None:
    cf = T.compile_field("n", FIELDS["number"], T.FieldRule(kind="amount"))
    texts = ["1,234.50", "(5)", "x", None, "12"]
    got = SR.apply_field(cf, ["A"], [pa.array(texts, type=pa.string())])
    want = SR.apply_field(cf, ["A"], [texts])
    assert got.table.equals(want.table)
    ints = SR.apply_field(cf, ["A"], [pa.array([5, None, -3], type=pa.int64())])
    assert ints.value == [Decimal(5), None, Decimal(-3)]
    floats = SR.apply_field(cf, ["A"], [pa.array([1500.0, 2.5], type=pa.float64())])
    assert [str(v) for v in floats.value] == ["1500.0", "2.5"]


def test_many_distinct_tuples_are_evaluated_in_processes_with_the_same_result(monkeypatch: pytest.MonkeyPatch) -> None:
    cf = T.compile_field("n", FIELDS["number"], T.FieldRule(kind="amount"))
    texts = [f"{i:,}.{i % 100:02d}" if i % 7 else f"({i})" for i in range(600)] + ["x", None, "", "12%"]
    alone = SR.apply_field(cf, ["A"], [texts])
    monkeypatch.setattr(SR, "PARALLEL_TUPLES", 10)
    many = SR.apply_field(cf, ["A"], [texts])
    assert many.table.equals(alone.table) and len(many) == len(texts)
