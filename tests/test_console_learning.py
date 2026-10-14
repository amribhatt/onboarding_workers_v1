"""UI parity: the job screen's learning parts (reconcile, teach-back, feedback, rulebook) from real state."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from id_decision import decide_ids
from worker import console_engine as CE
from worker import console_learning as L
from worker.audit import AuditLog
from worker.console import Console
from worker.web import STATIC, create_app


@pytest.fixture
def c(tmp_path: Path) -> Console:
    return Console(tmp_path / "inbox")


def start(c: Console, sponsor: str, typ: str, fund: str | None = None) -> dict[str, Any]:
    c.act(f"start|{sponsor}|{typ}")
    if typ.startswith("GL"):
        if fund:
            c.act(f"ask_set|fund|{fund}")
    else:
        c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    j = c.job()
    assert j is not None
    return j


def investors(c: Console) -> dict[str, Any]:
    j = start(c, "Sponsor A", "Investors")
    c.act("ex|no")
    c.act("go")
    return j


def gl_fund(c: Console) -> dict[str, Any]:
    c.act("prov|paid")
    j = start(c, "Sponsor B", "GL Fund", "B Fund I")
    c.act("go")
    return j


# ---- reconcile -----------------------------------------------------------------------------------------------
def test_recon_empty_before_the_file_is_converted(c: Console, tmp_path: Path) -> None:
    c.act("start|Sponsor A|Investors")
    c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    j = c.job()
    assert j is not None
    j["runs"] = []
    v = L.learning_view(c, tmp_path, "recon", j["id"])
    assert v["checks"] == [] and "not been converted" in v["empty"]


def test_recon_static_is_the_tally_and_nothing_else(c: Console, tmp_path: Path) -> None:
    j = investors(c)
    v = L.learning_view(c, tmp_path, "recon", j["id"])
    t = CE.tally(c, j)
    assert v["gl"] is False and v["fund_types"] == [] and [k["id"] for k in v["checks"]] == ["tie"]
    row = v["checks"][0]["rows"][0]
    assert (row["a"], row["b"]) == (t["read"], t["out"] + t["skip"])
    assert row["diff"] == t["err"] and (row["status"] == "PASS") == (t["err"] == 0)


def test_recon_static_ties_once_everything_is_resolved(c: Console, tmp_path: Path) -> None:
    j = investors(c)
    c.act("acceptall")
    v = L.learning_view(c, tmp_path, "recon", j["id"])
    assert v["checks"][0]["worst"] == "PASS" and v["checks"][0]["rows"][0]["diff"] == 0


def test_recon_gl_figures_come_from_the_journals(c: Console, tmp_path: Path) -> None:
    j = gl_fund(c)
    v = L.learning_view(c, tmp_path, "recon", j["id"])
    ids = [k["id"] for k in v["checks"]]
    assert ids[:2] == ["tie", "bal"] and len(ids) == 2 + 11
    e = CE.gl_eval(c, j)
    bal = next(k for k in v["checks"] if k["id"] == "bal")
    last = bal["rows"][-1]
    assert last["a"] == round(sum(x["dr"] for x in e["js"]), 2) and last["b"] == round(sum(x["cr"] for x in e["js"]), 2)
    for r in bal["rows"][:-1]:
        x = next(z for z in e["js"] if f"Journal {z['j']}" == r["label"])
        assert r["diff"] == x["diff"]


def test_recon_guide_checks_that_need_a_report_carry_no_client_figures(c: Console, tmp_path: Path) -> None:
    j = gl_fund(c)
    v = L.learning_view(c, tmp_path, "recon", j["id"])
    need = [k for k in v["checks"] if k["state"] != "run"]
    assert {k["id"] for k in need} >= {"tb", "bs", "soi", "is", "cap", "cf", "pcap", "irr", "unf", "wf", "evl"}
    for k in need:  # the upload side may be computed; a client figure is never invented
        assert all(r["b"] is None or r.get("note", "").startswith("tested against zero") for r in k["rows"])
    assert "client" in next(k for k in need if k["id"] == "tb")["note"]
    assert next(k for k in need if k["id"] == "tb")["state"] == "needs-report"
    assert next(k for k in need if k["id"] == "evl")["state"] == "waiting"


def test_recon_tiles_count_per_fund_type(c: Console, tmp_path: Path) -> None:
    j = gl_fund(c)
    v = L.learning_view(c, tmp_path, "recon", j["id"])
    de = v["by_ft"]["DE"]
    on = [k for k in v["checks"] if "DE" in k["scope"]]
    assert de["applies"] == len(on)
    assert sum(de[k] for k in ("pass", "warn", "error", "review", "needs_report", "gap", "waiting")) == len(on)
    assert v["by_ft"]["LP"]["applies"] > v["by_ft"]["EL"]["applies"] - 1  # the LP-only guide checks apply to LP


def test_recon_entity_vs_lp_runs_when_both_exist(c: Console, tmp_path: Path) -> None:
    gl = gl_fund(c)
    c.act("acceptall")
    decide_ids(c)
    c.act("approve", who="reviewer-a")
    assert gl["approved"]
    lp = start(c, "Sponsor B", "GL LP", "B Fund I")
    c.act("go")
    v = L.learning_view(c, tmp_path, "recon", lp["id"])
    evl = next(k for k in v["checks"] if k["id"] == "evl")
    assert evl["state"] == "run" and "GL Fund" in evl["note"]
    off = next(k for k in v["checks"] if k["id"] == "off")
    assert off["state"] == "run" and off["rows"][0]["b"] == sum(1 for o in CE.gl_eval(c, lp)["offsets"]
                                                                if o["st"] == "ok")


# ---- teach-back -----------------------------------------------------------------------------------------------
def learned(c: Console, tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    j = investors(c)
    c.act("acceptall")
    v = L.learning_view(c, tmp_path, "teach", j["id"])
    assert v["items"], "accepting suggestions teaches the worker something"
    return j, v


def test_teach_lists_what_accepting_taught_and_the_share_decided_by_rules(c: Console, tmp_path: Path) -> None:
    j, v = learned(c, tmp_path)
    t = CE.tally(c, j)
    people = len(j["dec"]) + len(j["qa"]) + len(j["dates"]) + len(j["acct"])
    assert v["by_you"] == people and v["rules_pct"] == round((t["read"] - min(people, t["read"])) / t["read"] * 100)
    assert all(x["st"] == "new" and x["scope"] in (None, "sponsor") for x in v["items"])
    assert "From this job I learned" in v["title"]


def test_teach_with_nothing_learned_is_honest(c: Console, tmp_path: Path) -> None:
    j = investors(c)
    v = L.learning_view(c, tmp_path, "teach", j["id"])
    assert v["items"] == [] and v["title"] == "Nothing new to learn from this job."


def act(c: Console, root: Path, **body: Any) -> dict[str, Any]:
    return L.learning_act(c, root, body, "reviewer-1")


def test_confirm_needs_a_scope_first(c: Console, tmp_path: Path) -> None:
    j, _v = learned(c, tmp_path)
    j["learn"][0]["scope"] = None
    with pytest.raises(L.LearningError, match="how far"):
        act(c, tmp_path, action="confirm", job=j["id"], i=0)
    assert j["learn"][0]["st"] == "new"


def test_sponsor_scope_writes_a_rule_into_the_rulebook_and_the_audit(c: Console, tmp_path: Path) -> None:
    j, v = learned(c, tmp_path)
    text = v["items"][0]["t"]
    assert L.learning_view(c, tmp_path, "book", j["id"])["rules"] == []
    act(c, tmp_path, action="scope", job=j["id"], i=0, scope="sponsor")
    r = act(c, tmp_path, action="confirm", job=j["id"], i=0)
    assert r["rule"] == "R1" and j["learn"][0]["st"] == "ok"
    book = L.learning_view(c, tmp_path, "book", j["id"])
    assert [(x["text"], x["by"], x["job"]) for x in book["rules"]] == [
        (text, "reviewer-1", f"{j['id']} (Investors, {j['fund']})")]
    log = AuditLog(tmp_path / "feedback" / "sponsor-a" / "feedback.jsonl")
    assert log.first_break()[1] is None
    rec = log.read()[-1]["inputs"]
    assert rec["scope"] == "sponsor" and rec["action"] == "accept" and rec["shown"] == text
    assert AuditLog(c.job_dir(j) / "audit.jsonl").first_break()[1] is None
    teach = L.learning_view(c, tmp_path, "teach", j["id"])["items"][0]
    assert teach["st"] == "ok" and teach["where"] == "in Sponsor A's rulebook"


def test_job_and_org_scope_never_touch_the_sponsor_rulebook(c: Console, tmp_path: Path) -> None:
    j, _v = learned(c, tmp_path)
    act(c, tmp_path, action="scope", job=j["id"], i=0, scope="job")
    assert act(c, tmp_path, action="confirm", job=j["id"], i=0)["rule"] is None
    assert L.learning_view(c, tmp_path, "book", j["id"])["rules"] == []
    if len(j["learn"]) > 1:
        act(c, tmp_path, action="scope", job=j["id"], i=1, scope="org")
        act(c, tmp_path, action="confirm", job=j["id"], i=1)
        assert L.learning_view(c, tmp_path, "book", j["id"])["rules"] == []
        last = AuditLog(tmp_path / "feedback" / "sponsor-a" / "feedback.jsonl").read()[-1]["inputs"]
        assert last["scope"] == "organisation"
    with pytest.raises(L.LearningError, match="already decided"):
        act(c, tmp_path, action="scope", job=j["id"], i=0, scope="sponsor")


def test_not_a_rule_is_recorded_as_a_rejection(c: Console, tmp_path: Path) -> None:
    j, _ = learned(c, tmp_path)
    act(c, tmp_path, action="reject", job=j["id"], i=0)
    assert j["learn"][0]["st"] == "no"
    assert AuditLog(tmp_path / "feedback" / "sponsor-a" / "feedback.jsonl").read()[-1]["inputs"]["action"] == "reject"
    assert L.learning_view(c, tmp_path, "book", j["id"])["rules"] == []


def test_nothing_changes_when_the_record_cannot_be_written(c: Console, tmp_path: Path) -> None:
    j, _ = learned(c, tmp_path)
    act(c, tmp_path, action="scope", job=j["id"], i=0, scope="sponsor")
    blocker = tmp_path / "feedback"
    blocker.write_text("not a folder", encoding="utf-8")
    with pytest.raises(L.LearningError):
        act(c, tmp_path, action="confirm", job=j["id"], i=0)
    assert j["learn"][0]["st"] == "new"
    assert not (tmp_path / "memory").exists()


# ---- feedback ----------------------------------------------------------------------------------------------
def test_feedback_is_empty_then_lists_a_flag_with_its_reason(c: Console, tmp_path: Path) -> None:
    j = investors(c)
    v = L.learning_view(c, tmp_path, "fb", j["id"])
    assert v["items"] == [] and "Use the flag" in v["empty"]
    act(c, tmp_path, action="flag", job=j["id"], kind="mapping", ref="Investor_Type", skill="detect-columns@4",
        text="this is the wrong column")
    act(c, tmp_path, action="flag", job=j["id"], kind="row", ref="row 7")  # no comment still counts
    items = L.learning_view(c, tmp_path, "fb", j["id"])["items"]
    assert [(x["kind"], x["ref"], x["text"]) for x in items] == [
        ("flag · mapping", "Investor_Type", "this is the wrong column"), ("flag · row", "row 7", "")]
    assert items[0]["skill"] == "detect-columns@4" and items[0]["routed"]


def test_feedback_shows_the_overrides_the_console_kept(c: Console, tmp_path: Path) -> None:
    j = investors(c)
    c.act("col|Investor_Name|", who="reviewer-a")
    items = L.learning_view(c, tmp_path, "fb", j["id"])["items"]
    assert any(x["kind"] == "override" and x["ref"] == "Investor_Name" for x in items)


def test_flag_refuses_an_unknown_kind(c: Console, tmp_path: Path) -> None:
    j = investors(c)
    with pytest.raises(L.LearningError, match="(?i)kind must be"):
        act(c, tmp_path, action="flag", job=j["id"], kind="mood", ref="x")


def test_book_is_empty_without_rules(c: Console, tmp_path: Path) -> None:
    j = investors(c)
    v = L.learning_view(c, tmp_path, "book", j["id"])
    assert v["rules"] == [] and v["pending"] == [] and "Rules you confirm" in v["empty"]


def test_unknown_job_and_part_are_refused(c: Console, tmp_path: Path) -> None:
    with pytest.raises(L.LearningError):
        L.learning_view(c, tmp_path, "recon", "j999")
    investors(c)
    with pytest.raises(L.LearningError):
        L.learning_view(c, tmp_path, "nope", c.job()["id"])  # type: ignore[index]


# ---- endpoints and page wiring -----------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(str(tmp_path / "st")))


def test_endpoints_round_trip(client: TestClient) -> None:
    for a in ("start|Sponsor A|Investors", "ask_set|cov|all", "ask_sample", "ask_go", "ex|no", "go", "acceptall"):
        client.post("/api/console/act", json={"action": a})
    jid = client.get("/api/console/state").json()["job"]["id"]
    rc = client.get("/api/console/learning/recon", params={"job": jid}).json()
    assert rc["checks"][0]["id"] == "tie"
    teach = client.get("/api/console/learning/teach", params={"job": jid}).json()
    assert teach["items"]
    r = client.post("/api/console/learning/act", json={"action": "scope", "job": jid, "i": 0, "scope": "sponsor"})
    assert r.status_code == 200
    r = client.post("/api/console/learning/act", json={"action": "confirm", "job": jid, "i": 0})
    assert r.json()["rule"] == "R1"
    assert client.get("/api/console/learning/book", params={"job": jid}).json()["rules"][0]["id"] == "R1"
    assert client.get("/api/console/learning/recon", params={"job": "j404"}).status_code == 404
    bad = client.post("/api/console/learning/act", json={"action": "confirm", "job": jid, "i": 0})
    assert bad.status_code == 409


def test_page_loads_the_learning_script_once(client: TestClient) -> None:
    html = (STATIC / "console.html").read_text(encoding="utf-8")
    assert html.count('src="/learning.js"') == 1
    js = client.get("/learning.js")
    assert js.status_code == 200 and "window.LEARNING" in js.text and "render" in js.text


def test_the_reconcile_rules_are_given_the_sponsor_root_not_its_tables_folder(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from worker import recon as R
    from worker import templates as T

    root = tmp_path / "templates"
    (root / "sponsors" / "sponsor-a" / "skills" / "reconcile-gl").mkdir(parents=True)
    monkeypatch.setenv("WORKER_TEMPLATES_DIR", str(root))
    seen: list[Any] = []
    monkeypatch.setattr(R, "rules_for", lambda *a, **k: seen.append(a) or "rules")
    assert L._rules("Sponsor A", None) == "rules"
    assert seen == [(T.sponsors_dir() / "sponsor-a", None, "sponsor-a")]  # the root, never <s>/tables
    seen.clear()
    L._rules("Sponsor Z", None)  # no folder: nothing is passed, the engine finds the organisation's rules
    assert seen == [(None, None, "sponsor-z")]
