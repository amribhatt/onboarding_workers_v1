"""Kinds `target` and `synonyms`: what a record type of the upload template requires, how its IDs are made, how
it deduplicates and which staged checks run, plus the header words a source file may use for each field."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from worker.rule_schema import RECORD_RE, SEVERITIES, Context, Loaded, Rep, at, is_int, numify, sev_rank

ID_MODES = ("source", "sequential", "custom")
ID_TOKENS = ("PREFIX", "SEQ", "NAMECODE", "YEAR")  # what a custom pattern may use; a target file lists the ones it allows
ID_PATTERN_MAX = 60  # code bound: the longest custom pattern (the same bound as a safe identifier)
DEDUP_POLICIES = ("exclude", "keep")
DEDUP_REASONS = ("exists", "id_conflict", "name_match")
OUTPUT_TYPES = ("xlsx", "csv")
ID_MAX_LENGTH_BOUND = (1, 30)  # 30 = the longest ID limit a target has today (Affiliates)
REPORT_CONCEPTS = ("line", "investor", "date", "tt", "measure", "period", "amount")
MEMBERSHIP_FLAGS = ("static", "new_id_always", "unique_per_sponsor", "existing_store")


def validate_synonyms(rep: Rep, data: Mapping[str, Any], base: Loaded | None, ctx: Context) -> dict[str, Any]:
    scope = data.get("scope")
    by_type = scope == "target" and "types" in data  # one block per record type instead of one record_type
    if by_type:
        req: tuple[str, ...] = ("schema_version", "kind", "scope", "types")
    else:
        req = ("schema_version", "kind", "scope", "concepts") + (("record_type",) if scope == "target" else ())
    rep.shape("", data, req, ("description", "record_type") if scope == "report" else ("description",))
    if by_type:
        return _synonym_types(rep, data, base, ctx)
    rep.description(data)
    if "scope" in data:
        rep.enum("scope", scope, ("target", "report"), "a scope")
    rt = data.get("record_type")
    if "record_type" in data and scope == "report":
        rep.add("record_type", "unknown_field", "a report synonym file has no record_type")
    if scope == "target" and isinstance(rt, str):
        if not RECORD_RE.match(rt):
            rep.add("record_type", "wrong_type", f"{rt!r} is not a record type name")
        elif ctx.record_types is not None and rt not in ctx.record_types:
            rep.add("record_type", "unknown_reference", f"{rt!r} is not a target record type")
    con = data.get("concepts")
    if "concepts" in data:
        if isinstance(con, Mapping) and con:
            for name, heads in con.items():
                p = at("concepts", name)
                rep.text_list(p, heads, unique_ci=True, empty_ok=False)
                if scope == "report" and name not in REPORT_CONCEPTS:
                    rep.add(p, "unknown_reference", f"{name!r} is not a report column the engine reads; known: {list(REPORT_CONCEPTS)}")
                if scope == "target" and ctx.target_fields is not None and isinstance(rt, str) \
                        and rt in ctx.target_fields and name not in ctx.target_fields[rt]:
                    rep.add(p, "unknown_reference", f"{name!r} is not a field of the {rt} template")
        else:
            rep.add("concepts", "wrong_type", "concepts is a non-empty mapping of field to a list of header words")
    if base is not None and (base.data.get("scope") != scope
                             or base.data.get("record_type") != data.get("record_type")):
        rep.add("scope", "overlay_forbidden",
                "an overlay adds words to the same scope and record type as the organisation's file")
    return {}


def _synonym_types(rep: Rep, data: Mapping[str, Any], base: Loaded | None, ctx: Context) -> dict[str, Any]:
    """A target synonyms file with a block per record type: `types: {Investors: {Investor_ID: [words]}, ...}`."""
    rep.description(data)
    types = data.get("types")
    if not isinstance(types, Mapping) or not types:
        rep.add("types", "wrong_type", "types is a non-empty mapping of record type to its concepts")
        return {}
    for rt, block in types.items():
        p = at("types", rt)
        if not isinstance(rt, str) or not RECORD_RE.match(rt):
            rep.add(p, "wrong_type", f"{rt!r} is not a record type name")
        elif ctx.record_types is not None and rt not in ctx.record_types:
            rep.add(p, "unknown_reference", f"{rt!r} is not a target record type")
        if not isinstance(block, Mapping) or not block:
            rep.add(p, "wrong_type", "a record type block is a non-empty mapping of field to a list of header words")
            continue
        for name, heads in block.items():
            q = at(p, name)
            rep.text_list(q, heads, unique_ci=True, empty_ok=False)
            if ctx.target_fields is not None and isinstance(rt, str) and rt in ctx.target_fields \
                    and name not in ctx.target_fields[rt]:
                rep.add(q, "unknown_reference", f"{name!r} is not a field of the {rt} template")
    if base is not None:
        mine = set(types) if isinstance(types, Mapping) else set()
        theirs = set(base.data.get("types") or ())
        if base.data.get("scope") != "target" or "types" not in base.data or not mine <= theirs:
            rep.add("types", "overlay_forbidden",
                    "an overlay adds words to record types the organisation's file already has blocks for")
    return {}


def validate_target(rep: Rep, data: Mapping[str, Any], stem: str, base: Loaded | None, ctx: Context,
                     ) -> dict[str, Any]:
    rep.shape("", data, ("schema_version", "kind", "id", "template", "output", "controls"),
           ("name_field", "description", "membership"))
    rep.ident(data, stem, RECORD_RE)
    rt = data.get("id") if isinstance(data.get("id"), str) else stem
    rep.description(data)
    if "template" in data and (not rep.text("template", data["template"]) or "/" in data["template"]
                               or "\\" in data["template"]) and isinstance(data["template"], str):
        rep.add("template", "wrong_type", "template is a file name in the same folder")
    if "output" in data:
        rep.enum("output", data["output"], OUTPUT_TYPES, "an output type")
    if "name_field" in data:
        rep.text("name_field", data["name_field"], blank_ok=True)
    c = data.get("controls")
    out: dict[str, Any] = {"output": data.get("output")}
    fields = ctx.target_fields.get(rt) if ctx.target_fields and isinstance(rt, str) else None
    if "membership" in data:
        out["membership"] = _membership(rep, data["membership"], fields, c)
    if "controls" not in data or not rep.shape("controls", c, ("required", "key"),
                                            ("id_formats", "depends", "dedup", "checks", "levels")):
        return out
    for key in ("required", "key"):
        if key in c:
            got = rep.text_list(at("controls", key), c[key], empty_ok=False)
            out[key] = sorted(got) if key == "required" else got
            if fields is not None:
                for j, f in enumerate(got):
                    if f not in fields:
                        rep.add(at(at("controls", key), j), "unknown_reference", f"{f!r} is not a field of the {rt} template")
    if "id_formats" in c:
        out["id_formats"] = _id_formats(rep, c["id_formats"])
    if "depends" in c:
        dep = c["depends"]
        res = []
        if rep.items("controls.depends", dep, what="a list of {field, record_type}"):
            for i, d in enumerate(dep):
                p = at("controls.depends", i)
                if rep.shape(p, d, ("field", "record_type")):
                    if isinstance(d.get("record_type"), str) and ctx.record_types is not None \
                            and d["record_type"] not in ctx.record_types:
                        rep.add(at(p, "record_type"), "unknown_reference", f"{d['record_type']!r} is not a target record type")
                    if isinstance(d.get("field"), str) and fields is not None and d["field"] not in fields:
                        rep.add(at(p, "field"), "unknown_reference", f"{d['field']!r} is not a field of the {rt} template")
                    res.append([d.get("field"), d.get("record_type")])
        out["depends"] = sorted(res, key=str)
    if "dedup" in c:
        dd = c["dedup"]
        if rep.shape("controls.dedup", dd, (), DEDUP_REASONS):
            for reason, action in dd.items():
                rep.enum(at("controls.dedup", reason), action, DEDUP_POLICIES, "a dedup policy")
            out["dedup"] = dict(dd)
    if "checks" in c:
        got = rep.text_list("controls.checks", c["checks"])
        if ctx.staged_checks is not None:
            for j, code in enumerate(got):
                if code not in ctx.staged_checks:
                    rep.add(at("controls.checks", j), "unknown_reference", f"{code!r} is not a staged check")
        out["checks"] = sorted(got)
    if "levels" in c:
        lv = c["levels"]
        if isinstance(lv, Mapping):
            for code, level in lv.items():
                rep.enum(at("controls.levels", code), level, SEVERITIES, "a level")
                if ctx.staged_checks is not None and code not in ctx.staged_checks:
                    rep.add(at("controls.levels", code), "unknown_reference", f"{code!r} is not a staged check")
            out["levels"] = dict(lv)
        else:
            rep.add("controls.levels", "wrong_type", "levels is a mapping of check code to ERR or WARN")
    if base is not None:
        _target_overlay(rep, out, base)
    return numify(out)  # type: ignore[no-any-return]


def _membership(rep: Rep, m: Any, fields: frozenset[str] | None, controls: Any) -> dict[str, Any]:
    """Where this record type sits in the sample world: the names it is listed under, the flags the job code reads
    (static, always a new ID, unique for the sponsor, held by the existing-records store), the ID field generated when
    blank, and the type that follows the last of its names in the onboarding order."""
    p = "membership"
    if not rep.shape(p, m, ("types",), (*MEMBERSHIP_FLAGS, "id_when_blank", "next")):
        return {}
    out: dict[str, Any] = {}
    if "types" in m:
        out["types"] = rep.text_list(at(p, "types"), m["types"], unique_ci=True, empty_ok=False)
    for key in MEMBERSHIP_FLAGS:
        if key in m and rep.flag(at(p, key), m[key]):
            out[key] = m[key]
    if "id_when_blank" in m and rep.text(at(p, "id_when_blank"), m["id_when_blank"]):
        f = m["id_when_blank"]
        out["id_when_blank"] = f
        if fields is not None and f not in fields:
            rep.add(at(p, "id_when_blank"), "unknown_reference", f"{f!r} is not a field of this template")
        key_fields = controls.get("key") if isinstance(controls, Mapping) else None
        if isinstance(key_fields, list) and f not in key_fields:
            rep.add(at(p, "id_when_blank"), "unknown_reference", f"{f!r} is not in this record type's key")
    if "next" in m and rep.text(at(p, "next"), m["next"]):
        out["next"] = m["next"]
        if m["next"] in out.get("types", ()):
            rep.add(at(p, "next"), "wrong_type", "next is the type after this file's own names, not one of them")
    return out


def _id_formats(rep: Rep, f: Any) -> dict[str, Any]:
    p = "controls.id_formats"
    if not rep.shape(p, f, (), ("max_length", "generation", "tokens", "namecode_length")):
        return {}
    out: dict[str, Any] = {}
    if "tokens" in f:
        got = rep.text_list(at(p, "tokens"), f["tokens"], empty_ok=False)
        for i, tok in enumerate(got):
            rep.enum(at(at(p, "tokens"), i), tok, ID_TOKENS, "a pattern token")
        out["tokens"] = got
    if "namecode_length" in f:
        v = f["namecode_length"]
        if not is_int(v) or not 1 <= v <= ID_MAX_LENGTH_BOUND[1]:
            rep.add(at(p, "namecode_length"), "out_of_bounds", "namecode_length is a whole number from 1 to the ID limit")
        out["namecode_length"] = v
    if "max_length" in f:
        v = f["max_length"]
        lo, hi = ID_MAX_LENGTH_BOUND
        if not is_int(v):
            rep.add(at(p, "max_length"), "wrong_type", "max_length is a whole number")
        elif not lo <= v <= hi:
            rep.add(at(p, "max_length"), "out_of_bounds", f"{v} is outside the code bound {lo} to {hi}")
        out["max_length"] = v
    if "generation" in f:
        g = f["generation"]
        gp = at(p, "generation")
        if rep.shape(gp, g, ("mode",), ("prefix", "width", "start", "pattern")):
            mode = g.get("mode")
            if "mode" in g and rep.enum(at(gp, "mode"), mode, ID_MODES, "an ID mode"):
                if mode == "custom" and "pattern" not in g:
                    rep.add(at(gp, "pattern"), "missing_field", "the custom mode needs a pattern")
                if mode != "custom" and "pattern" in g:
                    rep.add(at(gp, "pattern"), "unknown_field", "only the custom mode has a pattern")
                if mode != "sequential":
                    for key in ("prefix", "width", "start"):
                        if key in g:
                            rep.add(at(gp, key), "unknown_field", f"only the sequential mode has {key}")
            for key in ("width", "start"):
                if key in g and (not is_int(g[key]) or g[key] < (1 if key == "width" else 0)):
                    rep.add(at(gp, key), "wrong_type", f"{key} is a whole number")
            for key in ("prefix", "pattern"):
                if key in g:
                    rep.text(at(gp, key), g[key])
            out["generation"] = dict(g)
    return out


def _target_overlay(rep: Rep, mine: Mapping[str, Any], base: Loaded) -> None:
    b = base.controls
    loose = "looser_than_control"
    for key in ("key", "output"):
        if key in b and mine.get(key) != b.get(key):
            rep.add(f"controls.{key}" if key == "key" else key, "overlay_forbidden",
                    f"{key} is locked to the organisation's value; a sponsor overlay cannot change it")
    if "membership" in mine and mine["membership"] != b.get("membership"):
        rep.add("membership", "overlay_forbidden",
                "membership is locked to the organisation's value; a sponsor overlay cannot change it")
    if (mine.get("id_formats") or {}).get("generation") != (b.get("id_formats") or {}).get("generation"):
        rep.add("controls.id_formats.generation", "overlay_forbidden",
                "ID generation is locked to the organisation's value; a sponsor overlay cannot change it")
    if set(b.get("required") or []) - set(mine.get("required") or []):
        rep.add("controls.required", loose, f"drops required field(s) "
                f"{sorted(set(b['required']) - set(mine.get('required') or []))} of the organisation's target; only a "
                "controller can lower it")
    extra = set((mine.get("id_formats") or {}).get("tokens") or ()) - set((b.get("id_formats") or {}).get("tokens") or ())
    if extra:
        rep.add("controls.id_formats.tokens", loose, f"adds pattern token(s) {sorted(extra)} the organisation's target "
                "does not allow; an overlay may only remove them")
    bm, mm = (b.get("id_formats") or {}).get("max_length"), (mine.get("id_formats") or {}).get("max_length")
    if bm is not None and (mm is None or mm > bm):
        rep.add("controls.id_formats.max_length", loose, f"{mm} is above the organisation's {bm}; an overlay may only shorten it")
    if {tuple(x) for x in b.get("depends") or []} - {tuple(x) for x in mine.get("depends") or []}:
        rep.add("controls.depends", loose, "drops a dependency of the organisation's target")
    for reason, action in (b.get("dedup") or {}).items():
        if action == "exclude" and (mine.get("dedup") or {}).get(reason, "exclude") != "exclude":
            rep.add(at("controls.dedup", reason), loose, f"{reason} is exclude in the organisation's target; keeping is looser")
    bc, mc = b.get("checks") or [], mine.get("checks") or []
    if (not bc and mc) or (bc and mc and set(bc) - set(mc)):  # an empty list runs every check: the strictest
        rep.add("controls.checks", loose, "runs fewer staged checks than the organisation's target; "
                "an empty list (all checks) or a superset is allowed")
    for code, level in (b.get("levels") or {}).items():
        if sev_rank((mine.get("levels") or {}).get(code, level)) < sev_rank(level):
            rep.add(at("controls.levels", code), loose, f"{code} is {(mine.get('levels') or {}).get(code)}, below "
                    f"the organisation's {level}; an overlay may only raise it")
