"""The closed transform vocabulary (M5): one test per op and per failure code, the real values named in
vocabulary.md, determinism, bounds, hashing, and "day and month are never guessed"."""

from __future__ import annotations

import datetime as dt
import locale
from decimal import Decimal

import pytest

from worker import setrecipe as SR
from worker import transform_check, transform_core
from worker import transforms as T
from worker.transforms import Context, FieldRule, Row, apply_field, compile_field

D = Decimal


def run(step, cell=None, rule=None, context=None, **others):
    """Apply a one-column field to one cell (other columns by keyword) and return the Outcome."""
    row = Row.of("S!2", {"X": cell, **others})
    return apply_field(compile_field("F", step, rule, context), row, context)


def ops(*o, src="X"):
    return {"from": src, "ops": list(o)}


NUM = {"op": "number_parse", "decimal": ".", "thousands": [","]}


# ---------------------------------------------------------------------------------------- the vocabulary


def test_seventeen_ops_three_sources_sixteen_failure_codes():
    assert len(T.OPS) == 17
    assert transform_check.SOURCES == ("from", "supply", "missing")
    assert len(T.FAILURE_CODES) == len(set(T.FAILURE_CODES)) == 16
    assert set(T.FAILURE_SENTENCES) == set(T.FAILURE_CODES)
    assert set(transform_core.LOAD_CODES) == {"unknown_operator", "wrong_type", "unknown_reference", "out_of_bounds",
                                             "looser_than_control"}


def test_unknown_op_and_unknown_parameter_are_refused():
    with pytest.raises(T.RecipeError) as e:
        compile_field("F", ops({"op": "eval", "code": "1+1"}))
    assert e.value.code == "unknown_operator"
    with pytest.raises(T.RecipeError) as e:
        compile_field("F", ops({"op": "trim", "extra": 1}))
    assert e.value.code == "unknown_operator"


# --------------------------------------------------------------------------------------------- sources


def test_from_reads_the_column_and_matches_headers_after_casefold_and_whitespace():
    out = apply_field(compile_field("F", {"from": "  commit   DATE "}), Row.of("S!2", {"Commit Date": "x1"}))
    assert (out.status, out.value) == ("ok", "x1")


def test_from_occurrence_picks_the_nth_column_of_the_same_name():
    row = Row("S!2", ("LP Number", "LP Number"), ("a", "b"))
    one = compile_field("F", {"from": "LP Number"})
    assert apply_field(one, row).code == "ambiguous_column"
    second = compile_field("F", {"from": {"header": "LP Number", "occurrence": 2}})
    assert apply_field(second, row).value == "b"
    third = compile_field("F", {"from": {"header": "LP Number", "occurrence": 3}})
    assert apply_field(third, row).code == "no_column"


def test_supply_sets_every_row_typed_by_the_field_kind():
    assert run({"supply": {"value": "2025-03-31"}}, rule=FieldRule(kind="date")).value == dt.date(2025, 3, 31)
    assert run({"supply": {"value": "10.50"}}, rule=FieldRule(kind="amount")).value == D("10.50")
    assert run({"supply": {"value": "HJE"}}).value == "HJE"
    with pytest.raises(T.RecipeError) as e:
        compile_field("F", {"supply": {"value": "03/31/2025"}}, FieldRule(kind="date"))
    assert e.value.code == "wrong_type"


def test_missing_source_is_blank_and_reported_missing():
    out = run({"missing": {"reason": "the source has no fund column"}})
    assert (out.status, out.value, out.code) == ("missing", None, None)


def test_blank_cell_is_missing_not_failed():
    for cell in (None, "", "   ", "n/a", "TBD", "--", "-"):
        assert run(ops({"op": "case", "to": "upper"}), cell).status == "missing"


# ------------------------------------------------------------------------------------------------- ops


def test_trim_keeps_inner_spaces_or_collapses_them():
    assert run(ops({"op": "trim"}), "i-001 ").value == "i-001"
    assert run(ops({"op": "trim"}), "  Ruiz-Gutierrez   0004 ").value == "Ruiz-Gutierrez   0004"
    out = run(ops({"op": "trim", "inner": "collapse"}), "  Ruiz-Gutierrez   0004")
    assert out.value == "Ruiz-Gutierrez 0004"


def test_case():
    assert run(ops({"op": "case", "to": "upper"}), "i-001").value == "I-001"
    assert run(ops({"op": "case", "to": "lower"}), "INV0000001").value == "inv0000001"


def test_treat_as_blank_turns_listed_values_into_missing():
    step = ops({"op": "treat_as_blank", "values": ["TBC", "See file", "(blank)"]})
    assert run(step, "tbc").status == "missing"
    assert run(step, " See file ").status == "missing"
    assert run(step, "Reviewed").value == "Reviewed"
    exact = ops({"op": "treat_as_blank", "values": ["TBC"], "ci": False})
    assert run(exact, "tbc").value == "tbc"


def test_strip_prefix_and_suffix_once_and_only_when_present():
    assert run(ops({"op": "strip_prefix", "text": "INV"}), "INV0000018").value == "0000018"
    assert run(ops({"op": "strip_prefix", "text": "INV"}), "0000018").value == "0000018"
    assert run(ops({"op": "strip_prefix", "text": "inv", "ci": True}), "INVINV1").value == "INV1"
    assert run(ops({"op": "strip_suffix", "text": "]"}), "INV0000018]").value == "INV0000018"
    assert run(ops({"op": "strip_suffix", "text": "]"}), "INV0000018").value == "INV0000018"


def test_split_real_values():
    acct = "1000 - Cash and Cash Equivalents"
    assert run(ops({"op": "split", "sep": " - ", "index": 1}), acct).value == "1000"
    assert run(ops({"op": "split", "sep": " - ", "index": 2}), acct).value == "Cash and Cash Equivalents"
    pivot = "Morgan Inc 0022 [INV0000018]"
    assert run(ops({"op": "split", "sep": " [", "index": 1}), pivot).value == "Morgan Inc 0022"
    got = run(ops({"op": "split", "sep": " [", "index": 2}, {"op": "strip_suffix", "text": "]"}), pivot)
    assert got.value == "INV0000018"
    assert run(ops({"op": "split", "sep": " - ", "index": -1}), acct).value == "Cash and Cash Equivalents"


def test_split_with_too_few_parts_fails_split_no_part():
    assert run(ops({"op": "split", "sep": " - ", "index": 3}), "1000 - Cash").code == "split_no_part"
    assert run(ops({"op": "split", "sep": "|", "index": -2}), "one").code == "split_no_part"


def test_join_builds_text_from_columns_literals_and_setup():
    ctx = Context(setup={"entity_id": "E-7"})
    step = ops({"op": "join", "parts": [{"setup": "entity_id"}, {"text": " "}, {"col": "Journal Type"},
                                        {"text": "#"}, {"col": "Batch"}]})
    out = run(step, "x", context=ctx, **{"Journal Type": "HJE", "Batch": 42.0})
    assert out.value == "E-7 HJE#42"


def test_join_with_only_blank_columns_is_missing_never_literals_alone():
    step = ops({"op": "join", "parts": [{"text": "To record "}, "value", {"text": " Commitment"}]})
    assert run(step, None).status == "missing"
    assert run(step, "I-001").value == "To record I-001 Commitment"


def test_join_skip_blank_drops_the_part_and_its_separator():
    step = ops({"op": "join", "parts": ["value", {"col": "B"}, {"col": "C"}], "sep": " | "})
    assert run(step, "a", B=None, C="c").value == "a | c"
    keep = ops({"op": "join", "parts": ["value", {"col": "B"}, {"col": "C"}], "sep": " | ", "skip_blank": False})
    assert run(keep, "a", B=None, C="c").value == "a |  | c"


def test_coalesce_takes_the_first_value_and_flags_the_fallback():
    step = ops({"op": "coalesce", "cols": [{"col": "MEMO"}]}, src="Transaction Comments")
    row = Row.of("S!2", {"Transaction Comments": "", "MEMO": "from memo"})
    out = apply_field(compile_field("F", step), row)
    assert out.value == "from memo" and out.flags == {"fallback"}
    row = Row.of("S!2", {"Transaction Comments": "own", "MEMO": "from memo"})
    out = apply_field(compile_field("F", step), row)
    assert out.value == "own" and not out.flags
    row = Row.of("S!2", {"Transaction Comments": "", "MEMO": ""})
    assert apply_field(compile_field("F", step), row).status == "missing"


def test_regex_extract_group_one_of_the_first_match():
    step = ops({"op": "regex_extract", "pattern": r"^(\d{4})"})
    assert run(step, "1000-01").value == "1000"
    assert run(step, "ab").code == "no_match"
    soft = ops({"op": "regex_extract", "pattern": r"^(\d{4})", "on_miss": "missing"})
    assert run(soft, "ab").status == "missing"
    ci = ops({"op": "regex_extract", "pattern": r"id-(\d+)", "ci": True})
    assert run(ci, "ID-77").value == "77"


def test_regex_extract_refuses_what_is_outside_the_safe_subset():
    unsafe = [r"(a|b)", r"(a)\1", r"(?=a)(b)", r"(a+?)", r"(a)(b)", r"(.)", r"(a+)+", r"((a))", r"(?i)(a)",
              r"(?P<n>a)", r"a", r"(a", r"(a*)*", r"(\d{2,60})", r"(a)\z", r"a(b)++", r"()"]
    for pattern in unsafe:
        with pytest.raises(T.RecipeError) as e:
            compile_field("F", ops({"op": "regex_extract", "pattern": pattern}))
        assert e.value.code in {"unknown_operator", "out_of_bounds"}, pattern
    ok = [r"^(\d{4})", r"^([A-Z]{2}-\d+)$", r"\((\d+)\)", r"id-([a-z0-9]+)", r"^\s*(\d+)$"]
    for pattern in ok:
        compile_field("F", ops({"op": "regex_extract", "pattern": pattern}))


def test_regex_extract_input_over_a_thousand_characters_fails_too_long_to_match():
    step = ops({"op": "regex_extract", "pattern": r"^(\d)"})
    assert run(step, "1" * 1000).value == "1"
    assert run(step, "1" * 1001).code == "too_long_to_match"


def test_truncate_cuts_and_flags_every_cut_row():
    step = ops({"op": "truncate", "max": 5})
    cut = run(step, "abcdefgh")
    assert cut.value == "abcde" and cut.flags == {"truncated"}
    short = run(step, "abc")
    assert short.value == "abc" and not short.flags


def test_truncate_is_refused_on_an_id_or_key_field():
    with pytest.raises(T.RecipeError) as e:
        compile_field("Investor_ID", ops({"op": "truncate", "max": 20}))
    assert e.value.code == "looser_than_control"
    with pytest.raises(T.RecipeError) as e:
        compile_field("Code", ops({"op": "truncate", "max": 20}), FieldRule(is_key=True))
    assert e.value.code == "looser_than_control"
    compile_field("Description", ops({"op": "truncate", "max": 40}))


# ------------------------------------------------------------------------------------------------ dates

DATE = FieldRule(kind="date")


def dp(*patterns, pivot=None):
    op = {"op": "date_parse", "patterns": list(patterns)}
    if pivot is not None:
        op["yy_pivot"] = pivot
    return ops(op)


def test_date_parse_real_values_from_the_vocabulary():
    assert run(dp("YYYY-MM-DD"), "2021-08-03", DATE).value == dt.date(2021, 8, 3)
    both = dp("DD-Mon-YYYY", "YYYY-MM-DD")
    assert run(both, "01-Jun-2020", DATE).value == dt.date(2020, 6, 1)
    assert run(both, "2020-06-01", DATE).value == dt.date(2020, 6, 1)
    two = dp("DD/MM/YYYY", "MM/DD/YY", pivot=50)
    assert run(two, "01/06/2020", DATE).value == dt.date(2020, 6, 1)
    assert run(two, "06/01/20", DATE).value == dt.date(2020, 6, 1)
    mixed = dp("MM/DD/YY", "DD/MM/YYYY", "DD-Mon-YYYY", pivot=50)
    assert run(mixed, "02/24/15", DATE).value == dt.date(2015, 2, 24)
    assert run(mixed, "23/12/2016", DATE).value == dt.date(2016, 12, 23)
    assert run(mixed, "24-Jul-2014", DATE).value == dt.date(2014, 7, 24)
    assert run(mixed, "11/04/2018", DATE).value == dt.date(2018, 4, 11)  # 11 April, as the person stated
    assert run(dp("M/D/YYYY"), "5/11/2022", DATE).value == dt.date(2022, 5, 11)
    assert run(dp("M/D/YYYY"), "12/14/2020", DATE).value == dt.date(2020, 12, 14)


def test_yy_pivot_is_required_and_decides_the_century_without_a_clock():
    with pytest.raises(T.RecipeError) as e:
        compile_field("F", dp("MM/DD/YY"), DATE)
    assert e.value.code == "unknown_operator"
    assert run(dp("MM/DD/YY", pivot=50), "02/24/49", DATE).value == dt.date(2049, 2, 24)
    assert run(dp("MM/DD/YY", pivot=50), "02/24/50", DATE).value == dt.date(1950, 2, 24)
    assert run(dp("MM/DD/YY", pivot=0), "02/24/00", DATE).value == dt.date(1900, 2, 24)


def test_month_names_full_or_short_any_case():
    step = dp("Mon DD, YYYY")
    assert run(step, "JANUARY 05, 2021", DATE).value == dt.date(2021, 1, 5)
    assert run(step, "sep 05, 2021", DATE).value == dt.date(2021, 9, 5)
    assert run(step, "Sept 05, 2021", DATE).code == "not_a_date"


def test_a_date_cell_passes_as_is():
    assert run(dp("YYYY-MM-DD"), dt.datetime(2021, 8, 3), DATE).value == dt.date(2021, 8, 3)
    assert run(dp("YYYY-MM-DD"), dt.date(2021, 8, 3), DATE).value == dt.date(2021, 8, 3)


def test_failure_not_a_date_for_text_and_impossible_dates():
    assert run(dp("DD/MM/YYYY"), "hello", DATE).code == "not_a_date"
    assert run(dp("DD/MM/YYYY"), "31/02/2024", DATE).code == "not_a_date"
    assert run(dp("DD/MM/YYYY"), "2024-02-01", DATE).code == "not_a_date"
    assert run(dp("DD/MM/YYYY"), "1/2/2024", DATE).code == "not_a_date"  # DD needs exactly two digits


def test_failure_date_has_time():
    assert run(dp("YYYY-MM-DD"), dt.datetime(2021, 8, 3, 14, 30), DATE).code == "date_has_time"


def test_failure_number_not_date_excel_serials_are_not_read():
    assert run(dp("YYYY-MM-DD"), 44409, DATE).code == "number_not_date"
    assert run(dp("YYYY-MM-DD"), 44409.5, DATE).code == "number_not_date"


def test_day_and_month_are_never_guessed():
    """A value that reads two ways is asked. Order of the patterns and the other values in the column do not
    decide it, and the option set never recommends one."""
    for patterns in (("MM/DD/YYYY", "DD/MM/YYYY"), ("DD/MM/YYYY", "MM/DD/YYYY")):
        out = run(dp(*patterns), "11/04/2018", DATE)
        assert (out.status, out.code, out.value) == ("failed", "ambiguous_date", None)
    # unambiguous under both patterns: both give the same day, so it is fine
    assert run(dp("MM/DD/YYYY", "DD/MM/YYYY"), "05/05/2018", DATE).value == dt.date(2018, 5, 5)
    # only one pattern can read it, so there is nothing to guess
    assert run(dp("MM/DD/YYYY", "DD/MM/YYYY"), "23/12/2016", DATE).value == dt.date(2016, 12, 23)

    column = ["11/04/2018", "23/12/2016", "13/01/2019", "24/07/2014"]  # the evidence points day-first
    options = T.date_options(column, day_first=["DD/MM/YYYY"], month_first=["MM/DD/YYYY"])
    assert options["recommended"] is None
    assert [o["id"] for o in options["options"]] == ["day_first", "month_first"]
    assert options["options"][0]["preview"] == {"conform": 4, "total": 4, "failed": {}}
    assert options["options"][1]["preview"]["conform"] == 1
    assert options["evidence"]["first_number_above_12"] == 3
    # the evidence is shown, and even a column with no ambiguity at all gets no recommendation
    assert T.date_options(["11/04/2018"], day_first=["DD/MM/YYYY"], month_first=["MM/DD/YYYY"])["recommended"] is None


def test_date_parse_bounds():
    with pytest.raises(T.RecipeError) as e:
        compile_field("F", dp("YYYY-MM-DD", "DD/MM/YYYY", "MM/DD/YYYY", "D/M/YYYY", "YYYY/MM/DD"), DATE)
    assert e.value.code == "out_of_bounds"
    with pytest.raises(T.RecipeError):
        compile_field("F", dp("YYYY-MM"), DATE)  # no day token


# ---------------------------------------------------------------------------------------------- numbers

AMT = FieldRule(kind="amount")


def num(**kw):
    return ops({"op": "number_parse", "decimal": ".", **kw})


def test_number_parse_real_values_from_the_vocabulary():
    assert run(ops(NUM), "46,418.20", AMT).value == D("46418.20")
    assert run(num(percent="as_fraction"), "11.43%", AMT).value == D("0.1143")
    assert run(num(percent="as_number"), "11.43%", AMT).value == D("11.43")
    assert run(ops(NUM), "1,234,567", AMT).value == D("1234567")
    assert run(num(thousands=[","], negative=["minus", "brackets"], currency=["$"]), "($1,250.00)", AMT
               ).value == D("-1250.00")
    assert run(num(currency=["USD", "$"]), "USD 40.50", AMT).value == D("40.50")
    assert run(num(currency=["USD"]), "40.50 USD", AMT).value == D("40.50")
    assert run(ops(NUM), "-40996.5", AMT).value == D("-40996.5")
    assert run(num(negative=["trailing_minus"]), "40.00-", AMT).value == D("-40.00")


def test_number_cells_are_decimals_through_the_shortest_repr():
    assert run(ops(NUM), 40996.5, AMT).value == D("40996.5")
    assert run(ops(NUM), 1234.565, AMT).value == D("1234.565")
    assert run(ops(NUM), 88180, AMT).value == D("88180")


def test_decimal_mark_is_never_guessed():
    assert run(num(), "1.234", AMT).value == D("1.234")
    assert run(ops({"op": "number_parse", "decimal": ",", "thousands": ["."]}), "1.234", AMT).value == D("1234")
    assert run(ops({"op": "number_parse", "decimal": ",", "thousands": [" "]}), "1 234,50", AMT).value == D("1234.50")
    # an undeclared mark is refused, never read in the other way
    assert run(num(), "1,234", AMT).code == "not_a_number"
    assert run(ops({"op": "number_parse", "decimal": ","}), "1.234", AMT).code == "not_a_number"


def test_failure_not_a_number():
    for bad in ("abc", "1e5", "1.2.3", "12a", "1,234.5.6", "$5", "(5)", "5.", ".5", "1 2"):
        assert run(ops(NUM), bad, AMT).code == "not_a_number", bad


def test_failure_bad_grouping():
    for bad in ("1,23,456", "1234,567", "1,2345", ",123"):
        assert run(ops(NUM), bad, AMT).code == "bad_grouping", bad


def test_failure_percent_not_allowed():
    assert run(ops(NUM), "11.43%", AMT).code == "percent_not_allowed"


def test_failure_wrong_type_for_a_bool_cell():
    assert run(ops(NUM), True, AMT).code == "wrong_type"


def test_thousands_must_not_contain_the_decimal_mark():
    with pytest.raises(T.RecipeError) as e:
        compile_field("F", ops({"op": "number_parse", "decimal": ".", "thousands": ["."]}), AMT)
    assert e.value.code == "wrong_type"


def test_negate_flips_the_sign_exactly():
    step = ops(NUM, {"op": "negate"})
    assert run(step, "46,418.20", AMT).value == D("-46418.20")
    assert run(step, "-5", AMT).value == D("5")
    zero = run(step, "0", AMT).value
    assert zero == 0 and not zero.is_signed()
    big = "123456789012345678901234567890.123456789"
    assert run(step, big, AMT).value == D("-" + big)  # no context rounding


def test_debit_credit_nets_debit_minus_credit():
    step = {"from": "Debit", "ops": [{"op": "debit_credit", "debit": {"col": "Debit"}, "credit": {"col": "Credit"},
                                      "number": {"decimal": ".", "thousands": [","]}}]}
    c = compile_field("F", step, AMT)

    def got(d, cr):
        return apply_field(c, Row.of("S!2", {"Debit": d, "Credit": cr}))

    assert got(0, 88180).value == D("-88180")
    assert got(None, 40996.5).value == D("-40996.5")
    assert got("1,250.00", None).value == D("1250.00")
    assert got(100, 40).value == D("60")  # both sides: net, as today
    assert got(None, None).status == "missing"
    assert got("x", 5).code == "not_a_number"
    assert got(5, "x").code == "not_a_number"


def test_debit_credit_both_sides_fail_when_asked():
    step = {"from": "Debit", "ops": [{"op": "debit_credit", "debit": {"col": "Debit"}, "credit": {"col": "Credit"},
                                      "number": {"decimal": "."}, "both_sides": "fail"}]}
    c = compile_field("F", step, AMT)
    assert apply_field(c, Row.of("S!2", {"Debit": 1, "Credit": 2})).code == "both_sides"
    assert apply_field(c, Row.of("S!2", {"Debit": 1, "Credit": 0})).value == D("1")


# ------------------------------------------------------------------------------------------ crosswalk


def test_crosswalk_pairs_are_exact_never_fuzzy():
    step = ops({"op": "crosswalk", "pairs": {"Fund A": "F-001", "Institutional": "INST"}})
    assert run(step, " fund a ").value == "F-001"
    assert run(step, "Fund AA").code == "not_in_crosswalk"
    assert run(step, "Fund  A").code == "not_in_crosswalk"
    exact = ops({"op": "crosswalk", "pairs": {"Fund A": "F-001"}, "match": "exact"})
    assert run(exact, "fund a").code == "not_in_crosswalk"
    soft = ops({"op": "crosswalk", "pairs": {"Fund A": "F-001"}, "on_miss": "missing"})
    assert run(soft, "Fund B").status == "missing"


def test_crosswalk_keep_does_not_exist():
    with pytest.raises(T.RecipeError):
        compile_field("F", ops({"op": "crosswalk", "pairs": {"a": "b"}, "on_miss": "keep"}))


def test_crosswalk_table_with_a_pinned_checksum_and_blank_means_no_match():
    ctx = Context(tables={"country-codes": {"code": {"US": "US", "United States": "US", "Other": ""}}},
                  table_checksums={"country-codes": "c1"})
    step = ops({"op": "crosswalk", "table": "country-codes", "output": "code"})
    assert run(step, "united states", context=ctx).value == "US"
    assert run(step, "Other", context=ctx).code == "not_in_crosswalk"
    with pytest.raises(T.RecipeError) as e:
        compile_field("F", step)
    assert e.value.code == "unknown_reference"


def test_crosswalk_records_and_ambiguous_match():
    recs = [{"id": "INV-1", "key": "i-001", "name": "A"}, {"id": "INV-2", "key": "i-002", "name": "B"},
            {"id": "INV-3", "key": "i-003", "name": "A"}]
    ctx = Context(records={"Investors": recs}, records_checksums={"Investors": "r1"})
    by_key = ops({"op": "crosswalk", "records": "Investors", "match_on": "key", "take": "id"})
    assert run(by_key, "I-002", context=ctx).value == "INV-2"
    assert run(by_key, "i-009", context=ctx).code == "not_in_crosswalk"
    by_name = ops({"op": "crosswalk", "records": "Investors", "match_on": "name", "take": "id"})
    assert run(by_name, "A", context=ctx).code == "ambiguous_match"
    assert run(by_name, "B", context=ctx).value == "INV-2"
    with pytest.raises(T.RecipeError) as e:
        compile_field("F", by_key, context=Context())
    assert e.value.code == "unknown_reference"


# ------------------------------------------------------------------------------------- when and default


def test_when_text_comparisons_are_exact():
    def step(cmp, right, then="keep", other="blank"):
        return ops({"op": "when", "test": {"left": "value", "cmp": cmp, "right": right}, "then": then,
                    "else": other})

    assert run(step("eq", "Yes"), "Yes").value == "Yes"
    assert run(step("eq", "Yes"), "yes").status == "missing"  # no ci: put `case` first
    assert run(step("ne", "Yes"), "No").value == "No"
    assert run(step("in", ["a", "b"]), "b").value == "b"
    assert run(step("not_in", ["a", "b"]), "c").value == "c"
    assert run(step("starts_with", "1000"), "1000-01").value == "1000-01"
    assert run(step("eq", "x", then={"text": "HIT"}, other={"text": "MISS"}), "y").value == "MISS"


def test_when_blank_left_is_false_except_blank_ne_and_not_in():
    def step(cmp, right=None):
        t = {"left": "value", "cmp": cmp}
        if right is not None:
            t["right"] = right
        return ops({"op": "when", "test": t, "then": {"text": "T"}, "else": {"text": "F"}})

    assert run(step("eq", "a"), None).value == "F"
    assert run(step("starts_with", "a"), None).value == "F"
    assert run(step("ne", "a"), None).value == "T"
    assert run(step("not_in", ["a"]), None).value == "T"
    assert run(step("blank"), None).value == "T"
    assert run(step("not_blank"), None).value == "F"


def test_when_on_another_column_and_a_setup_branch():
    step = ops({"op": "when", "test": {"left": {"col": "Type"}, "cmp": "eq", "right": "Fee"}, "then":
                {"setup": "fund_id"}, "else": "keep"})
    ctx = Context(setup={"fund_id": "F-9"})
    assert run(step, "a", context=ctx, Type="Fee").value == "F-9"
    assert run(step, "a", context=ctx, Type="Other").value == "a"


def test_when_order_comparisons_split_increase_and_decrease():
    inc = ops(NUM, {"op": "when", "test": {"left": "value", "cmp": "gt", "right": "0"}, "then": "keep",
                    "else": "blank"})
    dec = ops(NUM, {"op": "when", "test": {"left": "value", "cmp": "lt", "right": "0"}, "then": "keep",
                    "else": "blank"}, {"op": "negate"})
    assert run(inc, "1,500.00", AMT).value == D("1500.00")
    assert run(inc, "-20", AMT).status == "missing"
    assert run(dec, "-20", AMT).value == D("20")
    assert run(dec, "1,500.00", AMT).status == "missing"
    on_date = ops({"op": "date_parse", "patterns": ["YYYY-MM-DD"]},
                  {"op": "when", "test": {"left": "value", "cmp": "ge", "right": "2021-01-01"}, "then": "keep",
                   "else": "blank"})
    assert run(on_date, "2021-08-03", DATE).value == dt.date(2021, 8, 3)
    assert run(on_date, "2020-08-03", DATE).status == "missing"


def test_when_order_comparison_on_text_is_wrong_type():
    with pytest.raises(T.RecipeError) as e:
        compile_field("F", ops({"op": "when", "test": {"left": "value", "cmp": "gt", "right": "0"},
                                "then": "keep", "else": "blank"}))
    assert e.value.code == "wrong_type"


def test_default_fills_blank_only_and_flags_the_row():
    step = ops({"op": "default", "value": {"text": "HJE"}}, src="Journal Type")
    filled = apply_field(compile_field("F", step), Row.of("S!2", {"Journal Type": ""}))
    assert filled.value == "HJE" and filled.flags == {"defaulted"}
    kept = apply_field(compile_field("F", step), Row.of("S!2", {"Journal Type": "ADJ"}))
    assert kept.value == "ADJ" and not kept.flags


def test_default_does_not_cover_a_failed_row():
    step = ops(NUM, {"op": "default", "value": {"text": "0"}})
    assert run(step, "abc", AMT).code == "not_a_number"
    assert run(step, None, AMT).value == D("0")


def test_default_must_be_canonical_for_the_type():
    with pytest.raises(T.RecipeError) as e:
        compile_field("F", ops(NUM, {"op": "default", "value": {"text": "1,5"}}), AMT)
    assert e.value.code == "wrong_type"


# ------------------------------------------------------------------------------------ type checking


def test_negate_before_number_parse_is_wrong_type():
    with pytest.raises(T.RecipeError) as e:
        compile_field("F", ops({"op": "negate"}, NUM), AMT)
    assert e.value.code == "wrong_type"


def test_final_type_must_match_the_field_kind():
    with pytest.raises(T.RecipeError) as e:
        compile_field("Amount", ops({"op": "trim"}), AMT)  # text into an amount
    assert e.value.code == "wrong_type"
    with pytest.raises(T.RecipeError):
        compile_field("When", ops({"op": "trim"}), DATE)  # text into a date
    with pytest.raises(T.RecipeError):
        compile_field("Code", ops({"op": "date_parse", "patterns": ["YYYY-MM-DD"]}))  # date into text
    compile_field("Code", ops(NUM))  # a number arriving at a text field is allowed


def test_a_number_at_a_text_field_is_written_as_canonical_decimal_text():
    assert run(ops(NUM), "1,000.50").value == "1000.5"
    assert run({"from": "X"}, 1000.0).value == "1000"


def test_text_ops_after_a_number_see_canonical_text():
    assert run(ops(NUM, {"op": "split", "sep": ".", "index": 1}), "1,000.50").value == "1000"


def test_date_parse_after_number_parse_is_wrong_type():
    with pytest.raises(T.RecipeError) as e:
        compile_field("F", ops(NUM, {"op": "date_parse", "patterns": ["YYYY-MM-DD"]}), DATE)
    assert e.value.code == "wrong_type"


def test_when_branches_must_agree_on_a_type():
    with pytest.raises(T.RecipeError) as e:
        compile_field("F", ops(NUM, {"op": "when", "test": {"left": "value", "cmp": "gt", "right": "0"},
                                     "then": "keep", "else": {"text": "none"}}), AMT)
    assert e.value.code == "wrong_type"


# ------------------------------------------------------------------------------------------- bounds


def test_bounds_are_refused_as_out_of_bounds():
    def refused(step, name="F"):
        with pytest.raises(T.RecipeError) as e:
            compile_field(name, step)
        assert e.value.code == "out_of_bounds"

    refused(ops(*[{"op": "trim"}] * 9))
    compile_field("F", ops(*[{"op": "trim"}] * 8))
    refused(ops({"op": "join", "parts": [{"text": "a"}] * 9}))
    refused(ops({"op": "join", "parts": [{"col": f"C{i}"} for i in range(7)]}))  # 7 column operands
    refused(ops({"op": "treat_as_blank", "values": [str(i) for i in range(21)]}))
    refused(ops({"op": "crosswalk", "pairs": {str(i): "x" for i in range(501)}}))
    refused(ops({"op": "strip_prefix", "text": "x" * 201}))
    refused({"from": "X", "note": "x" * 201})
    refused(ops({"op": "regex_extract", "pattern": "(" + "a" * 100 + ")"}))
    refused(ops({"op": "regex_extract", "pattern": r"(\d?\d?\d?\d?\d?\d?\d?)"}))  # 7 quantifiers


def test_unknown_setup_key_is_unknown_reference():
    with pytest.raises(T.RecipeError) as e:
        compile_field("F", ops({"op": "join", "parts": [{"setup": "not_a_key"}]}))
    assert e.value.code == "unknown_reference"


def test_a_source_takes_ops_only_with_from():
    with pytest.raises(T.RecipeError):
        compile_field("F", {"supply": {"value": "x"}, "ops": [{"op": "trim"}]})
    with pytest.raises(T.RecipeError):
        compile_field("F", {"from": "X", "missing": {"reason": "r"}})


# ----------------------------------------------------------------------------------------- explanation


def test_every_op_has_a_fixed_sentence_filled_from_its_parameters():
    cases = [
        (ops({"op": "trim"}), "Spaces trimmed."),
        (ops({"op": "trim", "inner": "collapse"}), "Spaces trimmed and repeated inner spaces made single."),
        (ops({"op": "case", "to": "upper"}), "Made upper case."),
        (ops({"op": "treat_as_blank", "values": ["TBC"]}), "«TBC» treated as no value."),
        (ops({"op": "strip_prefix", "text": "INV"}), "Leading «INV» removed."),
        (ops({"op": "strip_suffix", "text": "]"}), "Trailing «]» removed."),
        (ops({"op": "split", "sep": " - ", "index": 1}), "Part 1 of the value split at « - »."),
        (ops({"op": "negate"}), "Sign reversed."),
        (ops({"op": "truncate", "max": 40}), "Cut to 40 characters (each cut row is flagged)."),
        (ops({"op": "regex_extract", "pattern": r"^(\d{4})"}), "The part matching «^(\\d{4})»."),
    ]
    for step, sentence in cases:
        assert T.explain_op(step["ops"][0]) == sentence
    assert T.explain_op({"op": "date_parse", "patterns": ["MM/DD/YY", "DD/MM/YYYY"], "yy_pivot": 50}) == (
        "Read as a date written DD/MM/YYYY or MM/DD/YY, two-digit years below 50 as 20YY; a value that reads "
        "two ways is asked, not guessed.")
    assert T.explain_op(NUM | {"percent": "as_fraction", "negative": ["minus", "brackets"],
                               "currency": ["$"]}) == (
        "Read as a number with «.» for decimals, «,» between thousands, (brackets) as "
        "negative, ignoring $, percent as a fraction.")
    assert T.explain_op({"op": "join", "parts": [{"text": "To record "}, {"col": "Entity"}]}) == (
        "Built as «To record », column «Entity» joined with «».")
    assert T.explain_op({"op": "coalesce", "cols": [{"col": "MEMO"}]}) == (
        "The first of the current value, then column «MEMO» that has a value.")
    assert T.explain_op({"op": "debit_credit", "debit": {"col": "Dr"}, "credit": {"col": "Cr"},
                         "number": {"decimal": "."}, "both_sides": "fail"}) == (
        "Debit «Dr» minus credit «Cr»; a blank side counts as zero; a line with both sides "
        "is refused.")
    assert T.explain_op({"op": "crosswalk", "pairs": {"a": "b"}}) == (
        "Looked up in the sponsor's list of 1 pairs, ignoring case; a value not found is refused.")
    assert T.explain_op({"op": "when", "test": {"left": "value", "cmp": "gt", "right": "0"}, "then": "keep",
                         "else": "blank"}) == (
        "If the value is above «0», then the value is kept; otherwise it is left blank.")
    assert T.explain_op({"op": "default", "value": {"text": "HJE"}}, Context(approver="Ana")) == (
        "Where blank, set to «HJE» (supplied by Ana; each such row is flagged).")


def test_a_field_reads_as_the_source_sentence_then_each_op_joined_by_then():
    step = {"from": {"header": "LP Number", "occurrence": 2}, "ops": [{"op": "trim"}, {"op": "case", "to": "upper"}]}
    assert T.explain_field(step) == (
        "Taken from column «LP Number», the 2nd one with that name; then Spaces trimmed; then Made upper "
        "case.")
    assert T.explain_field({"supply": {"value": "2025-03-31"}}, Context(approver="Ana")) == (
        "Set to «2025-03-31» on every row, as supplied by Ana; not from the source.")
    assert T.explain_field({"missing": {"reason": "no fund column"}}) == (
        "Left blank on purpose: no fund column. Reported as missing on every row.")
    for code in T.FAILURE_CODES:
        assert T.FAILURE_SENTENCES[code].endswith(".")


# --------------------------------------------------------------------------------------------- hashing


def test_hashes_are_stable_and_ignore_the_note_and_unordered_lists():
    a = {"from": "X", "ops": [{"op": "date_parse", "patterns": ["DD/MM/YYYY", "MM/DD/YY"], "yy_pivot": 50}],
         "note": "first"}
    b = {"from": {"header": "X"}, "ops": [{"op": "date_parse", "patterns": ["MM/DD/YY", "DD/MM/YYYY", "MM/DD/YY"],
                                           "yy_pivot": 50}], "note": "other"}
    assert T.field_hash("F", a, DATE) == T.field_hash("F", b, DATE)
    c = {"from": "X", "ops": [{"op": "date_parse", "patterns": ["DD/MM/YYYY"], "yy_pivot": 50}]}
    assert T.field_hash("F", a, DATE) != T.field_hash("F", c, DATE)
    assert len(T.field_hash("F", a, DATE)) == 64
    assert T.field_hash("F", a, DATE) == T.field_hash("F", a, DATE)


def _op_hash(op, context=None):
    return T.field_hash("F", {"from": "X", "ops": [op]}, None, context)


def test_op_hash_fills_defaults_and_is_order_sensitive_for_join_parts():
    assert _op_hash({"op": "trim"}) == _op_hash({"op": "trim", "inner": "keep"})
    assert _op_hash({"op": "trim"}) != _op_hash({"op": "trim", "inner": "collapse"})
    j1 = {"op": "join", "parts": [{"col": "A"}, {"col": "B"}]}
    j2 = {"op": "join", "parts": [{"col": "B"}, {"col": "A"}]}
    assert _op_hash(j1) != _op_hash(j2)
    assert _op_hash(NUM | {"thousands": [",", " "]}) == _op_hash(NUM | {"thousands": [" ", ","]})
    # literal text is not trimmed or normalised
    assert _op_hash({"op": "strip_prefix", "text": "x"}) != _op_hash({"op": "strip_prefix", "text": "x "})


def test_crosswalk_op_hash_folds_in_the_table_pin():
    op = {"op": "crosswalk", "table": "t", "output": "o"}
    a = Context(tables={"t": {"o": {"k": "v"}}}, table_checksums={"t": "aaa"})
    b = Context(tables={"t": {"o": {"k": "v"}}}, table_checksums={"t": "bbb"})
    assert _op_hash(op, a) != _op_hash(op, b)
    assert _op_hash(op, a) == _op_hash(op, a)


def test_recipe_hash_ignores_approval_fields_and_notes_but_not_the_pipeline():
    base = {"schema_version": 1, "id": "r1", "sponsor": "sponsor-a",
            "fields": {"Investor_ID": {"from": "LP", "ops": [{"op": "trim"}], "note": "a"}}}
    other = {**base, "approved_by": "Ana", "approved_at": "2026-10-11", "reason": "ok",
             "fields": {"Investor_ID": {"from": "LP", "ops": [{"op": "trim"}], "note": "b"}}}
    changed = {**base, "fields": {"Investor_ID": {"from": "LP", "ops": [{"op": "case", "to": "upper"}]}}}
    assert T.recipe_hash(base) == T.recipe_hash(other)
    assert T.recipe_hash(base) != T.recipe_hash(changed)
    assert T.recipe_hash(base) != T.recipe_hash({**base, "sponsor": "sponsor-b"})


def test_canonical_json_is_sorted_compact_utf8_with_numbers_as_strings():
    assert T.canonical({"b": [1, 2.5], "a": "café", "c": None, "d": True}) == (
        '{"a":"café","b":["1","2.5"],"c":null,"d":true}')


# ------------------------------------------------------------------------------------- determinism


def test_no_clock_no_locale_same_input_same_output(monkeypatch):
    step = ops({"op": "date_parse", "patterns": ["DD-Mon-YYYY"]})
    cases = [("24-Jul-2014", dt.date(2014, 7, 24)), ("24-JUL-2014", dt.date(2014, 7, 24))]
    before = [run(step, v, DATE).value for v, _ in cases]
    saved = locale.setlocale(locale.LC_ALL)
    try:
        locale.setlocale(locale.LC_ALL, "de_DE.UTF-8")
    except locale.Error:
        pass
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    try:
        after = [run(step, v, DATE).value for v, _ in cases]
    finally:
        locale.setlocale(locale.LC_ALL, saved)
    assert before == after == [d for _, d in cases]
    assert run(ops(NUM), "46,418.20", AMT).value == D("46418.20")
    assert run(ops({"op": "case", "to": "upper"}), "straße").value == "STRASSE"


def test_ops_never_read_another_row_or_the_clock():
    c = compile_field("F", dp("MM/DD/YY", pivot=50), DATE)
    row = Row.of("S!2", {"X": "02/24/15"})
    first = apply_field(c, row)
    for _ in range(3):
        assert apply_field(c, row) == first
    assert first.value == dt.date(2015, 2, 24)


# ------------------------------------------------------------------------------- MISSING vs FAILED


def test_missing_means_the_source_has_nothing_failed_means_it_could_not_be_read():
    step = ops(NUM)
    missing = run(step, "", AMT)
    failed = run(step, "n/a?", AMT)
    assert (missing.status, missing.code, missing.value) == ("missing", None, None)
    assert (failed.status, failed.code, failed.value) == ("failed", "not_a_number", None)
    assert failed.source == "n/a?"  # the original cell is kept in the failure, never in the value


def test_no_column_fails_every_row():
    out = run({"from": "Commitment Date"})
    assert (out.status, out.code) == ("failed", "no_column")


# --------------------------------------------------------------------------------------------- verify


def test_verify_counts_conforming_and_returns_up_to_five_failing_examples_in_source_order():
    recipe = {"schema_version": 1, "id": "r", "sponsor": "sponsor-a", "fields": {
        "Commitment_Date": {"from": "Commit Date", "ops": [{"op": "date_parse", "patterns": ["MM/DD/YYYY"]}]},
        "Amount": {"from": "Amt", "ops": [NUM]},
    }}
    rows = [Row.of(f"S!{n}", {"Commit Date": d, "Amt": a}) for n, (d, a) in enumerate([
        ("02/24/2015", "1,000"), ("bad", "2,000"), ("03/01/2015", ""), ("13/13/2015", "x"), ("zz", "3"),
        ("yy", "4"), ("ww", "5"), ("01/01/2016", "1,23,4"), ("vv", "7")], start=2)]
    rules = {"Commitment_Date": FieldRule(kind="date", required=True), "Amount": FieldRule(kind="amount")}
    out = SR.verify(recipe, rows, rules)
    date_v, amt_v = out["Commitment_Date"], out["Amount"]
    assert (date_v.total, date_v.conform) == (9, 3)
    assert date_v.failed == {"not_a_date": 6}
    assert len(date_v.examples) == 5
    assert [e["src"] for e in date_v.examples] == ["S!3", "S!5", "S!6", "S!7", "S!8"]
    assert date_v.examples[0] == {"src": "S!3", "source_value": "bad", "result": None, "reason": "not_a_date"}
    assert not date_v.clean
    assert (amt_v.total, amt_v.conform, amt_v.missing) == (9, 7, 1)  # optional + blank conforms
    assert amt_v.failed == {"not_a_number": 1, "bad_grouping": 1}
    assert amt_v.field_hash == T.compile_recipe(recipe, rules).fields["Amount"].field_hash


def test_verify_rules_required_blank_length_reference_and_key_uniqueness():
    recipe = {"schema_version": 1, "id": "r", "sponsor": "sponsor-a", "fields": {
        "Req": {"from": "A"}, "Code": {"from": "B"}, "Ref": {"from": "C"}, "Key": {"from": "D"}}}
    rules = {"Req": FieldRule(required=True), "Code": FieldRule(is_id=True, max_len=5),
             "Ref": FieldRule(reference=frozenset({"x", "y"})), "Key": FieldRule(is_key=True)}
    rows = [Row.of("S!2", {"A": "v", "B": "abc", "C": "x", "D": "k1"}),
            Row.of("S!3", {"A": "", "B": "abcdef", "C": "z", "D": "k1"}),
            Row.of("S!4", {"A": "v", "B": "", "C": "y", "D": "k2"})]
    out = SR.verify(recipe, rows, rules)
    assert (out["Req"].conform, out["Req"].missing, out["Req"].examples[0]["reason"]) == (2, 1, "required_blank")
    assert (out["Code"].conform, out["Code"].examples[0]["reason"]) == (2, "id_too_long")
    assert (out["Ref"].conform, out["Ref"].examples[0]["reason"]) == (2, "not_in_reference")
    assert (out["Key"].conform, out["Key"].examples[0]["reason"]) == (2, "duplicate_key")


def test_verify_flags_truncated_defaulted_and_fallback_rows():
    recipe = {"schema_version": 1, "id": "r", "sponsor": "sponsor-a", "fields": {
        "Desc": {"from": "A", "ops": [{"op": "coalesce", "cols": [{"col": "B"}]}, {"op": "truncate", "max": 3},
                                      {"op": "default", "value": {"text": "none"}}]}}}
    rows = [Row.of("S!2", {"A": "abcdef", "B": ""}), Row.of("S!3", {"A": "", "B": "xy"}),
            Row.of("S!4", {"A": "", "B": ""})]
    v = SR.verify(recipe, rows)["Desc"]
    assert v.flagged == {"fallback": 1, "truncated": 1, "defaulted": 1}
    assert v.conform == v.total == 3


def test_verify_a_missing_column_fails_every_row_with_no_column():
    recipe = {"schema_version": 1, "id": "r", "sponsor": "sponsor-a",
              "fields": {"Commitment_Date": {"from": "Closing Date", "ops": [
                  {"op": "date_parse", "patterns": ["YYYY-MM-DD"]}]}}}
    rows = [Row.of(f"S!{n}", {"Commit Date": "2021-08-03"}) for n in (2, 3)]
    v = SR.verify(recipe, rows)["Commitment_Date"]
    assert (v.total, v.conform, v.failed) == (2, 0, {"no_column": 2})
