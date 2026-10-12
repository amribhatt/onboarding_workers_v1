"""Migration M8: skills load through the strict loader, sponsor tables sit per skill, and a refused pin stops a job.

Fictitious data only.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from langgraph.checkpoint.memory import InMemorySaver

from worker.agent import build_agent, start
from worker.audit import AuditError, AuditLog
from worker.context import RunContext
from worker.decide import Table, TableError
from worker.rulefiles import SchemaError
from worker.skills import (
    LegacySponsorTables,
    SkillLoadRefused,
    load_skills,
    require_valid_skills,
)
from worker.staging import Stage

REPO = Path(__file__).resolve().parents[1]
OLD_FILE = {
    "id": "coa-cascade",
    "inputs": {"historic": "bool", "pinned": "bool", "tt_known": "bool", "score": "number"},
    "rows": [{"id": "L3", "when": {"tt_known": True, "score": ">=0.9"},
              "then": {"tier": "L3 Hybrid", "action": "suggest"}}],
}


def _rows(table: Table) -> list[dict[str, Any]]:
    return [dict(r) for r in table.rows]


# ---- sponsor tables: per skill; the earlier place is refused, and a move brings the files across -----------
def test_sponsor_table_old_path_is_refused_by_name(tmp_path: Path) -> None:
    skill = load_skills()["map-accounts"]
    root = tmp_path / "sponsors" / "sponsor-a"
    (root / "tables").mkdir(parents=True)
    (root / "tables" / "coa-cascade.yaml").write_text(yaml.safe_dump(OLD_FILE), encoding="utf-8")

    with pytest.raises(LegacySponsorTables, match="coa-cascade.yaml"):  # never read, never quietly dropped
        skill.table("coa-cascade", root)
    with pytest.raises(LegacySponsorTables):
        skill.content_hash(root)
    with pytest.raises(ValueError, match="tables folder"):  # the old argument, the tables folder itself
        skill.table("coa-cascade", root / "tables")

    new = root / "skills" / "map-accounts" / "tables" / "coa-cascade.yaml"  # moved by hand, with the v1 header
    new.parent.mkdir(parents=True)
    (root / "tables" / "coa-cascade.yaml").rename(new)
    (root / "tables").rmdir()
    after = skill.table("coa-cascade", root)  # a header-less file is still read as a v1 overlay
    org = skill.table("coa-cascade")
    assert after.rows != org.rows and after.inputs == org.inputs
    assert next(r for r in after.rows if r["id"] == "L3")["then"]["action"] == "suggest"
    assert skill.content_hash(root) != skill.content_hash()


def test_a_sponsor_table_never_changes_the_organisation_inputs_and_unknown_inputs_are_refused(tmp_path: Path) -> None:
    org = load_skills()["map-accounts"].table("coa-cascade")
    row = {"id": "L3", "when": {"tt_known": True, "score": ">=0.9"}, "then": {"tier": "L3 Hybrid", "action": "suggest"}}
    merged = org.with_override({"id": "coa-cascade", "inputs": {"extra": "bool"}, "rows": [row]})
    assert merged.inputs == org.inputs and "extra" not in merged.inputs
    with pytest.raises(TableError, match="unknown inputs"):
        org.with_override({"id": "coa-cascade", "rows": [{**row, "when": {"colour": "red"}}]})


def test_a_sponsor_table_that_loosens_nothing_but_breaks_the_schema_is_refused_on_load(tmp_path: Path) -> None:
    skill = load_skills()["map-accounts"]
    place = tmp_path / "sponsors" / "sponsor-a" / "skills" / "map-accounts" / "tables"
    place.mkdir(parents=True)
    bad = {"schema_version": 1, "kind": "table", "id": "coa-cascade",
           "rows": [{"id": "L3", "when": {"score": ">=abc"}, "then": {"tier": "x", "action": "suggest"}}]}
    (place / "coa-cascade.yaml").write_text(yaml.safe_dump(bad), encoding="utf-8")
    with pytest.raises(TableError, match="coa-cascade"):
        skill.table("coa-cascade", tmp_path / "sponsors" / "sponsor-a")


# ---- decision tables: bad conditions and a missing catch-all are found when the table is built ----------
def test_decide_finds_a_bad_condition_and_a_missing_catch_all_at_load() -> None:
    ok = [{"id": "a", "when": {"n": ">=5"}, "then": {"out": "A"}}, {"id": "z", "then": {"out": "Z"}}]
    Table.from_dict({"id": "t", "inputs": {"n": "number"}, "rows": ok})
    with pytest.raises(TableError, match="catch-all"):
        Table.from_dict({"id": "t", "inputs": {"n": "number"}, "rows": ok[:1]})
    bad = [{"id": "a", "when": {"n": {"between": [1, 2]}}, "then": {"out": "A"}}, ok[1]]
    with pytest.raises(TableError, match="not a condition"):
        Table.from_dict({"id": "t", "inputs": {"n": "number"}, "rows": bad})
    wrong = [{"id": "a", "when": {"n": "high"}, "then": {"out": "A"}}, ok[1]]
    with pytest.raises(TableError, match="n"):
        Table.from_dict({"id": "t", "inputs": {"n": "number"}, "rows": wrong})


# ---- a refused pin is audited before the job starts, and the job does not start ------------------------
def _broken_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "skills"
    shutil.copytree(REPO / "skills", root)
    table = root / "map-accounts" / "tables" / "coa-cascade.yaml"
    table.write_text(table.read_text(encoding="utf-8").replace("kind: table\n", "kind: table\nbogus: 1\n", 1),
                     encoding="utf-8")
    monkeypatch.setenv("WORKER_SKILLS_DIR", str(root))
    return root


def _ctx(tmp_path: Path, audit: Any) -> RunContext:
    out = tmp_path / "run" / "out"
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    return RunContext(sponsor="sponsor-a", gp_id="GP-001", inbox_roots=[inbox], out_dir=out, audit=audit,
                      stage=Stage(out), accounts_root=tmp_path / "state" / "accounts")


def test_load_refused_is_audited_and_job_does_not_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _broken_skills(tmp_path, monkeypatch)
    with pytest.raises(SchemaError):
        load_skills()
    audit = AuditLog(tmp_path / "run" / "out" / "audit.jsonl")
    ctx = _ctx(tmp_path, audit)

    with pytest.raises(SkillLoadRefused) as e:  # the agent is not built
        build_agent(ctx, None, InMemorySaver())  # type: ignore[arg-type]
    assert any("bogus" in line for line in e.value.problems)
    with pytest.raises(SkillLoadRefused):  # and a run on a thread does not begin
        start(None, ctx, "build me the sheet", "t1")
    log = audit.read()
    assert [x["action"] for x in log] == ["skill.load_refused", "skill.load_refused"]
    rec = log[0]
    assert rec["rationale"] and any("coa-cascade.yaml" in line for line in rec["inputs"]["problems"])
    assert rec["outputs"] == {"started": False} and "unknown_field" in rec["inputs"]["codes"]
    assert not any(x["action"] == "run.start" for x in log)


def test_load_refused_with_no_audit_available_also_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _broken_skills(tmp_path, monkeypatch)
    with pytest.raises(SkillLoadRefused, match="no audit log"):
        require_valid_skills(None)

    class Broken:
        def record(self, *a: Any, **k: Any) -> dict[str, Any]:
            raise AuditError("disk full")

    with pytest.raises(SkillLoadRefused, match="could not be written"):
        build_agent(_ctx(tmp_path, Broken()), None, InMemorySaver())  # type: ignore[arg-type]


def test_a_valid_pin_loads_without_an_audit_record(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    assert {"map-accounts", "reconcile-gl", "check-data-quality"} <= set(require_valid_skills(audit))
    assert audit.read() == []
