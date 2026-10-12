"""Step 2 of ticket 11 (no container): what the host decides about a layout program's output, and the order of the
audit rows around a run. Real fixtures: `messy/investors_horizontal.xlsx` (transposed) and `investors_pivot.xlsx`
(fill-down, subtotal rows, a grand total). The programs are canned outputs, answered by a FakeRunner."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from port_fakes import FakeRunner
from worker.audit import AuditError
from worker.engine import Refused
from worker.flex import verify as V
from worker.flex.derive import run_layout
from worker.flex.grid import cell_index, grid_from_rows, grid_from_workbook, layout_sig, ref
from worker.flex.runner import RunResult

FIX = Path(__file__).parent / "fixtures" / "messy"
PROGRAM = "def build(book):\n    return {}\n"


def out(columns: list[tuple[str, str]], rows: list[list[Any]], dropped: list[dict] | None = None,
        case: str = "other") -> bytes:
    """A program output: `rows` hold refs (str), None for blank, or (ref, sep, index) for a part."""
    def cell(c: Any) -> dict:
        if c is None:
            return {"blank": True}
        if isinstance(c, tuple):
            return {"ref": c[0], "part": {"sep": c[1], "index": c[2]}}
        return {"ref": c}
    doc = {"columns": [{"name": n, "role": r} for n, r in columns],
           "rows": [{"cells": [cell(c) for c in row]} for row in rows], "dropped": dropped or [], "case": case}
    return json.dumps(doc).encode()


def drop(r: str, reason: str, covers: list[str] | None = None) -> dict:
    return {"ref": r, "reason": reason, **({"covers": covers} if covers else {})}


def codes(v: V.Verified, kind: str = "blocks") -> list[str]:
    return [i["code"] for i in getattr(v, kind)]


def small() -> dict:
    return grid_from_rows({"S": [
        ["Region", "Q1", "Q2"],
        ["North", 100, 50],
        ["South", 200, 25],
        ["Total", 300, 75],
    ]})


def small_ok() -> tuple[list, list, list]:
    cols = [("Region", "dimension"), ("Quarter", "dimension"), ("Amount", "measure")]
    rows = [["S!A2", "S!B1", "S!B2"], ["S!A2", "S!C1", "S!C2"], ["S!A3", "S!B1", "S!B3"], ["S!A3", "S!C1", "S!C3"]]
    dropped = [drop("S!A1", "header"), drop("S!A4", "subtotal"),
               drop("S!B4", "subtotal", ["S!B2", "S!B3"]), drop("S!C4", "subtotal", ["S!C2", "S!C3"])]
    return cols, rows, dropped


# ---- the happy path, and the host copying the values ------------------------------------------------------
def test_a_clean_program_passes_and_the_host_copies_the_values() -> None:
    cols, rows, dropped = small_ok()
    v = V.verify(small(), out(cols, rows, dropped, "subtotals"))
    assert v.ok and not v.warns
    assert v.table["rows"][0] == ["North", "Q1", "100"]  # copied from the cells, not typed by the program
    assert v.table["sources"][0] == ["S!A2", "S!B1", "S!B2"]
    assert v.report["info"]["numbers_in"] == "750" and v.report["info"]["numbers_out"] == "375"
    assert v.report_sha256 == V.verify(small(), out(cols, rows, dropped, "subtotals")).report_sha256


def test_a_literal_value_is_not_an_output() -> None:
    cols, rows, dropped = small_ok()
    doc = json.loads(out(cols, rows, dropped))
    doc["rows"][0]["cells"][2] = {"value": "1000"}
    assert codes(V.verify(small(), json.dumps(doc).encode())) == ["bad_output"]
    doc["rows"][0]["cells"][2] = {"ref": "S!B2", "value": "1000"}
    assert codes(V.verify(small(), json.dumps(doc).encode())) == ["bad_output"]


def test_a_reference_to_a_blank_or_missing_cell_is_refused() -> None:
    cols, rows, dropped = small_ok()
    rows[0][2] = "S!Z99"
    assert "bad_output" in codes(V.verify(small(), out(cols, rows, dropped)))


# ---- one test per BLOCK -----------------------------------------------------------------------------------
def test_unaccounted_cells_block() -> None:
    cols, rows, dropped = small_ok()
    v = V.verify(small(), out(cols, rows, dropped[:-1]))  # C4 is neither used nor dropped
    assert "unaccounted_cells" in codes(v) and v.blocks[0]["examples"] == ["S!C4"]
    assert v.table is None


def test_double_count_blocks() -> None:
    cols, rows, dropped = small_ok()
    rows.append(["S!A2", "S!C1", "S!B2"])  # the same number again
    assert "double_count" in codes(V.verify(small(), out(cols, rows, dropped)))


def test_a_number_cell_used_in_two_measure_columns_is_a_double_count() -> None:
    cols = [("Region", "dimension"), ("A", "measure"), ("B", "measure")]
    rows = [["S!A2", "S!B2", "S!B2"]]
    assert "double_count" in codes(V.verify(small(), out(cols, rows, [])))


def test_subtotal_unproven_blocks_when_a_covered_value_changes() -> None:
    cols, rows, dropped = small_ok()
    grid = small()
    for s in grid["sheets"]:
        for c in s["cells"]:
            if (c[0], c[1]) == (2, 2):
                c[3] = "101"  # B2 was 100: the printed total of 300 is no longer its sum
    v = V.verify(grid, out(cols, rows, dropped))
    assert "subtotal_unproven" in codes(v)


def test_a_subtotal_that_covers_nothing_proven_blocks() -> None:
    cols, rows, dropped = small_ok()
    dropped[2] = drop("S!B4", "subtotal")  # no covers
    assert "subtotal_unproven" in codes(V.verify(small(), out(cols, rows, dropped)))


def test_number_dropped_blocks_when_it_is_not_a_proven_subtotal() -> None:
    cols, rows, dropped = small_ok()
    rows = rows[:-1]  # C3 is no longer used...
    dropped = [*dropped[:-1], drop("S!C3", "note"), drop("S!C4", "subtotal", ["S!C2"])]
    v = V.verify(small(), out(cols, rows, dropped))
    assert "number_dropped" in codes(v)
    assert next(b for b in v.blocks if b["code"] == "number_dropped")["examples"] == ["S!C3"]


def test_label_numbers_are_a_warn_until_the_cap() -> None:
    g = grid_from_rows({"S": [["Report 2024", 2024], ["Region", "Amount"], ["North", 100]]})
    cols = [("Region", "dimension"), ("Amount", "measure")]
    rows = [["S!A3", "S!B3"]]
    dropped = [drop("S!A1", "title"), drop("S!B1", "label"), drop("S!A2", "header"), drop("S!B2", "header")]
    v = V.verify(g, out(cols, rows, dropped))
    assert v.ok and codes(v, "warns") == ["label_numbers_dropped"]
    many = grid_from_rows({"S": [[str(i), i] for i in range(40)] + [["North", 100]]})
    rows2 = [[f"S!A{i + 1}", f"S!B{i + 1}"] for i in range(40, 41)]
    dropped2 = [drop(f"S!A{i + 1}", "title") for i in range(40)] + [drop(f"S!B{i + 1}", "label") for i in range(40)]
    assert "number_dropped" in codes(V.verify(many, out(cols, rows2, dropped2)))  # 40 labels is over the cap


def test_control_total_guards_the_materialiser(monkeypatch: pytest.MonkeyPatch) -> None:
    cols, rows, dropped = small_ok()
    monkeypatch.setattr(V, "_is_num", lambda text: False)  # a materialiser that loses the measure values
    assert "control_total" in codes(V.verify(small(), out(cols, rows, dropped)))


def test_nondeterministic_blocks() -> None:
    cols, rows, dropped = small_ok()
    a = out(cols, rows, dropped)
    b = out(cols, list(reversed(rows)), dropped)
    assert "nondeterministic" in codes(V.verify(small(), a, b))
    assert V.verify(small(), a, a).ok


def test_measure_must_be_a_number_and_part_only_on_text() -> None:
    g = grid_from_rows({"S": [["Name", "Amt"], ["A", "1,000.50"], ["B", 5]]})
    cols = [("Name", "dimension"), ("Amt", "measure")]
    v = V.verify(g, out(cols, [["S!A2", "S!B2"], ["S!A3", "S!B3"]], [drop("S!A1", "header"), drop("S!B1", "header")]))
    assert codes(v) == ["measure_not_number"]
    cols2 = [("Name", "dimension"), ("Amt", "dimension")]
    v2 = V.verify(g, out(cols2, [["S!A2", ("S!B3", ",", 0)]], []))
    assert "part_on_number" in codes(v2)


def test_split_index_out_of_range_and_explode_width() -> None:
    g = grid_from_rows({"S": [["Investor", "Funds"], ["I-1", "Fund A, Fund C"]]})
    cols = [("Investor", "dimension"), ("Fund", "dimension")]
    drops = [drop("S!A1", "header"), drop("S!B1", "header")]
    ok = V.verify(g, out(cols, [["S!A2", ("S!B2", ",", 0)], ["S!A2", ("S!B2", ",", 1)]], drops, "explode"))
    assert ok.ok and codes(ok, "warns") == ["explode_used"]
    assert ok.table["rows"] == [["I-1", "Fund A"], ["I-1", "Fund C"]]  # trimmed, host-split
    assert "split_no_part" in codes(V.verify(g, out(cols, [["S!A2", ("S!B2", ",", 5)]], drops)))
    many = [["S!A2", ("S!B2", ",", 0)]] * 60
    assert "explode_too_wide" in codes(V.verify(g, out(cols, many, drops)))


def test_limits_on_output_rows() -> None:
    cols, rows, dropped = small_ok()
    assert "limits" in codes(V.verify(small(), out(cols, rows, dropped), flex={"max_rows_out": 2}))


# ---- WARNs ------------------------------------------------------------------------------------------------
def test_misaligned_warns_with_the_example() -> None:
    cols, rows, dropped = small_ok()
    rows[0][0] = "S!A3"  # the South label on a North number: below it, other row, other column
    rows[2][0] = "S!A2"
    v = V.verify(small(), out(cols, rows, dropped))
    assert v.ok and "misaligned" in codes(v, "warns")
    assert any("row 0" in e for w in v.warns if w["code"] == "misaligned" for e in w["examples"])


def test_a_kept_subtotal_row_warns() -> None:
    g = grid_from_rows({"S": [["Grp", "Item", "Amt"], ["G", "a", 10], ["G", "b", 20], ["G", "sum", 30]]})
    cols = [("Grp", "dimension"), ("Item", "dimension"), ("Amt", "measure")]
    rows = [[f"S!A{i}", f"S!B{i}", f"S!C{i}"] for i in (2, 3, 4)]
    v = V.verify(g, out(cols, rows, [drop("S!A1", "header"), drop("S!B1", "header"), drop("S!C1", "header")]))
    assert v.ok and codes(v, "warns") == ["possible_subtotal_kept"]


def test_key_distinct_changed_warns() -> None:
    g = grid_from_rows({"S": [["ID", "Amt"], ["A", 1], ["B", 2], ["C", 3]]})
    cols = [("ID", "key"), ("Amt", "measure")]
    # the key column draws from A..C but the rows only reach A and B; C's row is a different cell's number
    rows = [["S!A2", "S!B2"], ["S!A2", "S!B3"], ["S!A4", "S!B4"]]
    v = V.verify(g, out(cols, rows, [drop("S!A1", "header"), drop("S!B1", "header"), drop("S!A3", "note")]))
    assert v.ok and codes(v, "warns") == ["key_distinct_changed"]


# ---- the real fixtures ------------------------------------------------------------------------------------
def horizontal_program(g: dict, amount_role: str = "dimension") -> bytes:
    """Transposed: one output row per investor column; the field names (column A) and `#n` headers are dropped."""
    s = g["sheets"][0]["name"]
    fields = {str(v): r for r, c, t, v in g["sheets"][0]["cells"] if c == 1 and r >= 4}
    cols_in = sorted({c for r, c, t, v in g["sheets"][0]["cells"] if c > 1 and r == 3})
    names = [k for k in fields]
    cols = [(f"F{i}", "dimension") for i in range(len(names))]
    if amount_role == "measure":
        cols[names.index("AUM USD MILLIONS") if "AUM USD MILLIONS" in names else 5] = ("AUM", "measure")
    rows = [[ref(s, fields[n], c) for n in names] for c in cols_in]
    dropped = [drop(ref(s, 1, 1), "title"), drop(ref(s, 3, 1), "header")] + \
        [drop(ref(s, 3, c), "header") for c in cols_in] + [drop(ref(s, r, 1), "header") for r in fields.values()]
    return out(cols, rows, dropped, "transposed")


def test_investors_horizontal_is_a_clean_transposition() -> None:
    g = grid_from_workbook(FIX / "investors_horizontal.xlsx")
    v = V.verify(g, horizontal_program(g))
    assert v.ok, v.blocks
    assert len(v.table["rows"]) == 43 and len(v.table["columns"]) == 10
    idx = cell_index(g)
    assert v.table["rows"][0][1] == idx[v.table["sources"][0][1]][1]  # the host's copy of the Investor cell


def test_investors_horizontal_with_text_numbers_as_a_measure_is_refused() -> None:
    g = grid_from_workbook(FIX / "investors_horizontal.xlsx")
    v = V.verify(g, horizontal_program(g, "measure"))
    assert "measure_not_number" in codes(v)  # '5,648.88' is text in the source: never treated as a number


def pivot_program(g: dict, with_proofs: bool = True) -> bytes:
    s = g["sheets"][0]["name"]
    cells = {(r, c): (t, v) for r, c, t, v in g["sheets"][0]["cells"]}
    last_row = max(r for r, _ in cells)
    status_cols = sorted(c for (r, c) in cells if r == 3 and c >= 3)
    cols = [("Investor type", "dimension"), ("Investor", "key"), ("Status", "dimension"), ("Style", "dimension"),
            ("Commitment", "measure")]
    rows: list[list[str]] = []
    dropped = [drop(ref(s, 1, 1), "title"), drop(ref(s, 3, 1), "header"), drop(ref(s, 3, 2), "header"),
               drop(ref(s, 4, 12), "header")]
    group = ""
    group_measures: list[str] = []
    all_measures: list[str] = []
    group_totals: list[str] = []
    for r in range(5, last_row + 1):
        label = cells.get((r, 1))
        name = cells.get((r, 2))
        if name is not None:  # a data row: one measure in columns 3..11
            if label is not None:
                group = ref(s, r, 1)
            c = next(c for c in range(3, 12) if (r, c) in cells)
            status_c = max(x for x in status_cols if x <= c)
            rows.append([group, ref(s, r, 2), ref(s, 3, status_c), ref(s, 4, c), ref(s, r, c)])
            group_measures.append(ref(s, r, c))
            all_measures.append(ref(s, r, c))
            dropped.append(drop(ref(s, r, 12), "subtotal", [ref(s, r, c)] if with_proofs else None))
        elif label is not None and "total" in str(label[1]).lower():
            dropped.append(drop(ref(s, r, 1), "subtotal"))
            if str(label[1]).startswith("Grand"):
                dropped.append(drop(ref(s, r, 12), "grand_total", all_measures if with_proofs else None))
            else:
                dropped.append(drop(ref(s, r, 12), "subtotal", group_measures if with_proofs else None))
                group_totals.append(ref(s, r, 12))
                group_measures = []
    used = {x for row in rows for x in row}
    dropped += [drop(ref(s, 3, c), "header") for c in status_cols if ref(s, 3, c) not in used]
    dropped += [drop(ref(s, 4, c), "header") for c in range(3, 12) if ref(s, 4, c) not in used]
    return out(cols, rows, dropped, "nested_pivot")


def test_investors_pivot_subtotals_are_proven_by_sum_with_rounding_noted() -> None:
    g = grid_from_workbook(FIX / "investors_pivot.xlsx")
    v = V.verify(g, pivot_program(g))
    assert v.ok, v.blocks
    # the sheet prints 5714.85 for 5714.8516: proven only after rounding, so the person is told
    assert codes(v, "warns") == ["subtotal_rounded"] and v.report["info"]["subtotals_proven"] > 8
    assert len(v.table["rows"]) == 43 and v.table["rows"][0][0] == "Endowment"  # fill-down label carried


def test_investors_pivot_unproven_when_one_value_changes_or_covers_are_missing() -> None:
    g = grid_from_workbook(FIX / "investors_pivot.xlsx")
    changed = json.loads(json.dumps(g))
    for c in changed["sheets"][0]["cells"]:
        if (c[0], c[1]) == (6, 4):  # Perez, Campbell: 975.5843 -> 1975.5843; row total and subtotal now disagree
            c[3] = "1975.5843"
    assert "subtotal_unproven" in codes(V.verify(changed, pivot_program(g)))
    assert "subtotal_unproven" in codes(V.verify(g, pivot_program(g, with_proofs=False)))


def test_layout_sig_ignores_data_rows_and_row_counts_but_not_headers() -> None:
    a = grid_from_workbook(FIX / "investors_horizontal.xlsx")
    b = json.loads(json.dumps(a))
    b["sheets"][0]["cells"] = [c for c in b["sheets"][0]["cells"] if c[1] <= 20 or c[0] != 3 or True]
    for c in b["sheets"][0]["cells"]:
        if c[0] > 3 and c[1] > 1:
            c[3] = "changed" if c[2] == "s" else "1"
    assert layout_sig(a) == layout_sig(b)
    c = json.loads(json.dumps(a))
    c["sheets"][0]["cells"][1][3] = "Different"
    assert layout_sig(c) != layout_sig(a)
    assert layout_sig(grid_from_workbook(FIX / "investors_pivot.xlsx")) != layout_sig(a)


# ---- the lint ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("source,reason", [
    ("import os\ndef build(book): return {}\n", "import os"),
    ("import socket\ndef build(book): return {}\n", "socket"),
    ("def build(book): return eval('1')\n", "eval"),
    ("def build(book):\n    return open('/etc/passwd')\n", "open"),
    ("def build(book): return book.__class__\n", "__class__"),
    ("x = 1\n", "no `def build"),
    ("def build(book): return '" + "x" * 300 + "'\n", "200 characters"),
    ("def build(:\n", "syntax"),
])
def test_the_lint_refuses(source: str, reason: str) -> None:
    assert any(reason in p for p in V.lint_program(source))


def test_the_lint_accepts_a_plain_program() -> None:
    src = "import re\nfrom decimal import Decimal\n\ndef build(book):\n    return {'columns': [], 'rows': []}\n"
    assert V.lint_program(src) == []


# ---- the order of audit rows around a run -----------------------------------------------------------------
class Sink:
    def __init__(self, fail_on: str = "") -> None:
        self.rows: list[tuple[str, dict, dict]] = []
        self.fail_on = fail_on

    def record(self, action: str, rationale: str, inputs: dict | None = None, outputs: dict | None = None) -> dict:
        if action == self.fail_on:
            raise AuditError(f"sink down at {action}")
        self.rows.append((action, dict(inputs or {}), dict(outputs or {})))
        return {"seq": len(self.rows)}


def runner_for(payload: bytes | None) -> FakeRunner:
    return FakeRunner(lambda kind, program, inputs, seed: payload)


def test_a_run_writes_started_before_and_finished_before_the_output_is_used() -> None:
    cols, rows, dropped = small_ok()
    audit, runner = Sink(), runner_for(out(cols, rows, dropped))
    run = run_layout(runner=runner, audit=audit, grid=small(), program=PROGRAM, why="layout")
    assert [r[0] for r in audit.rows] == ["flex.exec.started", "flex.exec.finished", "flex.verify"]
    assert runner.calls == 2 and run.verified.ok and run.statuses == ("ok", "ok")
    assert run.output_sha256s[0] == run.output_sha256s[1] != ""
    assert audit.rows[1][2]["statuses"] == ["ok", "ok"] and "report_sha256" in audit.rows[2][1]


def test_audit_first_a_failing_sink_means_the_runner_is_never_called() -> None:
    runner = runner_for(b"{}")
    with pytest.raises(AuditError):
        run_layout(runner=runner, audit=Sink("flex.exec.started"), grid=small(), program=PROGRAM, why="x")
    assert runner.calls == 0


def test_finished_audit_failure_discards_the_output() -> None:
    cols, rows, dropped = small_ok()
    audit = Sink("flex.exec.finished")
    with pytest.raises(AuditError):
        run_layout(runner=runner_for(out(cols, rows, dropped)), audit=audit, grid=small(), program=PROGRAM, why="x")
    assert [r[0] for r in audit.rows] == ["flex.exec.started"]  # nothing was verified or shown


def test_a_lint_refusal_is_audited_and_nothing_runs() -> None:
    audit, runner = Sink(), runner_for(b"{}")
    with pytest.raises(Refused) as exc:
        run_layout(runner=runner, audit=audit, grid=small(), program="import socket\ndef build(book): return {}\n",
                   why="x")
    assert exc.value.code == "lint_refused" and runner.calls == 0
    assert [r[0] for r in audit.rows] == ["flex.lint_refused"]


def test_a_container_failure_is_a_block_not_an_exception() -> None:
    audit = Sink()
    runner = FakeRunner(lambda *a: RunResult("timeout", None, None, "", 60.0, "x", "e"))
    run = run_layout(runner=runner, audit=audit, grid=small(), program=PROGRAM, why="x")
    assert not run.verified.ok and run.verified.blocks[0]["code"] == "limits" and run.verified.table is None
    assert [r[0] for r in audit.rows][-1] == "flex.verify"


def test_amounts_are_decimal_exact() -> None:
    g = grid_from_rows({"S": [["A", "B"], ["x", Decimal("0.1")], ["y", Decimal("0.2")], ["t", Decimal("0.3")]]})
    cols = [("L", "dimension"), ("V", "measure")]
    rows = [["S!A2", "S!B2"], ["S!A3", "S!B3"]]
    dropped = [drop("S!A1", "header"), drop("S!B1", "header"), drop("S!A4", "subtotal"),
               drop("S!B4", "subtotal", ["S!B2", "S!B3"])]
    v = V.verify(g, out(cols, rows, dropped))
    assert v.ok and codes(v, "warns") == []  # 0.1 + 0.2 == 0.3 exactly, no float noise
