from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from worker import metrics
from worker.metrics import ChangeLog, harness_metrics, job_facts

PRICES = {"WORKER_PRICE_IN_PER_M": "3", "WORKER_PRICE_OUT_PER_M": "15", "WORKER_REVIEWER_RATE": "60"}


def _write(path: Path, entries: list[tuple[str, str, str, dict[str, Any], dict[str, Any]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for seq, (ts, action, why, inputs, outputs) in enumerate(entries, start=1):
        lines.append(json.dumps({"seq": seq, "ts": ts, "action": action, "rationale": why, "inputs": inputs,
                                 "outputs": outputs, "prev_hash": "x", "hash": "y"}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fixture(root: Path) -> None:
    d = "2026-10-06T"  # a Tuesday, ISO week 2026-W41
    _write(root / "a" / "audit.jsonl", [
        (f"{d}09:00:00+00:00", "run.start", "human request", {"sponsor": "sponsor-a"}, {}),
        (f"{d}09:00:10+00:00", "skill.loaded", "GL job", {"skill": "map-accounts", "version": "1.0", "scenario": None}, {}),
        (f"{d}09:00:20+00:00", "account.mapped", "L0", {"key": "k1"}, {"row": "L0", "action": "apply"}),
        (f"{d}09:00:20+00:00", "account.mapped", "L1", {"key": "k2"}, {"row": "L1", "action": "apply"}),
        (f"{d}09:00:20+00:00", "account.mapped", "L3", {"key": "k3"}, {"row": "L3", "action": "suggest"}),
        (f"{d}09:00:20+00:00", "account.mapped", "L5", {"key": "k4"}, {"row": "L5", "action": "needs_you"}),
        (f"{d}09:00:30+00:00", "model.call", "decide keys", {}, {"input_tokens": 1000, "output_tokens": 200, "seconds": 2.0}),
        (f"{d}09:00:31+00:00", "model.call", "decide keys", {}, {"input_tokens": 500, "output_tokens": 100, "seconds": 4.0}),
        (f"{d}09:00:40+00:00", "stage.failed", "timeout", {"stage": "map"}, {}),
        (f"{d}09:01:00+00:00", "feedback", "override", {"kind": "suggestion", "action": "override"}, {}),
        (f"{d}09:01:01+00:00", "feedback", "accept", {"kind": "suggestion", "action": "accept"}, {}),
        (f"{d}09:02:00+00:00", "row.excluded", "already onboarded", {"row": "Sheet!2"}, {}),
        (f"{d}09:02:01+00:00", "row.excluded", "already onboarded", {"row": "Sheet!3"}, {}),
        (f"{d}09:02:02+00:00", "conflict.resolved", "duplicate", {"row": "Sheet!4", "action": "exclude_duplicate"}, {}),
        (f"{d}09:09:00+00:00", "write_upload", "deliver", {"template": "GL"}, {"rows": 2000}),
        (f"{d}09:10:00+00:00", "gate.approve", "ok", {}, {}),
        (f"{d}09:10:01+00:00", "job.versions", "stamp", {"record_type": "GL"}, {}),
    ])
    _write(root / "b" / "audit.jsonl", [
        (f"{d}10:00:00+00:00", "run.start", "human request", {"sponsor": "sponsor-a"}, {}),
        (f"{d}10:00:30+00:00", "gate.reject", "wrong sheet", {}, {}),
        (f"{d}10:00:31+00:00", "feedback", "wrong sheet", {"kind": "job", "action": "reject"}, {}),
        (f"{d}10:03:00+00:00", "write_upload", "deliver", {"template": "Investors"}, {"rows": 500}),
        (f"{d}10:05:00+00:00", "gate.approve", "ok", {}, {}),
        (f"{d}10:05:01+00:00", "job.versions", "stamp", {"record_type": "Investors"}, {}),
        (f"{d}10:06:00+00:00", "feedback", "Ledgerline rejected 3 lines", {"kind": "outcome", "action": "flag",
                                                                         "outcome": "rejected"}, {}),
    ])


def test_metrics_match_hand_computed_values(tmp_path: Path) -> None:
    _fixture(tmp_path)
    report = harness_metrics(tmp_path, env=PRICES)
    t = report["totals"]
    assert report["jobs"] == 2 and t["rows"] == 2500
    assert t["overrides_per_1000_rows"] == 0.4  # 1 override / 2,500 rows
    assert t["questions_per_1000_rows"] == 2.4  # A: 1 gate + 2 keys for you + 1 resolved; B: 2 gates -> 6 / 2,500
    assert t["rows_left_out_per_1000_rows"] == 1.2  # 2 left out as existing + 1 excluded by the analyst
    assert t["accept_all_rate"] == 0.5
    assert t["keys_resolved_without_model"] == 0.5  # L0 + L1 of 4 keys
    assert t["first_pass_rate"] == 0.0  # A had a resolved conflict, B a reject
    assert t["upload_rejections"] == 1
    assert t["model_calls_per_1000_rows"] == 0.8 and t["tokens_per_1000_rows"] == 720.0
    assert t["success_rate"] == 0.5 and t["jobs"] == 2
    # cost per successful job = (model spend + reviewer time x rate) / successful jobs
    #   model: 1500 in x $3/M + 300 out x $15/M = 0.009; reviewer: (60 s + 150 s) = 0.0583 h x $60 = 3.5
    assert t["cost_breakdown"]["model_spend"] == 0.009 and t["cost_breakdown"]["reviewer_cost"] == 3.5
    assert t["cost_breakdown"]["retries"] == 1
    assert t["cost_per_successful_job"] == pytest.approx(3.509)
    assert t["exclusions_without_reason"] == 0


def test_grouping_by_sponsor_record_type_week_and_skill(tmp_path: Path) -> None:
    _fixture(tmp_path)
    report = harness_metrics(tmp_path, env=PRICES)
    assert set(report["groups"]) == {"sponsor-a/GL/2026-W41", "sponsor-a/Investors/2026-W41"}
    assert report["groups"]["sponsor-a/GL/2026-W41"]["rows"] == 2000
    assert report["by_skill"]["map-accounts"]["jobs"] == 1 and report["by_skill"]["(none)"]["jobs"] == 1


def test_skill_usage_is_seen_from_the_models_side(tmp_path: Path) -> None:
    _fixture(tmp_path)
    usage = harness_metrics(tmp_path)["skill_usage"]["map-accounts"]
    assert usage == {"loaded": 1, "needed": 1, "loaded_not_needed": 0, "needed_not_loaded": 0, "scenarios": []}
    # a job that used the skill without loading it, and one that loaded it for nothing
    d = "2026-10-07T08:00:00+00:00"
    _write(tmp_path / "c" / "audit.jsonl", [
        (d, "run.start", "x", {"sponsor": "sponsor-a"}, {}),
        (d, "account.mapped", "L0", {"key": "k"}, {"row": "L0", "action": "apply"}),
    ])
    _write(tmp_path / "d" / "audit.jsonl", [
        (d, "run.start", "x", {"sponsor": "sponsor-a"}, {}),
        (d, "skill.loaded", "just in case", {"skill": "map-accounts", "version": "1.0",
                                              "scenario": "bank-accounts.md"}, {}),
    ])
    usage = harness_metrics(tmp_path)["skill_usage"]["map-accounts"]
    assert (usage["loaded"], usage["needed"]) == (2, 2)
    assert usage["loaded_not_needed"] == 1 and usage["needed_not_loaded"] == 1
    assert usage["scenarios"] == ["map-accounts/bank-accounts.md"]


def test_a_definition_change_does_not_alter_past_numbers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fixture(tmp_path)
    before = harness_metrics(tmp_path, version="1", env=PRICES)

    def v2(jobs: Any, prices: Any) -> dict[str, Any]:
        out = metrics._definition_v1(jobs, prices)
        out["questions_per_1000_rows"] = 0.0  # a redefinition
        return out

    monkeypatch.setitem(metrics.DEFINITIONS, "2", v2)
    assert harness_metrics(tmp_path, version="2", env=PRICES)["totals"]["questions_per_1000_rows"] == 0.0
    assert harness_metrics(tmp_path, version="1", env=PRICES) == before  # the baseline recomputes unchanged
    with pytest.raises(ValueError, match="unknown metric definition"):
        harness_metrics(tmp_path, version="9")


def test_only_an_improver_change_is_the_last_promoted_change(tmp_path: Path) -> None:
    log = ChangeLog(tmp_path / "changes.jsonl")
    log.add("c1", "improver", "table:coa-cascade", "Valuation -> 15910", ts="2026-10-02T00:00:00+00:00")
    log.add("c2", "harness_fix", "tools.py", "fix repeated-failure counter", ts="2026-10-04T00:00:00+00:00")
    assert log.last_promoted()["id"] == "c1"  # only an improver change is "the last promoted change"
    with pytest.raises(ValueError):
        log.add("c3", "learning", "x", "y")


def test_no_job_has_no_facts() -> None:
    assert job_facts([]) is None
