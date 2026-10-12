"""Kinds `checks` and `controls`: the data-quality rules (one op or one guard per rule), the controller-only floors
and ceilings they may not loosen (`controls.yaml`), the code bounds (`CONTROL_BOUNDS`) a control itself must stay
inside, and the sponsor overlay that may only tighten a rule. CONTROL_BOUNDS equals worker.recon.CONTROL_LIMITS and
the check-data-quality limits in `schemas.md` section 7; tests/test_rulefiles.py asserts the recon part."""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from worker.rule_schema import (
    ID_RE,
    RECORD_RE,
    SEVERITIES,
    Loaded,
    Rep,
    at,
    dec,
    is_int,
    is_num,
    numify,
    sev_rank,
)

FAMILIES = ("completeness", "validity", "uniqueness", "consistency", "balance", "mapping", "reconcile")
FIELD_GROUPS = ("@required", "@ids", "@names", "@dates", "@amounts", "@key")
CHECK_OPS = ("blank", "max_length", "date", "number", "unique", "in_set", "reference", "balance")
ENGINE_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)+$")
SAFE_PLACEHOLDERS_CHECKS = ("field", "max")


_RULE_BASE = ("id", "family", "severity", "message")
_RULE_OPT = ("fix", "breakdown", "record_types", "op", "enforced_by", "fields", "when")
_OP_KEYS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "blank": (("fields",), ()),
    "max_length": (("fields", "max"), ()),
    "date": (("fields",), ()),
    "number": (("fields",), ()),
    "unique": (("fields",), ()),
    "in_set": (("fields", "set"), ()),
    "reference": (("fields", "set"), ()),
    "balance": (("group", "debit", "credit"), ("offset", "tolerance")),
}
_LOCKED_RULE_FIELDS = ("op", "enforced_by", "fields", "set", "group", "debit", "credit", "offset")


def _rule_fields(rep: Rep, ptr: str, v: Any) -> Any:
    if isinstance(v, str):
        if v.startswith("@"):
            if v not in FIELD_GROUPS:
                rep.add(ptr, "unknown_operator", f"{v!r} is not a field group; use one of {list(FIELD_GROUPS)}")
            return v
        rep.add(ptr, "wrong_type", "fields is a group (@required, @ids, ...) or a list of field names")
        return v
    return rep.text_list(ptr, v, empty_ok=False)


def validate_checks(rep: Rep, data: Mapping[str, Any]) -> dict[str, Any]:
    rep.shape("", data, ("schema_version", "kind", "rules"), ("description",))
    rep.description(data)
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        if "rules" in data:
            rep.add("rules", "wrong_type", "expected a non-empty list of rules")
        return {}
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for i, r in enumerate(rules):
        p = at("rules", i)
        if not isinstance(r, Mapping):
            rep.add(p, "wrong_type", "a rule is a mapping")
            continue
        op = r.get("op")
        extra_req: tuple[str, ...] = ()
        extra_opt: tuple[str, ...] = ()
        if "op" in r and op not in CHECK_OPS:
            rep.add(at(p, "op"), "unknown_operator", f"{op!r} is not an op; use one of {list(CHECK_OPS)}")
        elif op in _OP_KEYS:
            extra_req, extra_opt = _OP_KEYS[op]
        elif "op" not in r:
            extra_req = ("enforced_by",)  # a guard names the engine code that enforces it
        req = tuple(dict.fromkeys((*_RULE_BASE, *extra_req)))
        opt = tuple(k for k in dict.fromkeys((*_RULE_OPT, *extra_opt)) if k not in req)
        if op in CHECK_OPS:
            opt = tuple(k for k in opt if k != "enforced_by")
        elif "op" not in r:
            opt = tuple(k for k in opt if k not in ("fields", "when"))
        rep.shape(p, r, req, opt)
        rid = r.get("id")
        if "id" in r:
            if not isinstance(rid, str) or not ID_RE.match(rid.replace("_", "-")):
                rep.add(at(p, "id"), "wrong_type", f"rule id {rid!r} must be lower-case letters, digits, - or _")
            elif rid in seen:
                rep.add(at(p, "id"), "duplicate_id", f"rule id {rid!r} appears twice in this file")
            else:
                seen.add(rid)
        rep.when(p, r, "family", rep.enum, FAMILIES, "a rule family")
        rep.when(p, r, "severity", rep.enum, SEVERITIES, "a severity")
        rep.when(p, r, "message", rep.placeholders, SAFE_PLACEHOLDERS_CHECKS)
        rep.when(p, r, "fix", rep.placeholders, SAFE_PLACEHOLDERS_CHECKS)
        rep.when(p, r, "breakdown", rep.text)
        if "enforced_by" in r and (not isinstance(r["enforced_by"], str) or not ENGINE_NAME_RE.match(r["enforced_by"])):
            rep.add(at(p, "enforced_by"), "wrong_type", "enforced_by is a dotted engine name such as worker.recon")
        rtypes: list[str] = []
        if "record_types" in r:
            rtypes = rep.text_list(at(p, "record_types"), r["record_types"], empty_ok=False)
            for j, t in enumerate(rtypes):
                if not RECORD_RE.match(t):
                    rep.add(at(at(p, "record_types"), j), "wrong_type", f"{t!r} is not a record type name")
        fields_: Any = None
        if "fields" in r:
            fields_ = _rule_fields(rep, at(p, "fields"), r["fields"])
        ranges: list[str] = []
        wfield = ""
        if "when" in r:
            w = r["when"]
            if rep.shape(at(p, "when"), w, ("field", "ranges")):
                if "field" in w:
                    rep.text(at(at(p, "when"), "field"), w["field"])
                    wfield = str(w["field"])
                if "ranges" in w:
                    ranges = rep.ranges(at(at(p, "when"), "ranges"), w["ranges"])
        entry: dict[str, Any] = {"id": rid, "severity": r.get("severity")}
        for key in _LOCKED_RULE_FIELDS:
            if key in r:
                entry[key] = r[key]
        if "max" in r:
            if not is_int(r["max"]) or r["max"] < 1:
                rep.add(at(p, "max"), "wrong_type", "max is a whole number of at least 1")
            entry["max"] = r["max"]
        if "tolerance" in r:
            if not is_num(r["tolerance"]) or dec(r["tolerance"]) < 0:
                rep.add(at(p, "tolerance"), "wrong_type", "a tolerance is a number of at least 0")
            elif dec(r["tolerance"]) > CONTROL_BOUNDS["check-data-quality"]["balance_tolerance"]:
                rep.add(at(p, "tolerance"), "out_of_bounds",
                        f"{r['tolerance']} is over the code limit {CONTROL_BOUNDS['check-data-quality']['balance_tolerance']}")
            entry["tolerance"] = r["tolerance"]
        if rtypes:
            entry["record_types"] = sorted(rtypes)
        if ranges:
            entry["ranges"] = sorted(ranges)
            entry["range_field"] = wfield
        if fields_ is not None:
            entry["fields"] = fields_
        out.append(entry)
    return {"rules": numify(out)}



def checks_vs_controls(rep: Rep, checks: Loaded, controls: Loaded) -> None:
    """Layer 2 over layer 3: a learnable rule may only be at least as strict as the control it is bound by."""
    c = controls.controls
    rules = checks.data.get("rules") or []
    index = {r.get("id"): (i, r) for i, r in enumerate(rules) if isinstance(r, Mapping)}
    where = Path(controls.path).name
    for rid, floor in c.get("severity_floor", {}).items():
        if rid not in index:
            continue
        i, r = index[rid]
        if sev_rank(r.get("severity")) < sev_rank(floor):
            rep.add(at(at("rules", i), "severity"), "looser_than_control",
                    f"{rid} is {r.get('severity')}, below its floor {floor} in {where}; only a controller can lower it")
    for rid, ceil in c.get("max_ceiling", {}).items():
        if rid in index and is_int(index[rid][1].get("max")) and index[rid][1]["max"] > ceil:
            rep.add(at(at("rules", index[rid][0]), "max"), "looser_than_control",
                    f"{rid} allows {index[rid][1]['max']} characters, above its ceiling {ceil} in {where}; "
                    "only a controller can raise it")
    for rid, floor_ranges in c.get("ranges_floor", {}).items():
        if rid not in index:
            continue
        i, r = index[rid]
        have = set((r.get("when") or {}).get("ranges") or [])
        gone = sorted(set(floor_ranges) - have)
        if gone:
            rep.add(at(at(at("rules", i), "when"), "ranges"), "looser_than_control",
                    f"{rid} drops the range(s) {gone} that {where} requires; only a controller can remove one")
    for rid, row in c.get("balance", {}).items():
        if rid not in index:
            continue
        i, r = index[rid]
        if is_num(r.get("tolerance")) and dec(r["tolerance"]) > row["tolerance"]:
            rep.add(at(at("rules", i), "tolerance"), "looser_than_control",
                    f"{rid} tolerates {r['tolerance']}, above {row['tolerance']} in {where}; "
                    "only a controller can raise it")


def control_ids_exist(rep: Rep, controls: Loaded, rule_ids: set[str]) -> None:
    for section in ("severity_floor", "max_ceiling", "ranges_floor", "balance"):
        for rid in controls.controls.get(section, {}):
            if rid not in rule_ids:
                rep.add(at(section, rid), "unknown_reference",
                         f"{rid!r} is not a rule of this skill's checks.yaml; remove it or add the rule")


def checks_overlay(rep: Rep, data: Mapping[str, Any], base: Loaded) -> None:
    base_rules = {r["id"]: r for r in base.data.get("rules", []) if isinstance(r, Mapping)}
    for i, r in enumerate(data.get("rules") or []):
        if not isinstance(r, Mapping) or not isinstance(r.get("id"), str):
            continue
        p = at("rules", i)
        b = base_rules.get(r["id"])
        if b is None:
            if "op" not in r:
                rep.add(p, "overlay_forbidden",
                        f"rule {r['id']} is a guard enforced in engine code; a sponsor overlay may only add rules "
                        "that use an existing op")
            continue
        for key in _LOCKED_RULE_FIELDS:
            if r.get(key) != b.get(key):
                rep.add(at(p, key), "overlay_forbidden",
                        f"rule {r['id']}: {key} is locked to the organisation's value; a sponsor overlay can only "
                        "raise severity, add ranges or add a rule")
        if sev_rank(r.get("severity")) < sev_rank(b.get("severity")):
            rep.add(at(p, "severity"), "looser_than_control",
                    f"{r['id']} is {r.get('severity')}, below the organisation's {b.get('severity')}; "
                    "an overlay can only raise it")
        if is_int(r.get("max")) and is_int(b.get("max")) and r["max"] > b["max"]:
            rep.add(at(p, "max"), "looser_than_control",
                    f"{r['id']} allows {r['max']} characters, above the organisation's {b['max']}")
        mine = set((r.get("when") or {}).get("ranges") or [])
        theirs = set((b.get("when") or {}).get("ranges") or [])
        if theirs - mine:
            rep.add(at(at(p, "when"), "ranges"), "looser_than_control",
                    f"{r['id']} drops the range(s) {sorted(theirs - mine)} of the organisation's rule")
        if set(b.get("record_types") or []) - set(r.get("record_types") or []) and b.get("record_types"):
            rep.add(at(p, "record_types"), "looser_than_control",
                    f"{r['id']} drops a record type of the organisation's rule; removing one loosens it")
        if is_num(r.get("tolerance")) and is_num(b.get("tolerance")) and dec(r["tolerance"]) > dec(b["tolerance"]):
            rep.add(at(p, "tolerance"), "looser_than_control",
                    f"{r['id']} tolerates more than the organisation's rule")


TIER_STYLES = ("tiered", "flat")
_ZERO = Decimal(0)

CONTROL_BOUNDS: dict[str, Any] = {
    "reconcile-gl": {
        "tolerances": {  # key: (largest pass_up_to, largest warn_up_to, warn must equal pass)
            "money-tiered": (Decimal(5), Decimal(25), False),
            "money-flat": (Decimal(5), Decimal(5), True),
            "int": (_ZERO, _ZERO, True),
            "pct": (Decimal("0.05"), Decimal("0.05"), True),
        },
        "levels": {"within_warn_band": ("WARN", "ERROR"), "over_warn_limit": ("ERROR",),
                   "needs_judgement": ("REVIEW", "WARN", "ERROR")},
        "required_floor": {"is": {"gl_date"}, "cap": {"gl_date"}, "pcap": {"investor"}, "cf": {"gl_date"},
                           "irr": {"pr_date", "investor"}, "unf": {"investor"}, "wf": {"pr_date", "investor"}},
        "fields": ("gl_date", "pr_date", "investor"),
        "balance_target": _ZERO,
    },
    "check-data-quality": {
        "sections": ("severity_floor", "max_ceiling", "ranges_floor", "balance"),
        "max_ceiling": {"id_too_long": 20, "name_too_long": 100},  # 20 = target id limit; 100 = today's value
        "balance_tolerance": _ZERO,
    },
    "map-accounts": {"sections": ("severity_floor",)},
}


def validate_controls(rep: Rep, data: Mapping[str, Any], skill: str | None) -> dict[str, Any]:
    head = ("schema_version", "kind", "description")
    if skill not in CONTROL_BOUNDS:
        rep.add("", "unknown_reference", f"the engine has no bounds registered for the skill {skill!r}; an engineer adds them")
        return {}
    b = CONTROL_BOUNDS[skill]
    if skill == "reconcile-gl":
        rep.shape("", data, ("tolerances", "check_style", "levels", "balance", "required_fields"), head)
        return _controls_recon(rep, data, b)
    sections = b["sections"]
    rep.shape("", data, (), (*head, *sections))
    out: dict[str, Any] = {}
    if (sev := rep.section(data, "severity_floor", "rule id to ERR or WARN")) is not None:
        for rid, level in sev.items():
            rep.enum(at("severity_floor", rid), level, SEVERITIES, "a severity floor")
        out["severity_floor"] = dict(sev)
    if "max_ceiling" in sections and (ceil := rep.section(data, "max_ceiling", "rule id to a whole number")) is not None:
        for rid, v in ceil.items():
            p = at("max_ceiling", rid)
            if rid not in b["max_ceiling"]:
                rep.add(p, "unknown_reference", f"the engine registers no ceiling for {rid!r}; known: {sorted(b['max_ceiling'])}")
            elif not is_int(v) or v < 1:
                rep.add(p, "wrong_type", "a ceiling is a whole number of at least 1")
            elif v > b["max_ceiling"][rid]:
                rep.add(p, "out_of_bounds", f"{v} is over the code limit {b['max_ceiling'][rid]}")
        out["max_ceiling"] = dict(ceil)
    if "ranges_floor" in sections and (rf := rep.section(data, "ranges_floor", "rule id to a list of account ranges")) is not None:
        out["ranges_floor"] = {rid: sorted(rep.ranges(at("ranges_floor", rid), v)) for rid, v in rf.items()}
    if "balance" in sections and (bal := rep.section(data, "balance", "rule id to {tolerance: number}")) is not None:
        tol: dict[str, Any] = {}
        for rid, row in bal.items():
            p = at("balance", rid)
            if rep.shape(p, row, ("tolerance",)) and "tolerance" in row:
                v = row["tolerance"]
                if not is_num(v):
                    rep.add(at(p, "tolerance"), "wrong_type", "a tolerance is a number")
                elif dec(v) > b["balance_tolerance"] or dec(v) < 0:
                    rep.add(at(p, "tolerance"), "out_of_bounds",
                            f"{v} is outside the code limit: the balance tolerance is {b['balance_tolerance']}")
                else:
                    tol[rid] = {"tolerance": dec(v)}
        out["balance"] = tol
    return numify(out)  # type: ignore[no-any-return]


def _controls_recon(rep: Rep, data: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    tol = data.get("tolerances")
    if "tolerances" in data and rep.shape("tolerances", tol, tuple(b["tolerances"])):
        res: dict[str, Any] = {}
        for key, (max_pass, max_warn, same) in b["tolerances"].items():
            row = tol.get(key)
            p = at("tolerances", key)
            if row is None or not rep.shape(p, row, ("pass_up_to", "warn_up_to")):
                continue
            if not (is_num(row.get("pass_up_to")) and is_num(row.get("warn_up_to"))):
                if "pass_up_to" in row and "warn_up_to" in row:
                    rep.add(p, "wrong_type", "pass_up_to and warn_up_to are numbers")
                continue
            pv, wv = dec(row["pass_up_to"]), dec(row["warn_up_to"])
            res[key] = {"pass_up_to": pv, "warn_up_to": wv}
            if pv < 0 or pv > max_pass:
                rep.add(at(p, "pass_up_to"), "out_of_bounds", f"{pv} is outside 0 to the code limit {max_pass}")
            if wv < pv or wv > max_warn:
                rep.add(at(p, "warn_up_to"), "out_of_bounds",
                        f"{wv} must be at least pass_up_to and at most the code limit {max_warn}")
            elif same and wv != pv:
                rep.add(at(p, "warn_up_to"), "out_of_bounds", f"this tolerance has no WARN band: warn_up_to must equal {pv}")
        out["tolerances"] = res
    if (style := rep.section(data, "check_style", "check id to tiered or flat")) is not None:
        for cid, v in style.items():
            rep.enum(at("check_style", cid), v, TIER_STYLES, "a tolerance style")
        out["check_style"] = dict(style)
    lev = data.get("levels")
    if "levels" in data and rep.shape("levels", lev, tuple(b["levels"])):
        for name, allowed in b["levels"].items():
            v = lev.get(name)
            if v is not None and v not in allowed:
                rep.add(at("levels", name), "out_of_bounds", f"{v!r} is below the floor; allowed: {list(allowed)}")
        out["levels"] = dict(lev)
    bal = data.get("balance")
    if "balance" in data and rep.shape("balance", bal, ("target",)) and "target" in bal:
        if not is_num(bal["target"]):
            rep.add("balance.target", "wrong_type", "the balance target is a number")
        elif dec(bal["target"]) != b["balance_target"]:
            rep.add("balance.target", "out_of_bounds", f"the balance target must be {b['balance_target']}")
        out["balance"] = {"target": dec(bal["target"]) if is_num(bal["target"]) else bal["target"]}
    if (req := rep.section(data, "required_fields", "check id to a list of fields")) is not None:
        res2: dict[str, list[str]] = {}
        for cid, fields_ in req.items():
            p = at("required_fields", cid)
            got = rep.text_list(p, fields_)
            for f in got:
                if f not in b["fields"]:
                    rep.add(p, "out_of_bounds", f"{f!r} is not one of {list(b['fields'])}")
            gone = b["required_floor"].get(cid, set()) - set(got)
            if gone:
                rep.add(p, "out_of_bounds", f"{cid!r} cannot drop the required field(s) {sorted(gone)}; they are the floor")
            res2[cid] = sorted(got)
        out["required_fields"] = res2
    return numify(out)  # type: ignore[no-any-return]
