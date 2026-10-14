"""Questions about an unknown column or format, and the person's answers (migration M24a).

The target is the fixed contract; the source is open. When a file's column or format is not known, the console asks,
the person decides, and the decision is recorded and learned:

* **unknown_field**: a target field no column feeds. Options are the file's own columns that read like the field
  (header closeness, by code), each with its parse variants, plus "not in this file, left blank and reported".
* **unknown_format**: a mapped column whose values need a parse (a date not written YYYY-MM-DD, a number written with
  separators). Day-first against month-first, and the decimal mark, are the person's choice: code shows the counts
  and never recommends one.

Every option is one step of the closed vocabulary (`worker.transforms`) with a preview computed by code on every row
(`conform/total`) and up to five failing examples. At most four options; the scopes offered are this job and this
sponsor, never the organisation. A second sponsor that has the same header and value shape sees what other sponsors
decided as ONE option (`proposed_by: learned`); nothing is applied without the person's answer.

An answer is validated through the recipe schema and the transforms, written to the job's audit and the sponsor's
feedback log (a `field_decision`), then, at sponsor scope, to the sponsor's recipe (`recipe.changed` audited first).
The answerer is a named person: an agent, a role or a reserved name is refused.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from worker import console_demo as D
from worker import console_engine as CE
from worker import engine_console as EC
from worker import feedback as F
from worker import learnindex as L
from worker import persist, roles
from worker import recipes as R
from worker import record_facts as RF
from worker import setrecipe as SR
from worker import templates as T
from worker import transforms as TF
from worker.audit import AuditError
from worker.contracts import ContractError, Option, Preview, Question, Subject, is_agent_principal
from worker.hashing import sha256_file

SCOPES = ("job", "sponsor")  # the organisation is never offered: that is the improvement loop's, through a human gate
NEAR = 0.7  # how closely a header must read like the field (or a known synonym) to be offered as its column
MAX_COLUMNS = 2  # candidate columns offered per field
MAX_SHOWN = 3  # options other than "not in this file" (a question holds at most four)
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PLAIN = re.compile(r"^[+-]?\d+(\.\d+)?$")
_FEEDBACK_AS = "chosen in the plan table"


def _err(message: str) -> Exception:
    from worker.console import ConsoleError

    return ConsoleError(message)


# ---- the source ---------------------------------------------------------------------------------------------
_ROWS: dict[str, tuple[tuple[int, int], list[TF.Row]]] = {}


def _table(j: dict[str, Any]) -> tuple[str, list[str], list[dict[str, Any]], list[TF.Row]] | None:
    """(sheet, header, records, rows) of a static job's source, or None when it cannot be read."""
    path = (j.get("up") or {}).get("path")
    if not path or RF.is_gl(str(j["type"])):
        return None
    try:
        sheet, header, recs = CE.source_table(path)
        st = Path(path).stat()
    except (OSError, ValueError, KeyError):
        return None
    sig = (st.st_mtime_ns, st.st_size)
    hit = _ROWS.get(str(path))
    if hit is None or hit[0] != sig:
        rows = [TF.Row(f"{sheet}!{r['row']}", tuple(r["v"]), tuple(r["v"].values())) for r in recs]
        hit = _ROWS[str(path)] = (sig, rows)
    return sheet, header, recs, hit[1]


def _letter(header: list[str], name: str) -> str:
    return EC.col_letter(header.index(name) + 1) if name in header else ""


def _values(recs: list[dict[str, Any]], header: str) -> list[Any]:
    return [r["v"].get(header) for r in recs]


def _context(c: Any, j: dict[str, Any]) -> TF.Context:
    """The pinned things a recipe may read besides its row: setup values and the sponsor's existing records."""
    slug = T.sponsor_slug(j["sp"])
    known = {t: [{k: str(v) for k, v in x.items() if k in ("id", "key", "name")}
                 for x in EC.known_view(list(c.registry(j["sp"]).entries(t)))] for t in RF.stored_types()}
    return TF.Context(setup={"gp_id": slug, "fund_name": str(j["fund"]), "entity_id": c.entity_id(j["fund"])},
                      records=known, records_checksums={t: hashlib.sha256(TF.canonical(v).encode()).hexdigest()
                                                        for t, v in known.items()})


def _rule(c: Any, j: dict[str, Any], f: list[Any]) -> TF.FieldRule:
    return replace(TF.rule_for(f[0]), required=bool(c.req_of(j["type"], f, j["sp"])))


# ---- what needs a decision -----------------------------------------------------------------------------------
def _needs_parse(kind: str, values: list[Any]) -> str:
    """"date" or "number" when the column holds text a field of that kind cannot take as it is, else ""."""
    if kind not in ("date", "amount"):
        return ""
    for v in values:
        if not isinstance(v, str):  # a number or a date cell is already typed; only text needs a decision
            continue
        e = TF.enter(v)  # the reader's cleaning: outer spaces, and n/a, -, tbd as blank
        if not isinstance(e, str):
            continue
        if kind == "date" and not _ISO.match(e):
            return "date"
        if kind == "amount" and not _PLAIN.match(e):
            return "number"
    return ""


def _candidates(name: str, header: list[str], used: set[str], words: list[str]) -> list[str]:
    """The file's columns, not used by another field, that read like this field: closest first."""
    want = {L.header_norm(name.replace("_", " ")), *(L.header_norm(w) for w in words)}
    want.discard("")
    scored: list[tuple[float, int, str]] = []
    for pos, h in enumerate(header):
        if not h or h in used:
            continue
        hn = L.header_norm(h)
        if not hn:
            continue
        s = max((difflib.SequenceMatcher(None, hn, w).ratio() for w in want), default=0.0)
        if any(len(w) >= 4 and (w in hn or hn in w) for w in want):
            s = max(s, 0.9)
        if s >= NEAR:
            scored.append((-s, pos, h))
    return [h for _s, _p, h in sorted(scored)]


def _shape(step: dict[str, Any], verdict: TF.Verdict, person: str | None, sheet: str, letter: str,
           note: str = "") -> tuple[Preview, dict[str, Any]]:
    payload: dict[str, Any] = {"from": step["from"], "ops": list(step.get("ops") or []), "column": letter}
    if person:  # the person's choice: counts are evidence, never a recommendation
        payload["person_choice"] = person
        payload["evidence"] = {"conform": verdict.conform, "total": verdict.total}
        return Preview(0, 0), payload
    return Preview(verdict.conform, verdict.total, f"{sheet}!{letter}" if letter else None), payload


def _options_for(name: str, rule: TF.FieldRule, header_text: str, letter: str, sheet: str, values: list[Any],
                 rows: list[TF.Row]) -> list[tuple[Option, TF.Verdict]]:
    """The ways to fill `name` from one column (code's, at most three), each with its verdict on every row."""
    out: list[tuple[Option, TF.Verdict]] = []
    # a column of real numbers has no decimal mark to choose: the cells are already numbers
    typed_only = rule.kind == "amount" and not any(isinstance(v, str) and v.strip() for v in values)
    for o in roles.binding_options(name, rule, header_text, letter or "col", values):
        if o.action == "declare_missing":
            continue
        ops = list(o.payload.get("recipe") or [])
        person = o.payload.get("person_choice")
        if typed_only and person:
            if out:
                continue
            person = None
        step: dict[str, Any] = {"from": header_text, **({"ops": ops} if ops else {})}
        try:
            verdict = SR.verify({"fields": {name: step}}, rows, {name: rule})[name]
        except TF.RecipeError:
            continue
        preview, payload = _shape(step, verdict, person, sheet, letter)
        payload["field"] = name
        out.append((Option(option_id=o.option_id, action=o.action, proposed_by="mapper", preview=preview,
                           payload=payload, note=""), verdict))
    return out


# ---- the learned suggestion ----------------------------------------------------------------------------------
_INDEX: dict[str, Any] = {"sig": None, "entries": []}


def _index(c: Any) -> list[dict[str, Any]] | None:
    """What other sponsors decided, derived by code from the sponsors' feedback logs; None without a learning key
    (sponsor counts are never kept as names, so nothing is derived)."""
    try:
        key = L.learning_key(getattr(c, "learning_key", None))
    except L.LearningError:
        return None
    root = Path(c.root) / "feedback"
    sponsors = persist.feedback_sponsors(root)
    if not sponsors:
        return []
    sig = (key, persist.feedback_signature(root))
    if _INDEX["sig"] != sig:
        by, undone = {}, {}
        for sponsor in sponsors:
            store = persist.feedback(root, sponsor)
            by[sponsor] = store.list(kind=F.FIELD_DECISION)
            undone[sponsor] = store.undone()
        _INDEX.update(sig=sig, entries=L.build_index(by, key=key, undone=undone))
    return list(_INDEX["entries"])


def _learned(c: Any, j: dict[str, Any], name: str, rule: TF.FieldRule, headers: list[str], rows: list[TF.Row],
             sheet: str, header: list[str]) -> Option | None:
    index = _index(c)
    typ = str(j["type"])
    if not index or not any(e["key"]["record_type"] == typ and e["key"]["target_field"] == name for e in index):
        return None
    for h in headers:
        got = L.suggest(index, sponsor=T.sponsor_slug(j["sp"]), record_type=typ, target_field=name, header=h,
                        rows=rows, key=getattr(c, "learning_key", None), target_rules={name: rule},
                        sample_ref=f"{sheet}!{_letter(header, h)}")
        if got:
            o = got[0]
            return replace(o, payload={**dict(o.payload), "field": name, "column": _letter(header, h)})
    return None


# ---- building the questions ----------------------------------------------------------------------------------
_CACHE: dict[str, tuple[str, list[dict[str, Any]]]] = {}


def _signature(c: Any, j: dict[str, Any]) -> str:
    path = Path(str(j["up"]["path"]))
    st = path.stat()
    req = {k: v for k, v in c.S["req"].items() if k.startswith(f"{j['sp']}|{j['type']}|")}
    idx = _index(c)
    return json.dumps([j["id"], st.st_mtime_ns, st.st_size, j.get("map"), j.get("steps"),
                       CE.recipe_doc(c, j)[1], req, sorted(e["suggestion_id"] for e in idx or []),
                       idx is None], sort_keys=True, default=str)


def build(c: Any, j: dict[str, Any]) -> list[dict[str, Any]]:
    """The open questions of a static job, in plan order: dicts of {question (contracts.Question), field, header,
    rule, columns, examples, evidence, text}. Computed by code on every row; nothing is written."""
    if j.get("approved") or RF.is_gl(str(j["type"])) or not (j.get("up") or {}).get("path"):
        return []
    try:
        sig = _signature(c, j)
    except (OSError, CE.EvaluationError):
        return []
    hit = _CACHE.get(str(j["id"]))
    if hit is not None and hit[0] == sig:
        return hit[1]
    got = _build(c, j)
    _CACHE[str(j["id"])] = (sig, got)
    return got


def _build(c: Any, j: dict[str, Any]) -> list[dict[str, Any]]:
    tab = _table(j)
    if tab is None:
        return []
    sheet, header, recs, rows = tab
    typ, sp = str(j["type"]), j["sp"]
    slug = T.sponsor_slug(sp)
    words = EC.Words(slug)
    decided = {k for k, v in (CE.fieldrecipe_view(c, j)).items()}
    colmap = dict(j.get("map") or {})
    plan = D.plan(typ)
    bound: dict[str, str] = {}
    for f in plan:
        src = str(f[1] or "")
        if f[0] in decided or src == "generated" or src.startswith(("constant", "(")):
            continue
        h = colmap.get(f[0]) if f[0] in colmap else (words.detect(f[0], src, header, own=True) if src else "")
        if h:
            bound[f[0]] = h
    used = set(bound.values())
    syn = EC.synonyms(slug)
    out: list[dict[str, Any]] = []
    for f in plan:
        name, src = f[0], str(f[1] or "")
        if name in decided or src == "generated" or src.startswith(("constant", "(")):
            continue
        if name in colmap and not colmap[name]:
            continue  # the person said: not in this file, left blank and reported
        rule = _rule(c, j, f)
        required = rule.required
        h = bound.get(name, "")
        if h:
            vals = _values(recs, h)
            what = _needs_parse(rule.kind, vals)
            if not what:
                continue
            letter = _letter(header, h)
            opts = _options_for(name, rule, h, letter, sheet, vals, rows)
            kind, text = "unknown_format", (
                f"{name}: the {'dates' if what == 'date' else 'numbers'} in “{h}” are written in a form I will not "
                "guess. Say how to read them" + ("; I show the counts but never choose day or month for you."
                                                 if what == "date" else "; I never guess the decimal mark."))
            evidence = _evidence(what, vals)
        else:
            if typ in RF.id_decision_types() and name == RF.target(typ).key[-1]:
                continue  # no ID in the file: a coworker says so and makes them (the ID card), never "pick a column"
            near = _candidates(name, header, used, syn.get(name, []))
            if not (required or src or near):
                continue  # an optional field nothing feeds: reported MISSING in the plan, not asked
            opts = []
            for cand in near[:MAX_COLUMNS]:
                got = _options_for(name, rule, cand, _letter(header, cand), sheet, _values(recs, cand), rows)
                opts.extend(got if not opts else got[:1])
            kind = "unknown_field"
            what = ""
            text = (f"{name} has no column I can match" + (f" (the plan expected “{src}”)" if src else "") +
                    ". Pick the column it comes from, or leave it blank and reported.")
            if any(o.payload.get("person_choice") for o, _v in opts):
                kind = "unknown_format"
                text = (f"{name}: a column that may hold it is written in a form I will not guess. "
                        "Say how to read it.")
            evidence = {}
            if opts:
                first = str(opts[0][0].payload["from"])
                evidence = _evidence(_needs_parse(rule.kind, _values(recs, first)) or "", _values(recs, first))
        shown = opts[:MAX_SHOWN]
        learned = _learned(c, j, name, rule, [x for x in header if x and (x not in used or x == h)],
                           rows, sheet, header)
        if learned is not None:
            step = {"from": learned.payload["from"], "ops": list(learned.payload.get("ops") or [])}
            try:
                lh = TF.field_hash(name, step, rule)
            except TF.RecipeError:
                lh = ""
            same = []
            for k, (o, _v) in enumerate(shown):
                try:
                    if TF.field_hash(name, {"from": o.payload["from"], **({"ops": o.payload["ops"]}
                                                                         if o.payload.get("ops") else {})}, rule) == lh:
                        same.append(k)
                except TF.RecipeError:
                    continue
            lv = SR.verify({"fields": {name: step}}, rows, {name: rule})[name]
            # day-first against month-first (and a decimal mark both readings fit) stays the person's choice, even
            # when another sponsor chose it: the counts are shown, the option is never recommended
            person = next((o.payload.get("person_choice") for o, _v in shown if o.payload.get("person_choice")), None)
            if person:
                learned = replace(learned, preview=Preview(0, 0), payload={
                    **dict(learned.payload), "person_choice": person,
                    "evidence": {"conform": lv.conform, "total": lv.total}})
            if same:
                shown[same[0]] = (learned, lv)
            else:
                shown = [(learned, lv), *shown[: MAX_SHOWN - 1]]
        final = [o for o, _v in shown] + [roles.missing_option(name)]
        examples = {o.option_id: list(v.examples) for o, v in shown}
        q = Question.build(question_id="q-" + hashlib.sha256(f"{j['id']}|{name}".encode()).hexdigest()[:16],
                           unit_id=str(j["id"]), kind=kind, subject=[Subject("field", name)], options=final,
                           scopes=SCOPES, blocking=("convert",) if kind == "unknown_format" else ())
        out.append({"question": q, "field": name, "header": h, "rule": rule, "columns": [x for x in header if x],
                    "examples": examples, "evidence": evidence, "text": text, "required": required,
                    "expected": src, "sheet": sheet})
    return out




def _evidence(what: str, values: list[Any]) -> dict[str, Any]:
    if what != "date":
        return {}
    texts = [v.strip() for v in values if isinstance(v, str) and v.strip()]
    return dict(TF.date_options(texts, day_first=["DD/MM/YYYY"], month_first=["MM/DD/YYYY"])["evidence"])


# ---- the view -----------------------------------------------------------------------------------------------
def view(c: Any, j: dict[str, Any]) -> list[dict[str, Any]]:
    """The JSON the page renders (see STATUS.md, Migration M24a)."""
    out = []
    for r in build(c, j):
        q: Question = r["question"]
        opts = []
        for o in q.options:
            p = dict(o.payload)
            person = bool(p.get("person_choice"))
            ev = p.get("evidence") or {}
            conform = int(ev.get("conform", 0)) if person else o.preview.conform
            total = int(ev.get("total", 0)) if person else o.preview.total
            step = {"from": p.get("from"), **({"ops": p["ops"]} if p.get("ops") else {})}
            label = ("Not in this file: leave it blank and report it as MISSING" if o.action == "declare_missing"
                     else TF.explain_field(step) if p.get("from") else o.note)
            opts.append({
                "option_id": o.option_id, "action": o.action, "proposed_by": o.proposed_by, "label": label,
                "note": o.note, "suggestion_id": o.suggestion_id, "column": p.get("from") or "",
                "ops": list(p.get("ops") or []), "person_choice": person,
                "preview": {"conform": conform, "total": total, "text": f"{conform}/{total} rows"
                            if o.action != "declare_missing" else "0 rows filled"},
                "examples": [dict(x) for x in r["examples"].get(o.option_id, [])]})
        out.append({"question_id": q.question_id, "kind": q.kind, "field": r["field"], "record_type": j["type"],
                    "text": r["text"], "column": r["header"], "columns": r["columns"], "required": r["required"],
                    "blocking": bool(q.blocking), "scopes": list(q.scopes), "recommended": q.recommended,
                    "evidence": r["evidence"], "options": opts})
    return out


def answered_view(j: dict[str, Any]) -> list[dict[str, Any]]:
    """The answers of this job that are in force (not undone), newest last: what the page shows with an Undo."""
    out = []
    for a in j.get("answers") or []:
        if a.get("undone"):
            continue
        step = a.get("step") or {}
        what = TF.explain_field(step) if step else str(a.get("field") or "")
        where = f"for {j['sp']}" if a.get("scope") == "sponsor" else "for this job"
        out.append({"question_id": a["question_id"], "field": a["field"], "scope": a["scope"], "by": a["by"],
                    "reason": a.get("reason", ""),
                    "text": f"Saved {where}: {a.get('header') or a['field']}, {what}"})
    return out


def open_blockers(c: Any, j: dict[str, Any]) -> list[str]:
    """The fields whose open question stops approval (a format nobody decided, so it would be written as text)."""
    return [r["field"] for r in build(c, j) if r["question"].blocking]


# ---- answering ----------------------------------------------------------------------------------------------
def _person(name: str) -> str:
    from worker.improver import reserved_name

    who = " ".join(str(name or "").split())
    if not who:
        raise _err("I need to know who is answering: sign in, or enter your name.")
    if is_agent_principal(who) or reserved_name(who):
        raise _err(f"“{who}” isn't a person's name. Use your own name.")
    return who


def _custom(c: Any, j: dict[str, Any], rec: dict[str, Any], given: dict[str, Any], rows: list[TF.Row],
            recs: list[dict[str, Any]], reason: str) -> Option:
    """A custom option: the person's own closed action. A column must be one of the file's own headers; ops must
    pass the transform vocabulary; a supplied value is typed by the person."""
    name, rule = rec["field"], rec["rule"]
    action = str(given.get("action") or "")
    if not reason:
        raise _err("A choice of your own needs a reason.")
    header = str(given.get("header") or "")
    if action in ("map", "transform"):
        if header not in rec["columns"]:
            raise _err(f"“{header}” is not a column of this file. Pick one of: {', '.join(rec['columns'][:12])}.")
        ops = list(given.get("ops") or [])
        step: dict[str, Any] = {"from": header, **({"ops": ops} if ops else {})}
        try:
            verdict = SR.verify({"fields": {name: step}}, rows, {name: rule}, _context(c, j))[name]
        except TF.RecipeError as exc:
            raise _err(f"That is not a step the closed vocabulary allows: {exc}") from exc
        payload = {"from": header, "ops": ops, "field": name}
        return Option(option_id="custom-" + hashlib.sha256(TF.canonical(step).encode()).hexdigest()[:10],
                      action="transform" if ops else "map", proposed_by="human",
                      preview=Preview(verdict.conform, verdict.total), payload=payload, note=reason)
    if action == "supply":
        value = str(given.get("value") or "").strip()
        if not value:
            raise _err("A value you supply can't be blank.")
        try:
            TF.compile_field(name, {"supply": {"value": value}}, rule)
        except TF.RecipeError as exc:
            raise _err(f"That value does not fit {name}: {exc}") from exc
        return Option(option_id="custom-supply", action="supply", proposed_by="human",
                      preview=Preview(len(recs), len(recs)), payload={"value": value, "field": name}, note=reason)
    if action == "declare_missing":
        return Option(option_id="custom-missing", action="declare_missing", proposed_by="human",
                      preview=Preview(0, 0), payload={"field": name}, note=reason)
    raise _err("A choice of your own is a column (map), a column with steps (transform), a typed value (supply) "
               "or declare_missing.")


def _step(option: Option, reason: str) -> dict[str, Any]:
    try:
        return R.step_from_option(option.to_dict(), str(option.payload.get("from") or ""), reason)
    except R.RecipeChangeRefused as exc:
        raise _err(str(exc)) from exc


def _verdict(name: str, step: dict[str, Any], rows: list[TF.Row], rule: TF.FieldRule,
             ctx: TF.Context) -> dict[str, Any]:
    try:
        v = SR.verify({"fields": {name: step}}, rows, {name: rule}, ctx)[name]
    except TF.RecipeError:
        return {"bound": True, "conform": 0, "total": len(rows), "findings": {"needs_a_parse": len(rows)}}
    findings = {**{k: n for k, n in v.failed.items() if n}, **({"missing": v.missing} if v.missing else {})}
    return {"bound": True, "conform": v.conform, "total": v.total, "findings": findings}


def _pins(c: Any, j: dict[str, Any], recipe_pin: str | None) -> dict[str, Any]:
    try:
        m = CE.evaluation(c, j).manifest
        skills, controls, target = dict(m.pins.skills), m.pins.controls, m.pins.target
    except (CE.EvaluationError, OSError, ValueError, KeyError):  # a pin it cannot give is none, never invented
        skills, controls, target = {}, None, None
    return {"skills": skills, "controls": controls, "target": target, "recipe_before": recipe_pin,
            "model": None, "prompt": None, "mapper_handoff": None}


def _decision(c: Any, j: dict[str, Any], rec: dict[str, Any] | None, *, name: str, kind: str, qid: str,
              options: list[Option], chosen: dict[str, Any], option: Option, scope: str, who: str, reason: str,
              rule: TF.FieldRule, step: dict[str, Any], before: dict[str, Any], tab: Any, expected: str,
              recipe_pin: str | None, ctx: TF.Context) -> dict[str, Any]:
    sheet, header, recs, rows = tab
    h = str(option.payload.get("from") or (rec or {}).get("header") or "")
    shown = h if h in header else (expected or name)
    path = str(j["up"]["path"])
    fp = "sha256:" + sha256_file(path)
    col = _letter(header, h) or "none"
    return {
        "job": str(j["id"]), "unit_id": str(j["id"]), "sponsor": T.sponsor_slug(j["sp"]),
        "record_type": str(j["type"]), "target_field": name, "question": {"question_id": qid, "kind": kind},
        "source": {"file_fp": fp, "sheet": sheet, "header": shown, "col_id": col},
        "values": _values(recs, h) if h in header else [],
        "sample_ref": {"kind": "source_file", "id": f"{fp[7:19]}#{col}", "checksum": fp},
        "options_shown": [o.to_dict() for o in options], "chosen": chosen, "scope": scope, "principal": who,
        "pins": _pins(c, j, recipe_pin),
        "conformance": {"before": before, "after": _verdict(name, step, rows, rule, ctx)},
        "reason": reason, "field_required": rule.required,
    }


def answer(c: Any, j: dict[str, Any], *, question_id: str, option_id: str | None = None,
           custom: dict[str, Any] | None = None, scope: str = "", reason: str = "", principal: str = "") -> dict[str, Any]:
    """Apply one answer. Nothing changes unless every record could be written: the field decision goes to the job's
    audit and the sponsor's log first, then (sponsor scope) `recipe.changed` to the audit and only then the recipe
    file; a job-scope answer is this job's alone. The job is evaluated again."""
    who = _person(principal)
    if j.get("approved"):
        raise _err("This job is approved, so it is final. Nothing changed.")
    if scope not in SCOPES:
        raise _err(f"The answer reaches this job or this sponsor ({' or '.join(SCOPES)}); it cannot reach "
                   f"{scope or 'nothing'}.")
    rec = next((r for r in build(c, j) if r["question"].question_id == question_id), None)
    if rec is None:
        raise _err("That question is not open on this job.")
    q: Question = rec["question"]
    if (option_id is None) == (custom is None):
        raise _err("Answer with one option, or with one choice of your own.")
    tab = _table(j)
    assert tab is not None
    _sheet, _header, recs, rows = tab
    reason = " ".join(str(reason or "").split())
    name, rule = rec["field"], rec["rule"]
    if option_id is not None:
        try:
            option = q.option(option_id)
        except ContractError as exc:
            raise _err(f"Option {option_id!r} was not offered on this question.") from exc
        chosen: dict[str, Any] = {"option_id": option_id}
    else:
        option = _custom(c, j, rec, dict(custom or {}), rows, recs, reason)
        chosen = {"custom": option.to_dict()}
    if option.action == "declare_missing" and rule.required and not reason:
        raise _err(f"{name} is required. Leaving it out of this file needs a reason, so the record says why.")
    step = _step(option, reason)
    ctx = _context(c, j)
    try:
        TF.compile_field(name, step, rule, ctx)
    except TF.RecipeError as exc:
        raise _err(f"That step does not fit {name}: {exc}") from exc
    slug = T.sponsor_slug(j["sp"])
    fr = c.field_recipes(j["sp"])
    typ = str(j["type"])
    before = _before(c, j, name, rec, rows, rule, ctx)
    decision = _decision(c, j, rec, name=name, kind=q.kind, qid=question_id, options=list(q.options), chosen=chosen,
                         option=option, scope=scope, who=who, reason=reason, rule=rule, step=step, before=before,
                         tab=tab, expected=rec["expected"], recipe_pin=fr.pin(typ), ctx=ctx)
    store = persist.feedback(Path(c.root) / "feedback", slug)
    try:
        done = R.decide_field(persist.job_audit(c, j), store, fr if scope == "sponsor" else None, rows,
                              target_rules={name: rule}, context=ctx, **decision)
    except (F.FeedbackError, R.RecipeChangeRefused, AuditError, OSError, ValueError) as exc:
        raise _err(f"I couldn't record the answer, so nothing changed: {exc}") from exc
    saved = done["record"]
    prior = {"map": (j.get("map") or {}).get(name, _ABSENT), "step": (j.get("steps") or {}).get(name, _ABSENT)}
    _apply_to_job(j, name, scope, option, step)
    j.setdefault("answers", []).append({
        "question_id": question_id, "field": name, "kind": q.kind, "scope": scope, "by": who, "prior": prior,
        "record_type": typ, "step": step, "header": str(step.get("from") or ""),
        "option_id": option.option_id, "feedback_id": saved["feedback_id"], "action": saved["action"],
        "recipe_changed": bool((done.get("recipe") or {}).get("written")),
        "via_suggestion": saved.get("via_suggestion"), "reason": reason})
    where = f"every {j['sp']} job" if scope == "sponsor" else "this job"
    c.log(f"Decided how {name} is read for {where}: {TF.explain_field(step)}",
          f"{who} answered a question; " + ("took my recommendation" if saved["action"] == "accept"
                                             else "chose differently from the recommendation"), typ)
    c.S["fb"].append({"sp": j["sp"], "type": typ, "kind": saved["action"], "ref": name, "text": TF.explain_field(step),
                      "skill": "detect-columns@4"})
    CE.refresh_recipe(c, j)
    c.retally(j)
    recipe = done.get("recipe") or {}
    return {"question_id": question_id, "field": name, "scope": scope, "feedback_id": saved["feedback_id"],
            "action": saved["action"], "by": who, "step": step, "recipe_changed": bool(recipe.get("written")),
            "recipe_pin": recipe.get("pin_after")}


_ABSENT = "__absent__"  # a job key the answer found unset (JSON-safe, so it survives the saved state)


def undo(c: Any, j: dict[str, Any], *, question_id: str, principal: str, reason: str = "") -> dict[str, Any]:
    """Undo the latest answer to a question. Sponsor scope: the recipe step is reverted through FieldRecipes
    (`recipe.reverted` goes to the audit first); job scope or an answer that changed no recipe: a
    `field_decision.undone` record goes to the audit first. Then an `undo` record is appended to the sponsor's
    feedback log (the decision stays), the job's own choice is put back as it was, and the job is evaluated again.
    Refused for a name that is not a person, a job-scope answer of an approved job (finished work), or an answer
    that is not in force. A sponsor-scope answer of an approved job can be undone: that job is not evaluated
    again, only the recipe the next jobs read changes."""
    who = _person(principal)
    ans = next((a for a in reversed(j.get("answers") or []) if a.get("question_id") == question_id
                and not a.get("undone")), None)
    if ans is None:
        raise _err("There is no answer to that question to undo.")
    approved = bool(j.get("approved"))
    if approved and ans.get("scope") != "sponsor":
        raise _err("This job is approved, so it is final. Nothing changed.")
    reason = " ".join(str(reason or "").split()) or f"undone by {who} from the question card"
    name, scope, fid = str(ans["field"]), str(ans["scope"]), str(ans["feedback_id"])
    typ = str(ans.get("record_type") or j["type"])
    audit = persist.job_audit(c, j)
    store = persist.feedback(Path(c.root) / "feedback", T.sponsor_slug(j["sp"]))
    fr = c.field_recipes(j["sp"])
    reverted = False
    try:
        # did this decision change the sponsor's recipe? The recipe's own history says so (an answer saved by an
        # older build has no flag), and an undo of a step a newer decision replaced is refused by `revert`
        in_history = any(e["action"] == "recipe.changed" and e["inputs"].get("feedback_id") == fid
                         for e in fr.history(typ).read()) if scope == "sponsor" else False
        if scope == "sponsor" and in_history:
            fr.revert(typ, fid, principal=who, reason=reason, audit=audit, feedback_store=store)
            reverted = True
        else:
            audit.record("field_decision.undone", reason, {"feedback_id": fid, "field": name, "scope": scope,
                                                           "job": str(j["id"]), "by": who})
            store.add_undo(fid, who, reason)
    except (F.FeedbackError, R.RecipeChangeRefused, AuditError, OSError, ValueError) as exc:
        raise _err(f"I couldn't record the undo, so nothing changed: {exc}") from exc
    if scope == "job":
        prior = ans.get("prior") or {}
        for key, slot in (("map", j.setdefault("map", {})), ("step", j.setdefault("steps", {}))):
            was = prior.get(key, _ABSENT)
            if was == _ABSENT:
                slot.pop(name, None)
            else:
                slot[name] = was
    ans["undone"] = True
    c.log(f"Undid the decision on how {name} is read", f"{who} undid the answer: {reason}", typ)
    if not approved:  # an approved job is finished work: only the sponsor's recipe (for later jobs) changes
        CE.refresh_recipe(c, j)
        c.retally(j)
    return {"question_id": question_id, "field": name, "scope": scope, "feedback_id": fid, "by": who,
            "recipe_reverted": reverted}


def _before(c: Any, j: dict[str, Any], name: str, rec: dict[str, Any], rows: list[TF.Row], rule: TF.FieldRule,
            ctx: TF.Context) -> dict[str, Any]:
    h = rec["header"]
    if not h:
        return {"bound": False, "conform": 0, "total": len(rows), "findings": {}}
    return _verdict(name, {"from": h}, rows, rule, ctx)


def _apply_to_job(j: dict[str, Any], name: str, scope: str, option: Option, step: dict[str, Any]) -> None:
    """A job-scope answer is this job's: a plain column or "not in this file" is the job's column choice (as the
    plan table has always kept it); any other step is the job's own step. A sponsor answer is the recipe."""
    if scope != "job":
        j.get("map", {}).pop(name, None)
        (j.get("steps") or {}).pop(name, None)
        return
    steps = j.setdefault("steps", {})
    if option.action == "declare_missing":
        j["map"][name] = ""
        steps.pop(name, None)
    elif option.action == "map":
        j["map"][name] = str(step["from"])
        steps.pop(name, None)
    else:
        steps[name] = step
        j["map"].pop(name, None)


# ---- the plan table's column choice, recorded ---------------------------------------------------------------
def record_column(c: Any, j: dict[str, Any], name: str, header: str, principal: str) -> str | None:
    """The plan table's column dropdown is a job-scope answer: write the `field_decision` (audit first, then the
    sponsor's log) before the job's column changes. Returns the feedback id, or None for a job with no readable
    static source (a GL job, or a file that is gone), where only the console's own log applies."""
    tab = _table(j)
    f = next((x for x in D.plan(str(j["type"])) if x[0] == name), None)
    if tab is None or f is None:
        return None
    _sheet, header_row, _recs, rows = tab
    who = principal
    rule = _rule(c, j, f)
    open_q = next((r for r in build(c, j) if r["field"] == name), None)
    missing = roles.missing_option(name)
    shown = list(open_q["question"].options) if open_q else [missing]
    kind = open_q["question"].kind if open_q else "unknown_field"
    qid = open_q["question"].question_id if open_q else "q-" + hashlib.sha256(
        f"{j['id']}|{name}".encode()).hexdigest()[:16]
    reason = _FEEDBACK_AS
    if header:
        if header not in header_row:
            return None
        step = {"from": header}
        ctx = _context(c, j)
        try:
            verdict = SR.verify({"fields": {name: step}}, rows, {name: rule}, ctx)[name]
        except TF.RecipeError:
            return None  # the column needs a parse: the person is asked through the question, not this dropdown
        option = Option(option_id="custom-" + hashlib.sha256(TF.canonical(step).encode()).hexdigest()[:10],
                        action="map", proposed_by="human", preview=Preview(verdict.conform, verdict.total),
                        payload={"from": header, "ops": [], "field": name}, note=reason)
        chosen: dict[str, Any] = {"custom": option.to_dict()}
    else:
        option, chosen, step, ctx = missing, {"option_id": missing.option_id}, {}, _context(c, j)
    if not any(o.option_id == chosen.get("option_id") for o in shown) and "custom" not in chosen:
        shown = [*shown, missing]
    rec_ctx = {"header": header, "field": name}
    before = {"bound": False, "conform": 0, "total": len(rows), "findings": {}}
    pick_step = step or {"missing": {"reason": "declared not in this file"}}
    decision = _decision(c, j, rec_ctx, name=name, kind=kind, qid=qid, options=shown, chosen=chosen, option=option,
                         scope="job", who=who, reason=reason, rule=rule, step=pick_step, before=before, tab=tab,
                         expected=str(f[1] or ""), recipe_pin=c.field_recipes(j["sp"]).pin(str(j["type"])), ctx=ctx)
    try:
        saved = R.decide_field(persist.job_audit(c, j), persist.feedback(Path(c.root) / "feedback", T.sponsor_slug(j["sp"])),
                               None, rows, target_rules={name: rule}, context=ctx, **decision)
    except (F.FeedbackError, R.RecipeChangeRefused, AuditError, OSError, ValueError) as exc:
        raise _err(f"I couldn't record the choice, so nothing changed: {exc}") from exc
    return str(saved["record"]["feedback_id"])
