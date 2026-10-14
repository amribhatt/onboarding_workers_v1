"""Whole-screen view models of the console: the job screen, its chat, the plan table, the sponsor list.

Built on `console_view` (trace, sample, output facts, timeline). Plain dicts; the page renders them.
"""

from __future__ import annotations

from typing import Any

from worker import console_demo as D
from worker import console_engine as CE
from worker import console_idgen as CG
from worker import console_questions as Q
from worker import console_view as V
from worker import idgen as I
from worker import record_facts as RF

# from the GL process flows (entity level and LP level): target key, where it comes from, confidence H / M / L
GLMAP = [
    ("DATE", "GL Date", "H"), ("DEBIT", "Amount Local × (+1), negative = credit", "M"),
    ("EXCHANGE_RATE", "−Amount Legal Entity ÷ DEBIT, 1 when zero", "M"),
    ("ACCT_NO", "GL Account + Transaction Type → COA", "H"),
    ("DEPT_ID", "Deal Name + Position → Investment static data", "M"),
    ("REFERENCE_NO", "Entity ID + HJE + # + Batch ID", "H"), ("DESCRIPTION", "Transaction Comments, else MEMO", "M"),
    ("PR_DATE", "Effective Date", "L"), ("GLENTRY_VENDORID", "Vendor Id → Vendor static data", "L"),
]


def plan_view(c: Any, j: dict[str, Any]) -> dict[str, Any]:
    typ, sp = j["type"], j["sp"]
    gl = RF.is_gl(typ)
    sheet = CE.sheet_facts(j)
    lines = [ln for ln in V.plan_lines(c, j) if len(ln) < 3 and "not in the file, left blank" not in ln[0]
             and not ln[0].endswith(" on every row")]
    out: dict[str, Any] = {
        "file": j["up"]["file"], "sheet": sheet[0], "rows": sheet[1], "template": RF.template_stem(typ) + ".xlsx",
        "saved": j["rep"], "gl": gl,
        "rules": [{"text": x[0], "source": x[1], "yours": x[1] == "your rule"} for x in lines],
    }
    if gl:
        out["fields"] = [{"field": m[0], "from": m[1], "confidence": m[2]} for m in GLMAP]
        out["gap"] = []
        return out
    fields = []
    cols = CE.source_columns(j) or D.SRCCOLS.get(typ, [])  # the file's own headers, else the sample's
    for f in CE.fields_of(j):
        req = c.req_of(typ, f, sp)
        src = f[1]
        idf = typ in RF.id_decision_types() and f[0] == RF.target(typ).key[-1]
        kind = ("generated" if src == "generated" else "constant" if src.startswith("constant") else
                "user" if f[4] and src else "missing" if not src else "sure" if f[3] >= 95 else "glance")
        fields.append({
            "field": f[0], "required": req, "required_default": bool(f[2]), "differs": req != bool(f[2]),
            "source": src, "kind": kind, "confidence": f[3], "constant": src[10:] if kind == "constant" else "",
            "id_text": CG.id_text(c, sp, typ) if kind == "generated" else "",
            "columns": cols if kind not in ("generated", "constant") or idf else [],
            "col_fit": CG.fits(j, typ) if idf else {}, "id_field": idf, "id_mode": c.id_scheme_of(sp, typ)["mode"] if idf else "",
        })
        got = (j.get("fieldrecipe") or {}).get(f[0])
        if got is not None:  # decided by a recipe: for every job of this sponsor, or for this job only
            fields[-1]["recipe"] = {"scope": got["scope"], "source": got["source"], "ops": got["ops"],
                                       "header": got.get("header", ""), "value": got.get("value", "")}
    out["fields"] = fields
    out["gap"] = [f["field"] for f in fields if f["required"] and not f["source"]]
    return out


def nudge(c: Any, j: dict[str, Any]) -> dict[str, Any] | None:
    last = len(j["runs"])
    gl = RF.is_gl(j["type"])
    u = "lines" if gl else "rows"
    if j["failed"]:
        return {"role": "Checker", "text": f"I stopped at {j['failed']['stage']}. Everything before it is saved.",
                "options": [{"label": f"Resume from {j['failed']['stage']}", "action": "resume", "primary": True}]}
    if not last:
        gap = [] if gl else [f[0] for f in CE.fields_of(j) if c.req_of(j["type"], f, j["sp"]) and not f[1]]
        n = len(GLMAP) if gl else len(D.plan(j["type"]))
        return {"role": "Mapper", "text": f"The plan is on the left: {n} {'keys' if gl else 'fields'} into "
                f"{RF.template_stem(j['type'])}.xlsx" + (f", and {', '.join(gap)} is required but not in the file" if gap else "")
                + ". Change a column or a rule there, or tell me here.",
                "options": [{"label": "Looks right, convert it", "action": "go", "primary": True}]}
    if j["approved"]:
        nxt = RF.next_of(j["type"])
        return {"role": "Writer", "text": "Approved and written. " + (f"Next for {c.ent_label(j)}:" if nxt
                                                                       else "All record types are done for this fund."),
                "options": [{"label": nxt, "action": f"next|{nxt}", "primary": True}] if nxt else []}
    t = CE.tally(c, j)
    open_q = [] if gl else Q.open_blockers(c, j)
    if open_q and not t["err"]:
        return {"role": "Mapper", "text": f"{', '.join(open_q)} {'is' if len(open_q) == 1 else 'are'} written in a "
                                          "form I will not guess. Say how to read it in the question on the left; "
                                          "your answer is kept for next time.", "options": []}
    if t["err"]:
        one = t["err"] == 1
        return {"role": "Checker", "text": f"{t['err']} {u[:-1] if one else u} {'needs' if one else 'need'} your "
                                           "call. They're on the Issues tab, grouped by cause, each with my suggestion.",
                "options": []}
    from worker import console_issues as CI

    held = CI.held_count(c, j)
    if held:  # on-hold rows block the approval: the chat must not say it is ready
        return {"role": "Checker", "text": f"{held} {'row is' if held == 1 else 'rows are'} on hold. Release or decide "
                                           "them on the Issues tab before approval: nothing is written while any is on hold.",
                "options": []}
    if not t["read"]:  # a header-only or misplaced sheet: do not call a file with nothing in it ready
        return {"role": "Reader", "text": "I read no rows from this file, so there is nothing to approve. Check that "
                                          "the data starts under one header row on the first sheet, or attach the right "
                                          "file.", "options": []}
    if gl:
        off = len(CE.gl_eval(c, j)["offsets"])
        return {"role": "Writer", "text": f"Ready for your approval: {t['read']} lines read, {t['out'] - off} written"
                + (f" plus {off} offsets" if off else "") + f", {t['skip']} left out. The totals are on the left."}
    return {"role": "Writer", "text": f"Ready for your approval: {t['read']} read = {t['out']} written + "
                                      f"{t['skip']} left out. The sample is on the left."}


def chat_messages(c: Any, j: dict[str, Any]) -> list[dict[str, Any]]:
    t = j["type"]
    lt = t.lower()
    reg = c.reg_of(j["sp"]).get(t, [])
    e = c.ext_of(t, j["sp"])
    msgs: list[dict[str, Any]] = []

    def w(role: str, text: str, options: list[dict[str, Any]] | None = None) -> None:
        msgs.append({"who": "w", "role": role, "text": text, "options": options or []})

    for m in j.get("msgs0") or []:
        if m["who"] == "you":
            msgs.append({"who": "you", "text": m["text"]})
        else:
            w("", m["text"])
    if j.get("exAsk"):
        known = f" Right now I know {len(reg)}" + (", all from jobs approved in this app" if reg else "") + "."
        w("Checker",
          f"Before I map anything, one question about duplicates. I check every new {lt[:-1]} in this file against "
          f"the {lt} {j['sp']} already has in Ledgerline, so nothing gets loaded twice.{known} What I can't see is "
          "anything put into Ledgerline another way: keyed in by hand, loaded by another team, or there before this "
          f"app. Were any {j['sp']} {lt} loaded into Ledgerline outside this app?",
          [] if j["exA"] else [{"label": "Yes, I'll upload the export", "action": "ex|yes", "primary": True},
                               {"label": "No, go ahead", "action": "ex|no"},
                               {"label": "Not sure", "action": "ex|unsure"}])
        if j["exA"]:
            msgs.append({"who": "you", "text": j["exTxt"]})
            if j["exA"] == "no":
                w("Checker", f"Thanks. I'll check only against the {len(reg)} I know and won't ask again for "
                             f"{j['sp']} {lt}. If one turns up later, use Add as existing on that row and give me "
                             "its Ledgerline ID.")
            elif j["exA"] == "skip":
                w("Checker", "OK, going ahead without it. A " + lt[:-1] + " that is already in Ledgerline could come "
                  "through as new, so I'll mark this run not checked against Ledgerline and ask again on the next "
                  f"{j['sp']} job.")
            elif not j["exF"]:
                text = (f"Great. Export the {lt} list from Ledgerline and drop it here." if j["exA"] == "yes" else
                        "Then it's safer to check: a duplicate is much harder to undo once it's in Ledgerline. If you "
                        "can get the export, drop it here. If not, I can go ahead and mark the run as not checked.")
                opts = [{"label": "Use a sample export", "action": "ex|file"}]
                if j["exA"] == "unsure":
                    opts.append({"label": "Go ahead without it", "action": "ex|skip"})
                w("Checker", text, opts)
            else:
                n = [x for x in reg if e and e.get("file") and e["file"] in x["src"]]
                msgs.append({"who": "you", "text": (e or {}).get("file", "")})
                w("Reader", f"Read {len(n)} {lt} from {(e or {}).get('file', '')}. With the {len(reg) - len(n)} from "
                  f"earlier jobs I now know {len(reg)}. An exact match (same ID and name) is left out with its "
                  "reason; a clash (same ID and a different name, or the reverse) comes to you. Saved for "
                  f"{j['sp']}, so I won't ask again.")
    for m in j["chat"]:
        msgs.append({"who": m["who"], "role": m.get("role", ""), "text": m["text"],
                     "options": m.get("options", [])})
    d = j.get("draft")
    if d and d["ok"]:
        w("Mapper", f"Here is the change. Nothing is applied until you say so.\n{d['name']} ({d['hits']})\n"
                    f"Before: {d['before']}\nAfter: {d['after']}",
          [{"label": "Use it", "action": "save|job", "primary": True},
           {"label": f"Use it for every {j['sp']} job", "action": "save|sponsor"},
           {"label": "Cancel", "action": "disc"}])
    elif not c.ex_pending(j):
        if t in RF.id_decision_types() and not c.id_decision(j["sp"], t):
            seen_ids = CG._observed(c, j["sp"], t)[1]
            if seen_ids:
                w("Mapper", seen_ids)
        nud = nudge(c, j)
        if nud:
            w(nud["role"], nud["text"], nud.get("options"))
    return msgs


def signed_in() -> str:
    """The name a deployment sets for everyone (WORKER_USER), else none: with logins on, the page asks the server
    who is signed in. A reserved or agent name is not a person and is not offered."""
    import os

    from worker.contracts import UnnamedApprover, approver_name

    try:
        raw = os.environ.get("WORKER_USER", "")
        return approver_name(raw, demo=False)
    except UnnamedApprover:
        return ""


def job_view(c: Any, j: dict[str, Any]) -> dict[str, Any]:
    gl = RF.is_gl(j["type"])
    last = len(j["runs"])
    t = CE.tally(c, j) if last else None
    pending = c.ex_pending(j)
    v: dict[str, Any] = {
        "id": j["id"], "sponsor": j["sp"], "type": j["type"], "entities": c.ent_label(j), "file": j["up"]["file"],
        "timeline": V.timeline(c, j), "plan": plan_view(c, j), "approved": j["approved"], "written": j["written"],
        "failed": j["failed"], "ex_pending": pending, "store_text": c.store_text(j["type"], j["sp"]),
        "ran": bool(last), "saved_workflow": j["rep"], "rule_changes": len(j["logR"]),
        "columns_by_you": len(j["map"]), "chat": chat_messages(c, j), "route": j.get("route", ""),
        "replaced": list(j.get("replaced", []))[-10:], "can_replace": not j["approved"],
        "uploads": len(j.get("upload_log") or []) or 1,  # how many times the source file was uploaded, first one included
        "source_file": j["up"]["file"],  # `file` is the written upload once approved; this is always the job's source
        # a type whose IDs the worker may generate needs a named person's decision before approval: said the same way for every type
        "id_pending": bool(not j["approved"] and I.decision_refusal(j["sp"], j["type"], lambda: c.id_decision(j["sp"], j["type"]))),
    }
    # the duplicates question is answered on the plan itself, as every other question is; chat only repeats it
    asked = next((m for m in reversed(v["chat"]) if m["who"] == "w" and m.get("options")), None) if pending else None
    v["ex_ask"] = {"title": f"Were any {j['sp']} {j['type'].lower()} loaded into Ledgerline outside this app?",
                   "options": asked["options"]} if asked else None  # buttons here, the explaining words in chat
    v["questions"] = Q.view(c, j) if not gl and not j["approved"] else []
    v["answered"] = Q.answered_view(j)  # the answers in force (a reload keeps their Undo)
    blockers = [q["field"] for q in v["questions"] if q["blocking"]]
    v["questions_blocking"] = blockers
    if last and not pending:
        v["tally"] = t
        v["output"] = V.output_facts(c, j)
        from worker import console_issues as CI

        v["on_hold"] = CI.held_count(c, j)
        old, changed = CI.stale(c, j)
        v["stale"] = ({"n": len(old), "changed": changed, "can_reopen": any(not hd.get("irreversible") for hd in old)}
                      if old and not j["approved"] else None)
        v["empty"] = bool(t and not t["out"] and not j["approved"])  # every row is left out: no records to write
        v["empty_allowed"] = bool(v["empty"] and c.empty_allowed(j))
        v["can_approve"] = bool(t and not t["err"] and not j["failed"] and not blockers and not v["on_hold"]
                                and not v["id_pending"] and not v["stale"] and (not v["empty"] or v["empty_allowed"]))
        v["sample"] = V.sample_rows(c, j) if not gl else None
        if j["trace"] is not None:
            v["trace"] = V.trace(c, j, j["trace"])
        if j["approved"]:
            v["learn"], v["next"] = j["learn"], RF.next_of(j["type"])
            if j.get("out"):
                v["download"] = f"/api/console/jobs/{j['id']}/download"
                v["file"] = {"name": j["out"]["file"], "rows": j["out"]["rows"]}
    return v


def sponsors_view(c: Any) -> list[dict[str, Any]]:
    out = []
    for s in c.S["sponsors"]:
        n = s["n"]
        stored = c.registry(n).counts()  # one count per type, not one read of every record per type
        out.append({
            "id": s["id"], "name": n, "live": s["live"],
            "entities": [{"name": e, "id": c.entity_id(e), "live": c.S["EM"].get(e, {}).get("live", ""),
                          "gid": c.S["EM"].get(e, {}).get("gid", ""), "gn": c.S["EM"].get(e, {}).get("gn", ""),
                          "static_done": c.static_done(e, n), "gl_done": sum(c.S["F"][e]["gl"].values())}
                         for e in c.ents_of(n)],
            "approved_jobs": [{"type": r["type"], "fund": r["fund"], "rows": r.get("rows", 0)}
                              for r in c.S["jobs"] if r.get("sp") == n and r.get("status") == "approved"],
            "store": {t: stored.get(t, 0) for t in RF.stored_types()},
            "counters": c.counters_of(n),
        })
    return out


def ask_view(c: Any) -> dict[str, Any] | None:
    a = c.S.get("ask")
    if a is None:
        return None
    nxt = c.ask_next()
    return {
        "sponsor": a["sp"], "type": a["type"], "file": a["file"], "cov": a["cov"], "fund": a["fund"], "ents": a["ents"],
        "entities": c.ents_of(a["sp"]), "need": c.ask_need(a), "blocked": c.ask_block(a), "ready": c.ask_ready(a),
        "route": c.route(a)[0] if a["type"] else "",
        "template": (RF.template_stem(a["type"]) + ".xlsx") if a["type"] else "",
        "messages": [{"who": m["who"], "text": m["text"]} for m in a["msgs"]], "next": nxt,
    }


def workspace_view(c: Any) -> dict[str, Any]:
    """The sponsor workspace: lifecycle, the entity's job tiles, the per-entity sign-off chain."""
    sp = c.S["sp"]
    ents = c.ents_of(sp)
    fund = c.S["fund"] if c.S["fund"] in ents else (ents[0] if ents else "")
    em = c.S["EM"]
    done = c.static_done(fund, sp) if fund else 0

    def count(pred: Any) -> int:
        return sum(1 for n in ents if pred(c.S["F"][n]))

    tiles = []
    for t in RF.types():
        gl = RF.is_gl(t)
        ok = bool(c.S["F"][fund]["gl"].get(t)) if gl and fund else c.static_ok(t, sp, fund) if fund else False
        tiles.append({"type": t, "gl": gl, "approved": ok, "locked": gl and done < 5, "static_done": done})
    chain = []
    for n in ents:
        f = c.S["F"][n]
        sd = c.static_done(n, sp)
        gd = f["gl"]["GL Fund"] and f["gl"]["GL LP"]
        st = [("Static", f["signed"]["static"], sd == 5, f"{sd}/5 approved", "signstatic", "Sign off Static"),
              ("GL", f["signed"]["gl"], f["signed"]["static"] and gd,
               f"{f['gl']['GL Fund'] + f['gl']['GL LP']}/2 jobs approved", "signgl", "Sign off GL"),
              ("Bundle", f["bundle"], f["signed"]["gl"], "Bundle ready" if f["bundle"] else "Not generated", "bundle",
               "Generate bundle"),
              ("Client services", f["client"], f["bundle"], {1: "Client approved", -1: "Changes requested"}
               .get(f["client"], "Awaiting"), "client", "Send to client services"),
              ("Migrated", f["migrated"], f["client"] == 1, "Migrated to Ledgerline" if f["migrated"]
               else "Not uploaded", "migrate", "Mark uploaded")]
        cur = next((i for i, x in enumerate(st) if not x[1]), -1)
        chain.append({"entity": n, "id": em.get(n, {}).get("id", ""), "live": em.get(n, {}).get("live", ""),
                      "group": em.get(n, {}).get("gn", ""), "migrated": bool(f["migrated"]), "current": cur,
                      "stages": [{"name": x[0], "state": x[1], "open": bool(x[2]), "text": x[3], "action": x[4],
                                  "label": x[5]} for x in st]})
    return {
        "entity": fund, "static_done": done, "entities": [
            {"name": n, "id": em.get(n, {}).get("id", ""), "live": em.get(n, {}).get("live", ""),
             "group": em.get(n, {}).get("gn", ""), "static_done": c.static_done(n, sp)} for n in ents],
        "lifecycle": [
            {"name": "Onboarded", "text": "Sponsor configured", "state": "done"},
            {"name": "Entities", "state": "done", "text": f"{len(ents)} entities \u00b7 "
             f"{len({em[n].get('gid') for n in ents if em.get(n, {}).get('gid')})} fund group(s) \u00b7 "
             f"{len({em.get(n, {}).get('live', '-') for n in ents})} go-live date(s)"},
            {"name": "Static signed off", "text": f"{count(lambda f: f['signed']['static'])} / {len(ents)} entities",
             "state": "now" if done == 5 else ""},
            {"name": "GL signed off", "text": f"{count(lambda f: f['signed']['gl'])} / {len(ents)} entities", "state": ""},
            {"name": "Migration complete", "text": f"{count(lambda f: f['migrated'])} / {len(ents)} entities",
             "state": ""}],
        "tiles": tiles, "chain": chain, "tab": c.S.get("tab", "jobs"),
        "activity": [a for a in c.S["audit"] if a.get("sp") == sp][-60:],
    }


def state_view(c: Any, health: dict[str, Any] | None = None) -> dict[str, Any]:
    from worker.console_pages import pages_view

    j = c.job()
    return {
        "pages": pages_view(c, health),
        "view": c.S.get("view", "dash"), "workspace": workspace_view(c),
        "me": signed_in(), "sponsor": c.S["sp"], "provider": c.S["prov"], "sponsors": sponsors_view(c),
        "types": RF.types(),
        "ask": ask_view(c), "job": job_view(c, j) if j else None, "toast": c.S.get("toast"),
        "audit": c.S["audit"][-30:],
        "jobs": [{"id": r["id"], "sp": r["sp"], "type": r["type"], "fund": r["fund"], "status": r["status"],
                  "rows": r.get("rows", 0), "t": r.get("t", "")}
                 for r in c.S["jobs"] if r.get("sp") == c.S["sp"]],
    }
