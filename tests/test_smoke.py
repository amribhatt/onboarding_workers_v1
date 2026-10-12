"""M20: the model-change smoke test warns on a clearly unsuitable model and never blocks. Scripted answers only."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import openpyxl
import pytest

from scripted_model import ScriptedModel, say
from worker import smoke
from worker.audit import AuditLog
from worker.service import RunManager

ENV_A = {"WORKER_MODEL_PROVIDER": "openrouter", "WORKER_MODEL": "vendor-a/model-one"}
ENV_B = {**ENV_A, "WORKER_MODEL_SMALL": "vendor-b/model-two"}


def _answer(case: smoke.SmokeCase) -> dict[str, Any]:
    """What a suitable model says: the recorded decision, in the role's JSON."""
    if case.role == "key_resolver":
        return {"summary": "picked from the candidates", "confidence": 0.9, "output": {"picks": dict(case.expected)}}
    if case.role == "mapper":
        bindings = [{"field": f, "action": "map", "col_id": c, "reason": "the header names it", "confidence": 0.9}
                    for f, c in case.expected.items()]
        return {"summary": "bound the field", "confidence": 0.9, "output": {"bindings": bindings}}
    return {"summary": "the break rows are cited", "confidence": 0.8,
            "output": {"likely_cause": "a posting is missing from the ledger", "cites": list(case.expected)}}


def suitable(case: smoke.SmokeCase, messages: list[Any]) -> str:
    return json.dumps(_answer(case))


def outside_candidates(case: smoke.SmokeCase, messages: list[Any]) -> str:
    data = _answer(case)
    if case.role == "key_resolver":
        data["output"]["picks"] = {k: "99999" for k in case.expected}  # not a candidate
    return json.dumps(data)


def _entries(state: Path, action: str) -> list[dict[str, Any]]:
    return [e for e in AuditLog(state / "models" / "audit.jsonl").read() if e["action"] == action]


def test_the_fixed_case_set_covers_each_skill_and_role() -> None:
    cases = smoke.skill_cases()
    assert {c.role for c in cases} == {"key_resolver", "mapper", "explainer"}
    assert {c.source for c in cases} == {"skill:map-accounts/cases/cascade.json",
                                         "skill:build-static-records/cases/headers.json",
                                         "skill:reconcile-gl/cases/known-gl.json"}
    assert len({c.case_id for c in cases}) == len(cases)


def test_a_suitable_model_passes_quietly(tmp_path: Path) -> None:
    first = smoke.check_models(tmp_path, suitable, env=ENV_A)  # first sight: every role runs its cases
    assert first["warnings"] == [] and first["roles"]["search"]["cases"] == 0
    assert all(r["passed"] == r["cases"] > 0 for k, r in first["roles"].items() if k != "search")
    out = smoke.check_models(tmp_path, suitable, env=ENV_B)
    assert out["ran"] and out["warnings"] == [] and out["blocking"] is False
    small = out["roles"]["small"]
    assert small["cases"] > 0 and small["passed"] == small["cases"] and not small["warn"]
    assert _entries(tmp_path, "model.smoke_warn") == []
    assert AuditLog(tmp_path / "models" / "audit.jsonl").verify()[0]


def test_an_unchanged_model_id_does_not_run(tmp_path: Path) -> None:
    smoke.check_models(tmp_path, suitable, env=ENV_A)
    calls: list[str] = []

    def counting(case: smoke.SmokeCase, messages: list[Any]) -> str:
        calls.append(case.case_id)
        return suitable(case, messages)

    out = smoke.check_models(tmp_path, counting, env=ENV_A)
    assert out["ran"] is False and calls == []
    assert len(_entries(tmp_path, "model.changed")) == 4  # the four roles, once, at first sight


def test_a_changed_id_is_audited_with_old_and_new(tmp_path: Path) -> None:
    smoke.check_models(tmp_path, suitable, env=ENV_A)
    smoke.check_models(tmp_path, suitable, env=ENV_B)
    changed = _entries(tmp_path, "model.changed")[-1]
    assert changed["inputs"] == {"role": "small", "old": "openrouter:vendor-a/model-one",
                                 "new": "openrouter:vendor-b/model-two"}
    assert json.loads((tmp_path / "models" / "seen.json").read_text())["models"]["small"] == \
        "openrouter:vendor-b/model-two"


def test_a_pick_outside_the_candidates_warns(tmp_path: Path) -> None:
    smoke.check_models(tmp_path, suitable, env=ENV_A)
    out = smoke.check_models(tmp_path, outside_candidates, env=ENV_B)
    assert out["warnings"] and "small" in out["warnings"][0] and out["blocking"] is False
    assert any("pick outside the candidates" in h for h in out["roles"]["small"]["hard_zeros"])
    warned = _entries(tmp_path, "model.smoke_warn")
    assert len(warned) == 1 and warned[0]["inputs"]["new"] == "openrouter:vendor-b/model-two"


def test_hard_zeros_invented_binding_and_numbers(tmp_path: Path) -> None:
    mapper = next(c for c in smoke.skill_cases() if c.role == "mapper")
    explainer = next(c for c in smoke.skill_cases() if c.role == "explainer")
    bad_col = _answer(mapper)
    bad_col["output"]["bindings"][0]["col_id"] = "c99"
    got = smoke.run_case(mapper, lambda c, m: json.dumps(bad_col))
    assert not got.passed and any("invented binding" in h for h in got.hard_zeros)
    bad_num = _answer(explainer)
    bad_num["output"]["likely_cause"] = "a posting of 123457 is missing"
    got = smoke.run_case(explainer, lambda c, m: json.dumps(bad_num))
    assert not got.passed and any("not in the engine result" in h for h in got.hard_zeros)
    assert smoke.run_case(mapper, lambda c, m: "no json here").schema_valid is False


def test_pass_floor_is_a_control_unset_means_hard_zeros_only(tmp_path: Path) -> None:
    def wrong_but_valid(case: smoke.SmokeCase, messages: list[Any]) -> str:
        data = _answer(case)
        if case.role == "key_resolver":
            data["output"]["picks"] = {k: None for k in case.expected}  # a valid pick, just not the recorded one
        return json.dumps(data)

    smoke.check_models(tmp_path / "a", suitable, env=ENV_A)
    quiet = smoke.check_models(tmp_path / "a", wrong_but_valid, env=ENV_B)
    assert quiet["warnings"] == [] and quiet["roles"]["small"]["passed"] < quiet["roles"]["small"]["cases"]
    smoke.check_models(tmp_path / "b", suitable, env=ENV_A)
    floor = smoke.smoke_control({"pass_floor": 0.9})
    loud = smoke.check_models(tmp_path / "b", wrong_but_valid, env=ENV_B, control=floor)
    assert loud["warnings"] and "below the floor" in loud["warnings"][0]
    with pytest.raises(smoke.SmokeControlError):
        smoke.smoke_control({"pass_floor": 1.5})
    with pytest.raises(smoke.SmokeControlError):
        smoke.smoke_control({"floor": 0.5})


def test_model_ask_sends_the_role_brief_to_the_role_model(tmp_path: Path) -> None:
    case = next(c for c in smoke.skill_cases() if c.role == "key_resolver")
    model = ScriptedModel(script=[say(json.dumps(_answer(case)))])
    roles_asked: list[str] = []
    ask = smoke.model_ask(lambda role: roles_asked.append(role) or model)
    assert smoke.run_case(case, ask).passed
    assert roles_asked == ["small"] and "Key Resolver" in str(model.seen[0][0].content)


def _inbox(state: Path) -> None:
    (state / "inbox").mkdir(parents=True, exist_ok=True)
    book = openpyxl.Workbook()
    book.active.append(["ID", "Name"])
    book.active.append(["I1", "Alpha Capital"])
    book.save(state / "inbox" / "inv.xlsx")


def test_a_warning_shows_in_health_and_the_run_view_and_does_not_block(tmp_path: Path) -> None:
    state = tmp_path / "state"
    RunManager(state, lambda: ScriptedModel(script=[]), budget_env=ENV_A, smoke_ask=suitable).close()
    manager = RunManager(state, lambda: ScriptedModel(script=[say("looked at it")]), budget_env=ENV_B,
                         smoke_ask=outside_candidates)
    try:
        health = manager.health()["model_smoke"]
        assert health["warnings"] and health["blocking"] is False
        _inbox(state)
        run_id = manager.create_run("inv.xlsx", "sponsor-a", "GP-001")["id"]
        end = time.time() + 30
        while manager.get(run_id)["status"] == "working" and time.time() < end:
            time.sleep(0.05)
        view = manager.get(run_id)
        assert view["status"] == "idle" and view["error"] == ""  # the job ran: a warning never holds it
        assert view["model_warnings"] == health["warnings"]
    finally:
        manager.close()


def test_start_up_without_an_ask_does_nothing_unless_switched_on(tmp_path: Path) -> None:
    RunManager(tmp_path, lambda: ScriptedModel(script=[]), budget_env=ENV_A).close()
    assert not (tmp_path / "models").exists()


def test_a_smoke_test_that_cannot_run_is_reported_not_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(case: smoke.SmokeCase, messages: list[Any]) -> str:
        raise RuntimeError("provider down")

    smoke.check_models(tmp_path, suitable, env=ENV_A)
    out = smoke.check_models(tmp_path, broken, env=ENV_B)
    assert out["roles"]["small"]["passed"] == 0 and out["warnings"] == []  # failures, but no hard zero
    def unreadable() -> list[smoke.SmokeCase]:
        raise OSError("case file unreadable")

    monkeypatch.setattr(smoke, "skill_cases", unreadable)
    manager = RunManager(tmp_path / "x", lambda: ScriptedModel(script=[]), budget_env=ENV_A, smoke_ask=suitable)
    try:  # start-up went on; the health view says why there is no result, and the next start tries again
        assert "could not run" in manager.health()["model_smoke"]["error"]
        assert not (tmp_path / "x" / "models" / "seen.json").exists()
    finally:
        manager.close()
