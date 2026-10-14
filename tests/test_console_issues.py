"""Ticket 13: the Issues table. Groups by cause and pattern with exact counts, rows paged on request, a decision
applied to a group by rule with a preview, audited with the reviewer before it takes effect, and undone by an audited
revert. Options per cause are data in the check-data-quality skill. Fictitious data only."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from id_decision import decide_ids
from worker import console_engine as CE
from worker import console_issues as CI
from worker.console import Console, ConsoleError
from worker.console_screens import state_view
from worker.web import create_app


@pytest.fixture
def c(tmp_path: Path) -> Console:
    return Console(tmp_path / "inbox")


def job5(c: Console) -> dict[str, Any]:
    """Sponsor A investors with the existing-records file: 15 rows, 5 need a person."""
    c.act("start|Sponsor A|Investors")
    c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    c.act("ex|yes")
    c.act("ex|file")
    c.act("go")
    j = c.job()
    assert j is not None
    return j


def by_cause(m: dict[str, Any], cause: str) -> dict[str, Any]:
    return next(g for g in m["groups"] if g["cause"] == cause and g["state"] == "open")


# ---- the options are data ---------------------------------------------------------------------------------
def test_options_recommendation_and_impact_come_from_the_skill_table() -> None:
    cz = CI.causes()
    assert [o[0] for o in cz["idlong"].options] == ["fix", "edit", "out", "park"]
    assert cz["idlong"].recommended == "fix" and cz["idlong"].impact == "block"
    assert cz["blankreq"].recommended == ""  # never recommended: a person supplies the value
    for cause in cz.values():
        assert 1 <= len(cause.options) <= 4 and cause.recommended in ("", *[o[0] for o in cause.options])
    src = Path(CI.__file__).read_text(encoding="utf-8")
    assert "Update to the shortened" not in src and "Leave these out" not in src  # no label is a literal in code


def test_every_cause_is_in_one_of_four_families_and_the_actions_are_three() -> None:
    import yaml

    fams = {"duplicates", "missing", "too_long", "no_tie"}
    skill = Path(CI.__file__).parents[2] / "skills" / "check-data-quality" / "tables"
    engine = yaml.safe_load((skill / "causes.yaml").read_text(encoding="utf-8"))["rows"]
    assert all(r["then"]["issue_family"] in fams for r in engine)
    by_code = {r["id"]: r["then"]["issue_family"] for r in engine}
    for cause in CI.causes().values():
        assert cause.family in fams
        assert cause.engine_code in by_code and by_code[cause.engine_code] in fams or cause.id == "blankdate" or cause.id.startswith("rc")  # rc*: the Reconcile tab's
        assert {o[0] for o in cause.options} <= {"fix", "edit", "out", "park"} | ({"go"} if cause.id == "rctie" else set())


# ---- the model: exact counts, verdict that adds up ---------------------------------------------------------
def test_groups_carry_exact_counts_and_the_verdict_sums_to_the_rows_read(c: Console) -> None:
    job5(c)
    m = CI.model(c, c.job())  # type: ignore[arg-type]
    v = m["verdict"]
    assert v["N"] == 15 and v["blocked"] == 5 and v["sum"] == v["N"]
    assert sum(g["n"] for g in m["groups"] if g["state"] == "open") == 5
    g = by_cause(m, "idlong")
    assert g["n"] == 2 and g["recommended"] == "fix" and g["source"]["first"] == 9 and g["source"]["last"] == 10
    assert [o["id"] for o in g["options"]] == ["fix", "out", "park"]  # two rows: no one typed ID
    assert by_cause(m, "blankreq")["recommended"] == ""
    assert v["N"] == v["ready"] + v["blocked"] + v["hold"] + v["out"]


def test_the_collapsed_view_has_no_cell_range_codes_or_excel_recipes(c: Console) -> None:
    job5(c)
    m = CI.model(c, c.job())  # type: ignore[arg-type]
    shown = []
    for g in m["groups"]:
        shown += [g["label"], g["consequence"], g["pattern"], g["before"], g["after"], g["source"]["file"],
                  g["source"]["sheet"], *[o["label"] for o in g["options"]]]
    text = "\n".join(shown)
    assert not re.search(r"\b[A-Z]{1,2}\d+:[A-Z]{1,2}\d+\b", text), text
    assert "Data →" not in text and "Filter" not in text and "!A" not in text
    page = Path(CI.__file__).parent / "static" / "issues.js"
    js = page.read_text(encoding="utf-8")
    collapsed = js.split("/* ISSUES:TABLE")[1].split("/* ISSUES:END */")[0]
    assert "excel" not in collapsed.lower() and "locator" not in collapsed


def test_detail_holds_the_range_and_the_excel_steps_on_request(c: Console) -> None:
    j = job5(c)
    g = by_cause(CI.model(c, j), "idlong")
    d = CI.detail(c, j, g["id"])
    assert d["locator"]["range"] == "Investor!A9:L10" and d["excel"]["text"].startswith("In Excel: Data")


def test_rows_with_a_different_pattern_are_split_into_their_own_group(c: Console) -> None:
    j = job5(c)
    m = CI.model(c, j)
    clash = [g for g in m["groups"] if g["cause"] in ("keyclash", "nameclash") and g["state"] == "open"]
    assert len(clash) == 2 and all(g["n"] == 1 for g in clash)  # one per cause: they are never lumped
    assert all(g["recommended"] in ("", "out") for g in clash)


# ---- pages ---------------------------------------------------------------------------------------------------
def test_rows_are_paged_with_an_exact_total_in_source_row_order(c: Console) -> None:
    j = job5(c)
    g = by_cause(CI.model(c, j), "idlong")
    p = CI.rows_page(c, j, g["id"], per=1)
    assert (p["total"], p["start"], p["end"], p["pages"]) == (2, 1, 1, 2)
    assert [r["row"] for r in p["rows"]] == [9] and p["next_after"] == 9
    p2 = CI.rows_page(c, j, g["id"], per=1, after=p["next_after"])
    assert [r["row"] for r in p2["rows"]] == [10] and p2["next_after"] == 0
    assert p["rows"][0]["before"] == "HS-REF-0000000000000042" and p["rows"][0]["after"] == "0000000000000042"
    assert CI.rows_page(c, j, g["id"], per=10_000)["per"] == CI.PAGE_MAX  # a page never grows past the cap


def test_groups_are_paged_and_filtered_by_chip_and_search(c: Console) -> None:
    j = job5(c)
    m = CI.model(c, j, per=2)
    assert m["total"] == len(CI._all_groups(c, j)) and len(m["groups"]) == 2 and m["pages"] >= 2
    assert CI.model(c, j, q="alder")["total"] == 1 and CI.model(c, j, status="done")["total"] == 0
    assert CI.model(c, j, status="hold")["total"] == 0
    assert m["chips"]["need"] == m["total"]


def test_the_first_page_of_a_million_row_group_is_a_slice(monkeypatch: pytest.MonkeyPatch) -> None:
    n = 1_000_000
    items = [{"row": i + 2, "i": i + 1, "id": f"HS-REF-{i:016d}", "name": f"Investor {i}", "cid": "idlong", "err": True,
              "why": "Source key is 23 characters, limit is 20", "s": {"t": "Drop", "c": "rule|drop", "d": "x"},
              "r": {"i": i + 1, "id": f"HS-REF-{i:016d}", "nm": f"Investor {i}", "en": ["Fund A"], "fl": []}}
             for i in range(n)]
    sentinel = object()
    monkeypatch.setattr(CE, "evaluation", lambda c, j: sentinel)
    CI._GROUPED["big"] = (sentinel, {"idlong-000000": items})
    j = {"id": "big", "ents": ["Fund A"], "fund": "Fund A", "type": "Investors"}
    CI.rows_page(None, j, "idlong-000000")  # warm
    t = time.perf_counter()
    p = CI.rows_page(None, j, "idlong-000000", per=50)
    far = CI.rows_page(None, j, "idlong-000000", per=50, after=999_900)
    ms = (time.perf_counter() - t) * 1000
    assert p["total"] == n and (p["start"], p["end"]) == (1, 50) and len(p["rows"]) == 50
    assert far["start"] > 999_000 and ms < 100, ms
    del CI._GROUPED["big"]


# ---- deciding ----------------------------------------------------------------------------------------------------
def test_a_preview_changes_nothing_and_shows_a_sample_and_the_exact_count(c: Console) -> None:
    j = job5(c)
    g = by_cause(CI.model(c, j), "idlong")
    before = json.dumps(j["dec"], sort_keys=True), dict(j["rules"])
    pv = CI.preview(c, j, [{"id": g["id"], "option": "fix"}])
    assert pv["n"] == 2 and pv["groups"][0]["rows"][0]["after"] == "0000000000000042"
    assert (json.dumps(j["dec"], sort_keys=True), dict(j["rules"])) == before
    assert not (c.job_dir(j) / "audit.jsonl").exists() or "issues.decided" not in (
        c.job_dir(j) / "audit.jsonl").read_text(encoding="utf-8")


def test_a_group_is_decided_audited_with_the_reviewer_then_undone_by_an_audited_revert(c: Console) -> None:
    j = job5(c)
    g = by_cause(CI.model(c, j), "idlong")
    got = CI.apply(c, j, [{"id": g["id"], "option": "fix"}], "Priya Shah", rule=True)
    assert got["verdict"]["blocked"] == 3 and got["verdict"]["sum"] == 15
    log = (c.job_dir(j) / "audit.jsonl").read_text(encoding="utf-8")
    rec = [json.loads(x) for x in log.splitlines() if "issues.decided" in x]
    assert rec and rec[0]["inputs"]["reviewer"] == "Priya Shah" and "Priya Shah" in rec[0]["rationale"]
    m = CI.model(c, j)
    done = next(x for x in m["groups"] if x["state"] == "fix")
    assert done["n"] == 2 and done["by"] == "Priya Shah" and done["undo"]
    back = CI.undo(c, j, "Priya Shah", decision=done["undo"])
    assert back["verdict"]["blocked"] == 5 and j["rules"]["id"] == "error"
    assert "issues.reverted" in (c.job_dir(j) / "audit.jsonl").read_text(encoding="utf-8")
    assert by_cause(CI.model(c, j), "idlong")["n"] == 2


def test_nothing_changes_when_the_record_cannot_be_written(c: Console, monkeypatch: pytest.MonkeyPatch) -> None:
    j = job5(c)
    g = by_cause(CI.model(c, j), "blankreq")
    folder = c.job_dir(j)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "audit.jsonl").unlink(missing_ok=True)
    (folder / "audit.jsonl").mkdir()  # the log cannot be opened for appending
    with pytest.raises(CI.IssueError, match="so nothing changed"):
        CI.apply(c, j, [{"id": g["id"], "option": "out"}], "Priya Shah")
    assert j["dec"] == {} and not j.get("hist")


def test_an_agent_or_a_blank_name_cannot_decide(c: Console) -> None:
    j = job5(c)
    g = by_cause(CI.model(c, j), "blankreq")
    for who in ("", "local", "key_resolver", "agent"):
        with pytest.raises(CI.IssueError):
            CI.apply(c, j, [{"id": g["id"], "option": "out"}], who)
    assert j["dec"] == {}


def test_a_hold_needs_a_reason_and_nothing_happens_without_one(c: Console) -> None:
    j = job5(c)
    g = by_cause(CI.model(c, j), "blankreq")
    for bad in ("", "   ", "x" * (CI.NOTE_MAX + 1)):
        with pytest.raises(CI.IssueError):
            CI.apply(c, j, [{"id": g["id"], "option": "park", "value": bad}], "Priya Shah")
    assert j["dec"] == {} and not j.get("hist")
    assert not (c.job_dir(j) / "audit.jsonl").exists() or "issues.decided" not in (
        c.job_dir(j) / "audit.jsonl").read_text(encoding="utf-8")


def test_the_whole_life_of_a_hold_is_recorded_with_who_when_and_why(c: Console) -> None:
    j = job5(c)
    g = by_cause(CI.model(c, j), "blankreq")
    CI.apply(c, j, [{"id": g["id"], "option": "park", "value": "waiting on client for the name"}], "Priya Shah")
    parked = next(x for x in CI.model(c, j)["groups"] if x["state"] == "hold")
    with pytest.raises(CI.IssueError, match="note"):
        CI.undo(c, j, "Sam Lee", decision=parked["undo"], reason="  ")
    CI.undo(c, j, "Sam Lee", decision=parked["undo"], reason="client sent the name")
    reopened = by_cause(CI.model(c, j), "blankreq")
    assert reopened["hold_history"][0]["parked_by"] == "Priya Shah" and reopened["hold_history"][0]["released_by"] == "Sam Lee"
    with pytest.raises(CI.IssueError, match="note"):  # deciding rows that were on hold needs a note
        CI.apply(c, j, [{"id": reopened["id"], "option": "out"}], "Sam Lee")
    CI.apply(c, j, [{"id": reopened["id"], "option": "out"}], "Sam Lee", note="the client said to drop it")
    log = [json.loads(x) for x in (c.job_dir(j) / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    decided = [x for x in log if x["action"] == "issues.decided"]
    assert decided[0]["inputs"]["note"] == "waiting on client for the name" and decided[0]["inputs"]["at"]
    assert decided[-1]["inputs"]["note"] == "the client said to drop it" and decided[-1]["inputs"]["from_hold"] == [1]
    rel = next(x for x in log if x["action"] == "issues.reverted")
    assert rel["inputs"]["note"] == "client sent the name" and rel["inputs"]["reviewer"] == "Sam Lee"
    hist = {h["id"]: h for h in CI.history(j)}
    assert hist[1]["released"]["by"] == "Sam Lee" and hist[2]["from_hold"] == [1] and hist[2]["note"]


def test_a_release_is_a_sentence_in_the_chat_and_an_empty_note_is_refused(c: Console) -> None:
    j = job5(c)
    g = by_cause(CI.model(c, j), "blankreq")
    CI.apply(c, j, [{"id": g["id"], "option": "park", "value": "waiting on client"}], "Priya Shah")
    h = next(x for x in CI.model(c, j)["groups"] if x["state"] == "hold")
    ctx = {"job": j["id"], "group": h["id"]}
    c.act("say", "Release:", "Priya Shah", ctx)
    assert "needs a note" in next(m for m in reversed(state_view(c)["job"]["chat"]) if m["who"] == "w" and "note" in m["text"])["text"]
    assert CI.model(c, j)["verdict"]["hold"] == 1
    c.act("say", "Release: client answered", "Priya Shah", ctx)
    btn = worker_msg(c)["options"][0]
    assert btn["label"] == "Release" and CI.model(c, j)["verdict"]["hold"] == 1  # a sentence changes nothing
    c.act(btn["action"], "", "Priya Shah")
    assert CI.model(c, j)["verdict"]["hold"] == 0


def test_park_holds_rows_counts_them_apart_blocks_approval_and_release_is_audited(c: Console) -> None:
    j = job5(c)
    g = by_cause(CI.model(c, j), "blankreq")
    CI.apply(c, j, [{"id": g["id"], "option": "park", "value": "waiting on client for the name"}], "Priya Shah")
    m = CI.model(c, j)
    h = next(x for x in m["groups"] if x["state"] == "hold")
    assert h["n"] == 1 and h["release"] and m["verdict"]["hold"] == 1 and m["chips"]["hold"] == 1
    assert m["verdict"]["sum"] == m["verdict"]["N"]
    assert "waiting on client for the name" in j["dec"]["15"]  # the note is the reason kept with the hold
    c.act("acceptall")  # the other issues are decided; one row is still on hold
    with pytest.raises(ConsoleError, match="on hold"):
        decide_ids(c)
        c.act("approve", who="reviewer-a")
    assert state_view(c)["job"]["can_approve"] is False and state_view(c)["job"]["on_hold"] == 1
    CI.undo(c, j, "Priya Shah", decision=h["undo"], reason="client answered")  # Release
    log = (c.job_dir(j) / "audit.jsonl").read_text(encoding="utf-8")
    assert "issues.reverted" in log and CI.model(c, j)["verdict"]["hold"] == 0


def test_a_typed_value_needs_text_and_is_refused_for_many_rows_that_would_collide(c: Console) -> None:
    j = job5(c)
    g = by_cause(CI.model(c, j), "idlong")
    assert "edit" not in [o["id"] for o in g["options"]]  # two rows, one typed ID would collide
    assert all(o["id"] in ("fix", "edit", "out", "park") for o in g["options"])
    with pytest.raises(CI.IssueError):
        CI.apply(c, j, [{"id": g["id"], "option": "edit", "value": "X"}], "Priya Shah")
    b = by_cause(CI.model(c, j), "blankreq")
    with pytest.raises(CI.IssueError, match="Type the value"):
        CI.apply(c, j, [{"id": b["id"], "option": "edit", "value": " "}], "Priya Shah")
    CI.apply(c, j, [{"id": b["id"], "option": "edit", "value": "Cedar Hollow Partners"}], "Priya Shah")
    assert j["fill"]


def test_an_approved_job_never_changes(c: Console) -> None:
    j = job5(c)
    c.act("acceptall")
    decide_ids(c)
    c.act("approve", who="reviewer-a")
    with pytest.raises(CI.IssueError, match="approved"):
        CI.apply(c, j, [{"id": "x", "option": "out"}], "Priya Shah")


# ---- the source window -------------------------------------------------------------------------------------------
def test_the_source_window_is_read_from_the_parquet_cache_at_the_asked_rows(c: Console) -> None:
    j = job5(c)
    w = CI.source_window(c, j, row=9, count=2)
    assert w["sheet"] == "Investor" and [r["row"] for r in w["rows"]] == [9, 10]
    assert w["header"][0] == "Row Labels" and w["rows"][0]["cells"][0] == "HS-REF-0000000000000042"
    assert w["letters"][0] == "A" and w["total"] >= 16
    assert len(CI.source_window(c, j, row=1, count=10_000)["rows"]) <= CI.WINDOW_MAX


# ---- the routes ----------------------------------------------------------------------------------------------------
def test_the_routes_page_decide_and_undo_with_the_reviewer_header(tmp_path: Path) -> None:
    app = create_app(tmp_path / "state", environ={"WORKER_MODEL_PROVIDER": "demo",
                                                  "WORKER_ENV_FILE": str(tmp_path / "t.env")})
    http = TestClient(app)
    for a in ("start|Sponsor A|Investors", "ask_set|cov|all", "ask_sample", "ask_go", "ex|no", "go"):
        http.post("/api/console/act", json={"action": a})
    jid = http.get("/api/console/state").json()["job"]["id"]
    m = http.get(f"/api/console/jobs/{jid}/issues?per=1").json()
    assert m["verdict"]["sum"] == m["verdict"]["N"] and len(m["groups"]) == 1 and m["total"] >= 2
    gid = m["groups"][0]["id"]
    rows = http.get(f"/api/console/jobs/{jid}/issues/{gid}/rows?per=1").json()
    assert rows["total"] == m["groups"][0]["n"] and rows["per"] == 1
    body = {"picks": [{"id": gid, "option": m["groups"][0]["options"][0]["id"]}]}
    assert http.post(f"/api/console/jobs/{jid}/issues/preview", json=body).json()["n"] == m["groups"][0]["n"]
    assert http.post(f"/api/console/jobs/{jid}/issues/apply", json=body).status_code == 409  # "local" is no person
    ok = http.post(f"/api/console/jobs/{jid}/issues/apply", json=body, headers={"X-Reviewer": "Priya Shah"})
    assert ok.status_code == 200, ok.text
    did = ok.json()["decisions"][0]["id"]
    undone = http.post(f"/api/console/jobs/{jid}/issues/undo", json={"decision": did},
                       headers={"X-Reviewer": "Priya Shah"})
    assert undone.status_code == 200 and undone.json()["reverted"] == [did]
    assert http.get(f"/api/console/jobs/{jid}/source?row=2&count=3").json()["rows"][0]["row"] == 2


def worker_msg(c: Console) -> dict[str, Any]:
    """The last worker message that carries an action (the status nudge is always appended after the thread)."""
    return next(m for m in reversed(state_view(c)["job"]["chat"]) if m.get("options"))


def test_a_sentence_in_the_chat_becomes_a_preview_with_one_apply_and_nothing_changes_before(c: Console) -> None:
    j = job5(c)
    g = by_cause(CI.model(c, j), "idlong")
    ctx = {"job": j["id"], "group": g["id"]}
    c.act("say", "Update to the shortened IDs", "Priya Shah", ctx)
    last = worker_msg(c)
    assert "2 rows" in last["text"] and [o["label"] for o in last["options"]] == ["Apply"]
    assert j["dec"] == {} and j["rules"]["id"] == "error"  # a sentence changes nothing
    c.act(last["options"][0]["action"], "", "Priya Shah")
    assert j["rules"]["id"] == "drop"
    done = worker_msg(c)
    assert [o["label"] for o in done["options"]] == ["Undo"]
    c.act(done["options"][0]["action"], "", "Priya Shah")
    assert j["rules"]["id"] == "error"


def test_the_apply_button_refuses_a_name_that_is_no_person(c: Console) -> None:
    j = job5(c)
    g = by_cause(CI.model(c, j), "blankreq")
    c.act("say", "Leave these out until supplied", "Priya Shah", {"job": j["id"], "group": g["id"]})
    assert worker_msg(c)["options"][0]["label"] == "Apply"
    action = worker_msg(c)["options"][0]["action"]
    with pytest.raises(ConsoleError):
        c.act(action, "", "local")
    assert j["dec"] == {}


def test_secondary_actions_are_links_not_buttons() -> None:
    js = (Path(CI.__file__).parent / "static" / "issues.js").read_text(encoding="utf-8")
    pane = (Path(CI.__file__).parent / "static" / "console.js").read_text(encoding="utf-8")
    for key in ("why", "where", "next", "rows", "rvx"):
        m = re.search(r'<button class="([^"]*)" data-is="' + key, js)
        assert m and m.group(1) == "lnk", key
    assert re.search(r'<button class="lnk" data-\$\{k\}="rj"', pane)  # the shared pager's jump is a link too


def test_the_chat_does_not_say_ready_for_approval_while_rows_are_on_hold(c: Console) -> None:
    j = job5(c)
    groups = [g for g in CI.model(c, j)["groups"] if g["state"] != "hold"]
    CI.apply(c, j, [{"id": g["id"], "option": "park", "value": "waiting on client"} for g in groups], "Priya Shah")
    v = CI.model(c, j)["verdict"]
    assert v["hold"] >= 1 and v["blocked"] == 0
    last = state_view(c)["job"]["chat"][-1]["text"]
    assert "on hold" in last and "Ready for your approval" not in last


# ---- one row at a time -----------------------------------------------------------------------------------------
def _audit(c: Console, j: dict[str, Any]) -> list[dict[str, Any]]:
    path = c.job_dir(j) / "audit.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_each_row_of_a_group_can_get_its_own_value_and_the_audit_keeps_before_and_after(c: Console) -> None:
    j = job5(c)
    g = by_cause(CI.model(c, j), "idlong")
    page = CI.rows_page(c, j, g["id"])
    assert {o["id"] for o in page["row_options"]} >= {"edit", "out", "park"}  # a row offers edit though the group is not bulk
    CI.apply(c, j, [{"id": g["id"], "option": "edit", "value": "INV-A", "rows": [9]}], "Priya Shah", note="typo in the export")
    g2 = by_cause(CI.model(c, j), "idlong")
    assert g2["n"] == 1  # row 10 is still open: only row 9 was decided
    CI.apply(c, j, [{"id": g2["id"], "option": "edit", "value": "INV-B", "rows": [10]}], "Priya Shah")
    assert j["dec"]["8"].endswith("INV-A") and j["dec"]["9"].endswith("INV-B")  # keyed by record index = sheet row - 1
    done = [a for a in _audit(c, j) if a["action"] == "issues.decided"]
    assert [d["inputs"]["rows"] for d in done] == [1, 1] and all(d["inputs"]["named_rows"] for d in done)
    assert done[0]["inputs"]["changes"] == [{"row": 9, "before": "HS-REF-0000000000000042", "after": "INV-A"}]
    assert done[0]["inputs"]["reviewer"] == "Priya Shah" and "typo in the export" in done[0]["rationale"]
    # undo takes back that one row only
    CI.undo(c, j, "Priya Shah", decision=CI.history(j)[0]["id"])
    assert "9" not in j["dec"] and "8" in j["dec"]


def test_one_row_can_be_skipped_while_the_rest_of_the_group_is_fixed(c: Console) -> None:
    j = job5(c)
    g = by_cause(CI.model(c, j), "idlong")
    CI.apply(c, j, [{"id": g["id"], "option": "out", "rows": [10]}], "Priya Shah")
    g2 = by_cause(CI.model(c, j), "idlong")
    assert g2["n"] == 1 and CI.rows_page(c, j, g2["id"])["rows"][0]["row"] == 9
    assert j["dec"]["9"].startswith("exclude")


def test_naming_rows_that_are_not_in_the_group_is_refused(c: Console) -> None:
    j = job5(c)
    g = by_cause(CI.model(c, j), "idlong")
    for bad in ([1], [9, 999], ["x"], list(range(1, CI.ROWS_PER_DECISION + 2))):
        with pytest.raises(CI.IssueError):
            CI.apply(c, j, [{"id": g["id"], "option": "out", "rows": bad}], "Priya Shah")
    assert not j["dec"]


# ---- any cell of a row ------------------------------------------------------------------------------------------------
def test_any_cell_of_a_row_can_be_edited_and_the_row_is_checked_again_as_corrected(c: Console) -> None:
    j = job5(c)
    g = by_cause(CI.model(c, j), "blankreq")  # Investor_Name is blank on one row
    page = CI.rows_page(c, j, g["id"])
    assert "Investor" in page["columns"]  # the file's own headers: any of them can be edited
    before_blocked = CI.verdict(c, j)["blocked"]
    CI.apply(c, j, [{"id": g["id"], "option": "cell", "rows": [page["rows"][0]["row"]], "column": "Investor",
                     "value": "Typed In Holdings LLC"}], "Priya Shah", note="the client confirmed the name by email")
    assert CI.verdict(c, j)["blocked"] == before_blocked - 1  # the name is read corrected, so the row is no longer blank
    done = [a for a in _audit(c, j) if a["action"] == "issues.decided"][-1]["inputs"]
    assert done["option"] == "cell" and done["column"] == "Investor" and done["reviewer"] == "Priya Shah"
    assert done["changes"] == [{"row": page["rows"][0]["row"], "column": "Investor", "before": "", "after": "Typed In Holdings LLC"}]
    assert j["cells"] and CI.history(j)[0]["label"] == "Edit Investor"
    CI.undo(c, j, "Priya Shah", decision=CI.history(j)[0]["id"])  # one undo takes the correction back
    assert not j["cells"] and CI.verdict(c, j)["blocked"] == before_blocked


def test_a_cell_edit_that_does_not_fix_the_cause_leaves_the_row_open_and_the_shared_records_untouched(c: Console) -> None:
    from worker import engine_console as EC

    j = job5(c)
    g = by_cause(CI.model(c, j), "idlong")
    row = CI.rows_page(c, j, g["id"])["rows"][0]["row"]
    shared = EC.read_static_records(Path(j["up"]["path"]))[2]
    was = dict(shared[row - 2]["v"])
    CI.apply(c, j, [{"id": g["id"], "option": "cell", "rows": [row], "column": "Notes", "value": "unrelated"}], "Priya Shah")
    assert by_cause(CI.model(c, j), "idlong")["n"] == g["n"]  # the long ID is still long
    assert EC.read_static_records(Path(j["up"]["path"]))[2][row - 2]["v"] == was  # the file's records are never changed


@pytest.mark.parametrize("pick", [
    {"option": "cell", "rows": [9, 10], "column": "Investor", "value": "x"},   # one row at a time
    {"option": "cell", "rows": [9], "column": "Not A Column", "value": "x"},   # a column of this file
    {"option": "cell", "rows": [9], "column": "Investor", "value": "  "},      # a value
    {"option": "cell", "column": "Investor", "value": "x"},                    # a named row
])
def test_a_cell_edit_names_one_row_a_real_column_and_a_value(c: Console, pick: dict[str, Any]) -> None:
    j = job5(c)
    g = by_cause(CI.model(c, j), "idlong")
    with pytest.raises(CI.IssueError):
        CI.apply(c, j, [{"id": g["id"], **pick}], "Priya Shah")
    assert not j.get("cells")


def test_the_history_names_the_decision_not_the_person(c: Console) -> None:
    j = job5(c)
    c.actor = "Priya Shah <priya@example.com>"  # a decision is recorded for the person, but its label is what was decided
    g = by_cause(CI.model(c, j), "idlong")
    CI.apply(c, j, [{"id": g["id"], "option": "out", "rows": [9]}], "Priya Shah")
    h = CI.history(j)[0]
    assert h["label"] == dict(CI.causes()["idlong"].options)["out"] and h["who"] == "Priya Shah"
