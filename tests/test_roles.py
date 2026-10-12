"""Roles used: the Deep Agent runs on `exec`, account ranking on `small`; unset falls back."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripted_model import ScriptedModel, say
from worker import agent as agent_mod
from worker import cli, models, resolve
from worker.agent import DEFAULT_MODEL, role_setting

SET7 = Path(__file__).parent / "fixtures" / "set7"


class NamedModel(ScriptedModel):
    """A scripted model that remembers which model id it stands for."""

    label: str = ""


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Replace the real model constructor: record (role, model id) and return a scripted model, never a live one."""
    seen: list[tuple[str, str]] = []

    def fake_make_model(env: Any = None, role: str = "") -> NamedModel:
        name = role_setting(env or {}, role) or DEFAULT_MODEL
        seen.append((role, name))
        return NamedModel(label=name, script=[say("Nothing to build yet; tell me which sheet.")])

    monkeypatch.setattr(agent_mod, "make_model", fake_make_model)
    monkeypatch.setattr(agent_mod, "_models", {})
    return seen


def test_each_role_uses_its_env_model(tmp_path: Path, built: list[tuple[str, str]]) -> None:
    env = {
        "WORKER_ENV_FILE": str(tmp_path / "absent.env"),
        "WORKER_MODEL": "base-model",
        "WORKER_MODEL_EXEC": "exec-model",
        "WORKER_MODEL_SMALL": "small-model",
        "WORKER_RANK_ACCOUNTS": "1",
    }
    out = io.StringIO()
    code = cli.main(
        ["run", str(SET7 / "Fund_Roster.xlsx"), "--sponsor", "sponsor-a", "--gp-id", "GP-001",
         "--out", str(tmp_path / "out"), "--state-dir", str(tmp_path / "state")],
        input_fn=lambda _: "", out=out, environ=env,
    )
    assert code == 0, out.getvalue()
    assert ("exec", "exec-model") in built  # the Deep Agent
    assert ("small", "small-model") in built  # RunContext.ranker for map-accounts

    exec_model = models.model_for_role("exec", env)
    assert exec_model.label == "exec-model"
    ranker_model = models.model_for_role("small", env)
    assert ranker_model.label == "small-model"
    assert models.model_for_role("exec", env) is exec_model  # built once per process, reused

    # An unset role falls back to today's model.
    plain = {"WORKER_MODEL": "base-model"}
    assert models.model_for_role("small", plain).label == "base-model"
    assert models.model_for_role("exec", {}).label == DEFAULT_MODEL
    assert not any(role == "" for role, _ in built)  # no call site builds a model outside a role


def test_ranker_only_ranks_through_small_model(tmp_path: Path, built: list[tuple[str, str]]) -> None:
    pending = SimpleNamespace(
        key="1000 cash",
        source_account="1000 - Cash at bank",
        account_type="Bank",
        candidates=[{"account": "1000", "name": "Cash"}, {"account": "1010", "name": "Bank"}],
    )
    model = NamedModel(label="small-model", script=[say('{"1000 cash": "1010"}')])
    rank = resolve.model_ranker(model=model)
    assert rank([pending]) == {"1000 cash": "1010"}
    sent = str(model.seen[0][-1].content)
    assert "FILE DATA" in sent and "1000 - Cash at bank" in sent
