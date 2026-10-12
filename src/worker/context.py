"""Per-run context shared by the tools and the agent."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from worker.audit import AuditLog
from worker.feedback import FeedbackStore
from worker.memory import SponsorMemory
from worker.recipes import RecipeStore
from worker.registry import Registry
from worker.staging import Stage


class BudgetExceeded(RuntimeError):
    """A model call refused because the job's spend cap is used up. `budget.exceeded` is audited before it is raised."""


UNLIMITED = "unlimited"  # the word that lifts a cap: a blank value does not (a blank is "not set")


def _cap(env: Mapping[str, str], name: str, default: int) -> int | None:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    if raw.lower() == UNLIMITED:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got {raw!r}") from None
    if value < 0:
        raise ValueError(f"{name} must not be negative, got {value}")
    return value


@dataclass
class Budget:
    """Per-job spend cap on model calls (a control); a cap left None is no cap on that count. A job started from
    the environment (`from_env`) is capped by default at `DEFAULT_CALLS` calls and `DEFAULT_TOKENS` tokens, so a
    runaway ends in a refused call and a `budget.exceeded` record. `WORKER_BUDGET_*` takes a number or `unlimited`.

    Calls are counted when a call is admitted and tokens (input + output, from `usage_metadata`) when it returns, so
    with parallel batches the token total can pass the cap by the calls already in flight; the next call is refused.
    """

    DEFAULT_CALLS: ClassVar[int] = 300
    DEFAULT_TOKENS: ClassVar[int] = 5_000_000
    max_tokens: int | None = None
    max_calls: int | None = None
    tokens: int = 0
    calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Budget:
        """`WORKER_BUDGET_TOKENS` and `WORKER_BUDGET_CALLS`: a whole number, `unlimited` to lift that cap, or
        blank / absent for the default (`DEFAULT_TOKENS`, `DEFAULT_CALLS`)."""
        return cls(max_tokens=_cap(env, "WORKER_BUDGET_TOKENS", cls.DEFAULT_TOKENS),
                   max_calls=_cap(env, "WORKER_BUDGET_CALLS", cls.DEFAULT_CALLS))

    @property
    def is_set(self) -> bool:
        return self.max_tokens is not None or self.max_calls is not None

    def admit(self) -> str | None:
        """Count one call and return None, or return why it is refused (nothing counted)."""
        with self._lock:
            if self.max_calls is not None and self.calls >= self.max_calls:
                return f"call cap reached: {self.calls} of {self.max_calls} model calls used"
            if self.max_tokens is not None and self.tokens >= self.max_tokens:
                return f"token cap reached: {self.tokens} of {self.max_tokens} tokens used"
            self.calls += 1
            return None

    def add_tokens(self, n: int) -> None:
        with self._lock:
            self.tokens += max(0, int(n))

    def snapshot(self) -> dict[str, int | None]:
        with self._lock:
            return {
                "calls": self.calls,
                "tokens": self.tokens,
                "max_calls": self.max_calls,
                "max_tokens": self.max_tokens,
            }


@dataclass(frozen=True)
class Limits:
    """Per-request limits the loop enforces (see worker.stops): model calls and wall-clock seconds for one human
    message. None is no limit; code that builds its own context gets none, a job started from the environment gets
    the defaults unless `WORKER_BUDGET_ITERATIONS` / `WORKER_BUDGET_SECONDS` is a number or `unlimited`."""

    DEFAULT_ITERATIONS: ClassVar[int] = 100
    DEFAULT_SECONDS: ClassVar[int] = 1800
    max_iterations: int | None = None
    max_seconds: float | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Limits:
        seconds = _cap(env, "WORKER_BUDGET_SECONDS", cls.DEFAULT_SECONDS)
        return cls(max_iterations=_cap(env, "WORKER_BUDGET_ITERATIONS", cls.DEFAULT_ITERATIONS),
                   max_seconds=None if seconds is None else float(seconds))


@dataclass
class RunContext:
    sponsor: str
    gp_id: str
    inbox_roots: list[Path]
    out_dir: Path
    audit: AuditLog
    stage: Stage
    memory: SponsorMemory | None = None
    recipes: RecipeStore | None = None  # saved workflows (see worker.recipes)
    instruction: str = ""  # the human's request that started the job, kept with a saved recipe
    feedback: FeedbackStore | None = None  # the sponsor's feedback log (see worker.feedback)
    job_feedback: list[dict[str, Any]] = field(default_factory=list)  # this job's corrections
    ranker: Callable[[list[Any]], Mapping[str, str]] | None = None  # optional model ranking for map-accounts
    accounts_root: Path | None = None  # `<state>/accounts`: approved account mappings per sponsor
    registry: Registry | None = None  # the sponsor's existing records (see worker.registry)
    id_root: Path | None = None  # `<state>/ids`: the per-sponsor ID counters (see worker.idgen)
    reservations: dict[str, Any] = field(default_factory=dict)  # stage id -> idgen.Reservation, this run only
    quality_reports: dict[str, dict[str, Any]] = field(default_factory=dict)  # file -> last check_data_quality
    restore: bool = False  # a run revived after a restart: tools load worker.jobstate before they start
    max_tool_calls: int = 80  # per human message; agent.start resets the counter
    tool_calls: int = 0
    tool_errors: int = 0
    budget: Budget = field(default_factory=Budget)  # per-job spend cap on model calls; unset = unlimited
    limits: Limits = field(default_factory=Limits)  # per-request model calls and seconds; unset = unlimited
    turn_iterations: int = 0  # model calls in this request (agent.start resets; a resume continues the count)
    turn_started: float = 0.0  # monotonic clock at the start of this invocation (a resume restarts it)
    trace_id: str = ""  # one per invocation (start or resume); on every model.call and tool record
    # The settings the job was started with (`.env` under the real environment, see cli.load_env). Roles built inside
    # a tool (the Layout Drafter, the Explainer) read their model from here; None reads os.environ.
    env: Mapping[str, str] | None = None
