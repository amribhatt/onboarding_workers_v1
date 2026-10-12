"""One ID decision for the console and the agent, refused server-side while it is missing; no default approver.

The decision (keep the source ID, sequential, custom format) is a named person's, per sponsor and record type, held in
the one store `persist.id_scheme` (`IdScheme.change`). Approval and write are refused, with a reason that is on the
audit, until it exists. Approvers come only from a named human decision (`contracts.approver_name`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from worker import console_engine as CE
from worker import console_idgen as G
from worker import persist
from worker.agent import GateRefused, check_approval
from worker.audit import AuditLog
from worker.console import Console, ConsoleError
from worker.context import RunContext
from worker.contracts import UnnamedApprover
from worker.idgen import IdScheme, decision_refusal
from worker.staging import Stage
from worker.templates import TEMPLATES

SEQ = {"mode": "sequential", "prefix": "Z-", "width": 3}


def start(c: Console, typ: str = "Investors") -> dict[str, Any]:
    c.act(f"start|Sponsor A|{typ}")
    c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    j = c.job()
    assert j is not None
    if c.ex_pending(j):
        c.act("ex|no")
    c.act("go")
    c.act("acceptall")
    return j


@pytest.fixture
def c(tmp_path: Path) -> Console:
    con = Console(tmp_path / "inbox")
    con.act("prov|paid")
    return con


def engine_actions(c: Console) -> list[dict[str, Any]]:
    return AuditLog(Path(c.out_dir) / CE.ENGINE_LOG).read()


# ---- console: approval waits for the decision -----------------------------------------------------------------
def test_the_console_refuses_approval_until_the_id_decision_is_made_and_audits_the_refusal(c: Console) -> None:
    j = start(c)
    with pytest.raises(ConsoleError, match="ID decision for Sponsor A Investors has not been made"):
        c.act("approve", who="reviewer-a")
    assert not j.get("approved") and not j.get("out")
    refused = [e for e in engine_actions(c) if e["action"] == "id.decision_missing"]
    assert refused and refused[-1]["inputs"]["record_type"] == "Investors" and refused[-1]["rationale"]
    G.decide(c, {"sponsor": "Sponsor A", "record_type": "Investors", "scheme": SEQ, "reason": "client standard"},
             "reviewer-a", job_id=j["id"])
    c.act("approve", who="reviewer-a")
    assert c.job()["approved"]


def test_a_type_whose_ids_are_not_generated_needs_no_decision(c: Console) -> None:
    assert decision_refusal("Sponsor A", "Vendors", lambda: None) != ""  # every static type is asked, Vendors too
    assert decision_refusal("Sponsor A", "GL Fund", lambda: None) == ""
    assert decision_refusal("Sponsor A", "Investors", lambda: None) != ""
    assert decision_refusal("Sponsor A", "Investors", lambda: {"by": "reviewer-a"}) == ""


# ---- one decision path for the agent and the console ------------------------------------------------------------
def test_a_console_decision_is_the_agents_decision_and_the_other_way_round(c: Console) -> None:
    j = start(c)
    G.decide(c, {"sponsor": "Sponsor A", "record_type": "Investors", "scheme": SEQ, "reason": "client standard"},
             "reviewer-a", job_id=j["id"])
    store = persist.id_scheme(c.root / "ids", "sponsor-a", "Investors")
    assert store.scheme["prefix"] == "Z-" and store.decided["by"] == "reviewer-a"
    # the agent's set_id_scheme is IdScheme.change by the gate's person: the console sees it
    store.change({"mode": "sequential", "prefix": "Q-", "width": 4}, by="reviewer-b", reason="renumber",
                 record=lambda *a: None)
    assert c.id_decision("Sponsor A", "Investors")["by"] == "reviewer-b"
    assert c.id_scheme_of("Sponsor A", "Investors")["prefix"] == "Q-"
    # the same audit action and old/new shape from both
    rows = [e for e in engine_actions(c) if e["action"] == "id.scheme_changed"]
    assert rows and rows[0]["inputs"]["by"] == "reviewer-a" and set(rows[0]["inputs"]["old"]) == {"scheme", "counter"}


def test_the_agents_approval_is_refused_server_side_and_audited_until_decided(tmp_path: Path) -> None:
    ctx = RunContext(sponsor="sponsor-a", gp_id="sponsor-a", inbox_roots=[tmp_path], out_dir=tmp_path / "out",
                     audit=AuditLog(tmp_path / "out" / "audit.jsonl"), stage=Stage(tmp_path / "out"),
                     id_root=tmp_path / "ids")
    # a staged Investors table is enough for the check: it reads the template of the action, not the rows
    action = [{"name": "write_upload", "args": {"stage_id": "s1", "template": "Investors"}}]
    with pytest.raises(GateRefused, match="ID decision for sponsor-a Investors has not been made") as exc:
        check_approval(ctx, action)
    assert exc.value.review["open_err"] == ["id_decision_missing"]
    assert [e["action"] for e in ctx.audit.read()] == ["id.decision_missing"]
    IdScheme(tmp_path / "ids", "sponsor-a", "Investors", TEMPLATES["Investors"].id_generation).change(
        SEQ, by="reviewer-a", reason="client standard", record=ctx.audit.record)
    # the decision exists, so this check no longer stops it (the review of the staged table is another gate)
    try:
        check_approval(ctx, action)
    except GateRefused as exc2:
        assert "ID decision" not in str(exc2)
    except KeyError:
        pass  # no stage "s1" in this bare context: reached the table review, past the ID decision


# ---- the approver is only ever a named person ---------------------------------------------------------------------
@pytest.mark.parametrize("who", ["", "local", "agent", "assistant", "improver", "console"])
def test_the_console_has_no_default_approver(c: Console, who: str) -> None:
    j = start(c, "Vendors")
    with pytest.raises(ConsoleError):
        c.act("approve", who=who)
    assert not j.get("approved") and "approver" not in j


def test_a_named_person_approves_and_is_recorded(c: Console) -> None:
    j = start(c, "Vendors")
    G.decide(c, {"sponsor": "Sponsor A", "record_type": "Vendors", "scheme": SEQ, "reason": "client standard"},
             "reviewer-a", job_id=j["id"])
    c.act("approve", who="reviewer-a")
    assert c.job()["approver"] == "reviewer-a" and j["id"] == c.job()["id"]


def test_an_id_scheme_change_by_a_non_person_is_refused(tmp_path: Path) -> None:
    s = IdScheme(tmp_path / "ids", "sponsor-a", "Investors", TEMPLATES["Investors"].id_generation)
    for who in ("", "local", "agent", "key_resolver"):
        with pytest.raises(UnnamedApprover):
            s.change(SEQ, by=who, reason="x", record=lambda *a: None)
    assert s.decided is None


def test_a_gate_decision_with_no_named_reviewer_is_refused_by_the_library(tmp_path: Path) -> None:
    import types

    from worker.agent import decide

    agent = types.SimpleNamespace(get_state=lambda cfg: types.SimpleNamespace(tasks=[]),
                                  invoke=lambda *a, **k: {"resumed": True})
    ctx = RunContext(sponsor="sponsor-a", gp_id="sponsor-a", inbox_roots=[tmp_path], out_dir=tmp_path / "out",
                     audit=AuditLog(tmp_path / "out" / "audit.jsonl"), stage=Stage(tmp_path / "out"))
    for meta in (None, {}, {"reviewer": ""}, {"reviewer": "local"}, {"reviewer": "agent"}):
        with pytest.raises(UnnamedApprover):
            decide(agent, ctx, "t1", {"type": "approve"}, "ok", meta)
    assert ctx.audit.read() == []  # refused before anything was recorded or resumed
    decide(agent, ctx, "t1", {"type": "approve"}, "ok", {"reviewer": "local reviewer"})
    assert ctx.audit.read()[0]["inputs"]["reviewer"] == "local reviewer"


def test_the_console_refusal_points_to_the_plan_tab_not_to_an_agent_tool(c: Console) -> None:
    start(c)
    with pytest.raises(ConsoleError) as exc:
        c.act("approve", who="reviewer-a")
    assert "Plan tab" in str(exc.value) and "set_id_scheme" not in str(exc.value)


# ---- "use source ID" names the column; a column of names needs the person's own reasoning ---------------------------
def test_source_id_needs_a_column_the_person_points_to(c: Console) -> None:
    j = start(c, "Vendors")
    body = {"sponsor": "Sponsor A", "record_type": "Vendors", "scheme": {"mode": "source"}, "reason": "ids are in it"}
    with pytest.raises(ConsoleError, match="Point to the column"):
        G.decide(c, body, "reviewer-a", job_id=j["id"])
    header, recs = G._columns(c.job())
    assert header, "the sample job lists its columns"
    fit = {h: G.column_fit(recs, h, 20)[0] for h in header}  # judged by its values: a column may be called anything
    names = next(h for h, v in fit.items() if v == "names")
    random_col = next((h for h, v in fit.items() if v == "no"), "")
    if random_col:  # a column that cannot identify records is refused outright, with no tick to override it
        got = G.preview(c, {**body, "column": random_col, "name_ok": True, "job": j["id"]})
        assert any("cannot be the ID" in e["message"] for e in got["errors"])
    got = G.preview(c, {**body, "column": names, "job": j["id"]})
    assert any("reads as names" in e["message"] for e in got["errors"])
    ok = G.preview(c, {**body, "column": names, "name_ok": True, "job": j["id"]})
    assert not any("reads as names" in e["message"] for e in ok["errors"])
    shown = G.preview(c, {**body, "column": names, "name_ok": True, "job": j["id"]})
    assert [x["id"] for x in shown["ids"]] and all(x["id"] for x in shown["ids"])  # the column's own values
