"""Models: the role helper, the per-job spend cap, and the OpenRouter catalogue and probe.

- `model_for_role(role)` is the one way callers outside `worker.agent` get a chat model (roles: plan, exec,
  search, small; an unset `WORKER_MODEL_<ROLE>` falls back to `WORKER_MODEL`).
- `budgeted(model, budget, audit)` returns a per-job copy of the model that sums `usage_metadata` into the job's
  `Budget` and refuses the call after the cap, auditing `budget.exceeded` first.
- Listing free models is a public, unauthenticated call. A probe is one tiny real request, so it uses a little of
  the account's quota: it only runs when asked.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.outputs import LLMResult
from langchain_core.tools import tool

from worker.agent import OPENROUTER_URL, model_for
from worker.audit import AuditLog
from worker.context import Budget, BudgetExceeded


def model_for_role(role: str, env: Mapping[str, str] | None = None) -> BaseChatModel:
    """The chat model for `role` (plan | exec | search | small), built once per process; see `agent.model_for`."""
    return model_for(role, env)


def _usage_tokens(response: LLMResult) -> int:
    total = 0
    for gens in response.generations:
        for gen in gens:
            usage = getattr(getattr(gen, "message", None), "usage_metadata", None) or {}
            if usage:
                total += int(usage.get("total_tokens") or 0) or (
                    int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
                )
    if not total:
        usage = (response.llm_output or {}).get("token_usage") or {}
        total = int(usage.get("total_tokens") or 0)
    return total


class BudgetMeter(BaseCallbackHandler):
    """Callback that admits each model call against the job's budget and sums the tokens it used.

    `raise_error` makes the refusal stop the call before the request is sent. The refusal is audited first: if the
    audit record cannot be written, the AuditError propagates and the call does not happen either.
    """

    raise_error = True
    run_inline = True

    def __init__(self, budget: Budget, audit: AuditLog, role: str = "") -> None:
        self.budget, self.audit, self.role = budget, audit, role

    def on_chat_model_start(self, serialized: Any, messages: Any, **kwargs: Any) -> None:
        self._admit()

    def on_llm_start(self, serialized: Any, prompts: Any, **kwargs: Any) -> None:
        self._admit()

    def _admit(self) -> None:
        refused = self.budget.admit()
        if refused is None:
            return
        self.audit.record(
            "budget.exceeded",
            f"model call refused: {refused}",
            {"role": self.role or "default"},
            self.budget.snapshot(),
        )
        raise BudgetExceeded(f"spend cap for this job: {refused}; the call was refused and audited")

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        self.budget.add_tokens(_usage_tokens(response))


def budgeted(model: BaseChatModel, budget: Budget | None, audit: AuditLog, role: str = "") -> BaseChatModel:
    """The model counted against `budget` for one job. An unset budget returns `model` itself (today's behaviour).

    The result is a shallow copy of the same class carrying one more callback, so provider features (tool binding,
    model profile) are unchanged and the shared per-process client is reused; the original is never modified.
    """
    if budget is None or not budget.is_set:
        return model
    callbacks = model.callbacks
    if callbacks is None:
        kept: list[Any] = []
    elif isinstance(callbacks, list):
        kept = list(callbacks)
    else:  # a callback manager: keep its handlers
        kept = list(getattr(callbacks, "handlers", []) or [])
    return model.model_copy(update={"callbacks": [*kept, BudgetMeter(budget, audit, role)]})

LIST_URL = f"{OPENROUTER_URL}/models"
CACHE_SECONDS = 600
# Seen working with this project's tools (see STATUS.md); shown first, never assumed to still be up.
PREFERRED = (
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "poolside/laguna-s-2.1:free",
)
_cache: dict[str, Any] = {"at": 0.0, "data": []}


def _is_free(m: Mapping[str, Any]) -> bool:
    p = m.get("pricing") or {}
    return str(p.get("prompt")) == "0" and str(p.get("completion")) == "0"


def parse_models(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Free models from an OpenRouter /models payload, tool-capable first, preferred ones before the rest."""
    out: list[dict[str, Any]] = []
    for m in payload.get("data", []):
        if not _is_free(m):
            continue
        params = m.get("supported_parameters") or []
        inputs = (m.get("architecture") or {}).get("input_modalities") or []
        out.append(
            {
                "id": m["id"],
                "name": m.get("name", m["id"]),
                "context": m.get("context_length") or 0,
                "tools": "tools" in params,
                "vision": "image" in inputs,
                "router": m["id"].startswith("openrouter/"),
                "preferred": m["id"] in PREFERRED,
            }
        )

    def rank(m: dict[str, Any]) -> tuple[Any, ...]:
        pref = PREFERRED.index(m["id"]) if m["preferred"] else len(PREFERRED)
        return (not m["tools"], m["router"], pref, -m["context"], m["id"])

    return sorted(out, key=rank)


def free_models(
    fetch: Callable[[], Mapping[str, Any]] | None = None, *, refresh: bool = False
) -> list[dict[str, Any]]:
    """Cached free-model list. Raises httpx.HTTPError when OpenRouter cannot be reached and nothing is cached."""
    now = time.time()
    if not refresh and _cache["data"] and now - _cache["at"] < CACHE_SECONDS:
        return list(_cache["data"])

    def _get() -> Mapping[str, Any]:
        resp = httpx.get(LIST_URL, timeout=20)
        resp.raise_for_status()
        return resp.json()

    try:
        data = parse_models((fetch or _get)())
    except httpx.HTTPError:
        if _cache["data"]:
            return list(_cache["data"])
        raise
    _cache.update(at=now, data=data)
    return list(data)


@tool
def _ping(word: str) -> str:
    """Return the word unchanged."""
    return word


def probe(
    model_id: str,
    env: Mapping[str, str],
    model: BaseChatModel | None = None,
) -> dict[str, Any]:
    """Ask `model_id` to call a trivial tool once. {ok, seconds, error?}: ok means it emitted a tool call."""
    started = time.time()
    try:
        pinned = {"WORKER_MODEL": model_id, "WORKER_MODEL_EXEC": model_id,
                  "WORKER_MODEL_FALLBACKS": "", "WORKER_MODEL_FALLBACKS_EXEC": ""}
        llm = model or model_for_role("exec", {**env, **pinned})
        bound = llm.bind_tools([_ping])
        reply = bound.invoke([HumanMessage("Call the _ping tool with the word 'ok'. Do not answer in text.")])
        calls = getattr(reply, "tool_calls", None) or []
        ok = any(c.get("name") == "_ping" for c in calls)
        result: dict[str, Any] = {"ok": ok, "seconds": round(time.time() - started, 1)}
        if not ok:
            result["error"] = "the model answered without calling the tool"
        return result
    except Exception as exc:  # noqa: BLE001  a probe reports, it never raises
        return {"ok": False, "seconds": round(time.time() - started, 1), "error": probe_error(exc)}


def probe_error(exc: BaseException) -> str:
    """One line on why a probe failed. OpenRouter's 429 has two causes that need different actions: the account's
    free-model daily quota (wait for the reset) and a provider that is busy upstream (retry shortly or pick another
    model); a 403 can mean the model is restricted to listed apps."""
    text = f"{type(exc).__name__}: {exc}"
    low = text.lower()
    if "429" in text:
        if "per-day" in low or "per day" in low:
            return "rate limited (429): the account's free-model daily quota is used up; it resets daily"
        if "upstream" in low:
            return "rate limited (429) upstream: the provider is busy; retry shortly or pick another model"
        return "rate limited (429): " + text[:200]
    if "403" in text and "agentic harnesses" in low:
        return "refused (403): this free model is only offered to listed apps, not to an API key"
    return text[:300]
