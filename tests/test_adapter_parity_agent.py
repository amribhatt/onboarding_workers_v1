"""Migration M18: the agent adapter on `evaluate` (the agent half of the adapter parity test).

M17 creates `tests/test_adapter_parity.py` for the console half (`console_to_spec`). This file holds the agent half so
the two waves do not collide; when both are merged, move these tests into that file and add the one assertion that
`agent_to_spec(ctx, stage).eval_key() == console_to_spec(job).eval_key()` for the same fixture (see STATUS.md,
"Migration M18").

What is proved here, all with the scripted model:

* `agent_to_spec` writes the same `JobSpec` (same `eval_key`) as the engine's own expected spec for each fixture
  (`tests/test_engine_golden.py` builds those by hand);
* the gated `write_upload` goes through `evaluate(stop_after="write")` under an Approval whose approver is the
  reviewer of the gate decision, writes the file the old path wrote, and leaves ONE `write_upload` record that
  carries the job's manifest;
* a gate decision made by an agent-named reviewer writes nothing;
* `run_job` is read-only and its digest stays within 6 kB (and on a million-line file, in the slow test).
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import test_engine_golden as EG
import test_gl_agent as GL
import test_static_clean as F5
from id_decision import decide_ids
from scripted_model import ScriptedModel, approve, call, say
from worker import engine as E
from worker import sample as S
from worker.agent import (
    GateRefused,
    build_agent,
    check_approval,
    decide,
    pending_actions,
    pending_gate,
    start,
)
from worker.agentspec import NotTranslatable, agent_to_spec, engine_audit, person
from worker.audit import AuditLog
from worker.console import Console
from worker.context import RunContext
from worker.contracts import UnnamedApprover
from worker.hashing import sha256_file
from worker.registry import Registry
from worker.staging import Stage
from worker.tools import build_tools
from worker.tools_kit import MAX_RESPONSE_CHARS

SPONSOR = "sponsor-a"


def approve_as(agent: Any, ctx: RunContext, who: str, reason: str = "checked against the source") -> dict[str, Any]:
    """The write_upload gate decided by a named reviewer; every open WARN is acknowledged, as a reviewer would."""
    ack: dict[str, str] = {}
    try:
        check_approval(ctx, pending_actions(agent, "t1"))
    except GateRefused as exc:
        ack = {fid: "acknowledged in test" for fid in exc.review["unacknowledged_warn"]}
    return decide(agent, ctx, "t1", {"type": "approve"}, reason, {"reviewer": who}, ack)


def _entries(ctx: RunContext, action: str) -> list[dict[str, Any]]:
    return [e for e in ctx.audit.read() if e["action"] == action]


def _engine_entries(ctx: RunContext, action: str) -> list[dict[str, Any]]:
    """The engine's own record of the run (`<run>/engine/engine_audit.jsonl`)."""
    return [e for e in engine_audit(ctx).read() if e["action"] == action]


# what the run's own `write_upload` record holds: the keys it held before the switch, and since M19 the job's
# manifest, the approver and the approval id (the golden lock's decision digest was regenerated for it)
OLD_RECORD_KEYS = {"chars", "file", "path", "rows", "manifest", "approved_by", "approval_id"}


# ---- the static types ------------------------------------------------------------------------------------
def _static_agent(typ: str, tmp_path: Path) -> tuple[RunContext, Any, str, Path, str]:
    """The clean file of one static type, through the agent to the write_upload gate. Returns the context, the
    agent, the last staged table, the source file and its sheet."""
    c = Console(tmp_path / "inbox")
    c.act("prov|paid")
    if typ == "Commitments":  # its investors are approved first, in the shared store
        F5.console_job(c, "Investors")
        c.act("acceptall")
        decide_ids(c)
        c.act("approve", who="reviewer-a")
    state = tmp_path / "agent_state"
    if (c.root / "registry").is_dir():
        import shutil

        shutil.copytree(c.root / "registry", state / "registry")
    j = F5.console_job(c, typ)
    if (c.root / "ids").is_dir():  # the sponsor's ID decision is the agent's too (one store)
        import shutil

        shutil.copytree(c.root / "ids", state / "ids", dirs_exist_ok=True)
    out = tmp_path / "run" / "out"
    ctx = RunContext(sponsor=SPONSOR, gp_id=SPONSOR, inbox_roots=[c.inbox], out_dir=out,
                     audit=AuditLog(out / "audit.jsonl"), stage=Stage(out), registry=Registry(state, SPONSOR),
                     id_root=state / "ids")
    source = Path(j["up"]["path"])
    agent = build_agent(ctx, ScriptedModel(script=F5.agent_script(typ, source.name, j["up"]["sheet"])),
                        InMemorySaver())
    result = start(agent, ctx, f"build me the {typ} sheet", "t1")
    if typ in ("Investors", "Commitments"):  # the ID scheme is a human decision
        assert [g["name"] for g in pending_gate(result)] == ["set_id_scheme"]
        result = decide(agent, ctx, "t1", {"type": "approve"}, "the sponsor numbers these like the console",
                        {"reviewer": "reviewer-x"})
    assert [g["name"] for g in pending_gate(result)] == ["write_upload"]
    last = {"Investors": "s2", "Commitments": "s3"}.get(typ, "s1")
    return ctx, agent, last, source, j["up"]["sheet"]


@pytest.mark.parametrize("typ", list(F5.CLEAN_ROWS))
def test_agent_to_spec_equals_the_engines_spec_for_each_static_fixture(
    typ: str, clean_samples: None, tmp_path: Path
) -> None:
    ctx, _agent, last, source, sheet = _static_agent(typ, tmp_path)
    spec = agent_to_spec(ctx, last)
    want = EG.spec_of(typ, EG.file_spec(source, sheet=sheet), EG._static_steps(typ, clean=True), job="run")
    assert spec == want
    assert spec.eval_key() == want.eval_key()
    assert agent_to_spec(ctx).eval_key() == want.eval_key()  # the latest table is the default


@pytest.fixture
def clean_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    for typ, keep in F5.CLEAN_ROWS.items():
        monkeypatch.setitem(S.CFG[typ], "rows", [r for r in S.CFG[typ]["rows"] if r.i in keep])


@pytest.mark.parametrize("typ", list(F5.CLEAN_ROWS))
def test_the_gated_write_goes_through_the_engine_and_records_the_manifest(
    typ: str, clean_samples: None, tmp_path: Path
) -> None:
    ctx, agent, last, source, _sheet = _static_agent(typ, tmp_path)
    assert not list(ctx.out_dir.glob("*_Upload*.xlsx"))
    spec = agent_to_spec(ctx, last)
    approve(agent, ctx, "t1", "checked against the source")  # decided as `reviewer-a` (tests/scripted_model.py)
    written = ctx.out_dir / f"{typ}_Upload.xlsx"
    assert written.is_file()
    (entry,) = _entries(ctx, "write_upload")  # the run's own record of the upload is what it was
    assert set(entry["outputs"]) == OLD_RECORD_KEYS and entry["outputs"]["file"] == written.name
    assert not _engine_entries(ctx, "eval.legacy_write"), "the engine did not write this upload"
    (engine_record,) = _engine_entries(ctx, "write_upload")
    assert engine_record["outputs"]["sha256"] == sha256_file(written)
    assert engine_record["inputs"]["approved_by"] == "reviewer-a"
    (record,) = _engine_entries(ctx, "eval.manifest")  # the job's manifest, on the engine's record
    manifest = record["outputs"]["manifest"]
    assert record["inputs"]["approved_by"] == "reviewer-a"
    assert manifest["recipe_checksum"] == spec.recipe.checksum
    assert manifest["choices_checksum"] == E.sha("choices", list(spec.choices))
    assert manifest["sources"][0]["fingerprint"] == sha256_file(source)
    assert manifest["eval_key"] == engine_record["inputs"]["eval_key"] and manifest["pins"]["skills"]
    assert ctx.audit.verify()[0] and engine_audit(ctx).verify()[0]


# ---- a GL file -------------------------------------------------------------------------------------------
def _gl_agent(tmp_path: Path, reviewer: str | None = None) -> tuple[RunContext, Any, dict[str, str], Path]:
    c, _j, src = GL.console_file(tmp_path, "GL Fund",
                                 lambda _c: GL.write_clean(tmp_path / "src" / "gl.xlsx", GL.CLEAN_FUND))
    c.act("acceptall")
    decide_ids(c)
    c.act("approve", who="reviewer-a")
    ctx = GL.agent_ctx(tmp_path)
    (ctx.inbox_roots[0] / "gl.xlsx").write_bytes(src.read_bytes())
    picks = GL.picks_for(GL.CLEAN_FUND, c)
    script = [
        say("lift", call("lift_gl_lines", path="gl.xlsx", fund_id=c.entity_id("Fund A"), fund_name="Fund A",
                         why="stage the GL lines")),
        say("map", call("map_accounts", stage_id="s1", why="map the GL accounts")),
        say("confirm", call("confirm_account_mappings", stage_id="s2", picks=picks, why="the person confirmed")),
        say("check", call("check_data_quality", path="gl.xlsx", record_type="GL", columns=GL.COLUMNS,
                          why="find every problem")),
        say("write", call("write_upload", stage_id="s3", template="GL", why="deliver the file")),
        say("done"),
    ]
    agent = build_agent(ctx, ScriptedModel(script=script), InMemorySaver())
    result = start(agent, ctx, "build me the GL sheet", "t1")
    assert [g["name"] for g in pending_gate(result)] == ["confirm_account_mappings"]
    meta = {"reviewer": reviewer or "reviewer-a"}
    result = decide(agent, ctx, "t1", {"type": "approve"}, "these are the accounts", meta)
    assert [g["name"] for g in pending_gate(result)] == ["write_upload"]
    fund = c.entity_id("Fund A")
    ctx._fund = fund  # type: ignore[attr-defined]  # for the expected spec below
    return ctx, agent, picks, ctx.inbox_roots[0] / "gl.xlsx"


def test_agent_to_spec_equals_the_engines_spec_for_a_gl_file(tmp_path: Path) -> None:
    ctx, _agent, picks, gl = _gl_agent(tmp_path, reviewer="reviewer-x")
    spec = agent_to_spec(ctx, "s3")
    steps = [{"step": "lift_gl_lines", "source": 0, "fund_id": ctx._fund, "fund_name": "Fund A",  # type: ignore[attr-defined]
              "offsets": False},
             {"step": "map_accounts"},
             {"step": "check_data_quality", "source": 0, "record_type": "GL", "columns": GL.COLUMNS}]
    choices = tuple(sorted((E.Choice("map_account", f"key:{k}", a, "job", "reviewer-x", "the person confirmed")
                            for k, a in picks.items()), key=lambda c: c.subject))
    want = E.JobSpec("run", SPONSOR, "GL", None, (E.SourceRef("file", gl.name, None, None, sha256_file(gl)),),
                     E.PinSet(), E.Recipe.of(*steps), choices, {"gp_id": SPONSOR})
    assert spec == want and spec.eval_key() == want.eval_key()


def test_a_gl_upload_through_the_engine_equals_the_old_writer(tmp_path: Path) -> None:
    ctx, agent, _picks, _gl = _gl_agent(tmp_path, reviewer="reviewer-x")
    before = ctx.stage.rows("s3")
    approve_as(agent, ctx, "reviewer-x")
    got = GL.read_file(ctx.out_dir / "GL_Upload.xlsx")
    header = list(got[0])
    assert len(got) == 1 + len(before)
    assert sum(r[header.index("Total_Amount")] for r in got[1:]) == 0
    (entry,) = _entries(ctx, "write_upload")
    assert set(entry["outputs"]) == OLD_RECORD_KEYS
    assert not _engine_entries(ctx, "eval.legacy_write")
    (record,) = _engine_entries(ctx, "eval.manifest")
    assert record["inputs"]["approved_by"] == "reviewer-x"
    applied = [e for e in _engine_entries(ctx, "choice.applied") if e["inputs"].get("kind") == "map_account"]
    assert applied and {e["inputs"]["by"] for e in applied} == {"reviewer-x"}  # who confirmed each account, on the record


def test_the_manifest_can_go_in_the_runs_own_write_upload_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On by default since M19; `WORKER_AUDIT_MANIFEST=1` says so explicitly (and `0` leaves it on the engine log)."""
    monkeypatch.setenv("WORKER_AUDIT_MANIFEST", "1")
    ctx, agent, _picks, _gl = _gl_agent(tmp_path, reviewer="reviewer-x")
    approve_as(agent, ctx, "reviewer-x")
    (entry,) = _entries(ctx, "write_upload")
    out = entry["outputs"]
    assert out["approved_by"] == "reviewer-x" and out["manifest"]["pins"]["skills"] and out["approval_id"]
    assert out["manifest"]["eval_key"] == _engine_entries(ctx, "eval.manifest")[0]["outputs"]["manifest"]["eval_key"]
    assert "tie_out" not in out


@pytest.mark.parametrize("name", ["conductor", "agent", "Assistant", "system", "improver", "gpt-5.6-terra"])
def test_every_agent_or_reserved_reviewer_is_refused_at_the_decision(name: str, tmp_path: Path) -> None:
    """The library refuses the name when the decision is taken (`approver_name`), so no gate is released by it and
    nothing is written; `released_by` repeats the check on the record (tests/test_agent_gaps.py)."""
    with pytest.raises(UnnamedApprover):
        _gl_agent(tmp_path, reviewer=name)
    assert not list((tmp_path / "out").glob("GL_Upload*"))


def test_the_demo_reviewer_local_is_a_person_at_the_keyboard() -> None:
    assert person("local") == "local reviewer" and person("") == "" and person("reviewer-x") == "reviewer-x"


# ---- run_job -----------------------------------------------------------------------------------------------
def test_run_job_returns_a_digest_within_6kb_and_changes_nothing(tmp_path: Path) -> None:
    ctx, _agent, _picks, _gl = _gl_agent(tmp_path, reviewer="reviewer-x")
    ctx.restore = True  # a Coworker revived after a restart: the tools load the run's saved state
    tools = {t.name: t for t in build_tools(ctx)}
    text = tools["run_job"].invoke({"stage_id": "s3", "why": "see where the job stands"})
    assert text.startswith("outcome: complete; reached: check")
    assert len(text) <= MAX_RESPONSE_CHARS and "ties yes" in text
    assert "Fund A" not in text and "J-3001" not in text  # a digest has no rows
    assert not list(ctx.out_dir.glob("GL_Upload*"))
    (rec,) = _entries(ctx, "run_job")
    assert rec["outputs"]["outcome"] == "complete" and rec["outputs"]["err"] == 0
    assert "digest" not in rec["outputs"] and "_text" not in rec["outputs"]


def test_run_job_names_a_unit_by_its_file(tmp_path: Path) -> None:
    src = S.write_gl(tmp_path / "src" / "gl.xlsx", "GL Fund")
    ctx = GL.agent_ctx(tmp_path)
    (ctx.inbox_roots[0] / "gl.xlsx").write_bytes(src.read_bytes())
    tools = {t.name: t for t in build_tools(ctx)}
    text = tools["run_job"].invoke({"path": "gl.xlsx", "record_type": "GL", "columns": GL.COLUMNS,
                                    "fund_id": "5317401", "fund_name": "Fund A", "why": "a dirty file"})
    assert text.startswith("outcome: needs_human; reached: check") and len(text) <= MAX_RESPONSE_CHARS
    assert "ERR" in text and "check-data-quality.account_not_in_chart" in text
    assert not list(ctx.out_dir.glob("GL_Upload*"))
    both = tools["run_job"].invoke({"path": "gl.xlsx", "stage_id": "s1", "record_type": "GL", "why": "both"})
    assert both.startswith("ERROR") and "not both" in both


def test_agent_to_spec_says_when_a_table_is_not_one_the_closed_steps_can_say(tmp_path: Path) -> None:
    ctx = GL.agent_ctx(tmp_path)
    with pytest.raises(NotTranslatable, match="no staged table"):
        agent_to_spec(ctx)


@pytest.mark.slow
def test_run_job_digest_is_within_6kb_on_a_million_line_gl(tmp_path: Path) -> None:
    """The Coworker's `run_job` on a reference to a million-line file: the digest, not the rows, comes back."""
    script = tmp_path / "big_run_job.py"
    script.write_text(textwrap.dedent('''
        import sys
        from pathlib import Path
        sys.path.insert(0, sys.argv[2])
        import large_gl
        from worker.accounts import AccountStore, key_of
        from worker.audit import AuditLog
        from memprobe import peak_memory_mb
        from worker.context import RunContext
        from worker.staging import Stage
        from worker.tools import build_tools

        root = Path(sys.argv[1])
        inbox = root / "inbox"
        inbox.mkdir()
        large_gl.build(inbox / "gl.csv")
        out = root / "out"
        ctx = RunContext(sponsor="sponsor-a", gp_id="sponsor-a", inbox_roots=[inbox], out_dir=out,
                         audit=AuditLog(out / "audit.jsonl"), stage=Stage(out), accounts_root=root / "accounts")
        for acct in ("1000", "1500", "1600", "8050", "9010"):  # approved earlier: only 6150 is unmapped
            AccountStore(root / "accounts", "sponsor-a").add(key_of("", acct, "Expense"), "11010",
                                                             source="earlier job", reason="approved earlier")
        cols = {"Account": "GL Account", "Deal": "Investment", "Journal": "Journal", "Debit": "Debit",
                "Credit": "Credit"}
        tool = {t.name: t for t in build_tools(ctx)}["run_job"]
        text = tool.invoke({"path": "gl.csv", "record_type": "GL", "columns": cols, "fund_id": "F-1",
                            "fund_name": "Fund A", "why": "where does the file stand"})
        print("RESULT", len(text), text.splitlines()[0].split(";")[0].split(": ")[1], round(peak_memory_mb()))
        print("FIRST", text.splitlines()[1])
    '''), encoding="utf-8")
    env = {"SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), "PATH": "", "PYTHONPATH": os.pathsep.join(
        [str(Path("src").resolve()), str(Path("tests").resolve())])}
    done = subprocess.run([sys.executable, str(script), str(tmp_path), str(Path("tests").resolve())],
                          capture_output=True, text=True, timeout=3000, env=env, check=False)
    line = next((x for x in done.stdout.splitlines() if x.startswith("RESULT")), done.stderr[-2500:])
    assert done.returncode == 0, line
    _, size, outcome, peak = line.split()
    assert int(size) <= MAX_RESPONSE_CHARS and outcome == "needs_human" and int(peak) < 600
    first = next(x for x in done.stdout.splitlines() if x.startswith("FIRST"))
    assert "read 999999" in first
