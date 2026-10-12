"""What the job screen shows, computed by code from a job: the suggestion for each row, where it is in the source,
find, trace, sample, output facts and the timeline. No HTML here: structures the page renders."""

from __future__ import annotations

import math
import re
from typing import Any

from worker import console_demo as D
from worker import console_engine as CE
from worker import console_idgen as CG
from worker import engine_console as EC
from worker import record_facts as RF
from worker import templates as T

MAX_RANGES = 8


def _money(n: float) -> str:
    return f"{n:,.2f}"


def parse_focus(j: dict[str, Any], text: str) -> dict[str, str]:
    """What the worker heard in "show errors in Fund B for harbor", as structured filters."""
    q = f" {text} "
    out: dict[str, str] = {}
    for key, rx in (("err", r"\b(needs? you|errors?|problems?)\b"), ("warn", r"\b(checks?|warnings?)\b"),
                    ("skip", r"\b(left out|skipped|excluded)\b"), ("ok", r"\b(written|ready)\b")):
        if "st" not in out and re.search(rx, q, re.IGNORECASE):
            out["st"] = key
            q = re.sub(rx, " ", q, flags=re.IGNORECASE)
    for ent in j.get("ents") or []:
        rx = rf"\b{re.escape(ent)}\b"
        if re.search(rx, q, re.IGNORECASE):
            out["ent"] = ent
            q = re.sub(rx, " ", q, flags=re.IGNORECASE)
    q = re.sub(r"\b(rows?|records?|lines?|only|the|with|that|in|for|all|show|me|from)\b", " ", q, flags=re.IGNORECASE)
    out["q"] = re.sub(r"\s+", " ", q).strip().strip("\"“”'")
    return out


def _uniq(values: list[str]) -> list[str]:
    """Distinct values, numbers first and in numeric order: the order a person reads them in a filter box."""
    return sorted(dict.fromkeys(values), key=lambda x: (not str(x).isdigit(), int(x) if str(x).isdigit() else 0, str(x)))


# ---- suggestions ----------------------------------------------------------------------------------------
def sugg(j: dict[str, Any], r: dict[str, Any]) -> dict[str, Any] | None:
    f, w = r["fl"], r["why"]
    if r["st"] == "warn" and "confirm" in r["act"]:
        return {"t": f"It is the same investor as {r['hit']}. Link them.", "c": f"fz|{r['i']}",
                "d": "Already in Ledgerline? · fuzzy name, you confirm",
                "L": f"“{r['nm']}” is the same investor as {r['hit']}"}
    if r["st"] != "err":
        return None
    if "similar" in f:
        return {"t": f"Keep row 1 “{re.sub(r'^row [0-9]+ . ', '', r['hit'])}” and leave this one out",
                "c": f"dec|{r['i']}|exclude as duplicate of row 1", "d": "Duplicate in the file · punctuation only",
                "L": "When two affiliates differ only in punctuation, keep the first"}
    if "glOnly" in f:
        return {"t": "Add it as a new investment, named as in the GL", "c": f"dec|{r['i']}|corrected: added from the GL",
                "d": "GL-only investment", "L": "An investment found only in the GL is added with its GL name"}
    if r.get("failed"):  # a value the sponsor's recipe cannot read: never passed on, a person decides
        fd = r["failed"][0]["field"]
        return {"t": f"Leave it out until {fd} is fixed in the source, or type the value in",
                "c": f"dec|{r['i']}|exclude as incorrect ({fd} not readable)", "d": f"Value not readable · {fd}",
                "L": f"Rows whose {fd} cannot be read are left out until the client fixes it"}
    m = re.match(r"^(\w+) is blank$", w)
    if m:
        fd = m.group(1)
        d = f"Required field blank · {fd}"
        if fd == "Commitment_Date":
            return {"t": "Use 2025-06-30, the closing date of the other rows in this batch", "c": f"date|{r['i']}",
                    "d": d, "L": "When a commitment has no date, use the closing date of the other rows in its batch"}
        return {"t": f"Leave it out until the client supplies {fd}, or type it in",
                "c": f"dec|{r['i']}|exclude as incorrect ({fd} blank)", "d": d,
                "L": f"Rows with {fd} blank are left out until the client supplies it"}
    if "depMissing" in f:
        return {"t": f"Add {r['id']} {r['nm']} as an investor already in Ledgerline",
                "c": f"qa|{r['i']}|{r['id']}|{r['nm']}", "d": "Dependency: a commitment needs its investor"}
    if "badChars" in f:
        v = "V_" + re.sub(r"[^0-9]", "", r["id"])
        return {"t": f"Clean the ID to {v} (letters, digits and _ only)", "c": f"dec|{r['i']}|corrected ID to {v}",
                "L": "Vendor IDs keep only letters, digits and _; prefix V_ when the ID is numeric"}
    if "collideGen" in f:
        words = [w for w in (re.sub(r"[^A-Za-z0-9]", "", x) for x in r["nm"].split()) if w]
        v = "".join(words[:2]).upper()
        return {"t": f"Use {v} for this one, from the first two words", "c": f"dec|{r['i']}|corrected ID to {v}",
                "d": "Vendor class and ID · row 1",
                "L": "When two companies share a first word, the second ID uses the first two words"}
    if "after cutting" in w:
        return {"t": f"Make this one {r['nid']}-2", "c": f"dec|{r['i']}|corrected ID to {r['nid']}-2"}
    if re.match(r"^(ID|Source key) is \d+ characters", w):
        p = (re.match(r"^[A-Z]+-[A-Z]+-", r["id"]) or [""])[0] if re.match(r"^[A-Z]+-[A-Z]+-", r["id"]) else ""
        if p:
            return {"t": f"Drop “{p}” from every long ID like this one", "c": "rule|drop",
                    "d": "ID too long · row 3", "L": "Drop the leading prefix from IDs over the limit"}
        return {"t": "Correct the ID", "c": f"dec|{r['i']}|corrected"}
    if "dup" in r["act"]:
        row = 2 if re.search(r"same key", w, re.IGNORECASE) else 3
        return {"t": f"Leave it out. It is {r['hit']}, already in Ledgerline", "c": f"dec|{r['i']}|exclude as duplicate "
                f"of {r['hit']}", "d": f"Already in Ledgerline? · row {row}"}
    return None


def gl_items(c: Any, j: dict[str, Any]) -> list[dict[str, Any]]:
    e = CE.gl_eval(c, j)
    out = []
    for idx, x in enumerate(e["lines"]):
        if x["st"] != "err":
            continue
        ln = x["l"]
        err = x["errs"][0]
        a = err["a"]
        s: dict[str, Any] | None = None
        alt = ""
        if a[0] == "quick":
            s = {"t": f"Add “{a[2]}” as a {a[1][:-1].lower()} already in Ledgerline",
                 "c": f"quick|{a[1]}|{a[2]}", "d": "Crosswalk · not onboarded in the app, so loaded outside it"}
        elif a[0] == "acct" and a[1] in D.SUGA:
            s = {"t": f"Map {a[1]} {ln.an} to {D.SUGA[a[1]]}", "c": f"acctset|{a[1]}|{D.SUGA[a[1]]}",
                 "d": "Account match · L3 Hybrid, score 0.88", "v": "2 of 3 votes agree · your call",
                 "pick": a[1], "L": f"Source account {a[1]} “{ln.an}” maps to {D.SUGA[a[1]]}"}
        elif a[0] == "deal":
            s = {"t": "Ask the client for the deal names and leave these journals out until then",
                 "c": f"dec|{ln.j}|exclude as incorrect (deal name blank)", "d": "Deal name blank · DEPT_ID required",
                 "L": "GL lines without a deal on 1500s, 1600s, 8050s and 9000s are left out until the client names "
                      "the deal"}
        elif a[0] == "jr":
            s = {"t": f"Leave journal {a[1]} out and ask the client about the difference",
                 "c": f"dec|{a[1]}|exclude as incorrect (out of balance)", "d": "Journal balance · row 2",
                 "L": "Leave out journals that do not balance and ask the client"}
            alt = f"dec|{a[1]}|corrected"
        if s is None:
            s = {"t": f"Leave journal {ln.j} out and fix it in the source",
                 "c": f"dec|{ln.j}|exclude as incorrect", "d": err["m"].split(" “")[0]}
        out.append({"name": f"{ln.j} line {ln.ln}", "id": f"{ln.acct} {ln.an}", "why": err["m"], "s": s, "alt": alt,
                    "err": True, "amt": max(ln.dr, ln.cr), "l": ln, "row": x["row"], "a0": a[0]})
    return out


# ---- where a row is in the source file -------------------------------------------------------------------
def col_l(n: int) -> str:
    return EC.col_letter(n)


def row_ents(j: dict[str, Any], r: dict[str, Any]) -> list[str]:
    ents = j.get("ents") or [j["fund"]]
    mine = [e for e in r.get("en", []) if e in ents]
    return mine or [ents[(r["i"] - 1) % len(ents)]]


def src_loc(j: dict[str, Any], group: list[dict[str, Any]]) -> dict[str, Any]:
    sheet = CE.sheet_facts(j)
    rows = sorted(y["row"] for y in group)
    pr = EC.ranges_of(rows)
    rng = f"{sheet[0]}!A{rows[0]}:{col_l(sheet[2])}{rows[-1]}"
    text = f"{', '.join(pr[:MAX_RANGES])} and {len(pr) - MAX_RANGES} more ranges" if len(pr) > MAX_RANGES \
        else ", ".join(pr)
    return {"file": j["up"]["file"], "sheet": sheet[0], "rows": text, "row_list": rows, "single": len(pr) == 1,
            "range": rng if len(pr) == 1 else "", "copy": f"{j['up']['file']} {rng}" if len(pr) == 1 else "",
            "label": f"{j['up']['file']} · sheet {sheet[0]}, {'rows' if len(rows) > 1 else 'row'} {text}"
                     + (f" · {rng}" if len(pr) == 1 else "")}


def _field_src(j: dict[str, Any], name: str) -> str:
    f = next((x for x in CE.fields_of(j) if x[0] == name), None)
    return f[1] if f else ""


def xl_filter(j: dict[str, Any], g: list[dict[str, Any]]) -> dict[str, Any]:
    y = g[0]
    uniq = _uniq
    if "l" in y:
        a = (y["s"].get("c") or "").split("|")
        L = lambda k: col_l(D.GLSRC.index(k) + 1)
        if all(z["l"].noDeal for z in g):
            return {"conds": [{"col": "Investment", "letter": L("Investment"), "blank": True},
                              {"col": "GL Account", "letter": L("GL Account"), "vals": uniq([z["l"].acct for z in g])}],
                    "by": "GL Account", "key": lambda z: z["l"].acct}
        if a[0] == "quick":
            c = {"Investors": "Investor", "Vendors": "Vendor", "Affiliates": "Affiliate",
                 "Investments": "Investment"}[a[1]]
            return {"conds": [{"col": c, "letter": L(c), "vals": [a[2]]}]}
        if re.match(r"^Account ", y["why"]):
            return {"conds": [{"col": "GL Account", "letter": L("GL Account"), "vals": uniq([z["l"].acct for z in g])}]}
        if re.match(r"^Transaction type", y["why"]):
            return {"conds": [{"col": "Transaction Type", "letter": L("Transaction Type"),
                               "vals": uniq([z["l"].tt for z in g])}]}
        return {"conds": [{"col": "Journal", "letter": L("Journal"), "vals": uniq([z["l"].j for z in g])}]}
    cols = CE.source_columns(j) or D.SRCCOLS.get(j["type"], [])
    m = re.match(r"^(\w+) is blank$", y["why"])
    if m:
        col = _field_src(j, m.group(1))
        if not col:
            return {"conds": [], "none": m.group(1)}
        multi = len(j.get("ents") or []) > 1
        return {"conds": [{"col": col, "letter": col_l(cols.index(col) + 1) if col in cols else "", "blank": True}],
                "by": "Entity" if multi else None, "key": lambda z: row_ents(j, z["r"])[0]}
    by_name = bool(re.search(r"same name", y["why"], re.IGNORECASE)) or not y["r"]["id"]
    col = cols[1 if by_name else 0]
    return {"conds": [{"col": col, "letter": col_l(cols.index(col) + 1),
                       "vals": uniq([z["r"]["nm"] if by_name else z["r"]["id"] for z in g])}]}


def xl_how(j: dict[str, Any], g: list[dict[str, Any]], units: str) -> dict[str, Any]:
    x = xl_filter(j, g)
    if x.get("none"):
        return {"none": x["none"], "text": f"This file has no “{x['none']}” column, so every row is blank. "
                "Add the column, pick another one under Comes from in the plan, or untick Required?."}
    n = len(g)
    unit = units[:-1] if n == 1 else units

    def cv(c: dict[str, Any]) -> str:
        if c.get("blank"):
            return "= (Blanks)"
        v = c["vals"]
        return "= " + " or ".join(v) if len(v) <= 3 else f"= {', '.join(v[:3])} and {len(v) - 3} more"

    conds = ", and ".join(f"column {c['letter']} “{c['col']}” {cv(c)}" for c in x["conds"])
    vc = [c for c in x["conds"] if c.get("vals")]
    out: dict[str, Any] = {
        "text": f"In Excel: Data → Filter, {conds} → {n:,} {unit}",
        "conds": x["conds"], "copy_values": vc[-1]["vals"] if vc else [], "count": n, "breakdown": None,
    }
    if x.get("by"):
        m: dict[str, list[int]] = {}
        for z in g:
            m.setdefault(x["key"](z), []).append(z["row"])
        keys = sorted(m, key=lambda k: -len(m[k]))
        if len(keys) > 1:
            out["breakdown_by"] = x["by"]
            out["breakdown"] = []
            for k in keys:
                rs = sorted(m[k])
                pr = EC.ranges_of(rs)
                f = "; ".join(f"{c['col']} = {k if c['col'] == x['by'] else '(Blanks)' if c.get('blank') else ', '.join(c['vals'])}"
                              for c in x["conds"])
                out["breakdown"].append({"value": k, "count": len(rs), "rows": pr, "rows_text":
                                         (", ".join(pr[:4]) + f" and {len(pr) - 4} more") if len(pr) > 4 else ", ".join(pr),
                                         "copy_filter": f})
    return out


# ---- find, trace, sample ---------------------------------------------------------------------------------
def count_found(c: Any, j: dict[str, Any], q: str, st: str = "", ent: str = "") -> int:
    """How many rows match "find X": the source's rows (by status and entity) and, with no filter, the Ledgerline
    records the worker has read."""
    low = (q or "").lower()
    hit = lambda *vals: any(low in str(v or "").lower() for v in vals)
    ran = bool(j["runs"])
    keep = lambda s, ents=None: (not st or st == s) and (not ent or not ents or ent in ents)
    if RF.is_gl(j["type"]):
        return sum(1 for x in CE.gl_eval(c, j)["lines"]
                   if hit(x["l"].j, x["l"].acct, x["l"].an, x["l"].tt, x["l"].desc, x["l"].inv, x["l"].ven, x["l"].aff,
                          x["l"].invst) and keep(x["st"] if ran else ""))
    n = sum(1 for r in CE.static_rows(c, j)
            if hit(r["id"], r["nm"], r["x"], r["hit"]) and keep(r["st"] if ran else "", row_ents(j, r)))
    if not st and not ent:
        n += sum(1 for x in c.reg_of(j["sp"]).get(j["type"], []) if hit(x["id"], x["key"], x["name"]))
    return n


def trace(c: Any, j: dict[str, Any], i: int) -> dict[str, Any] | None:
    if RF.is_gl(j["type"]) or not j["runs"]:
        return None
    r = next((x for x in CE.static_rows(c, j) if x["i"] == i), None)
    if r is None:
        return None
    ids = CE.out_ids(c, j)[0]
    nm_f = next((f for f in CE.fields_of(j) if f[0].endswith("_Name")), ["Name", "name"])
    d = j["dec"].get(str(r["i"]))
    generated = j["type"] in RF.gen_types() or not r["nid"]
    return {
        "output": ids.get(r["i"], ""),
        "name": r["nm"],
        "source": f"{j['up']['file']} · sheet {CE.sheet_facts(j)[0]} · row {r['row']}",
        "id": (f"{ids.get(r['i'], 'not written')} · new from {j['sp']}'s counter (generate-ids@2)" if generated
               else f"{ids.get(r['i'], 'not written')} · " + (f"corrected from {r['id']}" if r["nid"] != r["id"]
                                                                  else "as supplied in the source")),
        "name_rule": f"{nm_f[0]} ← column “{nm_f[1]}”"
                     + (f", cut to {j['rules']['nameMax']} characters" if len(r["nm"]) > j["rules"]["nameMax"] else "")
                     + " · copied by code",
        "checks": "; ".join(r["flags"]) if r["flags"] else "none raised",
        "decision": f"you: {d}" if d else "none needed",
        "model_calls": "none · copied by code",
        "approval": "approved by you" if j["approved"] else "waiting for your approval",
    }


def sample_rows(c: Any, j: dict[str, Any]) -> dict[str, Any]:
    """The spot-check: sized for 95% confidence (+-5%), all rows when small, spread evenly, copied by code."""
    if RF.is_gl(j["type"]):
        return {"k": 0, "n": 0, "rows": [], "note": ""}
    o = [r for r in CE.static_rows(c, j) if r["st"] in ("ok", "warn")]
    n = len(o)
    if not n:
        return {"k": 0, "n": 0, "rows": [], "note": ""}
    k = min(n, math.ceil(n / (1 + n * 0.0025)))
    ids = CE.out_ids(c, j)[0]
    pick = [o[(i * n) // k] for i in range(k)]
    return {"k": k, "n": n, "all": k == n,
            "note": f"{k} of {n} written rows, sized for 95% confidence (±5%)" + (", so every row" if k == n else "") + ".",
            "rows": [{"row": r["row"], "source": f"{r['id'] or '(blank)'} · {r['nm']}", "output": ids[r["i"]],
                      "name": r["nm"][: j["rules"]["nameMax"]], "i": r["i"]} for r in pick]}


# ---- plan, output facts, timeline ------------------------------------------------------------------------
def plan_lines(c: Any, j: dict[str, Any]) -> list[list[Any]]:
    typ, sp = j["type"], j["sp"]
    w = c.world(sp)
    r = j["rules"]
    fund_id = c.entity_id(j["fund"])
    p = [[x.replace("5317401", fund_id).replace("Fund A", j["fund"]).replace("Sponsor A", sp), "process flow"]
         for x in D.PLAN.get(typ, [])]
    if RF.is_gl(typ):
        e = CE.gl_eval(c, j)
        accts = list(dict.fromkeys(x["l"].acct for x in e["lines"]))
        ok = sum(1 for a in accts if w.known_account(a, "") or any(z[0] == a and z[2] for z in D.ACCTS) or a in j["acct"])
        tts = list(dict.fromkeys(x["l"].tt for x in e["lines"]))
        refs = list(dict.fromkeys(v for x in e["lines"] for _k, v in EC.gl_refs(x["l"])))
        return [[(f"{len(e['lines'])} lines use only {len(accts)} distinct accounts, {len(tts)} transaction types and "
                 f"{len(refs)} names. You review those once each, never line by line"), "distinct values"],
                [f"Source accounts → Golden COA: {ok} of {len(accts)} matched before for {sp}", "Golden COA"],
                ["Transaction types → the standard mapping", "standard"],
                [f"Investor, vendor, affiliate and investment names → their new IDs in {sp}'s records", "crosswalk"],
                [f"A journal may be off by up to {_money(r['tolPass'])}" if r["tolPass"]
                 else "Every journal balances to zero", "standard"], *p]
    n = c.counter_of(sp, typ)
    out: list[list[Any]] = []
    for f in CE.fields_of(j):
        if f[1] == "generated":
            out.append([f"{f[0]} = new ID from {sp}'s counter", "sponsor rule", "generated"])
        elif f[1]:
            if f[1].startswith("constant"):
                out.append([f"{f[0]} = “{f[1][10:]}” on every row", "sponsor setup"])
            else:
                out.append([f"{f[0]} ← column “{f[1]}”",
                            "from the file" if f[3] >= 95 else f"from the file · {f[3]}% sure"])
        else:
            out.append([f"{f[0]}: not in the file, left blank and reported", "never invented"])
    if typ == "Investors":
        out.append(["Investor_Type: each distinct source value classified once by the Investor class decision table",
                    "decision model"])
    if typ == "Vendors":
        out.append([("Vendor_Type and generated IDs: each distinct name classified once by the Vendor class decision "
                    "table"), "decision model"])
    out.append([CG.id_text(c, sp, typ, True) if typ in RF.gen_types()
                else f"Source IDs used as supplied; a new ID from {RF.id_fmt(typ, n)} only where the source has none",
                "sponsor rule"])
    out.append([f"Records already in Ledgerline for {sp} are left out, each with its reason", "standard"])
    out.append([{"error": f"IDs over {RF.id_limit(typ)} characters are flagged, never cut",
                 "truncate": f"IDs over {RF.id_limit(typ)} characters are cut",
                 "drop": "Long ID prefixes are dropped"}[r["id"]], "standard" if r["id"] == "error" else "your rule"])
    if r["nameMax"] != EC.default_rules()["nameMax"]:
        out.append([f"Names cut at {r['nameMax']} characters", "your rule"])
    if r["suffix"]:
        out.append([("“Ltd” and “Limited” count as the same name"), "your rule"])
    return out + p


def output_facts(c: Any, j: dict[str, Any]) -> dict[str, Any]:
    t = CE.tally(c, j)
    typ = j["type"]
    name = (j.get("out") or {}).get("file") or T.file_name(j)
    if RF.is_gl(typ):
        e = CE.gl_eval(c, j)
        ls = [x for x in e["lines"] if x["st"] != "skip"]
        dr, cr = sum(x["l"].dr for x in ls), sum(x["l"].cr for x in ls)
        inc = [x for x in e["js"] if not str(j["dec"].get(x["j"], "")).startswith("exclude")]
        bal = sum(1 for x in inc if x["st"] != "err")
        corr = len(inc) - bal
        facts = (f"{t['read']} lines read · {t['out']} written{' incl. offsets' if e['offsets'] else ''} · "
                 f"{t['skip']} left out · {bal}/{len(inc)} journals balanced"
                 + (f" · {corr} marked corrected in the source, re-read on the next run" if corr else "")
                 + f" · debits {_money(dr)} · credits {_money(cr)}")
        ties = True
    else:
        ties = t["read"] == t["out"] + t["skip"]
        facts = f"{t['read']} read · {t['out']} written · {t['skip']} left out · " \
                f"{'ties' if ties else 'does not tie'}"
    return {"name": name, "facts": facts, "ties": ties, "tally": t}


def timeline(c: Any, j: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Read -> Map -> Convert -> Check -> Write. state: 0 to do, 1 done, 2 waiting on you, 3 stopped."""
    if j is None:
        a = c.S.get("ask")
        return [{"stage": "Read", "state": 0, "fact": "file in, read on Start" if a and a.get("file")
                 else "waiting for the file"}] + [{"stage": s, "state": 0, "fact": ""}
                                                  for s in ("Map", "Convert", "Check", "Write")]
    last = len(j["runs"])
    t = CE.tally(c, j) if last else None
    gl = RF.is_gl(j["type"])
    u = "lines" if gl else "rows"
    rows = CE.sheet_facts(j)[1]
    nf = len(D.GLSRC) if gl else len(D.plan(j["type"]))
    by = len(j["map"])
    ep = c.ex_pending(j)
    return [
        {"stage": "Read", "state": 1, "fact": f"{rows} {u}"},
        {"stage": "Map", "state": 2 if ep or not last else 1,
         "fact": "a question for you" if ep else (f"{nf} {'keys' if gl else 'fields'}" + (f" · {by} by you" if by else ""))
         if last else "plan ready · your OK"},
        {"stage": "Convert", "state": 1 if last or j["failed"] else 0, "fact": f"run {last}" if last else ""},
        {"stage": "Check", "state": 3 if j["failed"] else (2 if t["err"] else 1) if last else 0,
         "fact": "stopped, saved" if j["failed"] else (f"{t['err']} need you" if t["err"]
                                                      else f"ties · {t['skip']} left out") if last else ""},
        {"stage": "Write", "state": 1 if j["approved"] else 2 if last and not t["err"] and not j["failed"] else 0,
         "fact": f"{j['written']} written" if j["approved"] else "your approval" if last and not t["err"]
         and not j["failed"] else ""},
    ]
