"""UI parity: the job screen's "Show working" tabs, each a view model of the job's real state."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from worker import console_engine as CE
from worker import console_working as W
from worker.console import Console
from worker.web import STATIC, create_app


@pytest.fixture
def c(tmp_path: Path) -> Console:
    con = Console(tmp_path / "inbox")
    con.act("prov|paid")
    return con


def start(c: Console, typ: str, run: bool = True, fund: str | None = None) -> dict[str, Any]:
    gl = typ.startswith("GL")
    c.act(f"start|{'Sponsor B' if gl else 'Sponsor A'}|{typ}")  # Sponsor B has its static records approved
    if gl:
        if fund:
            c.act(f"ask_set|fund|{fund}")
    else:
        c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    j = c.job()
    assert j is not None
    if c.ex_pending(j):
        c.act("ex|no")
    if run:
        c.act("go")
    return j


# ---- the tab list -----------------------------------------------------------------------------------------
def test_gl_jobs_have_a_crosswalk_tab_and_static_jobs_do_not() -> None:
    assert [t for t, _ in W.tabs_for("Investors")] == ["fields", "dm", "results", "skills", "graph", "runs", "ask",
                                                           "audit"]
    assert [t for t, _ in W.tabs_for("GL Fund")] == ["fields", "dm", "results", "xwalk", "skills", "graph", "runs",
                                                    "ask", "audit"]


def test_an_unknown_tab_is_refused(c: Console) -> None:
    j = start(c, "Investors")
    with pytest.raises(W.WorkingError):
        W.working_view(c, j, "recon")


# ---- fields -----------------------------------------------------------------------------------------------
def test_fields_come_from_the_template_and_the_sponsor_requirement(c: Console) -> None:
    j = start(c, "Investors", run=False)
    v = W.working_view(c, j, "fields")
    assert v["kind"] == "fields" and v["rows"]
    names = [r["field"] for r in v["rows"]]
    assert names == [f[0] for f in CE.fields_of(j)]
    miss = [r for r in v["rows"] if r["source"] == ""]
    assert all(r["status"] in ("missing", "required_missing") for r in miss)
    assert all(r["required"] for r in miss if r["status"] == "required_missing")


def test_a_column_you_picked_shows_as_set_by_you(c: Console) -> None:
    j = start(c, "Investors", run=False)
    row = next(r for r in W.working_view(c, j, "fields")["rows"] if r["source"] and r["kind"] == "column")
    c.act(f"col|{row['field']}|{row['source']}", who="reviewer-a")
    after = next(r for r in W.working_view(c, j, "fields")["rows"] if r["field"] == row["field"])
    assert after["status"] == "set" and after["confidence"] == 100


def test_gl_mapping_lists_every_account_used_with_its_tier(c: Console) -> None:
    j = start(c, "GL Fund", run=False, fund="B Fund I")
    v = W.working_view(c, j, "fields")
    assert v["kind"] == "mapping" and v["accounts"]
    used = {x["l"].acct for x in CE.gl_eval(c, j)["lines"]}
    assert {a["account"] for a in v["accounts"]} == used
    assert v["stats"]["accounts"] == len(used)
    assert sum(1 for a in v["accounts"] if a["status"] == "needs") == v["stats"]["unmatched"]


# ---- results ----------------------------------------------------------------------------------------------
def test_results_before_a_run_is_an_honest_empty_state(c: Console) -> None:
    j = start(c, "Investors", run=False)
    v = W.working_view(c, j, "results")
    assert v["kind"] == "results" and v["rows"] == [] and "No run yet" in v["empty"]


def test_results_counts_equal_the_tally(c: Console) -> None:
    j = start(c, "Investors")
    t = CE.tally(c, j)
    v = W.working_view(c, j, "results")
    assert v["stats"]["read"] == t["read"] == v["total"]
    assert (v["stats"]["err"], v["stats"]["warn"], v["stats"]["out"]) == (t["err"], t["warn"], t["out"])
    assert len(v["rows"]) == min(v["total"], W.MAX_ROWS)
    assert {r["st"] for r in v["rows"]} <= {"ok", "warn", "skip", "err"}


def test_gl_results_carry_journals_that_tie_to_the_lines(c: Console) -> None:
    j = start(c, "GL Fund", fund="B Fund I")
    v = W.working_view(c, j, "results")
    assert v["journals"] and v["stats"]["journals"] == len(v["journals"])
    assert sum(x["lines"] for x in v["journals"]) == v["stats"]["read"]
    assert all(set(x) >= {"journal", "lines", "debit", "credit", "diff", "st"} for x in v["journals"])


# ---- crosswalk --------------------------------------------------------------------------------------------
def test_crosswalk_counts_distinct_and_resolved_references(c: Console) -> None:
    j = start(c, "GL Fund", fund="B Fund I")
    v = W.working_view(c, j, "xwalk")
    e = CE.gl_eval(c, j)
    inv = {val for x in e["lines"] for val in [x["l"].inv] if val}
    row = next(r for r in v["rows"] if r["ref"] == "Investors")
    assert row["distinct"] == len(inv)
    assert row["resolved"] + len(row["unresolved"]) == row["distinct"]


def test_adding_an_unknown_reference_resolves_it_in_the_crosswalk(c: Console) -> None:
    j = start(c, "GL Fund", fund="B Fund I")
    unknown = [(r["ref"], u) for r in W.working_view(c, j, "xwalk")["rows"] for u in r["unresolved"]]
    if not unknown:
        pytest.skip("the sample ledger has no unknown reference")
    kind, value = unknown[0]
    c.quick_add_ref(j, kind, value)
    after = W.working_view(c, j, "xwalk")
    assert value not in next(r for r in after["rows"] if r["ref"] == kind)["unresolved"]


def test_crosswalk_of_a_static_job_is_an_honest_empty_state(c: Console) -> None:
    j = start(c, "Investors")
    v = W.working_view(c, j, "xwalk") if "xwalk" in dict(W.tabs_for("Investors")) else None
    assert v is None


# ---- decisions --------------------------------------------------------------------------------------------
def test_gl_decision_tables_are_the_skill_tables_with_their_rows(c: Console) -> None:
    j = start(c, "GL Fund", run=False, fund="B Fund I")
    v = W.working_view(c, j, "dm")
    assert v["tables"], "map-accounts has a coa-cascade table"
    t = v["tables"][0]
    assert t["skill"] == "map-accounts" and t["rows"] and t["hit"]


def test_static_job_without_a_table_shows_the_rules_in_force(c: Console) -> None:
    j = start(c, "Investors", run=False)
    j["rules"]["nameMax"] = 60
    v = W.working_view(c, j, "dm")
    assert v["tables"] == []
    assert "no decision table" in v["empty"].lower()
    assert any("60" in r["rule"] for r in v["rules"])


# ---- skills -----------------------------------------------------------------------------------------------
def test_skills_used_are_judged_from_the_job_not_listed_blindly(c: Console) -> None:
    j = start(c, "Investors", run=False)
    used = {s["name"]: s for s in W.working_view(c, j, "skills")["skills"]}
    assert used["check-data-quality"]["used"] is False and used["map-accounts"]["used"] is False
    c.act("go")
    used = {s["name"]: s for s in W.working_view(c, j, "skills")["skills"]}
    assert used["check-data-quality"]["used"] is True and used["check-data-quality"]["evidence"]
    assert used["map-accounts"]["used"] is False  # a static job never maps accounts


def test_a_gl_job_uses_map_accounts(c: Console) -> None:
    j = start(c, "GL Fund", fund="B Fund I")
    used = {s["name"]: s for s in W.working_view(c, j, "skills")["skills"]}
    assert used["map-accounts"]["used"] is True and "accounts" in used["map-accounts"]["evidence"]


# ---- context ----------------------------------------------------------------------------------------------
def test_context_facts_are_counted_from_the_console_state(c: Console) -> None:
    j = start(c, "Investors")
    v = W.working_view(c, j, "graph")
    assert v["sponsor"] == "Sponsor A"
    assert [f["name"] for f in v["funds"]] == c.ents_of("Sponsor A")
    f0 = v["funds"][0]
    assert f0["static"] == c.static_done(f0["name"], "Sponsor A")
    labels = {f["label"]: f["value"] for f in v["facts"]}
    assert "Records in Ledgerline" in labels and "ID counters" in labels


# ---- runs -------------------------------------------------------------------------------------------------
def test_runs_empty_before_a_run_then_one_row_per_run(c: Console) -> None:
    j = start(c, "Investors", run=False)
    assert W.working_view(c, j, "runs")["runs"] == []
    c.act("go")
    v = W.working_view(c, j, "runs")
    assert len(v["runs"]) == len(j["runs"]) == 1
    assert v["runs"][0]["err"] == j["runs"][0]["t"]["err"]
    assert [s["stage"] for s in v["stages"]] == ["Read", "Map", "Convert", "Check", "Write"]
    assert "not recorded" in v["note"]


# ---- audit ------------------------------------------------------------------------------------------------
def test_audit_shows_this_jobs_entries_only(c: Console) -> None:
    j = start(c, "Investors")
    c.S["audit"].append({"t": "09:00:00", "who": "analyst", "a": "other job entry", "r": "x", "job": "Vendors",
                         "sp": "Sponsor A", "d": "2026-10-10"})
    v = W.working_view(c, j, "audit")
    acts = [r["a"] for r in v["rows"]]
    assert acts and "other job entry" not in acts
    assert all(r["reason"] for r in v["rows"])


def test_audit_empty_state(c: Console) -> None:
    j = start(c, "Investors", run=False)
    c.S["audit"] = []
    v = W.working_view(c, j, "audit")
    assert v["rows"] == [] and v["empty"]


# ---- endpoint and assets ----------------------------------------------------------------------------------
def test_endpoint_serves_a_tab_and_refuses_unknown_ones(tmp_path: Path) -> None:
    app = create_app(tmp_path / "state", environ={"WORKER_MODEL_PROVIDER": "demo",
                                                  "WORKER_ENV_FILE": str(tmp_path / "t.env")})
    http = TestClient(app)
    for a in ("start|Sponsor A|Investors", "ask_set|cov|all", "ask_sample", "ask_go"):
        http.post("/api/console/act", json={"action": a})
    st = http.get("/api/console/state").json()
    jid = st["job"]["id"]
    ok = http.get(f"/api/console/jobs/{jid}/working/fields")
    assert ok.status_code == 200 and ok.json()["kind"] == "fields"
    assert http.get(f"/api/console/jobs/{jid}/working/nope").status_code == 404
    assert http.get("/api/console/jobs/zzz/working/fields").status_code == 404
    js = http.get("/working.js")
    assert js.status_code == 200 and "WORKING" in js.text


def test_console_html_loads_working_js_once() -> None:
    html = (STATIC / "console.html").read_text(encoding="utf-8")
    assert html.count('src="/working.js"') == 1
