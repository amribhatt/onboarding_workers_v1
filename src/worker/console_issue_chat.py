"""The chat, about the rows of the open issue group, in the person's own words.

A sentence that is not one of the suggestions ("row 9 is INV-A and drop row 10", "hold the Fund II ones until the client
answers", "what is wrong with these?") goes to the Interpreter with the group and its rows. It answers with a closed
set: decisions {option, rows, value, note}, a question, an answer, or "this is not about these rows". Code checks every
part (the option is one the group offers, the rows are in the group, a typed value or a reason is the person's own
words) and builds the preview. The proposal is kept here and its Apply button names it; pressing Apply goes through the
same audited path as every other decision. The model never applies anything and never retypes a value."""

from __future__ import annotations

import uuid
from typing import Any

from worker import console_chat as CC
from worker import console_issues as CI
from worker.audit import AuditError
from worker.context import Budget, BudgetExceeded
from worker.role_interpret import interpret_issue
from worker.role_runner import RoleRunner, describe_model

SHOWN = 60  # rows of the group given to the model; the rest is addressed by "every row of the group"


def _say(j: dict[str, Any], text: str, where: str | None = None) -> None:
    CI._chat_msg(j, "w", text, "Checker", [{"label": f"Open {where}", "action": f"goto|{CC.SCREEN_TAB[where]}"}] if where else [])


def _group(c: Any, j: dict[str, Any], gid: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    cz = CI.causes()[items[0]["cid"]]
    one = [{"id": i, "label": lab} for i, lab in CI._opts(cz, 1, items[:1]) if i in ("edit", "out", "park", "fix")]
    lab, _b, _a = CI.pattern_of(cz.id, items[0])
    return {"label": cz.label, "what_is_wrong": cz.consequence, "pattern": lab, "units": CI._units(j),
            "options": one, "recommended": cz.recommended}


def reply(c: Any, j: dict[str, Any], gid: str, text: str) -> bool:
    """Answer a sentence typed while an open issue group is in the chat. False when it is not for this module (no group
    open, a held group, an approved job, or a sentence the Interpreter says is about something else)."""
    from worker.cli import load_env  # lazy: cli pulls in the agent stack

    if j.get("approved") or gid.startswith("H"):
        return False
    try:
        _g, items = CI._find(c, j, gid)
    except CI.IssueError:
        return False
    if not items:
        return False
    group = _group(c, j, gid, items)
    shown = [{"row": x["row"], "name": x["name"], **dict(zip(("pattern", "before", "after"), CI.pattern_of(items[0]["cid"], x), strict=True))}
             for x in items[:SHOWN]]
    known = {x["row"] for x in items}
    env = load_env()
    budget, trace, got, used = Budget.from_env(env), uuid.uuid4().hex, None, None
    j["chat"].append({"who": "you", "text": text.strip()})
    try:
        for n, model in enumerate(CC._models(c, env)):
            if n:
                CC._audit(c).record("chat.escalated", "the cheap model's reading was weak or refused; asking the stronger one",
                                    {"trace_id": trace, "from": describe_model(used), "to": describe_model(model)})
            runner = RoleRunner(audit=CC._audit(c), budget=budget, models={"interpreter": model}, env=env, trace_id=trace)
            got, used = interpret_issue(runner, unit_id=str(j["id"]), sentence=text, group=group, shown=shown, known=known,
                                        screens=list(CC.SCREEN_TAB)), model
            if not CC._weak(got):
                break
    except AuditError:
        raise
    except BudgetExceeded:
        _say(j, "This job's model budget is used up, so I can't read that. Use the buttons on the rows. Nothing changed.")
        return True
    except Exception as exc:  # noqa: BLE001 - provider unreachable or no model set up: say so, never guess
        busy = "RateLimit" in type(exc).__name__
        _say(j, "The model is rate-limited right now; use the buttons on the rows or try again in a minute. Nothing changed."
             if busy else f"I couldn't reach a model to read that ({type(exc).__name__}). The buttons on each row still "
             "work. Nothing changed.", "Plan")
        return True
    if got is None or got.result is None:
        _say(j, "I couldn't work out what you want done with these rows. Name the rows and what to do, for example "
                "“row 9 is INV-A, skip row 10”, or use the buttons on each row. Nothing changed.")
        return True
    r, words = got.env.output, got.result.summary
    if r.kind == "other":
        j["chat"].pop()  # the ordinary chat takes the sentence and shows it itself
        return False
    if r.kind != "decide":
        _say(j, words, r.where)
        return True
    picks = [{"id": gid, "option": d.option, **({"rows": sorted(d.rows)} if d.rows else {}),
              **({"value": " ".join(d.value.split())} if d.value else {})} for d in r.decisions]
    try:
        pv = CI.preview(c, j, picks)
        freed = CI._released(j)
        if any(x["row"] in freed for x in items) and any(p["option"] != "park" for p in picks):
            CI._note(" ".join(text.split()), "Deciding rows that were on hold")
    except CI.IssueError as exc:
        _say(j, f"{exc} Nothing changed.")
        return True
    lines = ["Here is what I read. Nothing changes until you press Apply:"]
    for p, x in zip(picks, pv["groups"], strict=True):
        unit = pv["units"][:-1] if x["n"] == 1 else pv["units"]
        lines.append(f"• {x['label']}: {x['n']:,} {unit}" + (f", value “{p['value']}”" if p.get("value") and p["option"] == "edit" else "")
                     + (f", reason “{p['value']}”" if p["option"] == "park" else ""))
        sample = x["rows"]  # at most three rows of the preview
        lines += [f"    row {s['row']:,}: {s['before']} → {s['after']}" for s in sample]
        if x["stay_open"]:
            lines.append(f"    {x['stay_open']:,} {pv['units']} cannot take this change and stay open.")
        if x["irreversible"]:
            lines.append("    This adds records to the sponsor's existing records; an undo does not remove them.")
        if p["option"] == "fix":
            lines.append(f"    Applying also saves this as a rule for {j['sp']}, so future files do the same.")
    note = " ".join(text.replace("|", "/").split())[:CI.NOTE_MAX]
    lines.append(f"Your sentence is recorded with your name as the note: “{note}”.")
    prop = uuid.uuid4().hex[:10]
    j["issue_prop"] = {"id": prop, "gid": gid, "picks": picks, "note": note}
    CI._chat_msg(j, "w", "\n".join(lines), "Checker", [{"label": "Apply", "action": f"issue_apply_prop|{prop}", "primary": True}])
    return True
