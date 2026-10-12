from __future__ import annotations

from pathlib import Path

import pytest

from worker.agent import make_model, model_for, role_setting
from worker.audit import AuditLog
from worker.metrics import latency, percentile
from worker.pipeline import plan_problems, route_record_type
from worker.resolve import batch_size, majority, parse_decisions, vote
from worker.templates import TEMPLATES


def test_batch_size_stays_in_range() -> None:
    assert batch_size({"WORKER_BATCH_SIZE": "5"}) == 25
    assert batch_size({"WORKER_BATCH_SIZE": "500"}) == 50
    assert batch_size({}) == 40


def test_votes_need_a_majority_otherwise_the_human_decides() -> None:
    assert majority(["A", "A", "B"]) == ("A", True)
    assert majority(["A", "B", "C"]) == (None, False)
    assert majority(["A", None, None]) == (None, False)
    rounds = [{"k": "A", "j": "X"}, {"k": "B", "j": "Y"}, {"k": "A", "j": "Z"}]
    it = iter(rounds)
    decided, human = vote(lambda keys: next(it), ["k", "j"])
    assert decided == {"k": "A"} and human == ["j"]


def test_parse_decisions_accepts_fenced_or_bare_json() -> None:
    assert parse_decisions('```json\n{"a": "1"}\n```') == {"a": "1"}
    assert parse_decisions('Here: {"a": "1"} done') == {"a": "1"}
    assert parse_decisions("no json here") == {}


def test_latency_report_per_record_type_and_per_thousand_rows(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "run" / "audit.jsonl")
    for n, rows in enumerate((1000, 2000)):
        audit.record("stage.done", "convert", {"trace_id": f"t{n}", "stage": "convert", "record_type": "GL",
                                               "sponsor": "sponsor-a"}, {"seconds": 1.0, "rows": rows})
    report = latency(tmp_path)
    assert report["by_record_type"]["GL/convert"]["n"] == 2
    assert report["per_1000_rows"]["GL"]["jobs"] == 2
    assert percentile([1, 2, 3, 4], 50) == 2 and percentile([], 95) == 0.0


def test_plan_evaluator_flags_gaps_before_the_human_sees_the_plan() -> None:
    spec = TEMPLATES["Investors"]
    good = {"columns": {"Investor_ID": "A", "Investor_Name": "B"}, "constants": {"GP_ID": "GP-001"}}
    assert plan_problems(good, spec) == []
    bad = {"columns": {"Investor_ID": "A", "Investor_Name": "a", "Country": "B"},
           "constants": {"Investor_Type": "LP"}}
    problems = " | ".join(plan_problems(bad, spec))
    assert "required fields with no source" in problems and "constants only come from setup" in problems
    assert "column A feeds both Investor_ID and Investor_Name" in problems


def test_routing_picks_the_record_type_by_code_first() -> None:
    kind, _ = route_record_type(["Investor ID", "Investor Name", "Type"], TEMPLATES)
    assert kind == "Investors"
    kind, _ = route_record_type(["Foo", "Bar"], TEMPLATES)
    assert kind is None


def test_models_per_role_with_fallbacks_and_one_client_per_process() -> None:
    env = {
        "WORKER_MODEL": "base-model", "WORKER_MODEL_PLAN": "big-model", "WORKER_MODEL_PROVIDER": "openrouter",
        "WORKER_MODEL_FALLBACKS": "fb-base", "WORKER_MODEL_FALLBACKS_PLAN": "fb-plan-1, fb-plan-2",
    }
    assert role_setting(env, "plan") == "big-model"
    assert role_setting(env, "exec") == "base-model"  # a role left unset uses WORKER_MODEL
    assert role_setting(env, "plan", "WORKER_MODEL_FALLBACKS") == "fb-plan-1, fb-plan-2"
    assert role_setting(env, "small", "WORKER_MODEL_FALLBACKS") == "fb-base"
    with pytest.raises(ValueError, match="unknown model role"):
        role_setting(env, "boss")
    env = {**env, "OPENROUTER_API_KEY": "k"}
    plan = make_model(env, "plan")
    assert plan.model_name == "big-model"  # type: ignore[attr-defined]
    assert plan.extra_body == {"models": ["big-model", "fb-plan-1", "fb-plan-2"]}  # type: ignore[attr-defined]
    assert model_for("plan", env) is model_for("plan", env)
