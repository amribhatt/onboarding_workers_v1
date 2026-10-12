from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import openpyxl
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from scripted_model import ScriptedModel, approve, call, say
from worker.agent import build_agent, start
from worker.audit import AuditError, AuditLog
from worker.cli import main
from worker.context import RunContext
from worker.idgen import IdScheme
from worker.recipes import RecipeStore
from worker.registry import Registry
from worker.staging import Stage


def _ctx(tmp_path: Path) -> RunContext:
    out = tmp_path / "run1" / "out"
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    book = openpyxl.Workbook()
    ws = book.active
    ws.append(["ID", "Name"])
    for r in [["I1", "Alpha"], ["I2", "Beta LLC"], ["I7", "Gamma"], ["I4", "Delta"], ["I5", "Echo"]]:
        ws.append(r)
    book.save(inbox / "f.xlsx")
    reg = Registry(tmp_path / "state", "sponsor-a")
    reg.add(
        "Investors",
        [{"key": "I1", "name": "Alpha"}, {"key": "I2", "name": "Beta"}, {"key": "I3", "name": "Gamma"}],
        source="app batch",
        batch="job-0",
    )
    IdScheme(tmp_path / "state" / "ids", "sponsor-a", "Investors").change(
        {"mode": "sequential", "prefix": "GINV-"}, by="reviewer-a", reason="sponsor standard", record=lambda *a: None
    )
    return RunContext(
        sponsor="sponsor-a",
        gp_id="GP-001",
        inbox_roots=[inbox],
        out_dir=out,
        audit=AuditLog(out / "audit.jsonl"),
        stage=Stage(out),
        registry=reg,
        recipes=RecipeStore(tmp_path / "state" / "recipes", "sponsor-a"),
        id_root=tmp_path / "state" / "ids",
        instruction="Build me the Ledgerline sheet",
    )


def _finished_job(tmp_path: Path) -> RunContext:
    ctx = _ctx(tmp_path)
    decisions: dict[str, Any] = {
        "Sheet!3": {"action": "exclude_incorrect", "reason": "name is wrong in the source"},
        "Sheet!4": {"action": "exclude_duplicate", "reason": "same investor as I3 under another ID"},
    }
    script = [
        say(
            "lift",
            call(
                "lift", path="f.xlsx", sheet="Sheet", template="Investors",
                columns={"Investor_ID": "A", "Investor_Name": "B"}, constants={"GP_ID": "GP-001"},
                header_row=1, first_row=2, why="copy",
            ),
        ),
        say("match", call("match_existing", stage_id="s1", why="dedup against existing records")),
        say("resolve", call("resolve_conflicts", stage_id="s2", decisions=decisions, why="human decided")),
        say(
            "ids",
            call("assign_ids", stage_id="s3", id_field="Investor_ID", source_key_field="Source_Investor_Key",
                 why="issue IDs"),
        ),
        say("write", call("write_upload", stage_id="s4", template="Investors", why="deliver")),
        say("done"),
    ]
    agent = build_agent(ctx, ScriptedModel(script=script), InMemorySaver())
    start(agent, ctx, "build", "t1")
    approve(agent, ctx, "t1", "reviewed")
    return ctx


def test_a_missing_reason_is_refused(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    with pytest.raises(AuditError, match="reason is required"):
        log.record("anything", "   ")
    with pytest.raises(AuditError):
        log.record_many("anything", [("fine", None, None), ("", None, None)])
    assert log.read() == []  # nothing from the refused batch was written


def test_trail_csv_lists_every_excluded_row_with_its_reason(tmp_path: Path) -> None:
    ctx = _finished_job(tmp_path)
    path = ctx.out_dir / "sponsor-a_AuditTrail.csv"
    assert path.is_file()
    rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
    assert list(rows[0]) == ["time", "who", "action", "record", "before", "after", "reason"]
    by_action: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        by_action.setdefault(r["action"], []).append(r)
    excluded = by_action["row.excluded"]
    assert [r["record"] for r in excluded] == ["Sheet!2 (I1)"] and "already onboarded" in excluded[0]["reason"]
    resolved = {r["record"].split(" ")[0]: r for r in by_action["conflict.resolved"]}
    assert resolved["Sheet!3"]["reason"] == "name is wrong in the source"
    assert resolved["Sheet!4"]["reason"] == "same investor as I3 under another ID"
    gate = by_action["gate.approve"][0]
    assert gate["who"] == "reviewer-a" and gate["reason"] == "reviewed"


def test_every_output_records_the_versions_it_used(tmp_path: Path) -> None:
    ctx = _finished_job(tmp_path)
    stamp = next(e for e in ctx.audit.read() if e["action"] == "job.versions")
    v = stamp["outputs"]
    assert v["template"]["record_type"] == "Investors" and len(v["template"]["sha256"]) == 64
    assert v["recipe"] == {"version": 1, "variant": "var1"}
    assert v["id_scheme"]["scheme"]["prefix"] == "GINV-" and v["id_scheme"]["counter_to"] == 3
    rules = (ctx.out_dir / "sponsor-a_Rules.md").read_text(encoding="utf-8")
    assert "| required | GP_ID, Investor_ID, Investor_Name | organisation |" in rules
    assert "saved workflow: version 1, variant var1" in rules
    assert "ID scheme" in rules and "Build me the Ledgerline sheet" in rules


def test_a_later_config_change_does_not_alter_the_finished_job(tmp_path: Path) -> None:
    ctx = _finished_job(tmp_path)
    rules_path = ctx.out_dir / "sponsor-a_Rules.md"
    before = rules_path.read_text(encoding="utf-8")
    IdScheme(tmp_path / "state" / "ids", "sponsor-a", "Investors").change(
        {"mode": "sequential", "prefix": "ZZ-"}, counter=500, by="reviewer-a", reason="later change", record=lambda *a: None
    )
    assert rules_path.read_text(encoding="utf-8") == before


def test_tampering_is_detected_and_reported_by_verify(tmp_path: Path) -> None:
    ctx = _finished_job(tmp_path)
    path = ctx.out_dir / "audit.jsonl"
    out = io.StringIO()
    assert main(["verify", str(path)], out=out) == 0 and "OK" in out.getvalue()
    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[4])
    entry["rationale"] = "changed after the fact"
    lines[4] = json.dumps(entry, separators=(",", ":"), ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = io.StringIO()
    assert main(["verify", str(path)], out=out) == 1
    assert "BROKEN at line 5" in out.getvalue() and "altered" in out.getvalue()
    assert AuditLog(path).first_break()[0] == 4
