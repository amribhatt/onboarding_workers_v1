"""Kind `recipe`: a sponsor's per-field steps from source columns to template fields, written from a closed set of
ops with bounded sizes. A recipe is checked here for type flow and safety; running it is `worker.transforms`."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import PurePosixPath
from typing import Any

from worker.rule_schema import RECORD_RE, Context, Rep, at, is_int, is_num

SPONSOR_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
SETUP_KEYS = ("gp_id", "fund_id", "fund_name", "entity_id")
OPS: tuple[str, ...] = (
    "trim", "case", "treat_as_blank", "strip_prefix", "strip_suffix", "split", "regex_extract", "truncate", "crosswalk",
    "join", "coalesce", "date_parse", "number_parse", "negate", "debit_credit", "when", "default",
)
RECIPE_BOUNDS = {"ops": 8, "columns": 6, "join_parts": 8, "date_patterns": 4, "pairs": 500, "blank_values": 20,
                 "literal": 200, "note": 200, "regex": 100, "regex_quantifiers": 6, "regex_repeat": 50}
_THOUSANDS = (",", ".", " ", "'", " ")
_CMPS = ("eq", "ne", "in", "not_in", "starts_with", "blank", "not_blank", "lt", "le", "gt", "ge")
_ORDERED = ("lt", "le", "gt", "ge")
_DATE_TOKENS = ("YYYY", "YY", "Mon", "MM", "DD", "M", "D")
_SUPPLY_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SUPPLY_NUM = re.compile(r"^-?\d+(\.\d+)?$")


def _date_pattern_problem(pat: str) -> tuple[str | None, bool]:
    """(problem, has_YY) for one date pattern made of the closed tokens and literal characters."""
    i, kinds, yy = 0, [], False
    while i < len(pat):
        tok = next((t for t in _DATE_TOKENS if pat.startswith(t, i)), None)
        if tok is None:
            i += 1
            continue
        kinds.append("year" if tok in ("YYYY", "YY") else "month" if tok in ("MM", "M", "Mon") else "day")
        yy = yy or tok == "YY"
        i += len(tok)
    for need in ("year", "month", "day"):
        if kinds.count(need) != 1:
            return f"pattern {pat!r} must contain exactly one {need} token (YYYY, YY, MM, M, Mon, DD, D)", yy
    return None, yy


def regex_problem(p: str) -> str | None:
    """None when `p` is inside the safe subset of ticket 02: literals, a few escapes, classes, anchors, bounded
    quantifiers and exactly one capturing group that carries no quantifier."""
    if len(p) > RECIPE_BOUNDS["regex"]:
        return f"longer than {RECIPE_BOUNDS['regex']} characters"
    i, n, groups, quant, in_class, after_group = 0, len(p), 0, 0, False, False
    while i < n:
        c = p[i]
        if c == "\\":
            if i + 1 >= n or p[i + 1] not in "dws.-[]()\\":
                return f"the escape at position {i} is not one of \\d \\w \\s \\. \\- \\[ \\] \\( \\) \\\\"
            i += 2
            after_group = False
            continue
        if in_class:
            in_class = c != "]"
            i += 1
            continue
        if c in "?*+{":
            if after_group:
                return "a quantifier on a group is not allowed"
            quant += 1
            if c == "{":
                m = re.match(r"\{(\d+)(?:,(\d+))?\}", p[i:])
                if not m or int(m.group(2) or m.group(1)) > RECIPE_BOUNDS["regex_repeat"]:
                    return f"a repeat count over {RECIPE_BOUNDS['regex_repeat']} or a malformed {{m,n}}"
                i += m.end()
            else:
                i += 1
            if i < n and p[i] in "?+":
                return "lazy and possessive quantifiers are not allowed"
            after_group = False
            continue
        after_group = False
        if c == "[":
            in_class = True
            i += 1
            if p[i:i + 1] == "^":
                i += 1
            continue
        if c == "(":
            if p[i + 1:i + 2] == "?":
                return "lookaround, named groups and inline flags are not allowed"
            groups += 1
            if groups > 1:
                return "exactly one capturing group is allowed"
        elif c == ")":
            after_group = True
        elif c == "|":
            return "alternation | is not allowed"
        elif c == ".":
            return "'.' is not allowed; use a class such as [^,]"
        i += 1
    if in_class:
        return "a [ class is not closed"
    if groups != 1:
        return "exactly one capturing group is required"
    if quant > RECIPE_BOUNDS["regex_quantifiers"]:
        return f"more than {RECIPE_BOUNDS['regex_quantifiers']} quantifiers"
    return None


@dataclass
class _Pipe:
    rep: Rep
    cols: int = 0


def _lit(rep: Rep, ptr: str, v: Any, *, blank_ok: bool = False) -> bool:
    return rep.text(ptr, v, longest=RECIPE_BOUNDS["literal"], blank_ok=blank_ok)


def _operand(pipe: _Pipe, ptr: str, v: Any, *, allow: tuple[str, ...] = ("col", "text", "setup")) -> str:
    """One operand; returns the type of value it yields ("any" for a column, "text" otherwise)."""
    rep = pipe.rep
    if not isinstance(v, Mapping) or not v:
        rep.add(ptr, "wrong_type", "an operand is {col: header}, {text: literal} or {setup: key}")
        return "any"
    kind = next((k for k in ("col", "text", "setup") if k in v), None)
    extra = set(v) - {kind, "occurrence"} if kind == "col" else set(v) - {kind}
    if kind is None or kind not in allow:
        rep.add(ptr, "unknown_operator", f"{dict(v)!r} is not an operand here; use one of {list(allow)}")
        return "any"
    for k in sorted(extra, key=str):
        rep.add(at(ptr, k), "unknown_field", f"{k!r} is not part of a {kind} operand")
    if kind == "col":
        pipe.cols += 1
        rep.text(at(ptr, "col"), v["col"])
        if "occurrence" in v and (not is_int(v["occurrence"]) or v["occurrence"] < 1):
            rep.add(at(ptr, "occurrence"), "wrong_type", "occurrence is a whole number from 1")
        return "any"
    if kind == "text":
        _lit(rep, at(ptr, "text"), v["text"], blank_ok=True)
        return "text"
    if v["setup"] not in SETUP_KEYS:
        rep.add(at(ptr, "setup"), "unknown_reference", f"{v['setup']!r} is not a job setup value; use one of {list(SETUP_KEYS)}")
    return "text"


def _operand_list(pipe: _Pipe, ptr: str, o: Mapping[str, Any], key: str, limit: int | None = None) -> None:
    items = o.get(key)
    if isinstance(items, list) and items:
        if limit is not None and len(items) > limit:
            pipe.rep.add(at(ptr, key), "out_of_bounds", f"{len(items)} {key}, over the limit of {limit}")
        for j, item in enumerate(items):
            _operand(pipe, at(at(ptr, key), j), item)
    elif key in o:
        pipe.rep.add(at(ptr, key), "wrong_type", f"{key} is a non-empty list of operands")


def _number_params(rep: Rep, ptr: str, o: Mapping[str, Any]) -> None:
    rep.when(ptr, o, "decimal", rep.enum, (".", ","), "the decimal mark")
    if "thousands" in o:
        th = o["thousands"]
        if rep.items(at(ptr, "thousands"), th, what="a list of separators"):
            for j, t in enumerate(th):
                rep.enum(at(at(ptr, "thousands"), j), t, _THOUSANDS, "a thousands separator")
            if o.get("decimal") in th:
                rep.add(at(ptr, "thousands"), "wrong_type", "the thousands separator cannot be the decimal mark")
    if "negative" in o:
        ng = o["negative"]
        if rep.items(at(ptr, "negative"), ng, what="a list of negative forms"):
            for j, t in enumerate(ng):
                rep.enum(at(at(ptr, "negative"), j), t, ("minus", "brackets", "trailing_minus"), "a negative form")
    if "currency" in o:
        for j, t in enumerate(rep.text_list(at(ptr, "currency"), o["currency"])):
            _lit(rep, at(at(ptr, "currency"), j), t)
    rep.when(ptr, o, "percent", rep.enum, ("reject", "as_fraction", "as_number"), "a percent treatment")


def _branch(pipe: _Pipe, ptr: str, b: Any, cur: str) -> str | None:
    """A `when` branch; returns its type, or None when it fits any type."""
    if b == "keep":
        return cur
    if b == "blank":
        return None
    if isinstance(b, Mapping):
        return _operand(pipe, ptr, b)
    pipe.rep.add(ptr, "unknown_operator", f"{b!r} is not a branch; use keep, blank, {{text}}, {{col}} or {{setup}}")
    return None


def _op(pipe: _Pipe, ptr: str, o: Any, cur: str) -> str:
    """Validate one op against the type flowing in; returns the type flowing out."""
    rep = pipe.rep
    if not isinstance(o, Mapping) or "op" not in o:
        rep.add(ptr, "missing_field" if isinstance(o, Mapping) else "wrong_type", "an op is a mapping with an 'op' name")
        return cur
    name = o["op"]
    if name not in OPS:
        rep.add(at(ptr, "op"), "unknown_operator", f"{name!r} is not an op; use one of {list(OPS)}")
        return cur
    spec: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
        "trim": ((), ("inner",)), "case": (("to",), ()), "treat_as_blank": (("values",), ("ci",)),
        "strip_prefix": (("text",), ("ci",)), "strip_suffix": (("text",), ("ci",)),
        "split": (("sep", "index"), ("trim",)), "join": (("parts",), ("sep", "skip_blank")),
        "coalesce": (("cols",), ()), "regex_extract": (("pattern",), ("ci", "on_miss")),
        "truncate": (("max",), ()), "date_parse": (("patterns",), ("yy_pivot",)),
        "number_parse": (("decimal",), ("thousands", "negative", "currency", "percent")), "negate": ((), ()),
        "debit_credit": (("debit", "credit", "number"), ("both_sides",)),
        "crosswalk": ((), ("pairs", "table", "output", "records", "match_on", "take", "match", "on_miss")),
        "when": (("test", "then", "else"), ()), "default": (("value",), ()),
    }
    req, opt = spec[name]
    for k in req:
        if k not in o:
            rep.add(at(ptr, k), "missing_field", f"the {name} op needs {k!r}")
    for k in o:
        if k != "op" and k not in req and k not in opt:
            rep.add(at(ptr, k), "unknown_field", f"{k!r} is not a parameter of {name}; allowed: {sorted({*req, *opt})}")
    if name == "negate":
        if cur != "number":
            rep.add(ptr, "wrong_type", "negate needs a number; put number_parse before it")
        return "number"
    if name == "trim" and "inner" in o:
        rep.enum(at(ptr, "inner"), o["inner"], ("keep", "collapse"), "inner")
    if name == "case" and "to" in o:
        rep.enum(at(ptr, "to"), o["to"], ("upper", "lower"), "to")
    if name == "treat_as_blank" and "values" in o:
        vals = rep.text_list(at(ptr, "values"), o["values"], empty_ok=False)
        if len(vals) > RECIPE_BOUNDS["blank_values"]:
            rep.add(at(ptr, "values"), "out_of_bounds", f"{len(vals)} values, over the limit of {RECIPE_BOUNDS['blank_values']}")
    if name in ("strip_prefix", "strip_suffix") and "text" in o:
        _lit(rep, at(ptr, "text"), o["text"])
    if name in ("treat_as_blank", "strip_prefix", "strip_suffix", "regex_extract") and "ci" in o:
        rep.flag(at(ptr, "ci"), o["ci"])
    if name == "split":
        if "sep" in o:
            _lit(rep, at(ptr, "sep"), o["sep"])
        if "index" in o and (not is_int(o["index"]) or o["index"] == 0):
            rep.add(at(ptr, "index"), "wrong_type", "index is a whole number, 1-based, negative from the end; never 0")
        rep.when(ptr, o, "trim", rep.flag)
    if name == "join":
        _operand_list(pipe, ptr, o, "parts", RECIPE_BOUNDS["join_parts"])
        if "sep" in o:
            _lit(rep, at(ptr, "sep"), o["sep"], blank_ok=True)
        rep.when(ptr, o, "skip_blank", rep.flag)
        return "text"
    if name == "coalesce":
        _operand_list(pipe, ptr, o, "cols")
        return cur
    if name == "regex_extract":
        if "pattern" in o and rep.text(at(ptr, "pattern"), o["pattern"]):
            problem = regex_problem(o["pattern"])
            if problem:
                rep.add(at(ptr, "pattern"), "unknown_operator", f"the pattern is outside the safe subset: {problem}")
        rep.when(ptr, o, "on_miss", rep.enum, ("fail", "missing"), "on_miss")
        return "text"
    if name == "truncate":
        if "max" in o and (not is_int(o["max"]) or o["max"] < 1):
            rep.add(at(ptr, "max"), "wrong_type", "max is a whole number from 1")
        return "text"
    if name == "date_parse":
        pats = o.get("patterns")
        has_yy = False
        if isinstance(pats, list) and pats:
            if len(pats) > RECIPE_BOUNDS["date_patterns"]:
                rep.add(at(ptr, "patterns"), "out_of_bounds",
                        f"{len(pats)} patterns, over the limit of {RECIPE_BOUNDS['date_patterns']}")
            for j, pat in enumerate(pats):
                if rep.text(at(at(ptr, "patterns"), j), pat):
                    problem, yy = _date_pattern_problem(pat)
                    has_yy = has_yy or yy
                    if problem:
                        rep.add(at(at(ptr, "patterns"), j), "wrong_type", problem)
        elif "patterns" in o:
            rep.add(at(ptr, "patterns"), "wrong_type", "patterns is a list of 1 to 4 date patterns")
        if has_yy and "yy_pivot" not in o:
            rep.add(at(ptr, "yy_pivot"), "missing_field", "a pattern with YY needs yy_pivot; there is no default century")
        if "yy_pivot" in o and (not is_int(o["yy_pivot"]) or not 0 <= o["yy_pivot"] <= 99):
            rep.add(at(ptr, "yy_pivot"), "wrong_type", "yy_pivot is a whole number from 0 to 99")
        if cur == "number":
            rep.add(ptr, "wrong_type", "date_parse reads text or a date, not a number; Excel serial numbers are not read")
        return "date"
    if name == "number_parse":
        _number_params(rep, ptr, o)
        if cur == "date":
            rep.add(ptr, "wrong_type", "number_parse reads text or a number, not a date")
        return "number"
    if name == "debit_credit":
        for side in ("debit", "credit"):
            if side in o:
                _operand(pipe, at(ptr, side), o[side], allow=("col",))
        number_fields = ("thousands", "negative", "currency", "percent")
        if "number" in o and rep.shape(at(ptr, "number"), o["number"], ("decimal",), number_fields):
            _number_params(rep, at(ptr, "number"), o["number"])
        rep.when(ptr, o, "both_sides", rep.enum, ("net", "fail"), "both_sides")
        return "number"
    if name == "crosswalk":
        _crosswalk(rep, ptr, o)
        return "text"
    if name == "when":
        return _when(pipe, ptr, o, cur)
    if name == "default":
        if "value" in o:
            _operand(pipe, at(ptr, "value"), o["value"], allow=("text", "setup"))
        return cur
    return "text"  # trim, case, treat_as_blank, strip_prefix, strip_suffix, split


def _crosswalk(rep: Rep, ptr: str, o: Mapping[str, Any]) -> None:
    given = [k for k in ("pairs", "table", "records") if k in o]
    if len(given) != 1:
        rep.add(ptr, "missing_field" if not given else "wrong_type",
                f"a crosswalk has exactly one of pairs, table or records, not {given or 'none'}")
        return
    src = given[0]
    if src == "pairs":
        pairs = o["pairs"]
        if isinstance(pairs, Mapping) and pairs:
            if len(pairs) > RECIPE_BOUNDS["pairs"]:
                rep.add(at(ptr, "pairs"), "out_of_bounds", f"{len(pairs)} pairs, over the limit of {RECIPE_BOUNDS['pairs']}")
            for k, v in pairs.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    rep.add(at(at(ptr, "pairs"), k), "wrong_type", "pairs map text to text")
                elif len(k) > RECIPE_BOUNDS["literal"] or len(v) > RECIPE_BOUNDS["literal"]:
                    rep.add(at(at(ptr, "pairs"), k), "out_of_bounds", "a pair is longer than 200 characters")
        else:
            rep.add(at(ptr, "pairs"), "wrong_type", "pairs is a non-empty mapping of source value to target value")
    elif src == "table":
        rep.text(at(ptr, "table"), o["table"])
        if "output" not in o:
            rep.add(at(ptr, "output"), "missing_field", "a table crosswalk names the output to take")
    else:
        if isinstance(o["records"], str) and not RECORD_RE.match(o["records"]):
            rep.add(at(ptr, "records"), "wrong_type", f"{o['records']!r} is not a record type name")
        for k, allowed in (("match_on", ("id", "key", "name")), ("take", ("id", "name"))):
            if k not in o:
                rep.add(at(ptr, k), "missing_field", f"a records crosswalk needs {k!r}")
            else:
                rep.enum(at(ptr, k), o[k], allowed, k)
    if src != "table" and "output" in o:
        rep.add(at(ptr, "output"), "unknown_field", "output belongs to a table crosswalk")
    if src != "records":
        for k in ("match_on", "take"):
            if k in o:
                rep.add(at(ptr, k), "unknown_field", f"{k} belongs to a records crosswalk")
    rep.when(ptr, o, "match", rep.enum, ("exact", "casefold"), "match")
    rep.when(ptr, o, "on_miss", rep.enum, ("fail", "missing"), "on_miss")


def _when(pipe: _Pipe, ptr: str, o: Mapping[str, Any], cur: str) -> str:
    rep = pipe.rep
    t = o.get("test")
    tp = at(ptr, "test")
    cmp_ = None
    if "test" in o and rep.shape(tp, t, ("left", "cmp"), ("right",)):
        left = t.get("left")
        if "left" in t and left != "value":
            _operand(pipe, at(tp, "left"), left, allow=("col",))
        cmp_ = t.get("cmp")
        if "cmp" in t and cmp_ not in _CMPS:
            rep.add(at(tp, "cmp"), "unknown_operator", f"{cmp_!r} is not a comparison; use one of {list(_CMPS)}")
            cmp_ = None
        if cmp_ in _ORDERED and (left != "value" or cur not in ("number", "date")):
            rep.add(at(tp, "cmp"), "wrong_type",
                    f"{cmp_} compares only the current value, and only once it is a number or a date")
        needs_right = cmp_ not in ("blank", "not_blank")
        if cmp_ and needs_right and "right" not in t:
            rep.add(at(tp, "right"), "missing_field", f"{cmp_} needs a right side")
        if cmp_ and not needs_right and "right" in t:
            rep.add(at(tp, "right"), "unknown_field", f"{cmp_} takes no right side")
        if "right" in t:
            r = t["right"]
            if cmp_ in ("in", "not_in"):
                rep.text_list(at(tp, "right"), r, empty_ok=False)
            else:
                _lit(rep, at(tp, "right"), r)
    a = _branch(pipe, at(ptr, "then"), o.get("then"), cur) if "then" in o else None
    b = _branch(pipe, at(ptr, "else"), o.get("else"), cur) if "else" in o else None
    known = {x for x in (a, b) if x and x != "any"}
    if len(known) > 1:
        rep.add(ptr, "wrong_type", f"the two branches give different types ({sorted(known)}); they must agree")
    return next(iter(known), cur)


def _field_kind(name: str) -> str:
    return "date" if name.endswith("_Date") else "number" if name.endswith("_Amount") else "text"


def validate_recipe(rep: Rep, data: Mapping[str, Any], stem: str, rel: str, ctx: Context) -> dict[str, Any]:
    rep.shape("", data, ("schema_version", "kind", "id", "sponsor", "fields", "approved_by", "approved_at", "reason"),
           ("description",))
    rep.ident(data, stem, RECORD_RE)
    rt = data.get("id") if isinstance(data.get("id"), str) else stem
    rep.description(data)
    if isinstance(rt, str) and ctx.record_types is not None and rt not in ctx.record_types:
        rep.add("id", "unknown_reference", f"{rt!r} is not a target record type; a recipe needs a target")
    if "sponsor" in data:
        s = data["sponsor"]
        folder = ctx.sponsor
        parts = PurePosixPath(rel).parts
        if folder is None and "sponsors" in parts and parts.index("sponsors") + 1 < len(parts):
            folder = parts[parts.index("sponsors") + 1]
        if not isinstance(s, str) or not SPONSOR_RE.match(s):
            rep.add("sponsor", "wrong_type", f"{s!r} is not a sponsor slug")
        elif folder and s != folder:
            rep.add("sponsor", "layout", f"the recipe says sponsor {s!r} but it is filed under {folder!r}")
    rep.each("", data, ("approved_by", "reason"), rep.text)
    if "approved_at" in data:
        v = data["approved_at"]
        try:
            ok = isinstance(v, (datetime, date)) or (isinstance(v, str) and bool(datetime.fromisoformat(v)))
        except ValueError:
            ok = False
        if not ok:
            rep.add("approved_at", "wrong_type", "approved_at is an ISO date and time, written by the approval")
    fields = data.get("fields")
    if "fields" in data:
        if not isinstance(fields, Mapping) or not fields:
            rep.add("fields", "wrong_type", "fields is a non-empty mapping of target field to a step")
            return {}
        known = ctx.target_fields.get(rt) if ctx.target_fields and isinstance(rt, str) else None
        keys = ctx.target_keys.get(rt, frozenset()) if ctx.target_keys and isinstance(rt, str) else frozenset()
        for name, step in fields.items():
            p = at("fields", name)
            if known is not None and name not in known:
                rep.add(p, "unknown_reference", f"{name!r} is not a field of the {rt} template")
            _step(rep, p, str(name), step, keys)
    return {}


def _step(rep: Rep, p: str, name: str, step: Any, keys: frozenset[str]) -> None:
    if not isinstance(step, Mapping):
        rep.add(p, "wrong_type", "a field step is a mapping with one of from, supply or missing")
        return
    sources = [k for k in ("from", "supply", "missing") if k in step]
    if len(sources) != 1:
        rep.add(p, "missing_field" if not sources else "wrong_type",
                f"a field step has exactly one of from, supply or missing, not {sources or 'none'}")
    for k in step:
        if k not in ("from", "supply", "missing", "ops", "note"):
            rep.add(at(p, k), "unknown_field", f"{k!r} is not part of a field step; use from, supply, missing, ops, note")
    rep.when(p, step, "note", rep.text, longest=RECIPE_BOUNDS["note"])
    if "ops" in step and sources != ["from"]:
        rep.add(at(p, "ops"), "unknown_field", "ops go only with from; a supplied or missing field has none")
    if "from" in step:
        f = step["from"]
        if isinstance(f, Mapping):
            if rep.shape(at(p, "from"), f, ("header",), ("occurrence",)):
                if "header" in f:
                    rep.text(at(at(p, "from"), "header"), f["header"])
                if "occurrence" in f and (not is_int(f["occurrence"]) or f["occurrence"] < 1):
                    rep.add(at(at(p, "from"), "occurrence"), "wrong_type", "occurrence is a whole number from 1")
        else:
            rep.text(at(p, "from"), f)
    kind = _field_kind(name)
    if "supply" in step:
        s = step["supply"]
        if rep.shape(at(p, "supply"), s, ("value",)) and "value" in s:
            v = s["value"]
            v = str(v) if is_num(v) else v
            if _lit(rep, at(at(p, "supply"), "value"), v):
                if kind == "date" and not (isinstance(v, str) and _SUPPLY_DATE.match(v)):
                    rep.add(at(at(p, "supply"), "value"), "wrong_type", f"{name} is a date; supply it as YYYY-MM-DD")
                if kind == "number" and not (isinstance(v, str) and _SUPPLY_NUM.match(v)):
                    rep.add(at(at(p, "supply"), "value"), "wrong_type", f"{name} is an amount; supply plain digits with '.'")
    if "missing" in step:
        m = step["missing"]
        if rep.shape(at(p, "missing"), m, ("reason",)) and "reason" in m:
            rep.text(at(at(p, "missing"), "reason"), m["reason"], longest=RECIPE_BOUNDS["literal"])
    if sources == ["from"] and "ops" in step:
        ops = step["ops"]
        if not rep.items(at(p, "ops"), ops, what="a list of ops"):
            return
        if len(ops) > RECIPE_BOUNDS["ops"]:
            rep.add(at(p, "ops"), "out_of_bounds", f"{len(ops)} ops, over the limit of {RECIPE_BOUNDS['ops']}")
        pipe = _Pipe(rep)
        cur = "any"
        for j, o in enumerate(ops):
            op_ptr = at(at(p, "ops"), j)
            if isinstance(o, Mapping) and o.get("op") == "truncate" and (name.endswith("_ID") or name in keys):
                rep.add(op_ptr, "looser_than_control",
                        f"{name} is an ID; its format is a control and an ID is never truncated automatically")
            cur = _op(pipe, op_ptr, o, cur)
        if pipe.cols > RECIPE_BOUNDS["columns"]:
            rep.add(at(p, "ops"), "out_of_bounds", f"{pipe.cols} column operands, over the limit of {RECIPE_BOUNDS['columns']}")
        ok = cur == "any" or cur == kind or (kind == "text" and cur == "number")
        if not ok:
            rep.add(at(p, "ops"), "wrong_type", f"the ops end in a {cur}, but {name} is a {kind} field")
