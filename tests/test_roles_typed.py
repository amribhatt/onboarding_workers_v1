"""Ticket 12: the typed-output layer. Pydantic checks each role's envelope and the contracts; Pydantic AI runs the
bounded repair loop; a provider failure moves to the fallback model and is audited. The scripted model stands in for a
live one, always."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from scripted_model import ScriptedModel
from test_roles_contracts import ENGINE_ROWS, INVESTORS, PROFILE, ROWS, actions, binding, reply, runner_for
from worker import contracts as c
from worker.context import Budget, BudgetExceeded
from worker.roles import explain, map_fields

GOOD = {"summary": "Late accrual.", "confidence": 0.7,
        "output": {"likely_cause": "A late accrual on account 1000.", "cites": ["r1"]}}


class Named(ScriptedModel):
    model_name: str = "scripted-a"


class Down(Named):
    """A provider that is unreachable."""

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Any:
        raise ConnectionError("503 upstream unavailable")


# ---- the envelope is a Pydantic model: a bad answer is a schema failure with a named field -------------------------


def test_a_blank_summary_is_a_schema_failure_that_names_the_field(tmp_path: Path) -> None:
    blank = reply({**GOOD, "summary": " "})
    model = ScriptedModel(script=[blank, reply(GOOD)])
    runner = runner_for(tmp_path, explainer=model)
    out = explain(runner, unit_id="u-r", cause_id="c-1", rows=ENGINE_ROWS)
    assert out.status == "ok" and len(model.seen) == 2
    assert "summary" in str(model.seen[1][-1].content)  # the repair prompt names the field
    first = next(e for e in runner.audit.read() if e["action"] == "handoff.rejected")
    assert first["inputs"]["schema_valid"] is False and first["inputs"]["verify_result"] == "not_run"


def test_an_unknown_key_in_the_answer_is_refused(tmp_path: Path) -> None:
    extra = reply({**GOOD, "plan": "do something else"})
    model = ScriptedModel(script=[extra, extra])
    out = explain(runner_for(tmp_path, explainer=model), unit_id="u-r", cause_id="c-1", rows=ENGINE_ROWS)
    assert out.status == "needs_human" and out.decided is None and len(model.seen) == 2


def test_mapper_alternatives_are_validated_by_the_envelope(tmp_path: Path) -> None:
    broken = {"summary": "Ids map.", "confidence": 0.8,
              "output": {"bindings": [binding("Investor_ID", "map", col_id="A")]},
              "alternatives": {"Commitment_Date": [{"field": "Commitment_Date", "action": "map"}]}}
    model = ScriptedModel(script=[reply(broken), reply(broken)])
    runner = runner_for(tmp_path, mapper=model)
    map_fields(runner, unit_id="u-1", spec=INVESTORS, profile=PROFILE, rows=ROWS)
    problems = next(e for e in runner.audit.read() if e["action"] == "handoff.rejected")["outputs"]["problems"]
    assert any("alternatives" in p for p in problems)
    assert len(model.seen) == 2  # the answer and ONE repair


# ---- model fallback on a provider failure, audited ------------------------------------------------------------------


def test_a_provider_failure_moves_to_the_fallback_and_is_audited(tmp_path: Path) -> None:
    primary, backup = Down(script=[]), Named(model_name="scripted-b", script=[reply(GOOD)])
    runner = runner_for(tmp_path, explainer=primary)
    runner.fallbacks = {"explainer": backup}
    out = explain(runner, unit_id="u-r", cause_id="c-1", rows=ENGINE_ROWS)
    assert out.status == "ok" and out.attempts == 1
    rows = runner.audit.read()
    (fb,) = [e for e in rows if e["action"] == "model.fallback"]
    assert fb["inputs"] == {"role": "explainer", "from": "scripted-a", "to": "scripted-b"}
    assert "503" in fb["outputs"]["error"] and fb["rationale"]
    returned = next(e for e in rows if e["action"] == "handoff.returned")
    assert returned["inputs"]["model_id"] == "scripted-b"
    assert actions(runner.audit, ("handoff.sent", "model.fallback", "handoff.returned")) == [
        "handoff.sent", "model.fallback", "handoff.returned"]


def test_without_a_fallback_a_provider_failure_is_not_swallowed(tmp_path: Path) -> None:
    runner = runner_for(tmp_path, explainer=Down(script=[]))
    with pytest.raises(ConnectionError):
        explain(runner, unit_id="u-r", cause_id="c-1", rows=ENGINE_ROWS)
    assert actions(runner.audit, ("handoff.sent", "model.fallback", "handoff.returned")) == ["handoff.sent"]


def test_the_spend_cap_is_never_a_reason_to_fall_back(tmp_path: Path) -> None:
    backup = Named(model_name="scripted-b", script=[reply(GOOD)])
    runner = runner_for(tmp_path, Budget(max_calls=1), explainer=Named(script=[reply(GOOD), reply(GOOD)]))
    runner.fallbacks = {"explainer": backup}
    assert explain(runner, unit_id="u-r", cause_id="c-1", rows=ENGINE_ROWS).status == "ok"
    with pytest.raises(BudgetExceeded):
        explain(runner, unit_id="u-r", cause_id="c-2", rows=ENGINE_ROWS)
    assert not any(e["action"] == "model.fallback" for e in runner.audit.read())
    assert backup.seen == []


def test_the_env_names_a_fallback_role(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from worker import models

    asked: list[str] = []

    def fake(role: str, env: Any = None) -> Named:
        asked.append(role)
        return Named(model_name=f"model-{role}", script=[])

    monkeypatch.setattr(models, "model_for_role", fake)
    runner = runner_for(tmp_path)
    runner.env = {"WORKER_ROLE_FALLBACK": "small"}
    chain = runner.chain("explainer")
    assert asked == ["exec", "small"] and len(chain) == 2
    runner.env = {"WORKER_ROLE_FALLBACK": "exec"}
    assert len(runner.chain("profiler")) == 2 and len(runner.chain("explainer")) == 2  # cached per role
    runner._wrapped.clear()
    assert len(runner.chain("explainer")) == 1  # a role is never its own fallback


# ---- contracts: field types are the validators ------------------------------------------------------------------------


def test_nested_dicts_become_contracts_and_are_validated() -> None:
    opt = c.Option(option_id="o1", action="map", proposed_by="mapper", preview={"conform": 2, "total": 2})  # type: ignore[arg-type]
    assert isinstance(opt.preview, c.Preview) and opt.preview.complete
    with pytest.raises(c.ContractError, match="unknown keys: extra"):
        c.Preview(conform=1, total=1, extra=1)  # type: ignore[call-arg]
    with pytest.raises(c.ContractError, match="missing total"):
        c.Preview(conform=1)  # type: ignore[call-arg]


def test_a_flag_is_not_a_count_and_a_number_is_not_text() -> None:
    with pytest.raises(c.ContractError):
        c.Preview(conform=True, total=1)  # type: ignore[arg-type]
    with pytest.raises(c.ContractError):
        c.ArtifactRef(kind="plan", id=7, checksum="x")  # type: ignore[arg-type]
    with pytest.raises(c.ContractError, match="not finite"):
        c.Option(option_id="o", action="map", proposed_by="mapper", preview=c.Preview(0, 0),
                 payload={"x": float("nan")})
    assert json.loads(c.Preview(conform=1, total=2).to_json()) == {"conform": 1, "total": 2, "sample_ref": None}


def test_a_stored_derived_id_must_match_its_content() -> None:
    unit = c.JobUnit(file_fp="f", sheet="s", record_type="Investors", entity=None, pins={"a": "b"})
    again = c.JobUnit(file_fp="f", sheet="s", record_type="Investors", entity=None, pins={"a": "b"},
                      unit_id=unit.unit_id)
    assert again == unit
    with pytest.raises(c.ContractError, match="does not match"):
        c.JobUnit(file_fp="f", sheet="other", record_type="Investors", entity=None, pins={"a": "b"},
                  unit_id=unit.unit_id)


def test_a_role_call_works_from_a_worker_thread_while_a_loop_runs(tmp_path: Path) -> None:
    """The resolver fans batches out with `asyncio.to_thread`; each role call runs its own agent loop there."""
    import asyncio

    runner = runner_for(tmp_path, explainer=ScriptedModel(script=[reply(GOOD)]))

    async def go() -> Any:
        return await asyncio.to_thread(explain, runner, unit_id="u-r", cause_id="c-1", rows=ENGINE_ROWS)

    assert asyncio.run(go()).status == "ok"


def test_a_non_json_answer_is_a_schema_failure_not_a_crash(tmp_path: Path) -> None:
    model = ScriptedModel(script=[AIMessage("I think it is a late accrual."), reply(GOOD)])
    runner = runner_for(tmp_path, explainer=model)
    assert explain(runner, unit_id="u-r", cause_id="c-1", rows=ENGINE_ROWS).status == "ok"
    assert "not one JSON object" in str(model.seen[1][-1].content)
