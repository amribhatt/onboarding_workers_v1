"""Model selection, agent assembly and the human-gate helpers."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from deepagents import create_deep_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.types import Command

from worker import feedback, persist, stops
from worker.context import RunContext
from worker.contracts import approver_name
from worker.idgen import decision_refusal
from worker.prompts import CELL_TEXT_TOOLS, as_data, system_prompt
from worker.review import review
from worker.skills import catalog, require_valid_skills
from worker.templates import TEMPLATES
from worker.tools import build_tools

DEFAULT_MODEL = "gpt-5.6-terra"
OPENROUTER_URL = "https://openrouter.ai/api/v1"
OLLAMA_URL = "http://localhost:11434/v1"
GATED_TOOLS = (
    "write_upload",
    "propose_rule",
    "set_id_scheme",
    "import_existing_records",
    "add_existing_record",
    "confirm_account_mappings",
    "supply_value",
    "approve_layout_program",
)


RECURSION_LIMIT = 400  # graph steps; the per-message tool-call budget (RunContext) trips well before this


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name) or default)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got {env.get(name)!r}") from None


ROLES = ("plan", "exec", "search", "small")
_models: dict[tuple[Any, ...], BaseChatModel] = {}


def role_setting(env: Mapping[str, str], role: str, base: str = "WORKER_MODEL") -> str:
    """`WORKER_MODEL_<ROLE>` (or `WORKER_MODEL_FALLBACKS_<ROLE>`) when set, else the plain setting."""
    if role and role not in ROLES:
        raise ValueError(f"unknown model role {role!r}; roles: {ROLES}")
    key = f"{base}_{role.upper()}" if role else base
    return env.get(key) or env.get(base) or ""


def model_for(role: str, env: Mapping[str, str] | None = None) -> BaseChatModel:
    """The model for a role, created once per process and reused (clients are never rebuilt per call)."""
    env = os.environ if env is None else env
    key = (role, role_setting(env, role), role_setting(env, role, "WORKER_MODEL_FALLBACKS"),
           env.get("WORKER_MODEL_PROVIDER", ""))
    if key not in _models:
        _models[key] = make_model(env, role)
    return _models[key]


def _timeouts(env: Mapping[str, str]) -> dict[str, Any]:
    return {"timeout": _int(env, "WORKER_TIMEOUT", 90), "max_retries": _int(env, "WORKER_MAX_RETRIES", 4)}


def _openai(env: Mapping[str, str], name: str, role: str) -> BaseChatModel:
    return ChatOpenAI(model=name, api_key=env.get("OPENAI_API_KEY") or None, **_timeouts(env))


def _openrouter(env: Mapping[str, str], name: str, role: str) -> BaseChatModel:
    extra: dict[str, Any] = {}
    fallbacks = [m.strip() for m in role_setting(env, role, "WORKER_MODEL_FALLBACKS").split(",") if m.strip()]
    if fallbacks:  # OpenRouter's own routing: try `model`, then each fallback on error / rate limit
        extra["extra_body"] = {"models": [name, *fallbacks]}
    return ChatOpenAI(model=name, base_url=OPENROUTER_URL, api_key=env.get("OPENROUTER_API_KEY") or None,
                      **_timeouts(env), **extra)


def _ollama(env: Mapping[str, str], name: str, role: str) -> BaseChatModel:
    """A local model through Ollama's OpenAI-compatible endpoint: no key, no provider quota."""
    return ChatOpenAI(model=name, base_url=env.get("OLLAMA_BASE_URL") or OLLAMA_URL, api_key="ollama", **_timeouts(env))


def _compatible(env: Mapping[str, str], name: str, role: str) -> BaseChatModel:
    """Any service that speaks the OpenAI chat API (LM Studio, vLLM, LiteLLM, a company gateway)."""
    url = env.get("WORKER_BASE_URL", "")
    if not url:
        raise ValueError("provider 'compatible' needs WORKER_BASE_URL, the service's OpenAI-style /v1 address")
    return ChatOpenAI(model=name, base_url=url, api_key=env.get("WORKER_API_KEY") or "none", **_timeouts(env))


def _vertex(env: Mapping[str, str], name: str, role: str) -> BaseChatModel:
    """Google Cloud (Vertex AI) with a service-account JSON file. Only the file's path is configured; its contents
    are read by Google's client and never copied, logged or stored by this app."""
    from pathlib import Path

    path = env.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not path or not Path(path).is_file():
        raise ValueError("provider 'vertex' needs GOOGLE_APPLICATION_CREDENTIALS: the path of the service-account "
                         "JSON file")
    from google.oauth2 import service_account
    from langchain_google_genai import ChatGoogleGenerativeAI

    creds = service_account.Credentials.from_service_account_file(
        path, scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return ChatGoogleGenerativeAI(model=name, vertexai=True, credentials=creds,
                                  project=env.get("GCP_PROJECT") or creds.project_id,
                                  location=env.get("GCP_LOCATION") or "us-central1", **_timeouts(env))


def _demo(env: Mapping[str, str], name: str, role: str) -> BaseChatModel:
    from worker.demo import DemoModel  # offline, scripted for the bundled sample

    return DemoModel()


@dataclass(frozen=True)
class Provider:
    build: Callable[[Mapping[str, str], str, str], BaseChatModel]
    key_env: str = ""  # the env name of the API key; "" = none needed


# Model-agnostic: the app knows a provider only by this table. A new model service is one entry here (and its key
# name); the settings page, the doctor check and the model roles all read the table.
PROVIDERS: dict[str, Provider] = {
    "openai": Provider(_openai, "OPENAI_API_KEY"),
    "openrouter": Provider(_openrouter, "OPENROUTER_API_KEY"),
    "ollama": Provider(_ollama),
    "compatible": Provider(_compatible, "WORKER_API_KEY"),
    "vertex": Provider(_vertex),
    "demo": Provider(_demo),
}


def make_model(env: Mapping[str, str] | None = None, role: str = "") -> BaseChatModel:
    """Chat model from env: WORKER_MODEL_PROVIDER (a name in `PROVIDERS`), WORKER_MODEL, the provider's key or file,
    WORKER_MODEL_FALLBACKS (comma list, OpenRouter only), WORKER_TIMEOUT (s), WORKER_MAX_RETRIES.

    `role` (plan | exec | search | small) reads `WORKER_MODEL_<ROLE>` and `WORKER_MODEL_FALLBACKS_<ROLE>` first;
    a role left unset uses the plain setting.
    """
    env = os.environ if env is None else env
    provider = (env.get("WORKER_MODEL_PROVIDER") or "openai").lower()
    got = PROVIDERS.get(provider)
    if got is None:
        raise ValueError(f"unknown WORKER_MODEL_PROVIDER {provider!r}; use one of: {', '.join(PROVIDERS)}")
    return got.build(env, role_setting(env, role) or DEFAULT_MODEL, role)


def _skills_for_job(ctx: RunContext) -> str:
    """The catalog of the skills this job runs with. A refused pin is audited as `skill.load_refused` before
    anything else and raises `SkillLoadRefused`: the job does not start (no audit log refuses too)."""
    return catalog(list(require_valid_skills(ctx.audit).values()))


def _frame(request: Any, result: Any) -> Any:
    name = (getattr(request, "tool_call", None) or {}).get("name", "")
    if name not in CELL_TEXT_TOOLS or not isinstance(result, ToolMessage) or not isinstance(result.content, str):
        return result
    if result.content.startswith(("ERROR", "REFUSED")):  # the code's own message, not file text
        return result
    return result.model_copy(update={"content": as_data(name, result.content)})


class UntrustedDataMiddleware(AgentMiddleware):
    """Wraps the results of tools that carry cell or file text in a labelled data block (see prompts.as_data)."""

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return _frame(request, handler(request))

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        return _frame(request, await handler(request))


def _explainer_for(ctx: RunContext, explainer: Any, env: Mapping[str, str]) -> Any:
    """The Explainer fan-out of `run_job` (migration M21): the runner passed in, else one over this run's audit and
    spend cap when `WORKER_EXPLAINER=1`, else none (no model call is added to a run)."""
    if explainer is not None:
        return explainer
    if (env.get("WORKER_EXPLAINER") or "").strip().lower() not in ("1", "true", "yes"):
        return None
    from worker.roles import RoleRunner  # lazy: roles imports the pipeline and the model stack

    return RoleRunner(audit=ctx.audit, budget=ctx.budget, env=env)


def _without_general_purpose_subagent(model: BaseChatModel) -> None:
    """Turn off Deep Agents' default general-purpose subagent (the `task` tool) for this model.

    Decision (user, 2026-10-11): the pipeline is run by code and the typed roles do the delegated work, so the
    generalist subagent, which would inherit every parent tool, stays off unless a genuine need appears. The switch
    is a harness profile keyed by the model's provider (and identifier when it reports one); registering merges
    with a built-in profile and only sets this one flag. A registered profile is process-global, so to allow the
    subagent set WORKER_ALLOW_GENERAL_SUBAGENT=1 BEFORE the first agent is built in the process.
    """
    if (os.environ.get("WORKER_ALLOW_GENERAL_SUBAGENT") or "").strip().lower() in ("1", "true", "yes"):
        return
    from deepagents import GeneralPurposeSubagentProfile, HarnessProfile, register_harness_profile
    from deepagents._models import get_model_identifier, get_model_provider

    provider, identifier = get_model_provider(model), get_model_identifier(model)
    keys = {k for k in ((f"{provider}:{identifier}" if provider and identifier else None), provider) if k}
    for key in keys:
        register_harness_profile(key, HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)))


def build_agent(ctx: RunContext, model: BaseChatModel, checkpointer: Any, *, explainer: Any = None) -> Any:
    """The Deep Agent. `write_upload` and `propose_rule` pause for approve / edit / reject; a checkpointer is required.

    When the job has a spend cap (`ctx.budget`), the model is counted against it (see worker.models.budgeted).
    `explainer` (a `roles.RoleRunner`) or `WORKER_EXPLAINER=1` lets `run_job` explain each non-PASS cause.
    """
    if checkpointer is None:
        raise ValueError("a checkpointer is required: gates pause and resume through it")
    from worker.models import budgeted  # worker.models imports this module

    skills_text = _skills_for_job(ctx)  # first: a refused skill pin stops the job before anything is built
    counted = budgeted(model, ctx.budget, ctx.audit, "exec")
    _without_general_purpose_subagent(counted)
    return create_deep_agent(
        model=counted,
        tools=build_tools(ctx, explainer=_explainer_for(ctx, explainer, ctx.env if ctx.env is not None else os.environ)),
        system_prompt=system_prompt(ctx.memory.rules_text() if ctx.memory else "", ctx.stage.templates, skills_text),
        middleware=[UntrustedDataMiddleware(), stops.RunLimits(ctx)],
        interrupt_on=dict.fromkeys(GATED_TOOLS, True),
        checkpointer=checkpointer,
    )


def new_thread_id() -> str:
    return uuid.uuid4().hex


def _config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}


def _begin(ctx: RunContext, *, new_message: bool) -> None:
    """Open one invocation: a new trace id and wall clock; a new human message also resets the per-request counters
    (a resume after a gate continues the same request's counts, but its waiting time is not counted)."""
    ctx.trace_id = uuid.uuid4().hex
    ctx.turn_started = time.monotonic()
    if new_message:
        ctx.tool_calls = ctx.tool_errors = ctx.turn_iterations = 0


def _run(agent: Any, ctx: RunContext, payload: Any, thread_id: str) -> dict[str, Any]:
    """One loop invocation with every way it can end defined (see worker.stops).

    A normal end is returned as the agent's result. A stop the harness defines (spend or iteration cap, wall clock,
    cut-off reply, guardrail, hook, audit unavailable) is audited first and returned as the same shape with the
    thread's messages and the outcome's text appended, plus `result["stop"]`; the checkpointed thread is untouched,
    so the next message continues from it. An error that is not a defined stop is raised as before.
    """
    config = _config(thread_id)
    try:
        result = agent.invoke(payload, config=config)
    except Exception as exc:
        outcome = stops.from_exception(ctx.audit, exc)
        if outcome is None:
            raise
        values = agent.get_state(config).values or {}
        return {"messages": [*values.get("messages", []), AIMessage(outcome.message)], "stop": outcome.as_dict()}
    if not result.get("__interrupt__"):  # a pause at a gate is the checkpoint; the decision is its own record
        stops.handle(ctx.audit, stops.Stop.END_TURN, trace_id=ctx.trace_id)
    return result


def start(
    agent: Any,
    ctx: RunContext,
    message: str,
    thread_id: str,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Send a human message (optionally with extra content blocks, e.g. an image) on a thread."""
    require_valid_skills(ctx.audit)  # a refused pin is audited and the run does not start
    _begin(ctx, new_message=True)
    ctx.instruction = ctx.instruction or message.split("\n")[0]
    won = {n: "sponsor override" if TEMPLATES.get(n) != spec else "organisation" for n, spec in ctx.stage.templates.items()}
    ctx.audit.record(
        "run.start", "human request", {"message": message, "sponsor": ctx.sponsor}, {"template_source": won}
    )
    content: Any = message
    if attachments:
        content = [{"type": "text", "text": message}, *attachments]
    return _run(agent, ctx, {"messages": [HumanMessage(content)]}, thread_id)


def pending_gate(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Action requests ({name, args, description}) waiting for a human decision; empty if none."""
    out: list[dict[str, Any]] = []
    for intr in result.get("__interrupt__", []) or []:
        out.extend(intr.value.get("action_requests", []))
    return out


def pending_actions(agent: Any, thread_id: str) -> list[dict[str, Any]]:
    state = agent.get_state(_config(thread_id))
    out: list[dict[str, Any]] = []
    for task in getattr(state, "tasks", ()) or ():
        for intr in getattr(task, "interrupts", ()) or ():
            value = getattr(intr, "value", None)
            if isinstance(value, dict):
                out.extend(value.get("action_requests", []))
    return out


class GateRefused(ValueError):
    """An approval the code refuses: an ERR is open, or a WARN has no acknowledgement with a reason."""

    def __init__(self, message: str, review_result: dict[str, Any]) -> None:
        super().__init__(message)
        self.review = review_result


def check_approval(
    ctx: RunContext, actions: list[dict[str, Any]], acknowledged: Mapping[str, str] | None = None
) -> None:
    """Raise GateRefused when approving these actions would pass an open ERR or an unacknowledged WARN."""
    for action in actions:
        args = action.get("args") or {}
        if action.get("name") != "write_upload" or not args.get("stage_id"):
            continue
        rt = str(args.get("template") or "")
        if ctx.id_root is not None and rt in ctx.stage.templates:  # the ID decision first; the same check as the write
            why = decision_refusal(ctx.sponsor, rt, lambda rt=rt: persist.id_scheme(
                ctx.id_root, ctx.sponsor, rt, ctx.stage.templates[rt].id_generation).decided)
            if why:
                ctx.audit.record("id.decision_missing", why, {"sponsor": ctx.sponsor, "record_type": rt,
                                                              "stage_id": args["stage_id"]}, {})
                raise GateRefused("Cannot approve: " + why, {
                    "can_approve": False, "open_err": ["id_decision_missing"], "unacknowledged_warn": [],
                    "findings": [], "err_count": 1, "warn_count": 0})
        result = review(
            ctx, args["stage_id"], bool(args.get("drop_duplicates")), args.get("duplicate_key"), acknowledged
        )
        if result["can_approve"]:
            continue
        by_id = {f["id"]: f for f in result["findings"]}
        parts = []
        if result["open_err"]:
            parts.append(f"{len(result['open_err'])} ERR must be resolved: " + "; ".join(
                by_id[i]["message"] for i in result["open_err"][:3]))
        if result["unacknowledged_warn"]:
            parts.append(f"{len(result['unacknowledged_warn'])} WARN need an acknowledgement with a reason: "
                         + "; ".join(by_id[i]["message"] for i in result["unacknowledged_warn"][:3]))
        raise GateRefused("Cannot approve. " + " | ".join(parts), result)


def decide(
    agent: Any,
    ctx: RunContext,
    thread_id: str,
    decision: dict[str, Any],
    reason: str = "",
    meta: dict[str, Any] | None = None,
    acknowledged: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Resume a paused run with one human decision, audited first.

    `decision` is {"type": "approve"} | {"type": "reject", "message": ...} |
    {"type": "edit", "edited_action": {"name": ..., "args": {...}}}.
    """
    _begin(ctx, new_message=False)
    pending = pending_actions(agent, thread_id)
    if decision["type"] != "reject":  # approve and edit both release the action, and only a named person's does
        meta = {**(meta or {}), "reviewer": approver_name(str((meta or {}).get("reviewer") or ""), demo=False)}
        try:
            check_approval(ctx, pending, acknowledged)
        except GateRefused as exc:  # a guardrail in code: audited, and the run stays at its gate
            stops.handle(ctx.audit, stops.Stop.GUARDRAIL, str(exc)[:200], refused="approval",
                         open_err=exc.review["open_err"], unacknowledged_warn=exc.review["unacknowledged_warn"])
            raise
    why = reason or decision.get("message", "") or "human decision"
    payload: dict[str, Any] = {
        "tools": [a.get("name") for a in pending] or list(GATED_TOOLS),
        "decision": decision,
        **(meta or {}),
    }
    ctx.audit.record(f"gate.{decision['type']}", why, payload)
    if decision["type"] != "reject" and acknowledged:
        ctx.audit.record_many(
            "finding.acknowledged",
            [(str(r).strip(), {"finding": fid, **(meta or {})}, {}) for fid, r in acknowledged.items()],
        )
    if decision["type"] == "reject":
        for action in pending:
            if action.get("name") in ("write_upload", "confirm_account_mappings"):
                feedback.record(
                    ctx.audit,
                    ctx.feedback,
                    kind="job",
                    action="reject",
                    shown=action.get("name"),
                    reason=decision.get("message") or reason,
                    sponsor=ctx.sponsor,
                    job=ctx.out_dir.parent.name if ctx.out_dir.name == "out" else ctx.out_dir.name,
                    record_type=((action.get("args") or {}).get("template")),
                )
        for action in pending:
            if action.get("name") == "propose_rule":
                args = action.get("args", {})
                ctx.audit.record(
                    "rule.rejected", why, {"text": args.get("text", ""), "why": args.get("why", "")}
                )
    return _run(agent, ctx, Command(resume={"decisions": [decision]}), thread_id)
