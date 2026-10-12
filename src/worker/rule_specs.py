"""Kinds `table`, `recon-specs` and `cases`: decision tables of first-hit rows (and the sponsor overlay that may only
add or refine rows around them), the reconcile checks (measures, selectors, derived lines, the client's report
wording) with their references into tables and controls, and the case files that exercise a skill's runners."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from worker.rule_schema import ID_RE, RECORD_RE, Loaded, Rep, at, is_num, numify

ROW_KEYS = ("acct", "fsl", "inv", "invst", "pr", "tt")
SAFE_PLACEHOLDERS_SPECS = ("pass", "warn")
CASE_RUNNERS: dict[str, dict[str, Any]] = {
    "map-accounts.cascade": {"fixtures": ("sponsor_store",), "case_fields": ("account_type", "account",
                                                                             "transaction_type")},
    "reconcile-gl.check": {"fixtures": ("accounts", "investors", "aliases", "fixtures"),
                           "case_fields": ("check", "ledger", "report", "fund_ledger", "incomplete")},
    # which column of a source file holds a template field: headers in, the column out
    "build-static-records.headers": {"fixtures": (), "case_fields": ("record_type", "field", "headers")},
}

_CHECK_REQ = ("id", "n", "per", "key", "needs", "rule", "sel", "steps", "measures", "order", "cols")
_CHECK_OPT = ("unmatched", "period", "interleave", "fixed_labels", "collapse_tt", "pair", "derived", "layout",
              "layout_text", "client")
_MEASURE_REQ = ("label", "where", "sign", "kind", "value", "key")
_MEASURE_OPT = ("line", "src", "need", "ytd", "hidden", "memo", "from_client", "balance", "review")
_SELECTORS = ("fsg", "fsl", "fsl_not", "fsl_prefix", "fsl_noprefix", "tt_any", "tt_none", "period", "inv",
              "pr_ne_date", "agn")


def _row_keys(rep: Rep, ptr: str, v: Any, *, none_ok: bool = False) -> None:
    if v is None and none_ok:
        return
    for i, k in enumerate(rep.text_list(ptr, v)):
        if k not in ROW_KEYS:
            rep.add(at(ptr, i), "unknown_operator", f"{k!r} is not a row field the engine knows; use one of {list(ROW_KEYS)}")


def validate_specs(rep: Rep, data: Mapping[str, Any]) -> dict[str, Any]:
    rep.shape("", data, ("schema_version", "kind", "checks"), ("description",))
    rep.description(data)
    checks = data.get("checks")
    ctl: dict[str, Any] = {}
    if not isinstance(checks, list) or not checks:
        if "checks" in data:
            rep.add("checks", "wrong_type", "expected a non-empty list of checks")
        return {}
    seen: set[str] = set()
    for i, c in enumerate(checks):
        p = at("checks", i)
        if not rep.shape(p, c, _CHECK_REQ, _CHECK_OPT):
            continue
        cid = c.get("id")
        if "id" in c:
            if not isinstance(cid, str) or not ID_RE.match(cid):
                rep.add(at(p, "id"), "wrong_type", f"check id {cid!r} must be lower-case letters and digits")
            elif cid in seen:
                rep.add(at(p, "id"), "duplicate_id", f"check id {cid!r} appears twice in this file")
            else:
                seen.add(cid)
        rep.each(p, c, ("n",), rep.text)
        for key in ("rule", "sel", "layout_text"):
            if key in c:
                rep.placeholders(at(p, key), c[key], SAFE_PLACEHOLDERS_SPECS)
        if "needs" in c and c["needs"] is not None:
            rep.text(at(p, "needs"), c["needs"])
        if "steps" in c:
            for j, s in enumerate(c["steps"] if isinstance(c["steps"], list) else []):
                rep.placeholders(at(at(p, "steps"), j), s, SAFE_PLACEHOLDERS_SPECS)
            if not isinstance(c["steps"], list):
                rep.add(at(p, "steps"), "wrong_type", "steps is a list of text")
        rep.when(p, c, "per", rep.enum, ("ITD", "YTD", "ITD + YTD"), "the period basis")
        if "key" in c:
            _row_keys(rep, at(p, "key"), c["key"])
        rep.when(p, c, "unmatched", rep.enum, ("zero", "review", "error"), "the unmatched treatment")
        if "period" in c and c["period"] is not True and c["period"] != "optional":
            rep.add(at(p, "period"), "wrong_type", "period is true or 'optional'")
        rep.each(p, c, ("interleave", "fixed_labels", "collapse_tt", "pair"), rep.flag)
        rep.each(p, c, ("cols", "layout"), rep.text_list)
        ids: set[str] = set()
        mctl: dict[str, Any] = {}
        meas = c.get("measures")
        if "measures" in c:
            if isinstance(meas, Mapping) and meas:
                for mid, m in meas.items():
                    ids.add(str(mid))
                    mp = at(at(p, "measures"), mid)
                    if rep.shape(mp, m, _MEASURE_REQ, _MEASURE_OPT):
                        mctl[str(mid)] = _measure(rep, mp, m)
            else:
                rep.add(at(p, "measures"), "wrong_type", "expected a non-empty mapping of measure id to measure")
        dctl: dict[str, Any] = {}
        if (der := rep.section(c, "derived", "derived id to derived line")) is not None:
            for did, d in der.items():
                dp = at(at(p, "derived"), did)
                if str(did) in ids:
                    rep.add(dp, "duplicate_id", f"{did!r} is both a measure and a derived line")
                ids.add(str(did))
                _derived(rep, dp, d)
                if isinstance(d, Mapping):
                    dctl[str(did)] = {"balance": d.get("balance", False)}
            for did, d in der.items():
                if isinstance(d, Mapping) and isinstance(d.get("of"), list):
                    for j, pair in enumerate(d["of"]):
                        if isinstance(pair, list) and pair and isinstance(pair[0], str) and pair[0] not in ids:
                            rep.add(at(at(at(at(p, "derived"), did), "of"), j), "unknown_reference",
                                    f"{pair[0]!r} is not a measure or derived line of check {cid}")
        if "order" in c:
            order = rep.text_list(at(p, "order"), c["order"])
            for j, oid in enumerate(order):
                if oid not in ids:
                    rep.add(at(at(p, "order"), j), "unknown_reference", f"{oid!r} is not a measure or derived line of check {cid}")
        if "client" in c:
            _client(rep, at(p, "client"), c["client"], ids)
        if isinstance(cid, str):
            ctl[cid] = {"unmatched": c.get("unmatched"), "measures": mctl, "derived": dctl}
    return {"checks": ctl}


def _measure(rep: Rep, p: str, m: Mapping[str, Any]) -> dict[str, Any]:
    rep.when(p, m, "label", rep.text, blank_ok=True)
    if "where" in m:
        w = m["where"]
        if isinstance(w, list):  # [] selects no upload row: a measure that comes only from the client's report
            for i, sel in enumerate(w):
                _selector(rep, at(at(p, "where"), i), sel)
        else:
            rep.add(at(p, "where"), "wrong_type", "where is a list of selectors ({} selects every row, [] none)")
    rep.when(p, m, "sign", rep.enum, ("raw", "natural", "flip"), "a sign")
    rep.when(p, m, "kind", rep.enum, ("money", "int", "pct"), "a measure kind")
    rep.when(p, m, "value", rep.enum, ("amt", "count", "shares"), "a value")
    if "key" in m:
        _row_keys(rep, at(p, "key"), m["key"], none_ok=True)
    rep.when(p, m, "src", rep.enum, ("fund", "lp"), "a source level")
    rep.when(p, m, "need", rep.enum, ("shares", "commit"), "a needed figure")
    rep.when(p, m, "line", rep.text)
    rep.each(p, m, ("ytd", "hidden", "memo", "from_client", "balance", "review"), rep.flag)
    return {"kind": m.get("kind"), "balance": m.get("balance", False), "review": m.get("review", False)}


def _selector(rep: Rep, p: str, sel: Any) -> None:
    if not isinstance(sel, Mapping):
        rep.add(p, "wrong_type", "a selector is a mapping (it may be empty)")
        return
    for key, v in sel.items():
        kp = at(p, key)
        if key not in _SELECTORS:
            rep.add(kp, "unknown_operator", f"{key!r} is not a selector; use one of {list(_SELECTORS)}")
        elif key in ("fsg", "fsl", "fsl_prefix", "fsl_noprefix", "tt_any"):
            rep.text(kp, v)
        elif key in ("fsl_not", "tt_none"):
            rep.text_list(kp, v, empty_ok=False)
        elif key == "period":
            rep.enum(kp, v, ("PRIOR", "YTD"), "a period")
        elif key == "inv":
            rep.enum(kp, v, ("present", "blank"), "an investor test")
        else:
            rep.flag(kp, v)


def _derived(rep: Rep, p: str, d: Any) -> None:
    if not rep.shape(p, d, ("label",), ("of", "section", "balance", "ytd", "key", "need", "need_src")):
        return
    rep.when(p, d, "label", rep.text)
    if ("of" in d) == ("section" in d):
        rep.add(p, "missing_field" if "of" not in d else "wrong_type",
                "a derived line has exactly one of 'of' (a sum of lines) or 'section' (a cash flow section)")
    if "of" in d:
        of = d["of"]
        if not isinstance(of, list) or not of:
            rep.add(at(p, "of"), "wrong_type", "of is a list of [line id, 1 or -1]")
        else:
            for j, pair in enumerate(of):
                pp = at(at(p, "of"), j)
                if not (isinstance(pair, list) and len(pair) == 2 and isinstance(pair[0], str)
                        and pair[1] in (1, -1) and not isinstance(pair[1], bool)):
                    rep.add(pp, "wrong_type", "expected [line id, 1 or -1]")
    rep.when(p, d, "section", rep.enum, ("operating", "investing", "financing"), "a cash flow section")
    rep.each(p, d, ("balance", "ytd"), rep.flag)
    if "key" in d:
        _row_keys(rep, at(p, "key"), d["key"])
    rep.when(p, d, "need", rep.enum, ("shares", "commit"), "a needed figure")
    rep.when(p, d, "need_src", rep.enum, ("fund", "lp"), "a source level")


def _client(rep: Rep, p: str, c: Any, ids: set[str]) -> None:
    if not rep.shape(p, c, ("key", "measure"), ("terminal", "irr")):
        return
    if "key" in c:
        _row_keys(rep, at(p, "key"), c["key"])
    rep.each(p, c, ("terminal", "irr"), rep.text)
    m = c.get("measure")
    if "measure" not in c:
        return
    mp = at(p, "measure")
    if not isinstance(m, list) or len(m) != 2:
        rep.add(mp, "wrong_type", "measure is [mode, target]: [fixed, id], [label, null or {text: id}] or [col, {text: id}]")
        return
    mode, target = m
    if not rep.enum(at(mp, 0), mode, ("fixed", "label", "col"), "a measure mode"):
        return
    if mode == "fixed":
        if not isinstance(target, str):
            rep.add(at(mp, 1), "wrong_type", "fixed takes one measure id")
        elif target not in ids:
            rep.add(at(mp, 1), "unknown_reference", f"{target!r} is not a measure or derived line of this check")
        return
    if target is None and mode == "label":
        return
    if not isinstance(target, Mapping) or not target:
        rep.add(at(mp, 1), "wrong_type", f"{mode} takes a mapping of the client's wording to a measure id")
        return
    for word, mid in target.items():
        if not isinstance(mid, str) or mid not in ids:
            rep.add(at(at(mp, 1), word), "unknown_reference", f"{mid!r} is not a measure or derived line of this check")


def resolve_specs(rep: Rep, specs: Loaded, tables: Mapping[str, Loaded], controls: Loaded | None) -> None:
    """References that cross files: series, trans type words, cash flow lines, style and applicability per check."""

    def names(table: str, inp: str) -> set[str] | None:
        t = tables.get(table)
        if t is None:
            return None
        out = set()
        for r in t.data.get("rows", []):
            out.add(str(r.get("id")))
            v = (r.get("when") or {}).get(inp)
            if isinstance(v, str):
                out.add(v)
        return out

    series, words, lines = names("account-series", "series"), names("trans-type-words", "words"), \
        names("cash-flow-sections", "line")
    applic = names("fund-type-applicability", "check")
    for i, c in enumerate(specs.data.get("checks", [])):
        p = at("checks", i)
        for mid, m in (c.get("measures") or {}).items():
            for j, sel in enumerate(m.get("where") or []):
                sp = at(at(at(at(p, "measures"), mid), "where"), j)
                for key, v in (sel.items() if isinstance(sel, Mapping) else ()):
                    vals = (v if isinstance(v, list) else str(v).split("+") if key == "fsl" else [v])
                    pool = words if key in ("tt_any", "tt_none") else series
                    what = ("tables/trans-type-words.yaml" if pool is words else "tables/account-series.yaml")
                    if key in ("fsg", "fsl", "fsl_not", "fsl_prefix", "fsl_noprefix", "tt_any", "tt_none") \
                            and pool is not None:
                        for item in vals:
                            if isinstance(item, str) and item not in pool:
                                rep.add(at(sp, key), "unknown_reference", f'{"series" if pool is series else "word list"} '
                                        f"{item!r} is not a row of {what}")
            if lines is not None and isinstance(m.get("line"), str) and m["line"] not in lines:
                rep.add(at(at(at(p, "measures"), mid), "line"), "unknown_reference",
                        f"cash flow line {m['line']!r} is not a row of tables/cash-flow-sections.yaml")
        cid = c.get("id")
        if applic is not None and cid not in applic:
            rep.add(at(p, "id"), "unknown_reference", f"check {cid!r} has no row in tables/fund-type-applicability.yaml")
        if controls is not None and cid not in (controls.data.get("check_style") or {}):
            rep.add(at(p, "id"), "unknown_reference", f"check {cid!r} has no check_style in {Path(controls.path).name}")


def controls_refs(rep: Rep, controls: Loaded, check_ids: set[str]) -> None:
    for section in ("check_style", "required_fields"):
        for cid in controls.data.get(section) or {}:
            if cid not in check_ids:
                rep.add(at(section, cid), "unknown_reference", f"{cid!r} is not a check of specs.yaml")


def validate_cases(rep: Rep, data: Mapping[str, Any], base: Loaded | None) -> dict[str, Any]:
    runner = data.get("runner")
    spec = CASE_RUNNERS.get(runner) if isinstance(runner, str) else None
    fixtures = spec["fixtures"] if spec else ()
    rep.shape("", data, ("schema_version", "kind", "runner", "cases"), ("description", *fixtures))
    rep.description(data)
    if "runner" in data and spec is None:
        rep.add("runner", "unknown_operator", f"{runner!r} is not a case runner; use one of {sorted(CASE_RUNNERS)}")
    for f in fixtures:
        if f in data and not isinstance(data[f], Mapping):
            rep.add(f, "wrong_type", "a fixture block is a mapping")
    cases = data.get("cases")
    if "cases" in data and (not isinstance(cases, list) or not cases):
        rep.add("cases", "wrong_type", "expected a non-empty list of cases")
        return {}
    seen = {c.get("name") for c in (base.data.get("cases") or [])} if base else set()
    for i, c in enumerate(cases or []):
        p = at("cases", i)
        case_fields = spec["case_fields"] if spec else ()
        if not rep.shape(p, c, ("name", "expect"), ("split", "holdout", *case_fields)):
            continue
        if "name" in c:
            if not isinstance(c["name"], str) or not c["name"].strip():
                rep.add(at(p, "name"), "wrong_type", "a case has a name")
            elif c["name"] in seen:
                rep.add(at(p, "name"), "duplicate_id", f"case name {c['name']!r} is already used")
            else:
                seen.add(c["name"])
        if "expect" in c and not isinstance(c["expect"], Mapping):
            rep.add(at(p, "expect"), "wrong_type", "expect is a mapping")
        rep.each(p, c, ("split",), rep.text)
        rep.each(p, c, ("holdout",), rep.flag)
        if runner == "build-static-records.headers":
            _header_case(rep, p, c)
        if runner == "reconcile-gl.check" and isinstance(data.get("fixtures"), Mapping):
            for key in ("ledger", "fund_ledger"):
                v = c.get(key)
                if isinstance(v, str) and v not in data["fixtures"]:
                    rep.add(at(p, key), "unknown_reference", f"{v!r} is not a block of this file's fixtures")
    return {}


def _header_case(rep: Rep, p: str, c: Mapping[str, Any]) -> None:
    rt = c.get("record_type")
    if not isinstance(rt, str) or not RECORD_RE.match(rt):
        rep.add(at(p, "record_type"), "wrong_type", "record_type is a target record type name")
    rep.text(at(p, "field"), c.get("field"))
    heads = c.get("headers")
    if not isinstance(heads, list) or not heads or not all(isinstance(h, str) and h.strip() for h in heads):
        rep.add(at(p, "headers"), "wrong_type", "headers is a non-empty list of column headers")
    exp = c.get("expect")
    if isinstance(exp, Mapping):
        rep.shape(at(p, "expect"), exp, ("column",))
        if "column" in exp and not isinstance(exp["column"], str):
            rep.add(at(at(p, "expect"), "column"), "wrong_type", "column is the header text, or '' for no column")


TABLE_INPUT_TYPES = ("bool", "number", "string")
TABLE_OUTPUT_TYPES = ("bool", "number", "string", "enum", "csv")
ROW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,60}$")
_CMP = re.compile(r"^(>=|<=|!=|>|<|==)\s*(.+)$")


def _outputs(rep: Rep, ptr: str, outs: Any) -> dict[str, Mapping[str, Any]]:
    if (named := rep.mapping(ptr, outs, "output name to {type, values?, unseen?}")) is None:
        return {}
    good: dict[str, Mapping[str, Any]] = {}
    for name, spec in named.items():
        p = at(ptr, name)
        if not rep.shape(p, spec, ("type",), ("values", "unseen")):
            continue
        t = spec.get("type")
        if "type" in spec and not rep.enum(at(p, "type"), t, TABLE_OUTPUT_TYPES, "an output type"):
            continue
        values = spec.get("values")
        if t == "enum":
            if "values" not in spec:
                rep.add(at(p, "values"), "missing_field", "an enum output lists its values")
            else:
                rep.text_list(at(p, "values"), values, empty_ok=False)
        elif "values" in spec:
            rep.add(at(p, "values"), "unknown_field", "only an enum output lists values")
        if "unseen" in spec:
            un = spec["unseen"]
            if rep.items(at(p, "unseen"), un):
                for i, u in enumerate(un):
                    ok = (isinstance(u, bool) if t == "bool" else
                          (u in values if t == "enum" and isinstance(values, list) else
                           (is_num(u) if t == "number" else isinstance(u, str))))
                    if not ok:
                        rep.add(at(at(p, "unseen"), i), "wrong_type", f"{u!r} is not a value of this {t} output")
        good[name] = spec
    return good


def _out_value_ok(spec: Mapping[str, Any], v: Any) -> bool:
    t = spec.get("type")
    if t == "bool":
        return isinstance(v, bool)
    if t == "number":
        return is_num(v)
    if t == "enum":
        return isinstance(v, str) and v in (spec.get("values") or ())
    return isinstance(v, str)


def _condition(rep: Rep, ptr: str, cond: Any, typ: str) -> None:
    if isinstance(cond, Mapping):
        if set(cond) != {"in"}:
            rep.add(ptr, "unknown_operator",
                    f"{dict(cond)!r} is not a condition; use a literal, >=, <=, >, <, ==, != or {{in: [...]}}")
            return
        items = cond["in"]
        if not isinstance(items, list) or not items:
            rep.add(at(ptr, "in"), "wrong_type", "in: needs a non-empty list")
            return
        for i, item in enumerate(items):
            _literal(rep, at(at(ptr, "in"), i), item, typ)
        return
    if isinstance(cond, str):
        m = _CMP.match(cond.strip())
        if typ == "bool":
            rep.add(ptr, "unknown_operator" if m else "wrong_type",
                    f"{cond!r}: a bool input takes true or false, never a comparison")
            return
        if m:
            if typ == "number":
                try:
                    float(m.group(2))
                except ValueError:
                    rep.add(ptr, "unknown_operator", f"{cond!r} compares with {m.group(2)!r}, which is not a number")
            return
        if typ == "number":
            rep.add(ptr, "unknown_operator",
                    f"{cond!r} is not a condition; use a literal, >=, <=, >, <, ==, != or {{in: [...]}}")
        return
    _literal(rep, ptr, cond, typ)


def _literal(rep: Rep, ptr: str, v: Any, typ: str) -> None:
    ok = (isinstance(v, bool) if typ == "bool" else is_num(v) if typ == "number" else isinstance(v, str))
    if not ok:
        rep.add(ptr, "wrong_type", f"{v!r} does not fit an input of type {typ}")


def _unseen_hit(outputs: Mapping[str, Mapping[str, Any]], then: Mapping[str, Any]) -> list[str]:
    hits = []
    for k, v in then.items():
        for u in (outputs.get(k) or {}).get("unseen") or ():
            if type(u) is type(v) and u == v:
                hits.append(f"{k} {v!r}")
    return hits


def _rows(rep: Rep, ptr: str, rows: Any, inputs: Mapping[str, str], outputs: Mapping[str, Mapping[str, Any]],
          ) -> list[Mapping[str, Any]]:
    if not isinstance(rows, list) or not rows:
        rep.add(ptr, "wrong_type", "expected a non-empty list of rows")
        return []
    seen: set[str] = set()
    good: list[Mapping[str, Any]] = []
    for i, row in enumerate(rows):
        p = at(ptr, i)
        if not rep.shape(p, row, ("id", "then"), ("when", "why")):
            continue
        rid = row.get("id")
        if "id" in row:
            if not isinstance(rid, str) or not ROW_ID_RE.match(rid):
                rep.add(at(p, "id"), "wrong_type", f"row id {rid!r} must be letters, digits, - or _")
            elif rid in seen:
                rep.add(at(p, "id"), "duplicate_id", f"row id {rid!r} appears twice in this table")
            else:
                seen.add(rid)
        when = row.get("when")
        if when is not None:
            if not isinstance(when, Mapping):
                rep.add(at(p, "when"), "wrong_type", "when: expects a mapping of input name to condition")
            else:
                for name, cond in when.items():
                    cp = at(at(p, "when"), name)
                    if name not in inputs:
                        rep.add(cp, "unknown_reference", f"{name!r} is not an input of this table")
                    else:
                        _condition(rep, cp, cond, inputs[name])
        then = row.get("then")
        if "then" in row:
            if not isinstance(then, Mapping) or not then:
                rep.add(at(p, "then"), "wrong_type", "then: expects a mapping of output name to value")
            else:
                for name, v in then.items():
                    tp = at(at(p, "then"), name)
                    if name not in outputs:
                        rep.add(tp, "unknown_reference", f"{name!r} is not an output of this table")
                    elif not _out_value_ok(outputs[name], v):
                        rep.add(tp, "wrong_type", f"{v!r} does not fit the {outputs[name].get('type')} output {name!r}")
        rep.when(p, row, "why", rep.text, longest=600)
        good.append(row)
    return good


def _catch_all(rep: Rep, ptr: str, rows: Sequence[Any]) -> None:
    last = len(rows) - 1
    for i, row in enumerate(rows):
        if not isinstance(row, Mapping):
            continue
        has_when = bool(row.get("when"))
        if i == last and has_when:
            rep.add(at(ptr, i), "no_catch_all",
                    f"the last row {row.get('id')!r} has a when, so a value that matches nothing has no answer; "
                    "make it the catch-all")
        elif i != last and not has_when:
            rep.add(at(ptr, i), "no_catch_all",
                    f"row {row.get('id')!r} has no when, so it matches everything and the rows after it never run")


def validate_table(rep: Rep, data: Mapping[str, Any], stem: str, base: Loaded | None) -> dict[str, Any]:
    if base is None:
        rep.shape("", data, ("schema_version", "kind", "id", "hit", "inputs", "outputs", "rows"), ("description",))
    else:
        rep.shape("", data, ("schema_version", "kind", "id", "rows"), ("description", "hit", "inputs", "outputs"))
        for forbidden in ("inputs", "outputs"):
            if forbidden in data:
                rep.add(forbidden, "overlay_forbidden",
                        f"a sponsor overlay cannot define {forbidden}; they belong to the organisation's table")
    rep.ident(data, stem, ID_RE)
    if "hit" in data:
        rep.enum("hit", data["hit"], ("first",), "the hit policy")
    rep.description(data)
    if base is not None:
        return _table_overlay(rep, data, base)
    inputs: dict[str, str] = {}
    if (raw_in := rep.section(data, "inputs", "input name to bool, number or string")) is not None:
        for name, t in raw_in.items():
            if rep.enum(at("inputs", name), t, TABLE_INPUT_TYPES, "an input type"):
                inputs[str(name)] = t
    outs = _outputs(rep, "outputs", data["outputs"]) if "outputs" in data else {}
    rows = _rows(rep, "rows", data.get("rows"), inputs, outs) if "rows" in data else []
    if isinstance(data.get("rows"), list):
        _catch_all(rep, "rows", data["rows"])
    control_rows = [{"id": r["id"], "when": r.get("when") or {}, "then": r["then"]}
                    for r in rows if isinstance(r.get("then"), Mapping) and _unseen_hit(outs, r["then"])]
    return {"control_rows": numify(control_rows),
            "unseen": {k: v.get("unseen") for k, v in sorted(outs.items()) if v.get("unseen")}}


def _table_overlay(rep: Rep, data: Mapping[str, Any], base: Loaded) -> dict[str, Any]:
    if data.get("id") != base.id:
        rep.add("id", "overlay_forbidden", f"this overlay is for table {data.get('id')!r}, not {base.id!r}")
    inputs = dict(base.data["inputs"])
    outs = dict(base.data["outputs"])
    rows = _rows(rep, "rows", data.get("rows"), inputs, outs) if "rows" in data else []
    base_rows = base.data["rows"]
    base_ids = {r["id"]: r for r in base_rows}
    last_id = base_rows[-1]["id"]
    base_control = {r["id"] for r in base_rows if _unseen_hit(outs, r["then"])}
    for i, row in enumerate(data.get("rows") or []):
        if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
            continue
        p = at("rows", i)
        rid = row["id"]
        then = row.get("then") if isinstance(row.get("then"), Mapping) else {}
        hits = _unseen_hit(outs, then)
        if rid in base_control:
            rep.add(p, "overlay_forbidden",
                    f"row {rid} is a control row (it passes something unseen); a sponsor overlay cannot change it")
        elif hits:
            rep.add(p, "overlay_forbidden",
                    f"row {rid} gives {hits[0]} (passes unseen); a sponsor overlay cannot change it")
        if rid in base_ids:
            if rid == last_id and row.get("when"):
                rep.add(p, "no_catch_all", f"row {rid} is the catch-all row and must stay without a when")
            elif rid != last_id and not row.get("when"):
                rep.add(p, "no_catch_all", f"row {rid} would match everything and shadow the rows after it")
        elif not row.get("when"):
            rep.add(p, "no_catch_all", f"new row {rid} is tried first, so it needs a when")
    return {"control_rows": [], "overlay_rows": len(rows)}
