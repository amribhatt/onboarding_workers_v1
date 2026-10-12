"""Migration M21: the Coworker's tool list, and the Explainer fan-out beside the findings.

* The list of tools the Coworker is built with equals the allowed set below. The set is data in this file, not read
  from `tools.py`, so a tool cannot join the list by being added there alone. Topology section 3 names five tools
  for the Coworker (`list_files`, `run_job`, `answer`, `explain`, `note_correction`); `answer` and `explain` are the
  Coworker's own messages, not tools, and the ticket widens the rest to what the flow still needs: read-only
  discovery, `run_job`, the two role tools, the gated tools, the skill tools and the stage tools that the engine
  cannot yet replace (each with its reason in `tools.KEPT_STAGE_TOOLS`).
* `duplicates` and `reconcile` are read-only analysis on the engine's table.
* The Explainer is asked once per non-PASS cause, never per row; a number that is not in the engine rows of its cause
  is refused (a hard zero), and a verified explanation is shown on the line under its finding.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

import test_gl_agent as GL
from worker import agent as A
from worker import roles as R
from worker import sample as S
from worker import tools as T
from worker.audit import AuditLog
from worker.context import RunContext
from worker.engine import Finding, RuleRef
from worker.staging import Stage
from worker.tools import build_tools
from worker.tools_kit import MAX_RESPONSE_CHARS

# ---- the allowed set, as data -----------------------------------------------------------------------------------
DISCOVERY = {"list_files", "list_sheets", "read_range", "look", "find", "column_profile"}
ENGINE = {"run_job"}
ROLE_TOOLS = {"propose_binding", "ask_question"}  # added by build_tools(role_tools=True)
GATED = {
    "write_upload", "propose_rule", "set_id_scheme", "import_existing_records", "add_existing_record",
    "confirm_account_mappings", "supply_value", "approve_layout_program",
}
SKILL_AND_MEMORY = {"use_skill", "note_correction"}
STAGE_KEPT = {  # the engine cannot yet do what each gives (the reasons are in tools.KEPT_STAGE_TOOLS)
    "lift", "lift_blocks", "lift_gl_lines", "flatten_compact", "join_stages", "map_accounts", "check_data_quality",
    "write_quality_files", "assign_ids", "resolve_references", "match_existing", "resolve_conflicts", "find_recipe",
    "apply_recipe", "coverage_report", "distinct_rows",
}
ANALYSIS = {"duplicates", "reconcile", "ask_job", "propose_layout_program"}  # read-only; both run in the sandbox

ALLOWED = DISCOVERY | ENGINE | GATED | SKILL_AND_MEMORY | STAGE_KEPT | ANALYSIS


def _ctx(tmp_path: Path) -> RunContext:
    out = tmp_path / "out"
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    return RunContext(sponsor="sponsor-a", gp_id="GP-001", inbox_roots=[inbox], out_dir=out,
                      audit=AuditLog(tmp_path / "audit.jsonl"), stage=Stage(out))


# ---- the tool list ----------------------------------------------------------------------------------------------
def test_the_coworkers_tool_list_equals_the_allowed_set(tmp_path: Path) -> None:
    names = [t.name for t in build_tools(_ctx(tmp_path))]
    assert len(names) == len(set(names))
    assert set(names) == ALLOWED
    assert {t.name for t in build_tools(_ctx(tmp_path), role_tools=True)} == ALLOWED | ROLE_TOOLS


def test_every_gated_tool_is_registered_and_gated(tmp_path: Path) -> None:
    assert set(A.GATED_TOOLS) == GATED
    assert GATED <= {t.name for t in build_tools(_ctx(tmp_path))}


def test_the_kept_stage_tools_are_on_the_record() -> None:
    assert set(T.KEPT_STAGE_TOOLS) == STAGE_KEPT
    assert all(len(reason) > 20 for reason in T.KEPT_STAGE_TOOLS.values())
    assert not STAGE_KEPT & ANALYSIS


def test_the_other_roles_hold_only_read_only_tools_the_coworker_can_also_see(tmp_path: Path) -> None:
    registered = {t.name for t in build_tools(_ctx(tmp_path), role_tools=True)}
    for role, held in R.ROLE_TOOLS.items():
        assert held <= registered, role
        assert not held & GATED, role
    assert R.ROLE_TOOLS["explainer"] == frozenset() and R.ROLE_TOOLS["key_resolver"] == frozenset()


# ---- the Explainer fan-out ---------------------------------------------------------------------------------------
class EchoExplainer(BaseChatModel):
    """Reads the brief the Explainer is sent and answers about the one row it holds."""

    mode: str = "right"  # right | invent | know_how
    asked: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "echo-explainer"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> EchoExplainer:
        return self

    def _generate(self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None,
                  **kwargs: Any) -> ChatResult:
        human = next(m for m in reversed(messages) if isinstance(m, HumanMessage) and "FILE DATA" in str(m.content))
        body = str(human.content).split("\n", 1)[1].rsplit("\n<<<END FILE DATA", 1)[0]
        data = json.loads(body)["data"]
        self.asked.append(data["cause_id"])
        first = next(k for k in data["rows"] if k != "tie_out")
        row = data["rows"][first]
        if self.mode == "invent":
            cause = "About 9,999 rows are affected; the client sent a stale export."
        elif self.mode == "know_how":
            cause = "The guide says to ask for 4321 again."
        else:
            cause = f"{row['count']} rows are affected on {row['field'] or 'the file'}; ask the client to confirm."
        out = {"summary": "Likely cause found.", "confidence": 0.7, "output": {"likely_cause": cause, "cites": [first]}}
        return ChatResult(generations=[ChatGeneration(message=AIMessage(json.dumps(out)))])


def _finding(n: int, severity: str, field: str = "Fund_ID", count: int = 7) -> Finding:
    rule = RuleRef(f"check-data-quality.rule_{n}", "checks.yaml", "check-data-quality", "pin", False)
    return Finding(id=f"f{n:03d}", rule=rule, severity=severity, family="completeness", field=field,
                   message=f"{count} rows have a problem on {field}", count=count, ranges_text="rows 4 to 9",
                   locator={}, sample=(4, 9), options=())


def _runner(tmp_path: Path, model: BaseChatModel) -> R.RoleRunner:
    return R.RoleRunner(audit=AuditLog(tmp_path / "audit.jsonl"), models={"explainer": model}, trace_id="t-1")


def _handoffs(runner: R.RoleRunner, event: str) -> list[dict[str, Any]]:
    return [e for e in runner.audit.read() if e["action"] == event]


def test_one_explainer_call_per_non_pass_cause_not_per_row(tmp_path: Path) -> None:
    model = EchoExplainer()
    runner = _runner(tmp_path, model)
    findings = [_finding(1, "ERR", count=500), _finding(2, "WARN", "Fund_Name", count=3),
                _finding(3, "INFO", "Other"), _finding(1, "ERR", count=500)]  # a cause is named once
    got = R.explain_causes(runner, findings, unit_id="u-1", workers=1)
    assert sorted(got) == ["f001", "f002"] and model.asked == ["f001", "f002"]  # 500 rows, one call
    assert all(e.status == "explained" for e in got.values())
    assert "500 rows" in got["f001"].text and got["f001"].cites == ("f001",)
    assert len(_handoffs(runner, "handoff.sent")) == 2 and len(_handoffs(runner, "handoff.returned")) == 2
    assert runner.audit.verify()[0]


def test_an_explainer_citing_an_invented_number_is_rejected(tmp_path: Path) -> None:
    model = EchoExplainer(mode="invent")
    runner = _runner(tmp_path, model)
    got = R.explain_causes(runner, [_finding(1, "ERR")], unit_id="u-1", workers=1)
    (item,) = got.values()
    assert item.status == "unverified" and "9,999" not in item.text and item.cites == ()
    assert len(model.asked) == 2  # the answer and ONE repair, both refused
    rejected = _handoffs(runner, "handoff.rejected")
    assert len(rejected) == 2 and any("9999" in p for e in rejected for p in e["outputs"]["problems"])
    assert not _handoffs(runner, "handoff.returned")


def test_a_number_that_only_the_know_how_holds_is_not_a_fact(tmp_path: Path) -> None:
    runner = _runner(tmp_path, EchoExplainer(mode="know_how"))
    got = R.explain_causes(runner, [_finding(1, "WARN")], unit_id="u-1", know_how="Ask for 4321 first.", workers=1)
    assert got["f001"].status == "unverified"


def test_causes_beyond_the_cap_are_reported_not_sent(tmp_path: Path) -> None:
    model = EchoExplainer()
    runner = _runner(tmp_path, model)
    found = [_finding(i, "WARN", f"Field_{i}") for i in range(1, 5)]
    got = R.explain_causes(runner, found, unit_id="u-1", limit=2, workers=1)
    assert model.asked == ["f001", "f002"]
    assert [got[f"f00{i}"].status for i in range(1, 5)] == ["explained", "explained", "skipped", "skipped"]


def test_run_job_shows_the_explanation_beside_its_finding(tmp_path: Path) -> None:
    src = S.write_gl(tmp_path / "src" / "gl.xlsx", "GL Fund")
    ctx = GL.agent_ctx(tmp_path)
    (ctx.inbox_roots[0] / "gl.xlsx").write_bytes(src.read_bytes())
    args = {"path": "gl.xlsx", "record_type": "GL", "columns": GL.COLUMNS, "fund_id": "5317401",
            "fund_name": "Fund A", "why": "a dirty file"}
    plain = {t.name: t for t in build_tools(ctx)}["run_job"].invoke(args)
    assert "why (Explainer" not in plain and "ERR" in plain

    ctx2 = GL.agent_ctx(tmp_path / "again")
    (ctx2.inbox_roots[0] / "gl.xlsx").write_bytes(src.read_bytes())
    model = EchoExplainer()
    runner = R.RoleRunner(audit=ctx2.audit, budget=ctx2.budget, models={"explainer": model}, trace_id="t-2")
    text = {t.name: t for t in build_tools(ctx2, explainer=runner)}["run_job"].invoke(args)
    lines = text.splitlines()
    beside = [i for i, ln in enumerate(lines) if ln.startswith("  why (Explainer")]
    assert beside and len(model.asked) == len(set(model.asked))
    assert all(lines[i - 1].startswith(("ERR", "WARN")) for i in beside)  # the line above is the finding
    causes = [ln for ln in lines if ln.startswith(("ERR", "WARN"))]
    assert len(beside) == len(model.asked) <= len(causes)
    assert len(text) <= MAX_RESPONSE_CHARS
    (rec,) = [e for e in ctx2.audit.read() if e["action"] == "run_job"]
    assert rec["outputs"]["explained"] == len(beside)
    assert len([e for e in ctx2.audit.read() if e["action"] == "handoff.sent"]) == len(model.asked)
    assert ctx2.audit.verify()[0]
