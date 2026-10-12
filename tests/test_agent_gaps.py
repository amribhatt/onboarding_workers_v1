"""Gaps closed on the agent path (the agent's world, its writer, its read-only analysis, its start-up and its spend).

* the agent's world holds the sponsor's field recipes, so the engine applies them on the agent path as it does in
  the console (`StoreWorld.field_recipe`); a table the engine cannot say is never written around a recipe;
* the fallback writer (the old `Stage.write_upload`) is released only by a verified approval, like the engine's;
* `duplicates` and `reconcile` are read-only tools again, on the engine's table (one pass through `evaluate`) and
  the one grouping function, not a second copy of it;
* a missing or refused skill file is said at start-up, not as a FileNotFoundError in the middle of a job;
* every job has a spend cap unless the person lifts it by name.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import test_adapter_parity_agent as AP
import test_gl_agent as GL
from id_decision import decide_agent_ids
from scripted_model import ScriptedModel, call, say
from test_field_decision import decide_field, decision
from test_recipes import _ctx, _file
from worker import agentspec as AS
from worker import engine as E
from worker import feedback as F
from worker import templates as T
from worker import transforms as TF
from worker.agent import build_agent, pending_gate, start
from worker.audit import AuditLog
from worker.context import Budget, RunContext
from worker.ports import StoreWorld
from worker.recipes import FieldRecipes
from worker.skills import SkillLoadRefused, require_valid_skills
from worker.staging import Stage
from worker.tools import build_tools

REPO = Path(__file__).resolve().parents[1]
UPPER = [{"op": "case", "to": "upper"}]


def _investor_recipe(root: Path, sponsor: str = "sponsor-a") -> FieldRecipes:
    """A sponsor recipe for Investors written through the person's own answer (audited first, then the file)."""
    rcp = FieldRecipes(root / "sponsors", sponsor, record_types=["Investors"],
                       target_fields={"Investors": T.TEMPLATES["Investors"].fields})
    names = ["Alpha", "Beta"]
    header = ("Inv ID", "Investor Name", "Type")
    rows = [TF.Row(f"Sheet!{i + 2}", header, (f"I{i}", n, "LP")) for i, n in enumerate(names)]
    option = {"option_id": "o1", "action": "transform", "proposed_by": "mapper",
              "payload": {"from": "Investor Name", "ops": UPPER}, "preview": {"conform": 2, "total": 2}}
    dec = decision(sponsor, record_type="Investors", field="Investor_Name", header="Investor Name", values=names,
                   options=[option], chosen={"option_id": "o1"})
    dec["pins"] = {**dec["pins"], "target": {"Investors": "sha256:dd"}}
    decide_field(AuditLog(root / "recipe-audit.jsonl"), F.FeedbackStore(root / "fb", sponsor), rcp, rows, **dec)
    return rcp


# ---- the agent's world holds the sponsor's recipes -------------------------------------------------------------
def test_the_agents_world_returns_the_sponsors_recipe_step_and_none_without_one(tmp_path: Path) -> None:
    rcp = _investor_recipe(tmp_path)
    world = StoreWorld("sponsor-a", templates=T.templates_for("sponsor-a"), recipes_root=tmp_path / "sponsors")
    step = world.field_recipe("Investors")
    assert step is not None and step["step"] == "transform"
    assert step["field_recipe"]["pin"] == rcp.pin("Investors") and step["field_recipe"]["record_type"] == "Investors"
    assert step["recipe"]["fields"]["Investor_Name"]["ops"] == UPPER
    assert world.field_recipe("Commitments") is None  # no recipe file for this record type
    bare = StoreWorld("sponsor-a", templates=T.templates_for("sponsor-a"))
    assert bare.field_recipe("Investors") is None  # a world with no recipes folder changes nothing


def test_the_agent_ports_read_the_recipe_from_the_state_folder(tmp_path: Path) -> None:
    state = tmp_path / "state"
    rcp = _investor_recipe(state)
    out = tmp_path / "run" / "out"
    ctx = RunContext(sponsor="sponsor-a", gp_id="GP-001", inbox_roots=[tmp_path], out_dir=out,
                     audit=AuditLog(out / "audit.jsonl"), stage=Stage(out), id_root=state / "ids")
    step = AS.agent_ports(ctx).world.field_recipe("Investors")  # type: ignore[attr-defined]
    assert step is not None and step["field_recipe"]["pin"] == rcp.pin("Investors")


def _investors_file(tmp_path: Path) -> None:
    _file(tmp_path, "inv.xlsx", [["I1", "Alpha", "LP"], ["I2", "Beta", "GP"]])


def test_the_engine_run_by_the_agent_names_the_recipe_and_applies_it(tmp_path: Path) -> None:
    _investors_file(tmp_path)
    state = tmp_path / "state"
    rcp = _investor_recipe(state)
    ctx = _ctx(tmp_path, "r")
    spec = AS.file_to_spec(ctx, tmp_path / "inbox" / "inv.xlsx", "Investors", sheet="Sheet",
                           columns={"Investor_ID": "A", "Investor_Name": "B", "Investor_Type": "C"})
    ev = E.evaluate(spec, AS.agent_ports(ctx), stop_after="check")
    assert dict(ev.manifest.field_recipes) == {"Investors": rcp.pin("Investors")}
    assert [r["Investor_Name"] for r in ev.staged()] == ["ALPHA", "BETA"]


# ---- the fallback writer needs a verified approval --------------------------------------------------------------
def _entry(action: str, reviewer: str | None = None) -> dict[str, Any]:
    return {"action": action, "inputs": {"reviewer": reviewer} if reviewer is not None else {}}


@pytest.mark.parametrize(("entries", "who"), [
    ([_entry("gate.approve", "reviewer-x")], "reviewer-x"),
    ([_entry("gate.approve", "reviewer-x"), _entry("lift")], "reviewer-x"),
    ([_entry("gate.edit", "reviewer-x")], "reviewer-x"),
    ([_entry("gate.approve", "local")], "local reviewer"),
])
def test_a_gate_decision_that_released_the_write_names_who_approved(entries: list[dict[str, Any]], who: str) -> None:
    assert AS.released_by(entries) == who


@pytest.mark.parametrize("entries", [
    [],
    [_entry("lift")],
    [_entry("gate.reject", "reviewer-x")],
    [_entry("gate.approve", "reviewer-x"), _entry("gate.reject", "reviewer-x")],
    [_entry("gate.approve", "agent")],
    [_entry("gate.approve", "improver")],
    [_entry("gate.approve", "gpt-5.6-terra")],
    [_entry("gate.approve")],  # no name: there is no default person
])
def test_no_gate_approval_or_a_non_person_releases_nothing(entries: list[dict[str, Any]]) -> None:
    with pytest.raises(AS.UnapprovedWrite):
        AS.released_by(entries)


def test_the_fallback_writer_is_verified_and_records_who_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("worker.tools_engine.agent_to_spec", _not_translatable)
    ctx, agent, _picks, _gl = AP._gl_agent(tmp_path, reviewer="reviewer-x")
    AP.approve_as(agent, ctx, "reviewer-x")
    assert list((ctx.out_dir).glob("GL_Upload*.xlsx"))  # the old writer wrote it
    fell = AP._engine_entries(ctx, "eval.legacy_write")
    assert fell and "not translatable" in fell[0]["rationale"] or fell
    (entry,) = AP._entries(ctx, "write_upload")
    assert entry["outputs"]["approved_by"] == "reviewer-x" and entry["outputs"]["approval_id"].startswith("appr-")
    assert entry["outputs"]["engine_fallback"]


def test_the_fallback_writer_refuses_an_agent_named_approver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decision itself is refused by the library now; the writer's own check of the record is test_no_gate_approval."""
    from worker.contracts import UnnamedApprover

    monkeypatch.setattr("worker.tools_engine.agent_to_spec", _not_translatable)
    with pytest.raises(UnnamedApprover):
        AP._gl_agent(tmp_path, reviewer="conductor")


def _not_translatable(*_a: Any, **_k: Any) -> Any:
    raise AS.NotTranslatable("a table the closed steps cannot say (test)")


def test_a_table_the_engine_cannot_say_is_not_written_around_the_sponsors_recipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _investors_file(tmp_path)
    _investor_recipe(tmp_path / "state")
    ctx = _ctx(tmp_path, "w")
    decide_agent_ids(ctx.id_root, "Investors")
    monkeypatch.setattr("worker.tools_engine.agent_to_spec", _not_translatable)
    script = [say("lift", call("lift", path="inv.xlsx", sheet="Sheet", template="Investors",
                               columns={"Investor_ID": "A", "Investor_Name": "B", "Investor_Type": "C"},
                               constants={"GP_ID": "GP-001"}, header_row=1, first_row=2, why="copy")),
              say("write", call("write_upload", stage_id="s1", template="Investors", why="deliver")),
              say("done")]
    agent = build_agent(ctx, ScriptedModel(script=script), InMemorySaver())
    result = start(agent, ctx, "build", "tw")
    assert [g["name"] for g in pending_gate(result)] == ["write_upload"]
    from scripted_model import approve

    approve(agent, ctx, "tw")
    assert not list(ctx.out_dir.glob("Investors_Upload*"))
    errors = [e for e in ctx.audit.read() if e["action"] == "write_upload.error"]
    assert errors and "recipe" in errors[0]["outputs"]["error"]


# ---- read-only analysis, on the engine's table -----------------------------------------------------------------
def _tools(ctx: RunContext) -> dict[str, Any]:
    ctx.restore = True
    return {t.name: t for t in build_tools(ctx)}


def test_duplicates_and_reconcile_are_read_only_tools_on_the_engines_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _agent, _picks, _gl = AP._gl_agent(tmp_path, reviewer="reviewer-x")
    seen: list[str] = []
    real = E.evaluate

    def spy(spec: Any, ports: Any, **kw: Any) -> Any:
        seen.append(str(kw.get("stop_after", "check")))
        return real(spec, ports, **kw)

    monkeypatch.setattr("worker.tools_kit.evaluate", spy)
    tools = _tools(ctx)
    text = tools["duplicates"].invoke({"stage_id": "s3", "key_fields": ["Journal_ID"], "why": "which journals repeat"})
    body = json.loads(text)
    want = ctx.stage.duplicates("s3", ["Journal_ID"])
    assert body["groups"] == len(want) > 0 and body["shown"][0]["rows"] == want[0]["rows"]
    assert seen == ["check"]  # the engine's table, read only
    rec = tools["reconcile"].invoke({"stage_id_left": "s3", "stage_id_right": "s3", "left_field": "Journal_ID",
                                     "right_field": "Journal_ID", "why": "a table against itself"})
    got = json.loads(rec)
    assert got == ctx.stage.reconcile("s3", "s3", "Journal_ID", "Journal_ID")
    assert got["only_left"] == got["only_right"] == 0 and got["both"] == got["left_distinct"] > 0
    assert seen == ["check", "check", "check"]
    assert not list(ctx.out_dir.glob("GL_Upload*"))
    assert [e["action"] for e in ctx.audit.read()].count("duplicates") == 1


def test_a_table_the_engine_cannot_say_is_analysed_by_nobody(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, _agent, _picks, _gl = AP._gl_agent(tmp_path, reviewer="reviewer-x")
    monkeypatch.setattr("worker.tools_engine.agent_to_spec", _not_translatable)
    out = _tools(ctx)["duplicates"].invoke({"stage_id": "s3", "key_fields": ["Journal_ID"], "why": "try"})
    assert out.startswith("ERROR") and "engine" in out


def test_the_tool_list_has_the_two_analysis_tools(tmp_path: Path) -> None:
    names = {t.name for t in build_tools(GL.agent_ctx(tmp_path))}
    assert {"duplicates", "reconcile"} <= names


# ---- start-up: the skill files the engine needs ------------------------------------------------------------------
def _skills_copy(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    shutil.copytree(REPO / "skills", root)
    return root


@pytest.mark.parametrize("gone", [
    "build-static-records/tables/legal-forms.yaml",
    "check-data-quality/tables/causes.yaml",
    "map-accounts/tables/deal-required.yaml",
    "check-data-quality/controls.yaml",
])
def test_a_missing_skill_file_is_said_at_start_up_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gone: str
) -> None:
    root = _skills_copy(tmp_path)
    (root / gone).unlink()
    monkeypatch.setenv("WORKER_SKILLS_DIR", str(root))
    audit = AuditLog(tmp_path / "audit.jsonl")
    with pytest.raises(SkillLoadRefused, match=gone.split("/")[-1]) as exc:
        require_valid_skills(audit)
    assert gone in " ".join(exc.value.problems)
    (entry,) = audit.read()
    assert entry["action"] == "skill.load_refused" and entry["outputs"] == {"started": False}


def test_a_refused_pin_is_still_said_at_start_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _skills_copy(tmp_path)
    path = root / "check-data-quality" / "checks.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("family: validity", "family: nonsense", 1),
                    encoding="utf-8")
    monkeypatch.setenv("WORKER_SKILLS_DIR", str(root))
    with pytest.raises(SkillLoadRefused):
        require_valid_skills(AuditLog(tmp_path / "audit.jsonl"))


def test_the_service_checks_the_skills_when_it_starts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from worker.service import RunManager

    root = _skills_copy(tmp_path)
    (root / "build-static-records" / "tables" / "legal-forms.yaml").unlink()
    monkeypatch.setenv("WORKER_SKILLS_DIR", str(root))
    with pytest.raises(SkillLoadRefused, match="legal-forms.yaml"):
        RunManager(tmp_path / "state", lambda: ScriptedModel(script=[]))
    assert not (tmp_path / "state" / "runs").exists() or not any((tmp_path / "state" / "runs").iterdir())


# ---- the per-job spend cap has a default -------------------------------------------------------------------------
def test_a_job_has_a_spend_cap_unless_it_is_lifted_by_name() -> None:
    cap = Budget.from_env({})
    assert cap.is_set and cap.max_calls == Budget.DEFAULT_CALLS and cap.max_tokens == Budget.DEFAULT_TOKENS
    assert Budget.DEFAULT_CALLS > 0 and Budget.DEFAULT_TOKENS > 0
    lifted = Budget.from_env({"WORKER_BUDGET_TOKENS": "unlimited", "WORKER_BUDGET_CALLS": "unlimited"})
    assert not lifted.is_set and lifted.max_calls is None and lifted.max_tokens is None
    mixed = Budget.from_env({"WORKER_BUDGET_CALLS": "12", "WORKER_BUDGET_TOKENS": "UNLIMITED"})
    assert mixed.max_calls == 12 and mixed.max_tokens is None
    blank = Budget.from_env({"WORKER_BUDGET_CALLS": "  ", "WORKER_BUDGET_TOKENS": ""})
    assert blank.max_calls == Budget.DEFAULT_CALLS and blank.max_tokens == Budget.DEFAULT_TOKENS  # blank is not a lift


def test_a_run_context_without_a_budget_is_still_unlimited_for_code_that_builds_its_own(tmp_path: Path) -> None:
    """The library default of `RunContext` is unchanged; the entry points (service, CLI) apply the env default."""
    assert not Budget().is_set
    assert GL.agent_ctx(tmp_path).budget.is_set is False


def test_the_readme_documents_the_default_and_the_override() -> None:
    text = (REPO / "README.md").read_text(encoding="utf-8")
    assert "WORKER_BUDGET_TOKENS" in text and "unlimited" in text
    assert str(Budget.DEFAULT_CALLS) in text.replace(",", "") and str(Budget.DEFAULT_TOKENS) in text.replace(",", "")

