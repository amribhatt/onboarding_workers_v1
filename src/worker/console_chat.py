"""The job chat in plain language. The Interpreter (`role_interpret`) reads a sentence; this module is the code that
decides what it means for the job: a rule becomes a draft the person saves, an ID decision becomes a preview with one
Apply button (and still needs a reason), a question or an answer is shown as it is, and where the person should
look on the screen is a button. The model never applies anything."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from worker import console_demo as D
from worker import console_engine as CE
from worker import console_idgen as G
from worker import feedback as F
from worker import idgen as I
from worker import persist
from worker import record_facts as RF
from worker import templates as T
from worker.audit import AuditError
from worker.context import Budget, BudgetExceeded
from worker.contracts import HANDOFF_REJECTED, HANDOFF_RETURNED, HANDOFF_SENT
from worker.roles import RoleRunner, interpret

SCREEN_TAB = {"Plan": "plan", "ID generation (on the Plan tab)": "plan", "Issues": "issues", "Reconcile": "recon",
              "Output": "output", "Activity": "activity"}
ID_LENGTH = {
    "error": "Use the source ID as it is. Over the limit is an ERR. It is never cut, because a cut ID can collide.",
    "truncate": "Cut IDs over the limit to the limit. Rows that end with the same ID are ERR.",
    "drop": "Drop the leading prefix from IDs over the limit. The result is a WARN you check.",
}
HITS = "IDs over the limit in this file"


def _audit(c: Any) -> Any:
    return persist.audit(Path(c.root) / "console_chat.audit.jsonl")


CHAT_TIMEOUT, CHAT_RETRIES = 30, 1  # a person is waiting: fail fast and say so, not 4 retries of 90 s
CHAT_CONFIDENCE = 0.6  # a reading below it, or none that passed the checks, goes once to the stronger model
EXAMPLES_KEPT = 5  # confirmed sentences shown to the model, newest first, per sponsor
THUMBS = [{"label": "👍 Right", "action": "chat_fb|up"},
          {"label": "👎 Not what I meant", "action": "chat_fb|down"}]
_MODELS: dict[tuple[str, ...], Any] = {}


def _model(env: dict[str, str], role: str = "small") -> Any:
    """The chat's model for `role` (`small`, then `exec` as the escalation) with its own short timeout and one retry,
    built once per process. The agent's and the roles' shared clients keep their own settings."""
    from worker.agent import make_model, role_setting

    key = (role, role_setting(env, role), role_setting(env, role, "WORKER_MODEL_FALLBACKS"),
           env.get("WORKER_MODEL_PROVIDER", ""))
    if key not in _MODELS:
        _MODELS[key] = make_model({**env, "WORKER_TIMEOUT": str(CHAT_TIMEOUT), "WORKER_MAX_RETRIES": str(CHAT_RETRIES)},
                                  role)
    return _MODELS[key]


def warm() -> None:
    """Pay the first message's one-time cost (imports, the model client) at start-up, off the request path."""
    from worker.cli import load_env  # lazy: cli pulls in the agent stack

    with contextlib.suppress(Exception):  # no model set up yet: the first message says so
        _model(load_env())


def _models(c: Any, env: dict[str, str]) -> list[Any]:
    """The cheap model, then the stronger one when the settings name a different one (tests inject both)."""
    from worker.agent import role_setting

    if "interpreter" in c.chat_models:
        return [c.chat_models["interpreter"], *([c.chat_models["escalation"]] if "escalation" in c.chat_models else [])]
    same = role_setting(env, "exec") == role_setting(env, "small")
    return [_model(env, "small"), *([] if same else [_model(env, "exec")])]


def _audit(c: Any) -> Any:
    return persist.audit(Path(c.root) / "console_chat.audit.jsonl")


def learned(c: Any, j: dict[str, Any]) -> list[dict[str, Any]]:
    """This sponsor's confirmed sentences: what a person kept (thumbs up, or saved the rule) or corrected to, each with
    the rule they meant. Only a person's own click makes an example; nothing a model said about itself does."""
    seen: dict[str, dict[str, Any]] = {}
    for r in persist.feedback(Path(c.root) / "feedback", T.sponsor_slug(j["sp"])).list(kind="chat"):
        shown, chosen = r.get("shown") or {}, r.get("chosen") or {}
        if r["action"] == "override":
            rule, value = chosen.get("rule"), chosen.get("value")
        elif r["action"] == "accept":
            rule, value = shown.get("rule"), shown.get("value")
        else:
            continue
        if rule and shown.get("sentence"):
            seen.pop(shown["sentence"], None)
            seen[shown["sentence"]] = {"sentence": shown["sentence"], "rule": rule, "value": value}
    return list(seen.values())[-EXAMPLES_KEPT:][::-1]


def _record(c: Any, j: dict[str, Any], action: str, shown: dict[str, Any],
            chosen: dict[str, Any] | None = None) -> None:
    F.record(persist.job_audit(c, j), persist.feedback(Path(c.root) / "feedback", T.sponsor_slug(j["sp"])),
             kind="chat", action=action, shown=shown, chosen=chosen, scope="sponsor", job=j["id"],
             sponsor=T.sponsor_slug(j["sp"]), record_type=j["type"], model=shown.get("model"))


def _drop_thumbs(j: dict[str, Any]) -> None:
    for m in j["chat"]:  # a rating applies to the latest reading only
        m["options"] = [o for o in m.get("options", []) if not str(o.get("action", "")).startswith("chat_fb|")]


def rate(c: Any, j: dict[str, Any], up: bool) -> None:
    """A person's thumbs on the latest reading, recorded in the sponsor's feedback; a thumbs down arms a correction."""
    from worker.console import ConsoleError

    last = j.get("chat_read")
    if not last:
        raise ConsoleError("There is nothing to rate yet.")
    _drop_thumbs(j)
    _record(c, j, "accept" if up else "reject", last)
    c.log(f"Chat reading rated {'right' if up else 'not what I meant'}", f"for {j['sp']}; it shapes later readings",
          j["type"])
    if up:
        j.pop("chat_miss", None)
        _say(j, f"Noted. I'll read sentences like that the same way for {j['sp']}.")
    else:
        j["chat_miss"] = last
        _say(j, "Sorry. Say what you meant and I'll read it again. When you save the rule I'll remember it for "
                f"{j['sp']}.")


def saved(c: Any, j: dict[str, Any], read: tuple[str, Any]) -> None:
    """A person saved or applied a rule from the chat: a confirmation, or the correction after a thumbs down."""
    miss, last = j.pop("chat_miss", None), j.get("chat_read")
    if miss:
        _record(c, j, "override", miss, {"rule": read[0], "value": read[1]})
    elif last:
        _record(c, j, "accept", last, {"rule": read[0], "value": read[1]})
    _drop_thumbs(j)


def traces(c: Any, limit: int = 12) -> list[dict[str, Any]]:
    """One row per chat message the Interpreter read, newest first, from the chat's own audit (its hand-off records):
    when, model, attempts, tokens, seconds and how it ended. No sentence or answer text is stored here."""
    by: dict[str, dict[str, Any]] = {}
    for e in _audit(c).read():
        if e["action"] not in (HANDOFF_SENT, HANDOFF_RETURNED, HANDOFF_REJECTED, "chat.escalated"):
            continue
        h = e["inputs"]
        if e["action"] == "chat.escalated":
            by.setdefault(h["trace_id"], {})["escalated"] = True
            continue
        row = by.setdefault(h["trace_id"], {"trace": h["trace_id"][:8], "at": e["ts"], "model": h["model_id"],
                                            "attempts": 0, "tokens": 0, "seconds": 0.0, "outcome": "sent",
                                            "problems": []})
        row.setdefault("escalated", False)
        row["model"] = h["model_id"]
        row["attempts"] = max(row["attempts"], int(e["outputs"].get("attempt", 0)))
        if e["action"] != HANDOFF_SENT:
            row["tokens"] += int(h.get("tokens_in") or 0) + int(h.get("tokens_out") or 0)
            row["seconds"] = round(row["seconds"] + float(h.get("seconds") or 0), 2)
            row["outcome"] = "read" if e["action"] == HANDOFF_RETURNED else "refused"
            row["problems"] = list(e["outputs"].get("problems") or [])[:3]
    return sorted(by.values(), key=lambda r: r["at"], reverse=True)[:limit]


def rules_of(j: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The closed set of rules this job's chat may change: what each does and the values it takes ([] is any number)."""
    out: dict[str, dict[str, Any]] = {
        "id_length": {"does": "what happens to an ID longer than the limit: error (keep it and flag it), truncate "
                              "(cut it), drop (remove its prefix)", "values": list(ID_LENGTH)},
        "name_length": {"does": "the most characters a name may have; a longer name is cut and flagged", "values": []},
        "ltd_limited": {"does": "treat the words Ltd and Limited as the same when comparing names", "values": ["on"]},
    }
    if RF.is_gl(j["type"]):
        out["balance_tolerance"] = {"does": "how far a journal may be off before it is flagged", "values": []}
    if j["type"] in RF.id_decision_types():
        out["id_decision"] = {"does": "how this sponsor's new IDs are made: keep the source IDs (source) or number "
                                      "them in sequence (sequential)", "values": ["source", "sequential"]}
    return out


def _say(j: dict[str, Any], text: str, role: str = "Mapper", where: str | None = None, rate: bool = False) -> None:
    opts = [{"label": f"Open {where}", "action": f"goto|{SCREEN_TAB[where]}"}] if where else []
    if rate:
        _drop_thumbs(j)
        opts += THUMBS
    j["chat"].append({"who": "w", "role": role, "text": text, "options": opts})


def _draft(j: dict[str, Any], text: str, rule: str, value: Any) -> dict[str, Any]:
    r = j["rules"]
    d: dict[str, Any] = {"text": text, "ok": True}
    if rule == "id_length":
        d.update(name="ID length", before=ID_LENGTH[r["id"]], after=ID_LENGTH[value], rule=("id", value), hits=HITS)
    elif rule == "name_length":
        n = int(value)
        hits = 0 if RF.is_gl(j["type"]) else sum(1 for nm in D.sample_names(j["type"]) if len(nm) > n)
        d.update(name="Name length", before=f"Keep the first {r['nameMax']} characters of the name. A cut name "
                 "is a WARN.", after=f"Keep the first {n} characters of the name. A cut name is a WARN.",
                 rule=("nameMax", n), hits=f"{hits} name(s)")
    elif rule == "ltd_limited":
        d.update(name="Name matching", before="Names are compared as written.",
                 after="Treat “Ltd” and “Limited” as the same word when comparing names.",
                 rule=("suffix", True), hits="rows with a matching name under another key")
    else:
        n = float(value)
        d.update(name="Balance tolerance", before=f"A journal off by up to {r['tolPass']:,.2f} passes, up to "
                 f"{r['tolWarn']:,.2f} is a WARN, more is ERR.", after=f"A journal off by up to {n:,.2f} passes, "
                 f"up to {max(n, r['tolWarn']):,.2f} is a WARN, more is ERR.", rule=("tol", n),
                 hits="journals with a small difference")
    d["read"] = (rule, value)  # the Interpreter's own names, for the feedback record
    return d


def _id_decision(c: Any, j: dict[str, Any], mode: str, reason: str, rate: bool = False) -> None:
    """Preview the IDs by code; the Apply button carries the person's own reason, or the worker asks for one."""
    scheme = {**dict(RF.target(j["type"]).id_generation), "mode": mode}
    pv = G.preview(c, {"sponsor": j["sp"], "record_type": j["type"], "job": j["id"], "scheme": scheme})
    if pv["errors"]:
        _say(j, "; ".join(e["message"] for e in pv["errors"]) + ". Nothing changed.", where="ID generation (on the Plan tab)")
        return
    ids = ", ".join(f"{x['name']} → {x['id']}" for x in pv["ids"])
    lead = f"{G.LABELS[mode]}: " + (f"the first IDs would be {ids}." if ids else "; ".join(pv["notes"]))
    reason = " ".join(reason.replace("|", "/").split())
    if not reason:
        _say(j, f"{lead}\nYour reason is recorded with this decision, under your name. Give a reason, for example "
                "“keep the source IDs because the client numbers them”.")
        return
    j["chat"].append({"who": "w", "role": "Mapper", "text": f"{lead}\nYour reason is recorded with your name: "
                      f"“{reason}”. It applies to every {j['sp']} entity and fund. Nothing changes until you press Apply.",
                      "options": [{"label": "Apply", "action": f"id_decide|{mode}|{reason}", "primary": True},
                                   *(THUMBS if rate else [])]})


def _weak(got: Any) -> bool:
    return got.result is None or got.result.confidence < CHAT_CONFIDENCE


def reply(c: Any, j: dict[str, Any], text: str) -> None:
    """Answer one sentence of the job chat. Sets `j["draft"]` for a rule the person can save; otherwise only chat.
    The cheap model reads first; a weak or refused reading goes once to the stronger model (same trace, one budget)."""
    import uuid

    from worker.cli import load_env  # lazy: cli pulls in the agent stack
    from worker.role_runner import describe_model

    j["draft"] = None
    t = CE.tally(c, j) if j["runs"] else None
    gate = I.decision_refusal(j["sp"], j["type"], lambda: c.id_decision(j["sp"], j["type"]))
    job = {"type": j["type"], "sponsor": j["sp"], "rules": dict(j["rules"]), "checked": t,
           "id_decision_made": not gate, "in_this_file": j.get("up", {}).get("file", ""),
           "confirmed_examples": learned(c, j)}
    env = load_env()
    budget, trace, got, used = Budget.from_env(env), uuid.uuid4().hex, None, None
    try:
        for n, model in enumerate(_models(c, env)):
            if n:  # the first reading was weak or refused: say so in the record before the stronger model is asked
                _audit(c).record("chat.escalated", "the cheap model's reading was weak or refused; asking the stronger one",
                                 {"trace_id": trace, "from": describe_model(used), "to": describe_model(model)})
            runner = RoleRunner(audit=_audit(c), budget=budget, models={"interpreter": model}, env=env, trace_id=trace)
            got, used = interpret(runner, unit_id=str(j["id"]), sentence=text, job=job, rules=rules_of(j),
                                  screens=list(SCREEN_TAB)), model
            if not _weak(got):
                break
    except AuditError:
        raise
    except BudgetExceeded:
        _say(j, "This job's model budget is used up, so I can't read that. Nothing changed.", where="Plan")
        return
    except Exception as exc:  # noqa: BLE001 - provider unreachable or no model set up: say so, never guess
        busy = "RateLimit" in type(exc).__name__
        _say(j, "The model is rate-limited right now; try again in a minute. Nothing changed." if busy else
                f"I couldn't reach a model to read that ({type(exc).__name__}). Choose one in Settings, or change "
                "the plan directly. Nothing changed.", where="Plan")
        return
    if got is None or got.result is None:
        _say(j, "I couldn't work out what you want changed from that. Tell me the field and what to do, or change "
                "it on the plan. Nothing changed.", where="Plan")
        return
    r, words = got.env.output, got.result.summary
    j["chat_read"] = {"sentence": " ".join(text.split())[:300], "kind": r.kind, "rule": r.rule, "value": r.value,
                      "model": describe_model(used)}
    if r.kind == "propose" and r.rule == "id_decision":
        _id_decision(c, j, str(r.value), r.reason or "", rate=True)
    elif r.kind == "propose":
        j["draft"] = d = _draft(j, text, str(r.rule), r.value)
        _say(j, f"{d['name']}: {d['after']} Save it for this job or for {j['sp']}; nothing changes until you do.",
             rate=True)
    else:
        _say(j, words, "Checker" if r.kind == "answer" else "Mapper", r.where, rate=True)
