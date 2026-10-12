"""The stages of a job (read -> map -> convert -> check -> write) and the two code-first checks that run before a
human sees a plan: `plan_problems` (what is wrong with a proposed plan) and `route_record_type` (which template the
headers match). The stages themselves run in `worker.engine`."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from worker.templates import TemplateSpec

STAGES = ("read", "map", "convert", "check", "write")


def plan_problems(
    plan: Mapping[str, Any], spec: TemplateSpec, allowed_constants: Sequence[str] = ("GP_ID",)
) -> list[str]:
    """The evaluator: what is wrong with a proposed plan before a human sees it.

    `plan` is {"columns": {field: column letter}, "constants": {field: value}}. Every required field must be
    covered, a constant may only be a field setup supplies, and no source column may feed two fields.
    """
    columns = dict(plan.get("columns") or {})
    constants = dict(plan.get("constants") or {})
    problems: list[str] = []
    unknown = [f for f in [*columns, *constants] if f not in spec.fields]
    if unknown:
        problems.append(f"fields not in the {spec.name} template: {sorted(unknown)}")
    missing = [f for f in spec.required if f not in columns and f not in constants]
    if missing:
        problems.append(f"required fields with no source or constant: {missing}")
    bad_const = [f for f in constants if f not in allowed_constants]
    if bad_const:
        problems.append(f"constants only come from setup ({list(allowed_constants)}): {bad_const}")
    seen: dict[str, str] = {}
    for fld, letter in columns.items():
        key = str(letter).upper()
        if key in seen:
            problems.append(f"column {key} feeds both {seen[key]} and {fld}")
        seen[key] = fld
    return problems


def route_record_type(
    headers: Sequence[str], templates: Mapping[str, TemplateSpec], threshold: float = 0.5
) -> tuple[str | None, dict[str, float]]:
    """Code-first routing: the record type whose field names best match the headers, or None when unsure.

    The score is the share of a template's required fields that some header matches (case and punctuation
    ignored). Below `threshold`, or when two types tie, the caller asks a small model or the human.
    """

    def norm(text: str) -> str:
        return re.sub(r"[^a-z0-9]", "", text.lower())

    have = {norm(h) for h in headers if str(h).strip()}
    scores: dict[str, float] = {}
    for name, spec in templates.items():
        wanted = [norm(f) for f in spec.required if f != "GP_ID"]
        scores[name] = sum(1 for w in wanted if w in have) / len(wanted) if wanted else 0.0
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    if not ranked or ranked[0][1] < threshold or (len(ranked) > 1 and ranked[1][1] == ranked[0][1]):
        return None, scores
    return ranked[0][0], scores

