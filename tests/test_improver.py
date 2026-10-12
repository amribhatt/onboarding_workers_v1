from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from worker.accounts import Coa, TypeMap, golden_versions, key_of, map_keys
from worker.audit import AuditLog
from worker.improver import (
    AccountReplay,
    ImprovementStore,
    ReplayCase,
    WriteRefused,
    check_prediction,
    cluster,
    evaluate,
    fast_loop,
    guard,
    noise_gate,
    propose_golden_mapping,
)
from worker.skills import load_skills
from worker.templates import standards_dir

SKILL = load_skills()["map-accounts"]
VAL = {"account_type": "", "account": "6150 Valuation adjustment", "transaction_type": "Valuation", "rows": 4}
BANK = {"account_type": "Bank", "account": "2000", "transaction_type": "Capital call", "rows": 9}
LEGAL = {"account_type": "", "account": "Legal fees", "transaction_type": "Quarterly review", "rows": 2}


def _key(info: dict[str, Any]) -> str:
    return key_of(info["account_type"], info["account"], info["transaction_type"])


def _case(sponsor: str, info: dict[str, Any], expected: str, **kw: Any) -> ReplayCase:
    return ReplayCase(f"{sponsor}:{_key(info)}", sponsor, "GL", _key(info), info, expected, **kw)


def _cases() -> list[ReplayCase]:
    return [
        _case("sponsor-a", VAL, "42020"),
        _case("sponsor-b", VAL, "42020"),
        _case("sponsor-c", VAL, "42020"),
        _case("sponsor-a", BANK, "11010", signed_off=True),  # applied by L1; a person signed this output off
        _case("sponsor-b", LEGAL, "51020"),
    ]


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Any]:
    base = standards_dir()
    return {
        "replay": AccountReplay(Coa.load(base / "Golden_COA.csv"),
                                TypeMap.load(base / "Transaction_Type_Mapping.csv"), SKILL.table("coa-cascade")),
        "store": ImprovementStore(tmp_path / "improvements", AuditLog(tmp_path / "audit.jsonl")),
        "golden": golden_versions(tmp_path / "accounts"),
        "tmp": tmp_path,
    }


def _proposal(key: str, account: str, scope: str = "organisation", by: str = "improver") -> dict[str, Any]:
    cause = {"key": key, "chosen": account, "count": 6, "sponsors": ["sponsor-a", "sponsor-b", "sponsor-c"]}
    return propose_golden_mapping(cause, proposed_by=by, scope=scope)


def test_a_row_that_fixes_three_cases_and_changes_no_signed_off_output_passes(world: dict[str, Any]) -> None:
    card = world["store"].file(_proposal(_key(VAL), "42020"), _cases(), world["replay"], world["golden"])
    gate = card["replay"]
    assert card["status"] == "ready" and gate["passed"]
    assert len(gate["fixes"]) == 3 and gate["regressions"] == [] and gate["signed_off_changed"] == []
    assert gate["asked_after"] == gate["asked_before"] - 3
    assert world["golden"].version == 0  # filing changes nothing live


def test_a_change_to_a_signed_off_output_is_blocked(world: dict[str, Any]) -> None:
    # 15910 is not allowed for a capital call: the signed-off L1 result (11010) would be lost
    card = world["store"].file(_proposal(_key(BANK), "15910"), _cases(), world["replay"], world["golden"])
    assert card["status"] == "blocked"
    assert "changes a signed-off output" in card["replay"]["reasons"]
    assert card["replay"]["signed_off_changed"] == [f"sponsor-a:{_key(BANK)}"]
    with pytest.raises(ValueError, match="only a ready card"):
        world["store"].promote(card["id"], "alex", world["golden"], admin=True, reason="try anyway")


def test_an_attempt_to_edit_the_scorer_or_the_replay_set_is_refused_and_audited(world: dict[str, Any]) -> None:
    for kind, target in [("know_how", "src/worker/score.py"), ("table_row", "tests/fixtures/set7/answer_key/x.csv"),
                         ("synonym", "skills/map-accounts/scripts/map_keys.py"), ("example", "src/worker/audit.py"),
                         ("know_how", "src/worker/prompts.py"), ("threshold", ".worker_state/memory/s/cases/c.json"),
                         ("script", "skills/x/scripts/run.py")]:
        with pytest.raises(WriteRefused):
            guard(kind, target)
    bad = {**_proposal("k", "11010"), "kind": "know_how", "target": "src/worker/score.py"}
    with pytest.raises(WriteRefused):
        world["store"].file(bad, _cases(), world["replay"], world["golden"])
    refused = [e for e in AuditLog(world["tmp"] / "audit.jsonl").read() if e["action"] == "improver.refused"]
    assert refused and "score.py" in refused[0]["rationale"]
    guard("table_row", "skills/map-accounts/tables/coa-cascade.yaml")  # an allowed surface does not raise


def test_a_gain_within_noise_is_rejected() -> None:
    assert not noise_gate([0.80, 0.82, 0.78], [0.81, 0.80, 0.83])["passed"]
    ok = noise_gate([0.80, 0.82, 0.78], [0.95, 0.96, 0.94])
    assert ok["passed"] and ok["gain"] > ok["noise"]
    few = noise_gate([0.8, 0.8], [0.9, 0.9])
    assert not few["passed"] and "at least 3 runs" in few["reason"]


def test_rollback_restores_the_previous_version_exactly(world: dict[str, Any]) -> None:
    store, golden = world["store"], world["golden"]
    first = store.file(_proposal(_key(VAL), "42020"), _cases(), world["replay"], golden)
    store.promote(first["id"], "alex", golden, admin=True, reason="three sponsors agree")
    v1 = golden.current()
    assert golden.version == 1 and v1[_key(VAL)]["account"] == "42020"
    second = store.file(_proposal(_key(LEGAL), "51020"), _cases(), world["replay"], golden)
    assert second["status"] == "ready"
    store.promote(second["id"], "alex", golden, admin=True, reason="legal fees")
    assert golden.version == 2 and _key(LEGAL) in golden.current()
    store.rollback(golden, "alex", "legal row was premature")
    assert golden.version == 1 and golden.current() == v1
    assert json.dumps(golden.current(), sort_keys=True) == json.dumps(v1, sort_keys=True)
    assert [c["status"] for c in store.cards()].count("rolled_back") == 1
    with pytest.raises(PermissionError):
        store.rollback(golden, " ", "no name")


def test_the_improver_never_approves_its_own_change_and_organisation_scope_needs_an_admin(world: dict[str, Any]) -> None:
    store, golden = world["store"], world["golden"]
    card = store.file(_proposal(_key(VAL), "42020"), _cases(), world["replay"], golden)
    with pytest.raises(PermissionError, match="never approves its own"):
        store.promote(card["id"], "improver", golden, admin=True, reason="self approved")
    with pytest.raises(PermissionError, match="needs an admin"):
        store.promote(card["id"], "alex", golden, admin=False, reason="not an admin")
    with pytest.raises(ValueError, match="reason is required"):
        store.promote(card["id"], "alex", golden, admin=True, reason=" ")
    done = store.promote(card["id"], "alex", golden, admin=True, reason="three sponsors agree")
    assert done["status"] == "promoted" and done["approved_by"] == "alex" and done["version"] == 1


def test_a_promoted_row_is_pinned_to_future_jobs_and_changes_no_finished_work(world: dict[str, Any]) -> None:
    store, golden = world["store"], world["golden"]
    coa, types, table = world["replay"].coa, world["replay"].types, world["replay"].table
    info = {_key(VAL): VAL}
    before = map_keys(info, coa, types, {}, golden.current(), table)[0]
    assert before.action == "needs_you"
    card = store.file(_proposal(_key(VAL), "42020"), _cases(), world["replay"], golden)
    store.promote(card["id"], "alex", golden, admin=True, reason="agreed")
    after = map_keys(info, coa, types, {}, golden.current(), table)[0]
    assert (after.row, after.account, after.action) == ("L0", "42020", "apply")
    assert golden.at(0) == {}  # the version a finished job recorded is still there, unchanged


def test_budget_parity_a_gain_bought_with_more_questions_is_rejected(world: dict[str, Any]) -> None:
    # a "better" row that fixes nothing but stops L1 from applying: more questions, and L1 outputs are lost
    row = {"id": "L1", "when": {"pinned": True, "score": ">=0.99"}, "then": {"tier": "L1 Transaction type",
                                                                            "action": "apply", "confidence": 0.85}}
    proposal = {"kind": "table_row", "target": "skills/map-accounts/tables/coa-cascade.yaml", "scope": "sponsor",
                "change": {"row": row}, "evidence": {}, "proposed_by": "improver", "speed": "fast",
                "summary": "stricter L1"}
    card = world["store"].file(proposal, _cases(), world["replay"], world["golden"], golden=world["golden"])
    reasons = card["replay"]["reasons"]
    # L1 is a control row (it applies unseen): the gate still says no, and the card waits for a controller (M11b)
    assert card["replay"]["passed"] is False and card["status"] == "awaiting_controller"
    assert "asks the analyst more often" in reasons and "changes a signed-off output" in reasons
    with pytest.raises(PermissionError):
        world["store"].promote(card["id"], "alex", world["golden"], admin=True, reason="ok")


def test_the_holdout_contributes_counts_only_and_can_block(world: dict[str, Any]) -> None:
    cases = _cases() + [_case("sponsor-d", BANK, "99999", holdout=True)]  # a holdout case the row would break
    baseline = world["replay"].run(cases, {})
    cand = world["replay"].run(cases, {_key(VAL): {"account": "42020"}, _key(BANK): {"account": "11020"}})
    gate = evaluate(cases, baseline, cand)
    assert set(gate.holdout) <= {"fixes", "regressions", "signed_off_changed"}  # numbers, never rows
    assert all(isinstance(v, int) for v in gate.holdout.values())
    assert not gate.passed


def test_fast_loop_never_auto_promotes_organisation_scope(world: dict[str, Any]) -> None:
    store, golden = world["store"], world["golden"]
    proposals = [_proposal(_key(VAL), "42020", scope="organisation"),
                 _proposal(_key(LEGAL), "51020", scope="sponsor")]
    off = fast_loop(store, proposals, _cases(), world["replay"], golden)
    assert [c["status"] for c in off] == ["ready", "ready"] and golden.version == 0
    on = fast_loop(store, proposals, _cases(), world["replay"], golden, auto_promote_sponsor=True, approver="owner-1")
    assert [c["status"] for c in on] == ["ready", "promoted"]  # the organisation one waits for an admin
    assert golden.version == 1 and on[1]["approved_by"] == "owner-1"


def test_feedback_clusters_by_cause_and_the_smallest_change_is_one_row() -> None:
    fb = [
        {"action": "override", "skill": "map-accounts@1.0", "table_row": "L5", "lineage": _key(VAL),
         "shown": "(no account) L5", "chosen": "42020", "sponsor": s, "job": f"j{i}"}
        for i, s in enumerate(["sponsor-a", "sponsor-a", "sponsor-b", "sponsor-c", "sponsor-c", "sponsor-c"])
    ] + [{"action": "accept", "lineage": "x"}, {"action": "override", "lineage": "y", "shown": "1", "chosen": "2",
                                                 "sponsor": "sponsor-a", "job": "j9"}]
    groups = cluster(fb)
    assert groups[0]["count"] == 6 and groups[0]["sponsors"] == ["sponsor-a", "sponsor-b", "sponsor-c"]
    assert len(groups) == 2  # accepts are not corrections
    p = propose_golden_mapping(groups[0])
    assert p["change"] == {"key": _key(VAL), "account": "42020"} and p["speed"] == "fast"
    assert "6 override(s) across 3 sponsor(s)" in p["summary"]


def test_predicted_versus_observed_alerts_when_they_diverge() -> None:
    card = {"id": "imp-1", "predicted": {"fixes": 6}}
    assert not check_prediction(card, 6)["alert"]
    assert check_prediction(card, 1)["alert"] and check_prediction(card, 1)["difference"] == -5
