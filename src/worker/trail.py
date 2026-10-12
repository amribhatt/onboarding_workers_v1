"""The two human-readable records of a finished job: `<Sponsor>_AuditTrail.csv` and `<Sponsor>_Rules.md`.

Both are plain renderings of the append-only audit log and of the version stamps recorded when the upload was
approved (`job.versions`); they hold no information the log does not. Finished jobs never change: a later change
to a template, recipe or ID scheme is a new record, listed in the trail of the job it happens in.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

TRAIL_COLUMNS = ("time", "who", "action", "record", "before", "after", "reason")
_HUMAN = ("gate.", "finding.acknowledged", "run.start")
_CODE = ("row.excluded", "registry.added", "id.counter_committed", "recipe.", "job.versions")
CONFIG_CHANGES = ("id.scheme_changed", "rule.approved", "recipe.saved", "existing_record.added", "registry.added")


def _text(value: Any) -> str:
    if value in (None, "", {}, []):
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _who(entry: Mapping[str, Any]) -> str:
    reviewer = (entry.get("inputs") or {}).get("reviewer")
    if reviewer:
        return str(reviewer)
    action = entry["action"]
    if action.startswith(_HUMAN):
        return "human"
    if action.startswith(_CODE):
        return "code"
    return "agent"


def trail_row(entry: Mapping[str, Any]) -> dict[str, str]:
    inputs, outputs = entry.get("inputs") or {}, entry.get("outputs") or {}
    record = inputs.get("row") or inputs.get("key") or inputs.get("finding") or inputs.get("record_type") \
        or inputs.get("stage_id") or inputs.get("path") or outputs.get("file") or ""
    if inputs.get("row") and inputs.get("key"):
        record = f"{inputs['row']} ({inputs['key']})"
    before = outputs.get("before") or inputs.get("old") or ""
    after = outputs.get("after") or outputs.get("new") or inputs.get("values") or ""
    return {
        "time": str(entry["ts"]),
        "who": _who(entry),
        "action": str(entry["action"]),
        "record": _text(record),
        "before": _text(before),
        "after": _text(after),
        "reason": str(entry.get("rationale", "")),
    }


def trail_rows(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    return [trail_row(e) for e in entries]


def write_trail(entries: Sequence[Mapping[str, Any]], out_dir: str | Path, sponsor: str) -> Path:
    """Write `<Sponsor>_AuditTrail.csv` (UTF-8 with BOM so Excel opens it). Rebuilt from the log each time."""
    path = Path(out_dir) / f"{sponsor}_AuditTrail.csv"
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=TRAIL_COLUMNS)
        writer.writeheader()
        writer.writerows(trail_rows(entries))
    tmp.replace(path)
    return path


def rules_markdown(
    sponsor: str, job: Mapping[str, Any], versions: Mapping[str, Any], settings: Mapping[str, Mapping[str, Any]],
    entries: Sequence[Mapping[str, Any]],
) -> str:
    """What this job used, with where each setting came from and the version of each part."""
    lines = [f"# {sponsor}: rules used by this job", ""]
    lines += [f"- job: {job.get('job', '')}", f"- record type: {versions['template']['record_type']}",
              f"- instruction: {job.get('instruction', '') or '(none recorded)'}",
              f"- output: {job.get('output', '')} (sha256 {job.get('output_sha256', '')})", ""]
    lines += ["## Settings and where they come from", "", "| Setting | Value | Source |", "|---|---|---|"]
    for name, item in settings.items():
        value = item["value"]
        shown = ", ".join(map(str, value)) if isinstance(value, (list, tuple)) else _text(value)
        lines.append(f"| {name} | {shown.replace('|', '/')} | {item['source']} |")
    lines += ["", "## Versions", ""]
    tpl = versions["template"]
    lines.append(f"- template file: `{tpl.get('file', '')}` sha256 {tpl.get('sha256', '')} ({tpl.get('source', '')})")
    recipe = versions.get("recipe")
    lines.append(
        f"- saved workflow: version {recipe['version']}, variant {recipe['variant']}" if recipe
        else "- saved workflow: none"
    )
    ids = versions.get("id_scheme")
    if ids:
        lines.append(f"- ID scheme: {_text(ids['scheme'])}; counter {ids['counter_from']} -> {ids['counter_to']}")
    lines.append(f"- check levels: {_text(versions.get('levels') or {})}")
    for skill, version in (versions.get("skills") or {}).items():
        lines.append(f"- skill: {skill}@{version}")
    changes = [e for e in entries if e["action"] in CONFIG_CHANGES]
    lines += ["", "## Configuration changes recorded in this job", ""]
    lines += [f"- {e['ts']} {e['action']}: {e.get('rationale', '')}" for e in changes] or ["- none"]
    lines.append("")
    return "\n".join(lines)
