"""Latency numbers from the audit: p50 / p95 per record type, per sponsor and per 1,000 rows.

Reads the `stage.done` and `model.call` records that `worker.pipeline.Job` writes; nothing else is measured.
"""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile; 0.0 for no values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return round(ordered[min(rank, len(ordered)) - 1], 4)


def audit_files(root: str | Path) -> list[Path]:
    base = Path(root)
    if base.is_file():
        return [base]
    return sorted(base.rglob("audit.jsonl"))


def _entries(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue


def _spread(values: Sequence[float]) -> dict[str, Any]:
    return {"n": len(values), "p50": percentile(values, 50), "p95": percentile(values, 95)}


def latency(root: str | Path) -> dict[str, Any]:
    """{by_record_type, by_sponsor, per_1000_rows, model_calls} computed from every audit under `root`."""
    stage_secs: dict[tuple[str, str], list[float]] = defaultdict(list)
    sponsor_secs: dict[str, list[float]] = defaultdict(list)
    rate: dict[str, list[float]] = defaultdict(list)
    model_secs: list[float] = []
    jobs: dict[str, dict[str, Any]] = {}
    for e in _entries(audit_files(root)):
        if e["action"] == "model.call":
            model_secs.append(float((e.get("outputs") or {}).get("seconds") or 0))
        if e["action"] != "stage.done":
            continue
        i, o = e.get("inputs") or {}, e.get("outputs") or {}
        secs = float(o.get("seconds") or 0)
        stage_secs[(i.get("record_type", "?"), i.get("stage", "?"))].append(secs)
        sponsor_secs[i.get("sponsor", "?")].append(secs)
        job = jobs.setdefault(i.get("trace_id", "?"), {"type": i.get("record_type", "?"), "secs": 0.0, "rows": 0})
        job["secs"] += secs
        job["rows"] = max(job["rows"], int(o.get("rows") or 0))
    for job in jobs.values():
        if job["rows"]:
            rate[job["type"]].append(job["secs"] / job["rows"] * 1000)
    return {
        "by_record_type": {f"{rt}/{st}": _spread(v) for (rt, st), v in sorted(stage_secs.items())},
        "by_sponsor": {s: _spread(v) for s, v in sorted(sponsor_secs.items())},
        "per_1000_rows": {rt: {"jobs": len(v), **_spread(v)} for rt, v in sorted(rate.items())},
        "model_calls": _spread(model_secs),
    }


# ---- harness metrics in business units (ticket 23) -----------------------------------------------------------
# A definition is a function of the audit facts of a set of jobs. Each is versioned: a number is always recomputed
# with the definition pinned at the baseline version, so changing a definition never alters past numbers.
DEFINITION_VERSION = "1"
WRITE_ACTIONS = ("write_upload",)
EVIDENCE = {"map-accounts": ("account.mapped", "account.confirmed")}  # actions that show a skill was needed


def _ts(entry: dict[str, Any]) -> float:
    return datetime.fromisoformat(entry["ts"]).timestamp()


def _week(entry: dict[str, Any]) -> str:
    year, week, _ = datetime.fromisoformat(entry["ts"]).isocalendar()
    return f"{year}-W{week:02d}"


def job_facts(entries: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """Everything one job's audit says, as plain counts. None for an empty audit."""
    if not entries:
        return None
    start = next((e for e in entries if e["action"] == "run.start"), entries[0])
    sponsor = (start.get("inputs") or {}).get("sponsor", "?")
    versions = next((e for e in entries if e["action"] == "job.versions"), None)
    written = [e for e in entries if e["action"] in WRITE_ACTIONS]
    record_type = ((versions or {}).get("inputs") or {}).get("record_type") or next(
        ((e.get("inputs") or {}).get("template") for e in written), "?"
    )
    rows = int((written[-1].get("outputs") or {}).get("rows") or 0) if written else 0
    if not rows:
        stage_rows = [int((e.get("outputs") or {}).get("rows") or 0) for e in entries if e["action"] == "stage.done"]
        rows = max(stage_rows, default=0)
    fb = [e["inputs"] for e in entries if e["action"] == "feedback"]
    gates = [e for e in entries if e["action"].startswith("gate.")]
    approved = [e for e in entries if e["action"] == "gate.approve"]
    mapped = [e for e in entries if e["action"] == "account.mapped"]
    excluded = [e for e in entries if e["action"] == "row.excluded"]
    resolved = [e for e in entries if e["action"] == "conflict.resolved"]
    analyst_excluded = [e for e in resolved if str((e.get("inputs") or {}).get("action", "")).startswith("exclude")]
    calls = [e for e in entries if e["action"] == "model.call"]
    reviewer_secs = 0.0  # how long each gate decision waited on the human, from the record just before it
    for i, e in enumerate(entries):
        if e["action"].startswith("gate.") and i > 0:
            reviewer_secs += max(0.0, _ts(e) - _ts(entries[i - 1]))
    loaded = [e.get("inputs") or {} for e in entries if e["action"] == "skill.loaded"]
    rejected_after = [r for r in fb if r.get("kind") == "outcome" and r.get("outcome") == "rejected"]
    needs_revision = [r for r in fb if r.get("kind") == "job" and r.get("action") in ("reject", "flag")]
    done = bool(approved) and any(e["action"] == "job.versions" for e in entries)
    return {
        "sponsor": sponsor,
        "record_type": record_type,
        "week": _week(entries[0]),
        "rows": rows,
        "approved": done,
        "overrides": sum(1 for r in fb if r.get("action") == "override"),
        "accepts": sum(1 for r in fb if r.get("action") == "accept"),
        "questions": len(gates) + sum(1 for e in mapped if (e.get("outputs") or {}).get("action") != "apply")
        + len(resolved),
        "rows_left_out": len(excluded) + len(analyst_excluded),
        "exclusions_without_reason": sum(
            1 for e in excluded + analyst_excluded if not str(e.get("rationale", "")).strip()
        ),
        "keys": len(mapped),
        "keys_without_model": sum(1 for e in mapped if (e.get("outputs") or {}).get("row") in ("L0", "L1")),
        "first_pass": done and not any(e["action"] == "gate.reject" for e in entries) and not resolved,
        "upload_rejections": len(rejected_after),
        "needs_revision": len(needs_revision),
        "success": done and not rejected_after and not needs_revision,
        "seconds_to_approve": round(_ts(approved[0]) - _ts(start), 1) if approved else None,
        "model_calls": len(calls),
        "tokens_in": sum(int((e.get("outputs") or {}).get("input_tokens") or 0) for e in calls),
        "tokens_out": sum(int((e.get("outputs") or {}).get("output_tokens") or 0) for e in calls),
        "retries": sum(1 for e in entries if e["action"] == "stage.failed"),
        "reviewer_seconds": round(reviewer_secs, 1),
        "skills_loaded": sorted({str(i.get("skill")) for i in loaded if i.get("skill")}),
        "scenarios_loaded": sorted({f"{i.get('skill')}/{i['scenario']}" for i in loaded if i.get("scenario")}),
        "skills_needed": sorted(s for s, acts in EVIDENCE.items() if any(e["action"] in acts for e in entries)),
    }


def _per_1000(n: float, rows: int) -> float | None:
    return round(n / rows * 1000, 2) if rows else None


def _definition_v1(jobs: Sequence[dict[str, Any]], prices: dict[str, float]) -> dict[str, Any]:
    rows = sum(j["rows"] for j in jobs)
    ok = [j for j in jobs if j["success"]]
    keys = sum(j["keys"] for j in jobs)
    spend = sum(j["tokens_in"] / 1e6 * prices["in_per_m"] + j["tokens_out"] / 1e6 * prices["out_per_m"] for j in jobs)
    reviewer_hours = sum(j["reviewer_seconds"] for j in jobs) / 3600
    review_cost = reviewer_hours * prices["reviewer_per_hour"]
    judged = sum(j["accepts"] + j["overrides"] for j in jobs)
    times = [float(j["seconds_to_approve"]) for j in jobs if j["seconds_to_approve"] is not None]
    return {
        "jobs": len(jobs),
        "rows": rows,
        "overrides_per_1000_rows": _per_1000(sum(j["overrides"] for j in jobs), rows),
        "questions_per_1000_rows": _per_1000(sum(j["questions"] for j in jobs), rows),
        "rows_left_out_per_1000_rows": _per_1000(sum(j["rows_left_out"] for j in jobs), rows),
        "exclusions_without_reason": sum(j["exclusions_without_reason"] for j in jobs),
        "accept_all_rate": round(sum(j["accepts"] for j in jobs) / judged, 3) if judged else None,
        "keys_resolved_without_model": round(sum(j["keys_without_model"] for j in jobs) / keys, 3) if keys else None,
        "first_pass_rate": round(sum(1 for j in jobs if j["first_pass"]) / len(jobs), 3) if jobs else None,
        "upload_rejections": sum(j["upload_rejections"] for j in jobs),
        "seconds_to_approve_p50": percentile(times, 50) if times else None,
        "model_calls_per_1000_rows": _per_1000(sum(j["model_calls"] for j in jobs), rows),
        "tokens_per_1000_rows": _per_1000(sum(j["tokens_in"] + j["tokens_out"] for j in jobs), rows),
        "success_rate": round(len(ok) / len(jobs), 3) if jobs else None,
        "cost_per_successful_job": round((spend + review_cost) / len(ok), 4) if ok else None,
        "cost_breakdown": {
            "model_spend": round(spend, 4),
            "reviewer_cost": round(review_cost, 4),
            "reviewer_hours": round(reviewer_hours, 3),
            "retries": sum(j["retries"] for j in jobs),
        },
    }


DEFINITIONS: dict[str, Any] = {"1": _definition_v1}


def _prices(env: Mapping[str, str] | None) -> dict[str, float]:
    e = env if env is not None else os.environ
    return {
        "in_per_m": float(e.get("WORKER_PRICE_IN_PER_M") or 0),
        "out_per_m": float(e.get("WORKER_PRICE_OUT_PER_M") or 0),
        "reviewer_per_hour": float(e.get("WORKER_REVIEWER_RATE") or 0),
    }


def collect_jobs(root: str | Path) -> list[dict[str, Any]]:
    """The facts of every job under `root`, one per audit file."""
    jobs: list[dict[str, Any]] = []
    for path in audit_files(root):
        facts = job_facts(list(_entries([path])))
        if facts is not None:
            jobs.append(facts)
    return jobs


def harness_metrics(
    root: str | Path, *, version: str = DEFINITION_VERSION, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Business-unit metrics per sponsor, record type and ISO week, per skill, and skill usage, from every audit.

    `version` pins the definition: a baseline is always recomputed with its own version.
    """
    if version not in DEFINITIONS:
        raise ValueError(f"unknown metric definition version {version!r}; known: {sorted(DEFINITIONS)}")
    fn = DEFINITIONS[version]
    prices = _prices(env)
    jobs = collect_jobs(root)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    per_skill: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for j in jobs:
        groups[f"{j['sponsor']}/{j['record_type']}/{j['week']}"].append(j)
        for s in j["skills_needed"] or ["(none)"]:
            per_skill[s].append(j)
    usage: dict[str, dict[str, Any]] = {}
    for s in sorted({s for j in jobs for s in (*j["skills_loaded"], *j["skills_needed"])}):
        loaded = [j for j in jobs if s in j["skills_loaded"]]
        needed = [j for j in jobs if s in j["skills_needed"]]
        usage[s] = {
            "loaded": len(loaded),
            "needed": len(needed),
            "loaded_not_needed": sum(1 for j in loaded if s not in j["skills_needed"]),
            "needed_not_loaded": sum(1 for j in needed if s not in j["skills_loaded"]),
            "scenarios": sorted({sc for j in loaded for sc in j["scenarios_loaded"] if sc.startswith(f"{s}/")}),
        }
    return {
        "definition": version,
        "jobs": len(jobs),
        "totals": fn(jobs, prices),
        "groups": {k: fn(v, prices) for k, v in sorted(groups.items())},
        "by_skill": {k: fn(v, prices) for k, v in sorted(per_skill.items())},
        "skill_usage": usage,
    }


class ChangeLog:
    """Changes made to the harness, each tagged as an `improver` change or a `harness_fix`.

    Only improver changes count as learning; a bug fix is listed separately so it is never counted as a gain.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def add(self, change_id: str, tag: str, surface: str, note: str, ts: str | None = None) -> None:
        if tag not in ("improver", "harness_fix"):
            raise ValueError("tag must be 'improver' or 'harness_fix'")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"id": change_id, "tag": tag, "surface": surface, "note": note,
                 "ts": ts or datetime.now(UTC).isoformat()}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def all(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        with open(self.path, encoding="utf-8") as fh:
            return [json.loads(x) for x in fh if x.strip()]

    def last_promoted(self) -> dict[str, Any] | None:
        improver = [c for c in self.all() if c["tag"] == "improver"]
        return improver[-1] if improver else None
