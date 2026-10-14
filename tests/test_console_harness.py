"""Harness pages of the console: Improvements, Health, Skills and a skill's decision tables, from real state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from test_harness_metrics import _fixture
from worker.accounts import AccountStore, key_of
from worker.console_harness import (
    HarnessError,
    harness_act,
    harness_page,
    health_view,
    improvements_view,
    skill_view,
    skills_view,
)
from worker.web import STATIC, create_app

KEY = key_of("", "6150 Valuation adjustment", "Valuation")


def _app(tmp_path: Path, **env: str) -> Any:
    return create_app(tmp_path / "state", environ={"WORKER_MODEL_PROVIDER": "demo", "WORKER_ENV_FILE":
                                                   str(tmp_path / "t.env"), **env})


@pytest.fixture
def manager(tmp_path: Path) -> Any:
    return _app(tmp_path).state.manager


def _record_cases(manager: Any) -> None:
    for sp in ("sponsor-a", "sponsor-b"):
        AccountStore(manager.state_dir / "accounts", sp).add(KEY, "42020", source="approved", reason="signed off")


def _table_cells(manager: Any, edit: Any = None) -> list[dict[str, Any]]:
    cells = skill_view(manager, "map-accounts")["tables"][0]["rows"]
    cells = json.loads(json.dumps(cells))
    if edit:
        edit(cells)
    return cells


# ---- health -----------------------------------------------------------------------------------------------
def test_health_is_an_honest_empty_state_without_jobs(manager: Any) -> None:
    v = health_view(manager)
    assert v["jobs"] == 0 and v["rows"] == [] and "No finished job" in v["empty"]


def test_health_rows_are_computed_from_the_audits(manager: Any) -> None:
    _fixture(manager.runs_dir)
    v = health_view(manager, {"WORKER_PRICE_IN_PER_M": "3", "WORKER_PRICE_OUT_PER_M": "15",
                              "WORKER_REVIEWER_RATE": "60"})
    rows = {r["workflow"]: r for r in v["rows"]}
    assert set(rows) == {"GL", "Investors"} and v["priced"] is True
    gl = rows["GL"]
    assert gl["decided_without_model"] == 50 and gl["first_pass"] == 0 and gl["success_rate"] == 100
    assert gl["readiness"] == [{"week": "2026-W41", "value": 50, "promoted": False}]
    assert gl["cost_model"] == 0.009 and gl["retries_per_job"] == 1.0
    assert rows["Investors"]["upload_rejections"] == 1 and rows["Investors"]["success_rate"] == 0
    assert rows["Investors"]["readiness"] == []  # no keys: no readiness, shown as a dash, not as zero
    watch = {w["skill"]: w for w in v["watch"]}
    assert watch["map-accounts"]["loaded"] == 1 and watch["map-accounts"]["trend"][0]["value"] == 50


def test_health_without_prices_says_so(manager: Any) -> None:
    _fixture(manager.runs_dir)
    assert health_view(manager, {})["priced"] is False


# ---- skills -----------------------------------------------------------------------------------------------
def test_skill_library_lists_the_installed_skills_with_their_tables_and_cases(manager: Any) -> None:
    v = skills_view(manager)
    by = {s["name"]: s for s in v["skills"]}
    assert by["map-accounts"]["tables"] == ["coa-cascade", "deal-required"] and by["map-accounts"]["cases"] == 6
    assert by["map-accounts"]["version"] == "1.0" and by["map-accounts"]["jobs_used"] == 0
    assert by["check-data-quality"]["tables"] == ["causes", "issue-options"]  # the console cause tables; its rules are checks
    _fixture(manager.runs_dir)
    assert {s["name"]: s for s in skills_view(manager)["skills"]}["map-accounts"]["jobs_used"] == 1


def test_skill_detail_shows_the_real_decision_table_and_a_skill_of_checks(manager: Any) -> None:
    t = skill_view(manager, "map-accounts")["tables"][0]
    assert t["id"] == "coa-cascade" and t["hit"] == "first" and t["replayable"] is True
    assert list(t["inputs"]) == ["historic", "pinned", "tt_known", "score"]
    assert [r["id"] for r in t["rows"]] == ["L0", "L1", "L3", "L4", "L5"]
    assert t["rows"][2]["when"] == {"tt_known": "true", "score": ">=0.5"}
    assert t["rows"][0]["then"]["action"] == "apply" and "tier" in t["outputs"]
    plain = skill_view(manager, "check-data-quality")
    assert [t["id"] for t in plain["tables"]] == ["causes", "issue-options"] and plain["checks"]
    with pytest.raises(HarnessError) as e:
        skill_view(manager, "nope")
    assert e.value.status == 404


# ---- testing and proposing a table edit -------------------------------------------------------------------
def test_an_unchanged_table_is_not_a_passing_change(manager: Any) -> None:
    r = harness_act(manager, {"action": "test", "skill": "map-accounts", "table": "coa-cascade",
                              "rows": _table_cells(manager)}, "alex", False)
    assert r["replayable"] and r["changed"] is False and r["passed"] is False
    assert r["fixtures"]["pass_before"] == r["fixtures"]["total"] == 6


def test_a_bad_edit_is_caught_by_the_recorded_cases(manager: Any) -> None:
    def lower_l1(cells: list[dict[str, Any]]) -> None:
        next(r for r in cells if r["id"] == "L1")["when"]["pinned"] = "false"  # L1 no longer fires on pinned types

    r = harness_act(manager, {"action": "test", "skill": "map-accounts", "table": "coa-cascade",
                              "rows": _table_cells(manager, lower_l1)}, "alex", False)
    assert r["ok"] and r["changed"] and r["passed"] is False
    assert r["fixtures"]["broke"] and r["fixtures"]["pass_after"] < r["fixtures"]["pass_before"]


def test_a_malformed_table_is_refused_with_the_reason(manager: Any) -> None:
    def drop_catch_all(cells: list[dict[str, Any]]) -> None:
        cells.pop()

    with pytest.raises(HarnessError, match="last row must match anything"):
        harness_act(manager, {"action": "test", "skill": "map-accounts", "table": "coa-cascade",
                              "rows": _table_cells(manager, drop_catch_all)}, "alex", False)

    def unknown_input(cells: list[dict[str, Any]]) -> None:
        cells[0]["when"]["colour"] = "red"

    with pytest.raises(HarnessError, match="unknown inputs"):
        harness_act(manager, {"action": "test", "skill": "map-accounts", "table": "coa-cascade",
                              "rows": _table_cells(manager, unknown_input)}, "alex", False)


def test_a_table_without_a_recorded_job_set_cannot_be_tested_or_proposed(manager: Any) -> None:
    # check-data-quality has no tables; a table of another skill would be the same: no evaluator, no proposal
    with pytest.raises(HarnessError):
        harness_act(manager, {"action": "propose", "skill": "check-data-quality", "table": "x", "rows": []},
                    "alex", False)


def _loosen_l3(cells: list[dict[str, Any]]) -> None:
    next(r for r in cells if r["id"] == "L3")["when"]["score"] = ">=0.3"


def test_propose_files_a_gated_card_and_only_a_named_human_promotes_it(manager: Any) -> None:
    _record_cases(manager)
    body = {"action": "propose", "skill": "map-accounts", "table": "coa-cascade", "scope": "sponsor",
            "sponsor": "sponsor-a", "rows": _table_cells(manager, _loosen_l3)}
    filed = harness_act(manager, body, "alex", False)
    assert len(filed["cards"]) == 1
    card_id = filed["cards"][0]
    v = improvements_view(manager)
    card = next(c for c in v["ready"] + v["blocked"] if c["id"] == card_id)
    assert card["surface"] == "table_row" and card["sp"] == "sponsor-a" and card["kind"] == "fast loop"
    assert card["before"].startswith("L3: when") and ">=0.3" in card["after"]
    assert [g[0] for g in card["gates"]] == ["Fixes", "Signed-off outputs changed", "Regression on any split",
                                              "Noise", "Budget"]
    # no recorded case is fixed by loosening L3, so the gates block it and say why
    assert card["status"] == "blocked" and "no gain on any case" in card["reasons"]
    with pytest.raises(HarnessError) as blocked:
        harness_act(manager, {"action": "promote", "card": card_id}, "casey", False)
    assert blocked.value.status == 409
    # a replay that passes makes it ready (simulated here); then the overlay is versioned and reversible
    store = manager._improvement_parts()[1]
    stored = store.load(card_id)
    stored["status"] = "ready"
    store.save(stored)
    with pytest.raises(HarnessError) as e:  # the proposer cannot approve its own change
        harness_act(manager, {"action": "promote", "card": card_id}, "alex", False)
    assert e.value.status == 403
    done = harness_act(manager, {"action": "promote", "card": card_id}, "casey", False)
    assert done["status"] == "promoted" and done["version"] == 1
    assert skill_view(manager, "map-accounts")["tables"][0]["live_overlay"] == ["L3"]
    hist = improvements_view(manager)["history"]
    assert hist[0]["status"] == "promoted" and hist[0]["can_rollback"] is True
    harness_act(manager, {"action": "rollback", "surface": "table_row"}, "casey", False)
    assert improvements_view(manager)["history"][0]["status"] == "rolled_back"
    assert skill_view(manager, "map-accounts")["tables"][0]["live_overlay"] == []


def test_unchanged_table_cannot_be_proposed(manager: Any) -> None:
    with pytest.raises(HarnessError, match="(?i)nothing differs"):
        harness_act(manager, {"action": "propose", "skill": "map-accounts", "table": "coa-cascade",
                              "rows": _table_cells(manager)}, "alex", False)


# ---- improvements: golden mapping cards ---------------------------------------------------------------------
def test_improvements_page_groups_cards_and_an_org_change_needs_an_admin(tmp_path: Path) -> None:
    app = _app(tmp_path, WORKER_ADMINS="admin-1")
    m = app.state.manager
    assert improvements_view(m) == {"golden_version": 0, "admin": False, "ready": [], "blocked": [],
                                    "waiting": [], "history": []}
    for sp in ("sponsor-a", "sponsor-b", "sponsor-c"):
        AccountStore(m.state_dir / "accounts", sp).add(KEY, "42020", source="approved", reason="signed off")
    card = m.propose_improvement(KEY, "42020", "organisation", "casey")
    v = improvements_view(m, admin=True)
    c = next(x for x in v["ready"] if x["id"] == card["id"])
    assert c["scope"] == "org" and c["ev"] == 3 and c["gates"][0] == ["Fixes", "3 recorded cases", "ok"]
    assert c["after"].startswith(f"{KEY} → 42020") and "No approved mapping" in c["before"]
    with pytest.raises(HarnessError) as e:
        harness_act(m, {"action": "promote", "card": card["id"]}, "alex", False)
    assert e.value.status == 403
    ok = harness_act(m, {"action": "promote", "card": card["id"]}, "admin-1", True)
    assert ok["status"] == "promoted"
    hist = improvements_view(m)["history"]
    assert hist[0]["version"] == 1 and hist[0]["can_rollback"] is True
    assert harness_act(m, {"action": "rollback"}, "admin-1", True)["golden_version"] == 0
    assert improvements_view(m)["history"][0]["status"] == "rolled_back"


def test_waiting_evidence_is_counted_from_real_feedback(manager: Any) -> None:
    from worker.audit import AuditLog
    from worker.feedback import FeedbackStore, record

    store = FeedbackStore(manager.state_dir / "feedback", "sponsor-a")
    record(AuditLog(manager.state_dir / "a.jsonl"), store, sponsor="sponsor-a", job="j1", kind="suggestion",
           action="override", reason="wrong", shown="31010", chosen="42020", skill="map-accounts@1.0",
           lineage=KEY)
    w = improvements_view(manager)["waiting"]
    assert len(w) == 1 and w[0]["ev"] == 1 and w[0]["need"] == 3 and w[0]["sp"] == "sponsor-a"


def test_unknown_action_and_page(manager: Any) -> None:
    with pytest.raises(HarnessError):
        harness_act(manager, {"action": "approve_my_own"}, "alex", False)
    with pytest.raises(HarnessError):
        harness_page(manager, "nope")


# ---- http -------------------------------------------------------------------------------------------------
def test_http_endpoints_and_static_wiring(tmp_path: Path) -> None:
    app = _app(tmp_path, WORKER_ADMINS="admin-1")
    c = TestClient(app)
    assert c.get("/api/console/harness/health").json()["jobs"] == 0
    assert {s["name"] for s in c.get("/api/console/harness/skills").json()["skills"]} >= {"map-accounts"}
    sk = c.get("/api/console/harness/skill", params={"name": "map-accounts"}).json()
    assert sk["tables"][0]["id"] == "coa-cascade"
    assert c.get("/api/console/harness/skill", params={"name": "zzz"}).status_code == 404
    assert c.get("/api/console/harness/bogus").status_code == 404
    imp = c.get("/api/console/harness/improvements", headers={"x-reviewer": "admin-1"}).json()
    assert imp["admin"] is True and imp["ready"] == []
    r = c.post("/api/console/harness/act", json={"action": "test", "skill": "map-accounts",
                                                 "table": "coa-cascade", "rows": sk["tables"][0]["rows"]})
    assert r.status_code == 200 and r.json()["replayable"] is True
    bad = c.post("/api/console/harness/act", json={"action": "promote", "card": "imp-none"})
    assert bad.status_code == 404
    js = c.get("/harness.js")
    assert js.status_code == 200 and "window.HARNESS" in js.text
    assert '<script src="/harness.js">' in (STATIC / "console.html").read_text(encoding="utf-8")


def test_the_approver_token_guards_harness_actions(tmp_path: Path) -> None:
    c = TestClient(_app(tmp_path, WORKER_APPROVER_TOKEN="s3cret"))
    r = c.post("/api/console/harness/act", json={"action": "promote", "card": "x"})
    assert r.status_code == 401
