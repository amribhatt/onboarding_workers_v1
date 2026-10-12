"""Sanity checks, doctor / models commands, the demo model through the CLI."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from id_decision import decide_agent_ids
from worker import models as catalog
from worker.checks import stage_warnings
from worker.cli import main
from worker.demo import DEMO_FILE, SAMPLES, DemoModel, seed_inbox


def test_gp_id_taken_from_a_source_column_is_flagged() -> None:
    rows = [{"GP_ID": "I-001", "Investor_ID": "I-001"}, {"GP_ID": "I-002", "Investor_ID": "I-002"}]
    found = stage_warnings("Investors", rows, "GP-001")
    gp = [w for w in found if w.startswith("GP_ID differs")]
    assert len(gp) == 1 and "2 of 2" in gp[0] and "'I-001'" in gp[0]


def test_constant_gp_id_and_blank_gp_id_are_fine() -> None:
    assert stage_warnings("Investors", [{"GP_ID": "gp-001"}, {"GP_ID": ""}], "GP-001") == []
    assert stage_warnings("Investors", [], "GP-001") == ["ERR: no rows were staged"]


def test_doctor_demo_passes_and_reports_missing_key(tmp_path: Path) -> None:
    out = io.StringIO()
    code = main(
        ["doctor", "--state-dir", str(tmp_path)],
        out=out,
        environ={"WORKER_MODEL_PROVIDER": "demo", "WORKER_ENV_FILE": str(tmp_path / "none.env")},
    )
    assert code == 0, out.getvalue()
    assert "All good." in out.getvalue()

    out = io.StringIO()
    code = main(
        ["doctor", "--state-dir", str(tmp_path)],
        out=out,
        environ={"WORKER_MODEL_PROVIDER": "openrouter", "WORKER_ENV_FILE": str(tmp_path / "none.env")},
    )
    assert code == 1
    assert "[FAIL] OPENROUTER_API_KEY set" in out.getvalue()


def test_models_command_lists_tool_capable_and_probes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        catalog,
        "free_models",
        lambda *a, **k: [
            {
                "id": "a/b:free",
                "context": 64000,
                "tools": True,
                "vision": False,
                "router": False,
                "preferred": True,
            },
            {
                "id": "c/d:free",
                "context": 8000,
                "tools": False,
                "vision": False,
                "router": False,
                "preferred": False,
            },
        ],
    )
    monkeypatch.setattr(
        catalog, "probe", lambda mid, env, model=None: {"ok": False, "seconds": 0.1, "error": "429"}
    )
    out = io.StringIO()
    code = main(
        ["models", "--probe", "a/b:free"], out=out, environ={"WORKER_ENV_FILE": str(tmp_path / "none.env")}
    )
    text = out.getvalue()
    assert code == 0
    assert "1 free models can call tools (1 cannot)" in text and "a/b:free" in text
    assert "c/d:free" not in text
    assert "probe a/b:free: FAILED: 429" in text


def test_probe_reports_success_and_never_raises() -> None:
    from langchain_core.messages import AIMessage

    class Good:
        def bind_tools(self, tools):
            return self

        def invoke(self, msgs):
            return AIMessage("", tool_calls=[{"name": "_ping", "args": {"word": "ok"}, "id": "1"}])

    class Boom(Good):
        def invoke(self, msgs):
            raise RuntimeError("Error code: 429 - free-models-per-day")

    assert catalog.probe("x", {}, model=Good())["ok"] is True  # type: ignore[arg-type]
    bad = catalog.probe("x", {}, model=Boom())  # type: ignore[arg-type]
    assert bad["ok"] is False and "rate limited" in bad["error"] and "daily quota" in bad["error"]


def test_probe_tells_a_busy_provider_from_a_used_up_quota() -> None:
    """Live run finding: every 429 was reported as a used-up daily quota, but the live 429s said the provider was
    busy upstream (retry shortly). The two need different actions."""
    upstream = RuntimeError("Error code: 429 - {'error': {'metadata': {'raw': 'm:free is temporarily rate-limited "
                            "upstream. Please retry shortly'}}}")
    assert "upstream" in catalog.probe_error(upstream) and "daily" not in catalog.probe_error(upstream)
    assert "daily quota" in catalog.probe_error(RuntimeError("429 Rate limit exceeded: free-models-per-day"))
    harness = RuntimeError("Error code: 403 - m:free is only available on agentic harnesses.")
    assert "listed apps" in catalog.probe_error(harness)
    assert catalog.probe_error(ValueError("boom")) == "ValueError: boom"


def test_parse_models_orders_preferred_after_tool_support() -> None:
    payload = {
        "data": [
            {
                "id": "z/other:free",
                "pricing": {"prompt": "0", "completion": "0"},
                "context_length": 900000,
                "supported_parameters": ["tools"],
            },
            {
                "id": "google/gemma-4-31b-it:free",
                "pricing": {"prompt": "0", "completion": "0"},
                "context_length": 200000,
                "supported_parameters": ["tools"],
            },
            {
                "id": "n/t:free",
                "pricing": {"prompt": "0", "completion": "0"},
                "context_length": 1,
                "supported_parameters": [],
            },
        ]
    }
    ids = [m["id"] for m in catalog.parse_models(payload)]
    assert ids == ["google/gemma-4-31b-it:free", "z/other:free", "n/t:free"]


def test_demo_cli_chat_flow_end_to_end(tmp_path: Path) -> None:
    inbox = tmp_path / "in"
    assert DEMO_FILE in seed_inbox(inbox)
    out = io.StringIO()
    decide_agent_ids(tmp_path / "state" / "ids", "Investors")  # a named person keeps the roster's IDs, beforehand
    code = main(
        [
            "run",
            str(inbox / DEMO_FILE),
            "--sponsor",
            "sponsor-a",
            "--gp-id",
            "GP-001",
            "--out",
            str(tmp_path / "out"),
            "--state-dir",
            str(tmp_path / "state"),
            "--auto-approve",
        ],
        out=out,
        model=DemoModel(),
        environ={"WORKER_ENV_FILE": str(tmp_path / "none.env")},
    )
    assert code == 0, out.getvalue()
    assert (tmp_path / "out" / "Investors_Upload.xlsx").is_file()
    assert "Audit verification: OK" in out.getvalue()


def test_samples_are_bundled() -> None:
    assert {p.name for p in SAMPLES.glob("*.xlsx")} >= {DEMO_FILE, "Fund_Roster.xlsx"}


def test_a_field_copied_from_another_is_flagged() -> None:
    rows = [
        {"GP_ID": "GP-001", "Investor_ID": "I-1", "Source_Investor_Key": "I-1"},
        {"GP_ID": "GP-001", "Investor_ID": "I-2", "Source_Investor_Key": "I-2"},
    ]
    found = stage_warnings("Investors", rows, "GP-001")
    assert any("Investor_ID and Source_Investor_Key are identical" in w for w in found)
    distinct = [{**r, "Source_Investor_Key": "SRC" + r["Investor_ID"]} for r in rows]
    assert stage_warnings("Investors", distinct, "GP-001") == []
