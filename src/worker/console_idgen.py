"""The ID generation logic: one decision per sponsor and record type, asked first on the plan.

Three modes (`rule_target.ID_MODES`): use the source ID as it is, a sequential number with a prefix, or a custom
pattern of tokens. The token list, the ID length limit and the NAMECODE length are the target file's
(`templates/<RecordType>.target.yaml`), never literals here. Code validates the pattern, previews the first IDs on
the job's real rows and issues them; the model never types an ID.

A decision is a named person's (the reviewer), needs a reason, is written to the engine's audit with the old and the
new scheme BEFORE it takes effect, and then applies to every entity and fund of that sponsor, silently, on every
later job. Changing it is a new decision with a new audit row.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from worker import console_demo as D
from worker import console_engine as CE
from worker import console_questions as Q
from worker import idgen as I
from worker import record_facts as RF
from worker.audit import AuditError
from worker.errors import StatusError

PREVIEW_ROWS = 5  # the first IDs the preview shows
LABELS = {"source": "Use source ID directly", "sequential": "Sequential numeric", "custom": "Custom format"}


class IdgenError(StatusError):
    pass


def id_text(c: Any, sponsor: str, record_type: str, sentence: bool = False) -> str:
    """How new IDs are made for this sponsor, in a phrase the plan and its rules show."""
    s, n = c.id_scheme_of(sponsor, record_type), c.counter_of(sponsor, record_type)
    if s["mode"] == "source":
        text = "source ID, exactly as supplied"
    elif s["mode"] == "custom":
        text = f"new ID by your format {s['pattern']} · from number {n}"
    else:
        text = f"new ID from {sponsor}'s counter · {I.format_id(s, n)} onward"
    return text[:1].upper() + text[1:] if sentence else text


def _sponsor_type(c: Any, sponsor: str | None, record_type: str) -> tuple[str, str]:
    sp = sponsor or c.S["sp"]
    if sp not in c.S["ent"]:
        raise IdgenError(f"unknown sponsor {sp!r}", 404)
    if record_type not in RF.id_decision_types():
        raise IdgenError(f"{record_type!r} has no ID generation to decide", 404)
    return sp, record_type


def _job(c: Any, job_id: str | None, sp: str, rt: str) -> dict[str, Any] | None:
    if not job_id:
        return None
    j = next((x for x in c.S["jobs"] if x.get("id") == job_id), None)
    if j is None or (j["sp"], j["type"]) != (sp, rt):
        raise IdgenError(f"no job {job_id!r} for {sp} {rt}", 404)
    return j


def _source_column(j: dict[str, Any] | None, rt: str) -> str:
    """The file's column that holds the source ID, as the plan has it ("" when the file has none)."""
    if j is None:
        return ""
    return CE.source_id_column(CE.fields_of(j), rt)


def _columns(j: dict[str, Any] | None) -> tuple[list[str], list[dict[str, Any]]]:
    """The file's header and records, to let the person point at the column they think is the ID."""
    from worker import console_questions as Q

    if j is None:
        return [], []
    tab = Q._table(j)
    header = [h for h in (CE.source_columns(j) or D.SRCCOLS.get(str(j["type"]), [])) if h]  # as the plan lists them
    return header or ([h for h in tab[1] if h] if tab else []), (tab[2] if tab else [])


_DATE = re.compile(r"^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}")
_AMOUNT = re.compile(r"^[-(]?[$€£]?\s?\d[\d,]*[.]\d+\)?$|^[-(]\s?[$€£]?\d[\d,]*\)?$")


def column_fit(recs: list[dict[str, Any]], column: str, limit: int) -> tuple[str, str]:
    """Whether a column can be the ID, from its values alone (the header may be called anything): ("ok", ""),
    ("names", why) when it reads as names, a person may still mean it, or ("no", why) when it cannot identify records."""
    col_vals = Q._values(recs, column)
    allv = [str(v or "").strip() for v in col_vals]
    vals = [v for v in allv if v]
    if not vals or len(vals) * 2 < len(allv):
        return "no", "it is mostly blank"
    if sum(1 for v in vals if _DATE.match(v)) * 2 > len(vals):
        return "no", "it holds dates"
    if sum(1 for v in vals if _AMOUNT.match(v)) * 2 > len(vals):
        return "no", "it holds amounts"
    if len(set(vals)) * 10 < len(vals) * 8:
        return "no", "its values repeat too much to tell records apart"
    if sorted(len(v) for v in vals)[len(vals) // 2] > limit:
        return "no", f"its values are longer than {limit} characters"
    if sum(1 for v in vals if " " in v or not any(ch.isdigit() for ch in v)) * 2 > len(vals):
        return "names", "it reads as names, not IDs"
    return "ok", ""


def fits(j: dict[str, Any], rt: str) -> dict[str, list[str]]:
    """Per column of the file: how well it can be the ID, for the plan's dropdown."""
    header, recs = _columns(j)
    return {h: list(column_fit(recs, h, RF.id_limit(rt))) for h in header}


def _source_check(j: dict[str, Any] | None, body: dict[str, Any]) -> tuple[str, list[str]]:
    """The column a person says is the ID (required for "use source ID") and what stops it being used."""
    header, recs = _columns(j)
    col = str(body.get("column") or "")
    if col not in header:
        return "", ["Point to the column you think holds the ID: Use source ID needs one."]
    verdict, why = column_fit(recs, col, RF.id_limit(str((j or {}).get("type", ""))))
    if verdict == "no":
        return col, [f"“{col}” cannot be the ID: {why}. Pick another column, or generate IDs."]
    if verdict == "names" and not body.get("name_ok"):
        return col, [f"“{col}” reads as names, not IDs. If a name really is the ID, tick that you mean it and add a reason."]
    return col, []


def _rows(c: Any, j: dict[str, Any] | None) -> list[dict[str, Any]]:
    if j is None or not (j.get("up") or {}).get("path"):
        return []
    try:
        return CE.static_rows(c, j)
    except CE.EvaluationError:
        return []


def _gets_an_id(row: dict[str, Any]) -> bool:
    """A row written, or refused only for its ID (it would get one under another scheme)."""
    own = {"id_missing", "id_duplicate", "id_too_long"}
    return row["st"] in ("ok", "warn") or (row["st"] == "err" and set(row.get("codes", ())) <= own)


def _preview(c: Any, sp: str, rt: str, j: dict[str, Any] | None, scheme: dict[str, Any]) -> dict[str, Any]:
    tgt = RF.target(rt)
    rows = _rows(c, j)
    if not rows:
        note = "Start the job with its file to see the first IDs on its real rows."
        return {"ids": [], "rows": 0, "errors": [], "notes": [note]}
    taken = [e.get("key") for e in c.registry(sp).entries(rt) if j is None or e.get("job") != j["id"]]
    first = max(c.counter_of(sp, rt), int(scheme.get("start") or 1))
    year = int(str((j or {}).get("d") or date.today().isoformat())[:4])
    return I.preview(scheme, rows, first=first, taken=taken, year=year, max_len=RF.id_limit(rt),
                     namecode_len=tgt.namecode_length, limit=PREVIEW_ROWS, keep=_gets_an_id)


def _source_preview(j: dict[str, Any], rt: str, column: str) -> dict[str, Any]:
    """The values of the column the person points to, exactly as the file has them, with what would stop each.
    The column may be named anything: only its values are looked at."""
    recs = _columns(j)[1]
    name_col = CE._plan_columns(CE.fields_of(j), rt).get("name_col", "")
    col_vals = Q._values(recs, column)
    vals = [str(v or "").strip() for v in col_vals]
    limit, seen, notes = RF.id_limit(rt), set(), []
    blank = sum(1 for v in vals if not v)
    dup = sum(1 for v in vals if v and (v in seen or seen.add(v)))  # type: ignore[func-returns-value]
    long_ = sum(1 for v in vals if len(v) > limit)
    for n, text in ((blank, "blank (no ID to use)"), (dup, "a repeat of an earlier ID"),
                    (long_, f"longer than {limit} characters")):
        if n:
            notes.append(f"{n} of {len(vals)} row(s) in “{column}” are {text}.")
    names = Q._values(recs, name_col) if name_col else [""] * len(vals)
    ids = [{"name": str(n or ""), "id": v} for n, v in zip(names, vals) if v][:PREVIEW_ROWS]
    return {"ids": ids, "rows": len(vals), "errors": [], "notes": notes}


def _observed(c: Any, sponsor: str, record_type: str) -> tuple[dict[str, Any] | None, str]:
    """What the IDs already in Ledgerline for this sponsor look like, as a scheme to continue and a sentence that
    says so ("" when there are none). Plain counting on the registry: the person is told what was seen."""
    ids = [str(e.get("key") or "") for e in c.registry(sponsor).entries(record_type) if e.get("key_kind") != "name"]
    ids = [i for i in ids if i]
    if not ids:
        return None, ""
    got = I.infer_scheme(ids)
    if got is None:
        sample = ", ".join(sorted(ids)[:3])
        return None, (f"{len(ids)} {record_type.lower()} are already in Ledgerline and their IDs do not follow one "
                      f"pattern ({sample}…). New IDs will not match them; tell me if one of them is the pattern.")
    nxt = I.format_id({**got}, got["start"])
    return got, (f"{got['matched']} of your {got['of']} existing {record_type.lower()} IDs follow one pattern "
                 f"(…{got['last']}). I continue it: the next new ID is {nxt}.")


def view(c: Any, sponsor: str | None, record_type: str, job_id: str | None = None) -> dict[str, Any]:
    if record_type not in RF.id_decision_types():
        return {"offered": False, "record_type": record_type}
    sp, rt = _sponsor_type(c, sponsor, record_type)
    j = _job(c, job_id, sp, rt)
    tgt = RF.target(rt)
    held = c.id_decision(sp, rt) or {}
    col = _source_column(j, rt)
    header, _recs = _columns(j)
    why_not = "" if header else "Start the job with its file first, then point to the column that holds the ID."
    scheme = c.id_scheme_of(sp, rt)
    modes = [
        {"mode": "source", "label": LABELS["source"], "available": bool(header), "reason": why_not,
         "detail": f"Uses the ID in the column you point to, exactly as supplied. An ID longer than "
                   f"{RF.id_limit(rt)} characters is flagged, never cut."},
        {"mode": "sequential", "label": LABELS["sequential"], "available": True, "reason": "",
         "detail": "A prefix and a zero-padded number from this sponsor's counter."},
        {"mode": "custom", "label": LABELS["custom"], "available": bool(tgt.id_tokens),
         "reason": "" if tgt.id_tokens else "This target file defines no pattern tokens.",
         "detail": "Your own format, built from " + ", ".join("{" + t + "}" for t in tgt.id_tokens) + "."},
    ]
    out: dict[str, Any] = {
        "sponsor": sp, "record_type": rt, "job": j["id"] if j else "", "decided": bool(held), "scheme": scheme,
        "default": {**I.DEFAULT_SCHEME, **dict(tgt.id_generation)}, "by": held.get("by", ""),
        "at": held.get("at", ""), "reason": held.get("reason", ""), "max_length": RF.id_limit(rt),
        "tokens": list(tgt.id_tokens), "namecode_length": tgt.namecode_length, "pattern_max": I.ID_PATTERN_MAX,
        "source_column": col, "columns": header, "id_field": tgt.key[-1], "next_number": max(c.counter_of(sp, rt), int(scheme.get("start") or 1)),
        "modes": modes,
    }
    out["suggest"], out["observed"] = _observed(c, sp, rt)
    out["preview"] = _preview(c, sp, rt, j, scheme) if j is not None else None
    return out


def preview(c: Any, body: dict[str, Any]) -> dict[str, Any]:
    """The first IDs the proposed scheme would give the job's real rows, with every problem in words. Writes
    nothing."""
    sp, rt = _sponsor_type(c, body.get("sponsor"), str(body.get("record_type", "")))
    j = _job(c, body.get("job"), sp, rt)
    scheme, problems = I.normalize(RF.target(rt).id_tokens, dict(body.get("scheme") or {}))
    if scheme["mode"] == "source":
        column, more = _source_check(j, body)
        problems += more
    if problems:
        return {"ids": [], "rows": 0, "errors": [{"code": "pattern", "message": p} for p in problems], "notes": []}
    if scheme["mode"] == "source" and j is not None:
        return _source_preview(j, rt, column)
    return _preview(c, sp, rt, j, scheme)


def decide(c: Any, body: dict[str, Any], who: str, job_id: str | None = None) -> dict[str, Any]:
    """Record the person's choice. Nothing changes unless the audit record was written first."""
    from worker.console import ConsoleError
    from worker.console_questions import _person

    person = _person(who)
    try:
        sp, rt = _sponsor_type(c, body.get("sponsor"), str(body.get("record_type", "")))
        j = _job(c, job_id or body.get("job"), sp, rt)
    except IdgenError as exc:
        raise ConsoleError(str(exc)) from exc
    reason = " ".join(str(body.get("reason") or "").split())
    if not reason:
        raise ConsoleError("Add a reason: a change of ID scheme needs one, so the record has it.")
    scheme, problems = I.normalize(RF.target(rt).id_tokens, dict(body.get("scheme") or {}))
    column = ""
    if scheme["mode"] == "source":
        column, more = _source_check(j, body)
        problems += more
    if not problems and scheme["mode"] != "source":
        problems += [e["message"] for e in _preview(c, sp, rt, j, scheme)["errors"] if e["code"] == "too_long"]
    if problems:
        raise ConsoleError("Not saved: " + "; ".join(problems))
    try:  # the one decision path (`IdScheme.change`, as the agent's set_id_scheme): audited before it is written
        c.id_store(sp, rt).change(scheme, None, by=person, reason=reason, record=CE._engine_log(c).record,
                                  tokens=RF.target(rt).id_tokens, job=(j or {}).get("id", ""))
    except (AuditError, OSError, ValueError) as exc:
        raise ConsoleError(f"Nothing was changed, because the decision could not be recorded: {exc}") from exc
    c.log(f"Decided the {rt.lower()} ID logic for {sp}: {LABELS[scheme['mode']]}",
          f"{person}; applies to every {sp} entity and fund. {reason}", rt)
    c.save()
    if j is not None and column:  # the person's own column becomes the plan's source of the ID
        c.set_column(j, next((f[0] for f in CE.fields_of(j) if str(f[0]).startswith("Source_")
                              and str(f[0]).endswith("_Key")), RF.target(rt).key[-1]), column, person)
    elif j is not None:
        c.retally(j)
    return {**view(c, sp, rt, (j or {}).get("id")), "by": person}

