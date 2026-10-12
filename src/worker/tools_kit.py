"""What the agent's tool modules share: the kit every tool family is given, the response cap, text helpers."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from worker import feedback
from worker.agentspec import job_name
from worker.audit import AuditError
from worker.context import RunContext
from worker.engine import Refused, evaluate
from worker.guard import PathRefused
from worker.jobstate import JobState

MAX_RESPONSE_CHARS = 6000


class UnknownFile(Exception):
    """A relative name that matches no file under the inbox roots."""


class UploadRefused(PathRefused):
    """The engine refused to write: it is returned to the model as REFUSED, after the refusal is audited."""


@dataclass(frozen=True)
class Kit:
    """The audited wrapper (`run`) and the run's state, handed by `build_tools` to each tool family."""

    ctx: RunContext
    run: Callable[..., str]
    resolve: Callable[[str], Path]
    summary: Callable[[str], dict[str, Any]]
    state: JobState
    sandbox: Any
    explainer: Any
    manifest_in_record: bool
    layout_roles: Any
    sandbox_box: dict[str, Any] = field(default_factory=dict)  # the container runner and the layout service


def derive_stage(k: Kit, base_sid: str, sid: str, kind: str, **rest: Any) -> None:
    """The new stage `sid` keeps the constants of `base_sid`; its spec, when the base has one, is `kind` over it."""
    k.state.constants[sid] = dict(k.state.constants.get(base_sid, {}))
    base = k.state.specs.get(base_sid)
    if base is not None:
        k.state.specs[sid] = {"kind": kind, "path": base["path"], "of": base, **rest}


def cap(text: str) -> str:
    if len(text) <= MAX_RESPONSE_CHARS:
        return text
    return text[: MAX_RESPONSE_CHARS - 45] + "\n...[truncated to fit the response cap]"


def dump(obj: Any) -> str:
    return cap(json.dumps(obj, ensure_ascii=False, default=str))


def norm_text(value: Any) -> str:
    return " ".join(str(value).split()).lower()


def evaluate_checked(spec: Any, ports: Any, **kwargs: Any) -> Any:
    """`evaluate`, with the engine's refusals as the tool layer's: a missing audit is AuditError, the rest UploadRefused."""
    try:
        return evaluate(spec, ports, **kwargs)
    except Refused as exc:
        if exc.code == "audit_unavailable":
            raise AuditError(exc.sentence) from exc
        raise UploadRefused(f"{exc.code}: {exc.sentence}") from exc


def record_feedback(ctx: RunContext, **fields: Any) -> dict[str, Any]:
    """Write one feedback record for this job (audit + the sponsor's log) and keep it for the replay case."""
    rec = feedback.record(
        ctx.audit, ctx.feedback, sponsor=ctx.sponsor, job=job_name(ctx),
        model=os.environ.get("WORKER_MODEL_EXEC") or os.environ.get("WORKER_MODEL") or None, **fields,
    )
    ctx.job_feedback.append({k: rec[k] for k in ("kind", "action", "reason", "scope", "skill", "table_row")})
    return rec
