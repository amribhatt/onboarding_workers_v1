"""Run manager behind the web app: one agent conversation per run, turns executed in worker threads.

The agent proposes; a human decides. A gate is bound to a hash of the exact proposal (tool name, arguments and,
for an upload, the digest of the staged rows). A decision that names a different hash is refused, so an approval
can never be applied to something the reviewer did not see.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.sqlite import SqliteSaver

from worker import persist, stops
from worker.agent import (
    GateRefused,
    build_agent,
    check_approval,
    decide,
    new_thread_id,
    pending_actions,
    pending_gate,
    start,
)
from worker.audit import AuditError
from worker.checks import stage_warnings
from worker.context import Budget, Limits, RunContext
from worker.contracts import ContractError, Option, Preview, Question, Subject, UnnamedApprover, approver_name
from worker.feedback import FeedbackError
from worker.fsutil import atomic_write_text
from worker.inbox import inbox_message
from worker.memory import rows_digest
from worker.review import review
from worker.skills import require_valid_skills
from worker.staging import Stage
from worker.templates import templates_for
from worker.transcript import IMAGE_TYPES, image_block, last_coverage, last_say

SPONSOR_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
GP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
FILE_RE = re.compile(r"^[^\\/:*?\"<>|\x00-\x1f]{1,150}$")
UPLOAD_EXTENSIONS = {".xlsx", ".xlsm", ".csv", ".json", ".png", ".jpg", ".jpeg"}
DOWNLOAD_EXTENSIONS = {".xlsx", ".csv", ".jsonl", ".md"}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ACTIVE = ("working", "needs_approval")
DRAIN_SECONDS = 45.0  # WORKER_DRAIN_SECONDS: how long a shutdown waits for work that was accepted


class ServiceError(Exception):
    """A request the service refuses; `status` is the HTTP code the web layer uses."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@contextmanager
def _refused(*, audit: bool = True) -> Iterator[None]:
    """A card store's refusals as the service's: not permitted 403, audit not written 503 (`audit`), refused 409."""
    try:
        yield
    except PermissionError as exc:
        raise ServiceError(str(exc), 403) from exc
    except AuditError as exc:
        if not audit:
            raise
        raise ServiceError(f"the audit record could not be written, so nothing changed: {exc}", 503) from exc
    except ValueError as exc:
        raise ServiceError(str(exc), 409) from exc


def proposal_hash(name: str, args: Mapping[str, Any], content_digest: str = "") -> str:
    body = json.dumps({"name": name, "args": args, "content": content_digest}, sort_keys=True, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ---- Questions: the typed view of a gate, batched by cause -----------------------------------------------
MAX_SUPPLY_OPTIONS = 4


def _qid(*parts: str) -> str:
    return "q-" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _cause_total(ctx: RunContext, path: str, field_name: str, rule_id: str, fallback: int) -> int:
    """Rows in the cause, counted by the quality check when it has run on this file, else the rows named."""
    name = Path(path).name
    for key, report in ctx.quality_reports.items():
        if Path(key).name != name:
            continue
        for cause in report.get("causes", []):
            if cause.get("field") == field_name and rule_id in (cause.get("rule"), cause.get("id")):
                return max(int(cause.get("count", 0)), fallback)
    return fallback


def _supply_question(ctx: RunContext, group: list[dict[str, Any]]) -> Question:
    first = group[0]["args"]
    path, fld = str(first.get("path", "")), str(first.get("field", ""))
    rule_id = str(first.get("rule_id") or "required_blank")
    named = {int(g["args"].get("row") or 0) for g in group if g["args"].get("row")}
    every = any(not g["args"].get("row") for g in group)
    total = _cause_total(ctx, path, fld, rule_id, len(named))
    if every and total == 0:
        total = max(len(group), 1)
    by_value: dict[str, set[int]] = {}
    whole: set[str] = set()
    for g in group:
        value, row = str(g["args"].get("value", "")), int(g["args"].get("row") or 0)
        by_value.setdefault(value, set())
        if row:
            by_value[value].add(row)
        else:
            whole.add(value)
    values = sorted(by_value, key=lambda v: (v not in whole, -len(by_value[v]), v))
    room = MAX_SUPPLY_OPTIONS if len(values) > MAX_SUPPLY_OPTIONS - 1 else MAX_SUPPLY_OPTIONS - 1
    options = [
        Option(
            option_id=f"supply-{i + 1}", action="supply", proposed_by="human",
            preview=Preview(conform=total if v in whole else min(len(by_value[v]), total), total=total),
            payload={"field": fld, "value": v, "rows": "all blank rows" if v in whole else sorted(by_value[v])},
            note="the value the person typed; never inferred",
        )
        for i, v in enumerate(values[:room])
    ]
    if len(options) < MAX_SUPPLY_OPTIONS:
        options.append(Option(option_id="declare-missing", action="declare_missing", proposed_by="human",
                              preview=Preview(conform=0, total=total), payload={"field": fld},
                              note="leave blank and report as MISSING"))
    return Question.build(
        question_id=_qid("supply_value", path, fld, rule_id), unit_id=Path(path).name or "file",
        kind="missing_report", subject=[Subject(kind="field", ref=fld)], options=options, blocking=("check",),
    )


def _account_question(ctx: RunContext, group: list[dict[str, Any]]) -> Question:
    stage_id = str(group[0]["args"].get("stage_id", ""))
    picks: dict[str, str] = {}
    for g in group:
        picks.update({str(k): str(v) for k, v in (g["args"].get("picks") or {}).items()})
    known: set[str] = set()
    try:
        info = ctx.stage.meta(stage_id).get("account_mapping") or {}
        known = {str(x["key"]) for x in info.get("keys", [])}
    except KeyError:
        pass
    conform = sum(1 for k in picks if k in known)
    total = len(picks)
    options = [
        Option(option_id="apply-picks", action="map", proposed_by="mapper",
               preview=Preview(conform=conform, total=total), payload={"picks": picks},
               note="store these accounts for the sponsor and apply them to the lines"),
        Option(option_id="skip", action="skip_unit", proposed_by="human",
               preview=Preview(conform=0, total=total), payload={}, note="reject; nothing is stored"),
    ]
    return Question.build(
        question_id=_qid("confirm_account_mappings", stage_id), unit_id=stage_id or "stage",
        kind="low_confidence", subject=[Subject(kind="artifact", ref=stage_id or "stage")],
        options=options, blocking=("map",),
    )


def _questions(ctx: RunContext, gates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One Question per cause, however many gate rows share it. Scopes are job and sponsor only. A gate whose
    question cannot be built simply has none: the approve and reject buttons never depend on it."""
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for g in gates:
        a = g.get("args") or {}
        if g["name"] == "supply_value":
            key = ("supply_value", str(a.get("path", "")), str(a.get("field", "")),
                   str(a.get("rule_id") or "required_blank"))
        elif g["name"] == "confirm_account_mappings":
            key = ("confirm_account_mappings", str(a.get("stage_id", "")))
        else:
            continue
        groups.setdefault(key, []).append(g)
    out: list[dict[str, Any]] = []
    for key, group in groups.items():
        try:
            q = (_supply_question if key[0] == "supply_value" else _account_question)(ctx, group)
        except (ContractError, KeyError, ValueError, TypeError):
            continue
        out.append({**q.to_dict(), "_gates": [g["hash"] for g in group]})
    return out


@dataclass
class Run:
    id: str
    sponsor: str
    gp_id: str
    file: str
    thread_id: str
    out_dir: Path
    created: float = field(default_factory=time.time)
    status: str = "working"
    messages: list[dict[str, Any]] = field(default_factory=list)
    gates: list[dict[str, Any]] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)  # typed Question views, one per cause
    coverage: str = ""
    error: str = ""
    stop: dict[str, Any] = field(default_factory=dict)  # why the last turn ended early (worker.stops), else {}
    ctx: RunContext | None = None
    agent: Any = None
    lock: threading.RLock = field(default_factory=threading.RLock)

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sponsor": self.sponsor,
            "gp_id": self.gp_id,
            "file": self.file,
            "status": self.status,
            "created": self.created,
            "messages": list(self.messages),
            "gates": list(self.gates),
            "questions": list(self.questions),
            "coverage": self.coverage,
            "error": self.error,
            "stop": dict(self.stop),
            "outputs": self.outputs(),
        }

    def outputs(self) -> list[dict[str, Any]]:
        if not self.out_dir.is_dir():
            return []
        found = [p for p in sorted(self.out_dir.glob("*_Upload*.*")) if p.suffix in (".xlsx", ".csv") and p.is_file()]
        found += [p for p in sorted(self.out_dir.glob("*_Rules.md")) + sorted(self.out_dir.glob("*_AuditTrail.csv"))]
        found += [p for p in sorted(self.out_dir.glob("*_marked*.*")) + sorted(self.out_dir.glob("*_just_rows*.*"))
                  if p.suffix in (".xlsx", ".csv")]
        audit = self.out_dir / "audit.jsonl"
        if audit.is_file():
            found.append(audit)
        return [
            {
                "name": p.name,
                "bytes": p.stat().st_size,
                "kind": _kind(p),
                "modified": p.stat().st_mtime,
            }
            for p in found
        ]

    def record(self) -> dict[str, Any]:
        d = self.view()
        d.update(thread_id=self.thread_id, out_dir=str(self.out_dir))
        d.pop("outputs")
        return d


class RunManager:
    def __init__(
        self,
        state_dir: Path,
        model_factory: Callable[[], BaseChatModel],
        *,
        approver_token: str = "",
        max_concurrent: int = 3,
        max_tool_calls: int = 80,
        admins: Iterable[str] = (),
        budget_env: Mapping[str, str] | None = None,
        smoke_ask: Callable[..., str] | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        persist.use_state_dir(self.state_dir)  # audit streams are named relative to it
        # a missing or refused skill file is said now, by name, not as a FileNotFoundError mid-job
        require_valid_skills(persist.audit(self.state_dir / "startup.audit.jsonl"))
        self.budget_env = budget_env  # where WORKER_BUDGET_TOKENS / WORKER_BUDGET_CALLS are read; None = the process
        # the controllers list (WORKER_ADMINS). Names are self-declared: demo-grade, not production authentication
        self.admins = sorted({a.strip() for a in admins if a.strip()})
        self.inbox = self.state_dir / "inbox"
        self.runs_dir = self.state_dir / "runs"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.model_factory = model_factory
        self.approver_token = approver_token
        self.max_tool_calls = max_tool_calls
        self._pool = ThreadPoolExecutor(max_workers=max_concurrent, thread_name_prefix="run")
        self._runs: dict[str, Run] = {}
        self._draining = threading.Event()
        self._inflight: set[Future[None]] = set()
        self._saver = self._make_saver()
        self._load_existing()
        self._start_checks()
        self._smoke_on_start(smoke_ask)
        self._resume_accepted()

    def _make_saver(self) -> Any:
        """Checkpoints: PostgreSQL when `DATABASE_URL` is set, else the SQLite file under the state folder."""
        from worker.db.backend import database_url

        url = database_url()
        if url and url.startswith("postgresql"):
            from worker.db.checkpoint import postgres_saver

            return postgres_saver(url)
        return SqliteSaver(sqlite3.connect(str(self.state_dir / "checkpoints.sqlite"), check_same_thread=False))

    def _start_checks(self) -> None:
        """Loud at start: a changed controllers list, and controls edited outside the system."""
        from worker.improver import start_checks

        start_checks(persist.audit(self.state_dir / "improvements" / "audit.jsonl"), self.admins)  # the harness's log

    def _smoke_on_start(self, ask: Callable[..., str] | None) -> None:
        """The model-change smoke test (worker.smoke): warn only, never blocks start-up or a job. An injected `ask`
        runs now; otherwise the live models are asked in the background, and only when WORKER_SMOKE_ON_START=1."""
        from worker import smoke

        env = self.budget_env if self.budget_env is not None else os.environ
        if ask is None:
            if (env.get(smoke.SMOKE_ON_START) or "").strip() != "1":
                return
            threading.Thread(target=self._run_smoke, args=(smoke.live_ask(env), env), daemon=True,
                             name="model-smoke").start()
            return
        self._run_smoke(ask, env)

    def _run_smoke(self, ask: Callable[..., str], env: Mapping[str, str]) -> None:
        from worker import smoke

        try:
            smoke.check_models(self.state_dir, ask, env=env)
        except Exception as exc:  # noqa: BLE001  a smoke test that cannot run is reported, never fatal
            smoke.record_error(self.state_dir, exc)

    def _context(self, sponsor: str, gp_id: str, out_dir: Path, restore: bool = False) -> RunContext:
        return RunContext(
            sponsor=sponsor,
            gp_id=gp_id,
            inbox_roots=[self.inbox],
            out_dir=out_dir,
            audit=persist.audit(out_dir / "audit.jsonl"),
            stage=Stage(out_dir, templates_for(sponsor)),
            id_root=self.state_dir / "ids",
            accounts_root=self.state_dir / "accounts",
            feedback=persist.feedback(self.state_dir / "feedback", sponsor),
            registry=persist.registry(self.state_dir, sponsor),
            recipes=persist.recipe_store(self.state_dir / "recipes", sponsor),
            memory=persist.memory(self.state_dir / "memory", sponsor),
            restore=restore,
            max_tool_calls=self.max_tool_calls,
            budget=self._budget(),
            limits=self._limits(),
            env=self.budget_env,
        )

    def _budget(self) -> Budget:
        """The per-job spend cap on model calls (`WORKER_BUDGET_TOKENS`, `WORKER_BUDGET_CALLS`); unset means the
        default (`Budget.DEFAULT_TOKENS`, `Budget.DEFAULT_CALLS`), `unlimited` lifts a cap. Each job gets its own
        counters. A value that is neither a whole number nor `unlimited` refuses the job loudly."""
        try:
            return Budget.from_env(self.budget_env if self.budget_env is not None else os.environ)
        except ValueError as exc:
            raise ServiceError(str(exc), 400) from exc

    def _limits(self) -> Limits:
        """Per-request model calls and seconds (`WORKER_BUDGET_ITERATIONS`, `WORKER_BUDGET_SECONDS`), same rules."""
        try:
            return Limits.from_env(self.budget_env if self.budget_env is not None else os.environ)
        except ValueError as exc:
            raise ServiceError(str(exc), 400) from exc

    # ---- inbox -----------------------------------------------------------------------------
    def _inbox_path(self, name: str) -> Path:
        if (
            not FILE_RE.match(name)
            or name.startswith(".")
            or Path(name).suffix.lower() not in UPLOAD_EXTENSIONS
        ):
            raise ServiceError(
                f"file name not allowed; use a plain name ending in {', '.join(sorted(UPLOAD_EXTENSIONS))}"
            )
        return self.inbox / name

    def save_upload(self, name: str, data: bytes) -> dict[str, Any]:
        path = self._inbox_path(name)
        if not data:
            raise ServiceError("the file is empty")
        if len(data) > MAX_UPLOAD_BYTES:
            raise ServiceError(f"file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB", 413)
        path.write_bytes(data)
        return {"name": name, "bytes": len(data)}

    def delete_upload(self, name: str) -> None:
        path = self._inbox_path(name)
        if not path.is_file():
            raise ServiceError("no such file", 404)
        path.unlink()

    # ---- runs ------------------------------------------------------------------------------
    def _load_existing(self) -> None:
        for rec in sorted(self.runs_dir.glob("*/run.json")):
            try:
                d = json.loads(rec.read_text(encoding="utf-8"))
                run = Run(
                    id=d["id"],
                    sponsor=d["sponsor"],
                    gp_id=d["gp_id"],
                    file=d["file"],
                    thread_id=d["thread_id"],
                    out_dir=Path(d["out_dir"]),
                    created=d.get("created", 0.0),
                    status=d.get("status", "idle"),
                    messages=d.get("messages", []),
                    coverage=d.get("coverage", ""),
                    gates=d.get("gates", []),
                    questions=d.get("questions", []),  # absent in a run saved before Questions existed
                    error=d.get("error", ""),
                    stop=d.get("stop") or {},
                )
            except (OSError, KeyError, ValueError):
                continue
            if run.status == "working":  # a turn was cut off; the saved tables and the thread are still there
                run.status = "interrupted"
                run.error = "The server restarted while the worker was busy. Send a message to continue."
            self._runs[run.id] = run

    def _revive(self, run: Run) -> None:
        """Bring a run back after a restart: staged tables from disk, the bookkeeping of its tools, its agent on the
        same checkpointed thread, and the gate that was waiting. Done when the run is next used, not at start-up."""
        ctx = self._context(run.sponsor, run.gp_id, run.out_dir, restore=True)
        ctx.stage.restore()
        agent = build_agent(ctx, self.model_factory(), self._saver)
        run.ctx, run.agent = ctx, agent
        if run.status == "needs_approval":
            run.gates = self._gate_views(run, pending_actions(agent, run.thread_id))
            if not run.gates:
                run.status = "idle"

    # ---- the approval journal: an accepted decision is on disk before the person is told it was accepted ----
    def _journal(self, run: Run, record: dict[str, Any]) -> Path:
        d = self.runs_dir / run.id / "approvals"
        n = len(list(d.glob("*.json"))) + 1 if d.is_dir() else 1
        path = d / f"{n:04d}.json"
        atomic_write_text(path, json.dumps({**record, "accepted_at": time.time(), "executed_at": None},
                                           default=str))
        return path

    @staticmethod
    def _executed(path: Path, outcome: str) -> None:
        doc = json.loads(path.read_text(encoding="utf-8"))
        atomic_write_text(path, json.dumps({**doc, "executed_at": time.time(), "outcome": outcome}))

    def _resume_accepted(self) -> None:
        """An approval that was accepted but not executed (the process went down between the two) is executed
        once now, exactly as it was decided. If its gate is no longer pending the person is told; nothing is
        guessed."""
        for run in list(self._runs.values()):
            d = self.runs_dir / run.id / "approvals"
            for path in sorted(d.glob("*.json")) if d.is_dir() else []:
                doc = json.loads(path.read_text(encoding="utf-8"))
                if doc.get("executed_at") is None:
                    self._resume_one(run, path, doc)
                    break  # one at a time; a later one follows when this turn ends and the next start finds it

    def _resume_one(self, run: Run, path: Path, doc: dict[str, Any]) -> None:
        with run.lock:
            try:
                self._require_live(run)
                pending = pending_actions(run.agent, run.thread_id) if run.agent is not None else []
            except Exception as exc:  # noqa: BLE001  reported on the run, never fatal to start-up
                pending, run.error = [], f"an accepted approval could not be resumed: {_friendly(exc)}"
            if not pending:
                self._executed(path, "no gate was pending at restart")
                run.status = "error" if run.error else "idle"
                run.messages.append({"role": "system", "ts": time.time(), "text": (
                    "The server restarted after your decision was accepted, but nothing was waiting for it. "
                    "Check the audit trail before deciding again.")})
                self._persist(run)
                return
            ctx, agent = run.ctx, run.agent
            assert ctx is not None
            run.messages.append({"role": "system", "ts": time.time(), "text": (
                "The server restarted after your decision was accepted; it is being carried out now.")})
            self._submit(run, partial(decide, agent, ctx, run.thread_id, doc["decision"], doc["why"], doc["meta"],
                                      doc.get("acknowledged")), journal=path)

    def pending_approvals(self, older_than: float = 0.0) -> int:
        """Decisions accepted (at least `older_than` seconds ago) and not yet executed."""
        n, cutoff = 0, time.time() - older_than
        for p in self.runs_dir.glob("*/approvals/*.json"):
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
                n += doc.get("executed_at") is None and float(doc.get("accepted_at", 0)) <= cutoff
            except (OSError, ValueError):
                n += 1  # an unreadable journal entry is not "done"
        return n

    def counts(self) -> dict[str, Any]:
        """What the metrics endpoint reports: runs by status and the gates waiting."""
        by_status: dict[str, int] = {}
        gates = 0
        for run in list(self._runs.values()):
            by_status[run.status] = by_status.get(run.status, 0) + 1
            gates += len(run.gates)
        return {"runs": by_status, "gates_waiting": gates}

    def _require_open(self) -> None:
        if self._draining.is_set():
            raise ServiceError("the service is shutting down; try again in a moment", 503)

    @property
    def draining(self) -> bool:
        return self._draining.is_set()

    def _persist(self, run: Run) -> None:
        atomic_write_text(self.runs_dir / run.id / "run.json", json.dumps(run.record(), indent=2, default=str))

    def _get(self, run_id: str) -> Run:
        run = self._runs.get(run_id)
        if run is None:
            raise ServiceError("no such run", 404)
        return run

    def list_runs(self) -> list[dict[str, Any]]:
        rows = sorted(self._runs.values(), key=lambda r: r.created, reverse=True)
        return [
            {"id": r.id, "sponsor": r.sponsor, "file": r.file, "status": r.status, "created": r.created}
            for r in rows
        ]

    def downloads(self) -> list[dict[str, Any]]:
        """Every file a reviewer can download, newest first, across all runs."""
        out: list[dict[str, Any]] = []
        for r in self._runs.values():
            for o in r.outputs():
                out.append(
                    {**o, "run_id": r.id, "sponsor": r.sponsor, "gp_id": r.gp_id, "source": r.file,
                     "url": f"/api/runs/{r.id}/files/{o['name']}"}
                )
        return sorted(out, key=lambda o: o["modified"], reverse=True)

    def get(self, run_id: str) -> dict[str, Any]:
        from worker.smoke import warnings as model_warnings

        run = self._get(run_id)
        with run.lock:
            return {**run.view(), "model_warnings": model_warnings(self.state_dir)}  # information only

    def create_run(self, file: str, sponsor: str, gp_id: str, request: str = "") -> dict[str, Any]:
        self._require_open()
        if not SPONSOR_RE.match(sponsor):
            raise ServiceError("sponsor must be lowercase letters, digits, '-' or '_' (max 40)")
        if not GP_RE.match(gp_id):
            raise ServiceError("GP_ID must be letters, digits, '-', '_' or '.' (max 40)")
        path = self._inbox_path(file)
        if not path.is_file():
            raise ServiceError(f"{file!r} is not in the inbox; upload it first", 404)
        run_id = uuid.uuid4().hex[:12]
        out_dir = self.runs_dir / run_id / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        ctx = self._context(sponsor, gp_id, out_dir)
        try:
            agent = build_agent(ctx, self.model_factory(), self._saver)
        except Exception as exc:
            raise ServiceError(f"model is not configured: {exc}", 503) from exc
        run = Run(run_id, sponsor, gp_id, file, new_thread_id(), out_dir, ctx=ctx, agent=agent)
        self._runs[run_id] = run
        text = (request.strip() or "Build me the Ledgerline sheet") + f" from {file}."
        message = f"{text} Sponsor {sponsor}, GP_ID {gp_id}.\n" + inbox_message([self.inbox])
        attachments = [image_block(path)] if path.suffix.lower() in IMAGE_TYPES else None
        run.messages.append({"role": "human", "text": text, "ts": time.time()})
        self._submit(run, lambda: start(agent, ctx, message, run.thread_id, attachments))
        return run.view()

    def send(self, run_id: str, text: str) -> dict[str, Any]:
        self._require_open()
        run = self._get(run_id)
        with run.lock:
            self._require_live(run)
            if run.status in ACTIVE:
                raise ServiceError(f"the run is {run.status.replace('_', ' ')}; wait for it first", 409)
            if not text.strip():
                raise ServiceError("message is empty")
            run.messages.append({"role": "human", "text": text.strip(), "ts": time.time()})
            ctx, agent = run.ctx, run.agent
            assert ctx is not None
            self._submit(run, lambda: start(agent, ctx, text.strip(), run.thread_id))
            return run.view()

    def decide(
        self,
        run_id: str,
        gate_hash: str,
        verdict: str,
        feedback: str = "",
        reviewer: str = "local",
        acknowledged: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self._require_open()
        run = self._get(run_id)
        try:  # who decides is on the record: a blank, a reserved or (with a token) the demo name is refused first
            reviewer = approver_name(reviewer, demo=not self.approver_token)
        except UnnamedApprover as exc:
            raise ServiceError(str(exc), 403) from exc
        with run.lock:
            self._require_live(run)
            if run.status != "needs_approval" or not run.gates:
                raise ServiceError("nothing is waiting for a decision", 409)
            if verdict not in ("approve", "reject"):
                raise ServiceError("decision must be 'approve' or 'reject'")
            current = {g["hash"] for g in run.gates}
            if gate_hash not in current:
                raise ServiceError(
                    "this proposal changed since you looked at it; reload and review it again", 409
                )
            ctx0 = run.ctx
            assert ctx0 is not None
            if verdict == "approve":
                try:
                    check_approval(ctx0, [{"name": g["name"], "args": g["args"]} for g in run.gates],
                                   acknowledged)
                except GateRefused as exc:
                    stops.handle(ctx0.audit, stops.Stop.GUARDRAIL, str(exc)[:200], refused="approval",
                                 open_err=exc.review["open_err"], unacknowledged_warn=exc.review["unacknowledged_warn"])
                    raise ServiceError(str(exc), 409) from exc
            decision: dict[str, Any] = {"type": verdict}
            if verdict == "reject":
                decision["message"] = feedback.strip() or "Rejected by the human without comment."
            why = f"{reviewer}: {feedback.strip() or verdict}"
            meta = {"proposal_hash": gate_hash, "reviewer": reviewer}
            ctx, agent = run.ctx, run.agent
            assert ctx is not None
            run.messages.append(
                {"role": "human", "text": f"[{verdict}] {feedback.strip()}".strip(), "ts": time.time()}
            )
            try:  # accepted means on disk: before the person is told, and before anything is executed
                journal = self._journal(run, {"gate_hash": gate_hash, "verdict": verdict, "decision": decision,
                                              "why": why, "meta": meta, "acknowledged": acknowledged,
                                              "reviewer": reviewer})
            except OSError as exc:
                raise ServiceError(f"your decision could not be recorded, so nothing was done: {exc}", 500) from exc
            run.gates = []
            run.questions = []
            self._submit(
                run, lambda: decide(agent, ctx, run.thread_id, decision, why, meta, acknowledged), journal=journal
            )
            return run.view()

    def _require_live(self, run: Run) -> None:
        if run.agent is None or run.ctx is None:
            try:
                self._revive(run)
            except Exception as exc:
                raise ServiceError(
                    f"this run was loaded from disk and could not be restored ({exc}); start a new run "
                    "from the same file", 409
                ) from exc

    def _submit(self, run: Run, fn: Callable[[], dict[str, Any]], journal: Path | None = None) -> None:
        run.status = "working"
        run.error = ""
        run.stop = {}
        self._persist(run)
        future = self._pool.submit(self._turn, run, fn, journal)
        self._inflight.add(future)
        future.add_done_callback(self._inflight.discard)

    def _turn(self, run: Run, fn: Callable[[], dict[str, Any]], journal: Path | None = None) -> None:
        outcome = "done"
        try:
            result = fn()
            with run.lock:
                said = last_say(result)
                if said:
                    run.messages.append({"role": "agent", "text": said, "ts": time.time()})
                cov = last_coverage(result)
                if cov:
                    run.coverage = cov
                run.gates = self._gate_views(run, pending_gate(result))
                run.stop = result.get("stop") or {}  # a defined early stop: kept, audited, and the thread resumes
                run.status = "needs_approval" if run.gates else "error" if run.stop.get("needs_human") else "idle"
                run.error = run.stop["message"] if run.stop.get("needs_human") else ""
        except Exception as exc:  # noqa: BLE001  surfaced to the person, never swallowed
            outcome = f"error: {_friendly(exc)}"
            with run.lock:
                run.status = "error"
                run.error = _friendly(exc)
                run.messages.append({"role": "system", "text": run.error, "ts": time.time()})
        finally:
            with run.lock:
                self._persist(run)
                if journal is not None:
                    self._executed(journal, outcome)

    def _gate_views(self, run: Run, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        views: list[dict[str, Any]] = []
        ctx = run.ctx
        assert ctx is not None
        for action in actions:
            name, args = action.get("name", "?"), dict(action.get("args") or {})
            view: dict[str, Any] = {"name": name, "args": args}
            digest = ""
            if name == "write_upload" and args.get("stage_id"):
                try:
                    digest = rows_digest(ctx.stage.rows(args["stage_id"]))
                    view["stage"] = ctx.stage.summary(args["stage_id"])
                    view["warnings"] = stage_warnings(
                        view["stage"]["template"], ctx.stage.rows(args["stage_id"]), run.gp_id, ctx.stage.templates
                    )
                    view["review"] = review(
                        ctx, args["stage_id"], bool(args.get("drop_duplicates")), args.get("duplicate_key")
                    )
                except KeyError:
                    view["stage"] = None
            view["hash"] = proposal_hash(name, args, digest)
            views.append(view)
        run.questions = _questions(ctx, views)
        qid_by_gate = {h: q["question_id"] for q in run.questions for h in q.get("_gates", [])}
        for q in run.questions:
            q.pop("_gates", None)
        for view in views:
            if view["hash"] in qid_by_gate:
                view["question_id"] = qid_by_gate[view["hash"]]
        return views

    # ---- read side -------------------------------------------------------------------------
    def audit(self, run_id: str, after: int = 0) -> dict[str, Any]:
        run = self._get(run_id)
        log = persist.audit(run.out_dir / "audit.jsonl")
        entries = [
            {
                "seq": e["seq"],
                "ts": e["ts"],
                "action": e["action"],
                "why": e.get("rationale", ""),
                "inputs": e.get("inputs", {}),
                "outputs": e.get("outputs", {}),
            }
            for e in log.read()
            if e["seq"] > after
        ]
        ok, n = log.verify()
        return {"entries": entries, "verified": ok, "count": n}

    def output_path(self, run_id: str, name: str) -> Path:
        run = self._get(run_id)
        if not FILE_RE.match(name) or Path(name).suffix.lower() not in DOWNLOAD_EXTENSIONS:
            raise ServiceError("no such file", 404)
        path = run.out_dir / name
        if not path.is_file():
            raise ServiceError("no such file", 404)
        return path

    def feedback(self, run_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Record one piece of human feedback on this job (a flag, an override, a note, an upload outcome)."""
        from worker import feedback as fb

        run = self._get(run_id)
        ctx = run.ctx
        try:
            rec = fb.record(
                persist.audit(run.out_dir / "audit.jsonl"),
                persist.feedback(self.state_dir / "feedback", run.sponsor),
                sponsor=run.sponsor,
                job=run_id,
                **{k: v for k, v in fields.items() if k not in ("sponsor", "job")},
            )
        except FeedbackError as exc:
            raise ServiceError(str(exc)) from exc
        if ctx is not None:
            ctx.job_feedback.append({k: rec[k] for k in ("kind", "action", "reason", "scope", "skill", "table_row")})
        return {"recorded": True, "target": fb.route(rec)["target"], "scope": rec["scope"]}

    def health(self, sponsor: str | None = None, skill: str | None = None) -> dict[str, Any]:
        """Harness metrics in business units for the Health view, with the last promoted change marked."""
        from worker.metrics import ChangeLog, harness_metrics
        from worker.smoke import status as smoke_status

        report = harness_metrics(self.runs_dir)
        if sponsor:
            report["groups"] = {k: v for k, v in report["groups"].items() if k.split("/")[0] == sponsor}
        if skill:
            report["by_skill"] = {k: v for k, v in report["by_skill"].items() if k == skill}
            report["skill_usage"] = {k: v for k, v in report["skill_usage"].items() if k == skill}
        report["last_promoted_change"] = ChangeLog(self.state_dir / "changes.jsonl").last_promoted()
        report["model_smoke"] = smoke_status(self.state_dir)  # warn only (worker.smoke)
        return report

    # ---- improvements: console edits go through the same replay gates as the improver's own proposals --------
    def _improvement_parts(self) -> tuple[Any, Any, Any]:
        from worker.accounts import Coa, TypeMap
        from worker.improver import AccountReplay
        from worker.skills import load_skills
        from worker.templates import standards_dir

        base = standards_dir()
        skill = load_skills()["map-accounts"]
        replay = AccountReplay(Coa.load(base / "Golden_COA.csv"), TypeMap.load(base / "Transaction_Type_Mapping.csv"),
                               skill.table("coa-cascade"))
        store = persist.improvements(self.state_dir / "improvements", persist.audit(self.state_dir / "improvements" / "audit.jsonl"))
        return replay, store, persist.golden(self.state_dir / "accounts")

    def improvements(self) -> dict[str, Any]:
        _replay, store, golden = self._improvement_parts()
        return {"golden_version": golden.version, "cards": store.cards()}

    def propose_improvement(self, key: str, account: str, scope: str, proposer: str) -> dict[str, Any]:
        from worker.improver import build_cases, info_from_key, propose_golden_mapping

        if scope not in ("sponsor", "organisation"):
            raise ServiceError("scope must be sponsor or organisation")
        replay, store, golden = self._improvement_parts()
        if account not in replay.coa:
            raise ServiceError(f"{account!r} is not in the Golden chart of accounts")
        try:
            info_from_key(key)
        except ValueError as exc:
            raise ServiceError(str(exc)) from exc
        entries = {k: v for k, v in persist.account_stores(self.state_dir / "accounts").items() if k != "_golden"}
        keys = {k: info_from_key(k) for e in entries.values() for k in e}
        cases = build_cases(entries, keys)
        cause = {"key": key, "chosen": account, "count": 1, "sponsors": [], "shown": None}
        proposal = propose_golden_mapping(cause, proposed_by=proposer or "console", scope=scope)
        proposal["summary"] = f"{proposer or 'console'} proposes {key} -> {account}"
        card = store.file(proposal, cases, replay, golden)
        return card

    def propose_control(self, controls: Mapping[str, Any], proposer: str, summary: str = "",
                        evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """File a change to the reconcile-gl controls. It waits for another controller; nothing changes now."""
        _replay, store, _golden = self._improvement_parts()
        proposal = {"kind": "control", "target": "skills/reconcile-gl/controls.yaml",
                    "change": {"controls": dict(controls)}, "proposed_by": proposer,
                    "evidence": dict(evidence or {}), "summary": summary or f"{proposer} proposes a controls change"}
        try:
            return store.file_control(proposal)
        except PermissionError as exc:
            raise ServiceError(str(exc), 403) from exc
        except (TypeError, ValueError) as exc:
            raise ServiceError(str(exc), 400) from exc

    def decide_improvement(self, card_id: str, verdict: str, reason: str, approver: str, admin: bool,
                           versions: Any = None, seen_sha256: str = "") -> dict[str, Any]:
        """Approve or reject a card. A card that changes a control goes to `approve_control`: the approver must be
        in the controllers list and must not be the proposer; `promote` refuses it."""
        _replay, store, golden = self._improvement_parts()
        try:
            card = store.load(card_id)
        except (OSError, ValueError) as exc:
            raise ServiceError("no such improvement", 404) from exc
        with _refused():
            if verdict == "reject":
                if not reason.strip():
                    raise ServiceError("a reason is required")
                return store.reject(card["id"], approver, reason)  # audited before the card changes
            if verdict != "approve":
                raise ServiceError("verdict must be approve or reject")
            if card["status"] == "awaiting_controller":
                return store.approve_control(card_id, approver, controllers=self.admins, reason=reason,
                                             versions=versions, seen_sha256=seen_sha256)
            return store.promote(card_id, approver, versions or golden, admin=admin, reason=reason)

    def rollback_improvement(self, approver: str, reason: str) -> dict[str, Any]:
        _replay, store, golden = self._improvement_parts()
        with _refused(audit=False):
            return store.rollback(golden, approver, reason, kinds=("golden_mapping",))

    def rollback_control(self, who: str, reason: str) -> dict[str, Any]:
        """Any controller reverses the latest control change (pin checked, audit first)."""
        _replay, store, _golden = self._improvement_parts()
        with _refused():
            return store.rollback_control(who, reason, controllers=self.admins)

    def sponsor_feedback(self, sponsor: str, skill: str | None = None) -> dict[str, Any]:
        if not SPONSOR_RE.match(sponsor):
            raise ServiceError("bad sponsor name")
        items = persist.feedback(self.state_dir / "feedback", sponsor).list(skill=skill)
        return {"sponsor": sponsor, "count": len(items), "items": items[-200:]}

    def memory(self, sponsor: str) -> dict[str, Any]:
        if not SPONSOR_RE.match(sponsor):
            raise ServiceError("bad sponsor name")
        mem = persist.memory(self.state_dir / "memory", sponsor)
        return {
            "sponsor": sponsor,
            "rules": mem.rules_text(),
            "pending": mem.pending(),
            "cases": [
                {k: c.get(k) for k in ("case_id", "template", "created_at", "rows")} for c in mem.cases()
            ],
        }

    def close(self, timeout: float | None = None) -> None:
        """Stop taking work, let what was accepted finish (up to `timeout`; WORKER_DRAIN_SECONDS, 45 by default),
        then stop. A decision that is still queued after that is on the journal and runs once at the next start."""
        self._draining.set()
        wait = timeout if timeout is not None else float(os.environ.get("WORKER_DRAIN_SECONDS") or DRAIN_SECONDS)
        wait_futures(set(self._inflight), timeout=wait)
        self._pool.shutdown(wait=False, cancel_futures=True)
        pool = getattr(self._saver, "conn", None)
        if pool is not None and type(pool).__name__ == "ConnectionPool":  # the PostgreSQL saver's own pool
            pool.close()


def _kind(path: Path) -> str:
    if path.suffix == ".jsonl":
        return "audit"
    if path.name.endswith("_Rules.md"):
        return "rules"
    if path.name.endswith("_AuditTrail.csv"):
        return "trail"
    if "_marked" in path.name or "_just_rows" in path.name:
        return "quality"
    return "upload"


def _friendly(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    low = text.lower()
    if "429" in text or "rate limit" in low or "free-models-per-day" in low:
        return (
            "The model provider rate-limited this request (free models have a daily cap). Wait, add "
            "credits, or choose another model in Settings, then send a message to continue."
        )
    if "401" in text or "invalid api key" in low or "no auth" in low:
        return "The model provider rejected the API key. Check it in Settings."
    if "recursion" in low:
        return "The agent took too many steps without finishing. Send a message to tell it how to proceed."
    return text[:600]
