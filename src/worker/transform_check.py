"""Validation: every op parameter checked and defaulted, the bounded regex subset, the type walk along a field's
ops, and the compiled field with its hashes. Refusals are `RecipeError` with one of `LOAD_CODES`."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from worker.money import to_decimal
from worker.transform_core import (
    _EMPTY_CONTEXT,
    _ISO_DATE,
    _ORDER_CMPS,
    _PLAIN_DECIMAL,
    OPS,
    CompiledField,
    CompiledRecipe,
    Context,
    FieldRule,
    RecipeError,
    _compile_date_pattern,
    _sha,
    _tokenize_pattern,
    rule_for,
)

SOURCES: tuple[str, ...] = ("from", "supply", "missing")
SETUP_KEYS: tuple[str, ...] = ("gp_id", "fund_id", "fund_name", "entity_id")
_THOUSANDS_CHARS = (",", ".", " ", "'", " ")
# Bounds on what a recipe may hold (refused as `out_of_bounds`).
MAX_OPS = 8
MAX_COLUMN_OPERANDS = 6
MAX_JOIN_PARTS = 8
MAX_DATE_PATTERNS = 4
MAX_CROSSWALK_PAIRS = 500
MAX_BLANK_VALUES = 20
MAX_LITERAL = 200
MAX_NOTE = 200
MAX_REGEX_LEN = 100
MAX_REGEX_QUANTIFIERS = 6
MAX_REGEX_UNBOUNDED = 2  # tighter than the vocabulary: keeps worst-case backtracking polynomial and small
MAX_REGEX_REPEAT = 50


def _params(op: Mapping[str, Any], required: Sequence[str], defaults: Mapping[str, Any], where: str) -> dict[str, Any]:
    allowed = {"op", *required, *defaults}
    extra = sorted(set(op) - allowed)
    if extra:
        raise RecipeError("unknown_operator", f"unknown parameter {extra[0]!r}", where)
    missing = [k for k in required if k not in op]
    if missing:
        raise RecipeError("unknown_operator", f"missing parameter {missing[0]!r}", where)
    return {**defaults, **{k: v for k, v in op.items() if k != "op"}}


def _lit(x: Any, what: str, where: str, *, allow_empty: bool = True) -> str:
    if not isinstance(x, str):
        raise RecipeError("wrong_type", f"{what} must be text", where)
    if len(x) > MAX_LITERAL:
        raise RecipeError("out_of_bounds", f"{what} is longer than {MAX_LITERAL} characters", where)
    if not allow_empty and x == "":
        raise RecipeError("unknown_operator", f"{what} must not be empty", where)
    return x


def _int(x: Any, what: str, where: str, lo: int, hi: int | None = None) -> int:
    if isinstance(x, bool) or not isinstance(x, int):
        raise RecipeError("wrong_type", f"{what} must be a whole number", where)
    if x < lo or (hi is not None and x > hi):
        raise RecipeError("out_of_bounds", f"{what} must be {lo}..{hi if hi is not None else 'any'}", where)
    return x


def _bool(x: Any, what: str, where: str) -> bool:
    if not isinstance(x, bool):
        raise RecipeError("wrong_type", f"{what} must be true or false", where)
    return x


def _choice(x: Any, what: str, options: Sequence[str], where: str) -> str:
    if x not in options:
        raise RecipeError("unknown_operator", f"{what} must be one of {', '.join(options)}", where)
    return str(x)


def _strlist(x: Any, what: str, where: str, *, max_items: int | None = None, non_empty: bool = False) -> list[str]:
    if not isinstance(x, list | tuple):
        raise RecipeError("wrong_type", f"{what} must be a list", where)
    if non_empty and not x:
        raise RecipeError("unknown_operator", f"{what} must not be empty", where)
    if max_items is not None and len(x) > max_items:
        raise RecipeError("out_of_bounds", f"{what} has more than {max_items} items", where)
    return [_lit(i, what, where) for i in x]


def _header_ref(x: Any, where: str) -> dict[str, Any]:
    """`"<header>"` or `{header, occurrence}` as {header, occurrence (None when not given)}."""
    if isinstance(x, str):
        return {"header": _lit(x, "header", where, allow_empty=False), "occurrence": None}
    if isinstance(x, Mapping) and set(x) <= {"header", "occurrence"} and "header" in x:
        occ = x.get("occurrence")
        return {"header": _lit(x["header"], "header", where, allow_empty=False),
                "occurrence": None if occ is None else _int(occ, "occurrence", where, 1)}
    raise RecipeError("unknown_operator", "a column is a header or {header, occurrence}", where)


def _operand(x: Any, where: str, *, allow: tuple[str, ...] = ("col", "text", "setup", "value")) -> dict[str, Any]:
    """The closed operands: {col, occurrence?}, {text}, {setup}, and `value` (the current pipeline value)."""
    if x == "value" and "value" in allow:
        return {"value": True}
    if isinstance(x, Mapping):
        keys = set(x)
        if "col" in keys and "col" in allow and keys <= {"col", "occurrence"}:
            ref = _header_ref({"header": x["col"], "occurrence": x.get("occurrence")}, where)
            return {"col": ref["header"], "occurrence": ref["occurrence"]}
        if keys == {"text"} and "text" in allow:
            return {"text": _lit(x["text"], "text", where)}
        if keys == {"setup"} and "setup" in allow:
            if x["setup"] not in SETUP_KEYS:
                raise RecipeError("unknown_reference", f"setup key {x['setup']!r} is not one of "
                                  f"{', '.join(SETUP_KEYS)}", where)
            return {"setup": x["setup"]}
    raise RecipeError("unknown_operator", f"not an operand here: {x!r}", where)


def _number_params(raw: Mapping[str, Any], where: str) -> dict[str, Any]:
    p = _params(raw, ["decimal"], {"thousands": [], "negative": ["minus"], "currency": [], "percent": "reject"},
                where)
    decimal = _choice(p["decimal"], "decimal", [".", ","], where)
    thousands = sorted(set(_strlist(p["thousands"], "thousands", where)))
    for t in thousands:
        if t not in _THOUSANDS_CHARS:
            raise RecipeError("unknown_operator", f"thousands separator {t!r} is not allowed", where)
    if decimal in thousands:
        raise RecipeError("wrong_type", "the decimal mark cannot also be a thousands separator", where)
    negative = _strlist(p["negative"], "negative", where)
    for n in negative:
        _choice(n, "negative", ["minus", "brackets", "trailing_minus"], where)
    currency = sorted(set(_strlist(p["currency"], "currency", where, max_items=MAX_BLANK_VALUES)))
    if "" in currency:
        raise RecipeError("unknown_operator", "a currency token must not be empty", where)
    percent = _choice(p["percent"], "percent", ["reject", "as_fraction", "as_number"], where)
    return {"decimal": decimal, "thousands": thousands, "negative": negative, "currency": currency,
            "percent": percent}


_CMPS = ("eq", "ne", "in", "not_in", "starts_with", "blank", "not_blank", "lt", "le", "gt", "ge")


def _branch(x: Any, where: str) -> dict[str, Any]:
    if x in ("keep", "blank"):
        return {x: True}
    return _operand(x, where, allow=("col", "text", "setup"))


def _normalize(op: Any, where: str) -> dict[str, Any]:
    """The op with every default filled in and every parameter checked. Raises RecipeError."""
    if not isinstance(op, Mapping):
        raise RecipeError("unknown_operator", "an op is a mapping with an `op` name", where)
    name = op.get("op")
    if name not in OPS:
        raise RecipeError("unknown_operator", f"unknown op {name!r}", where)
    where = f"{where}: {name}"
    if name == "trim":
        p = _params(op, [], {"inner": "keep"}, where)
        return {"op": name, "inner": _choice(p["inner"], "inner", ["keep", "collapse"], where)}
    if name == "case":
        p = _params(op, ["to"], {}, where)
        return {"op": name, "to": _choice(p["to"], "to", ["upper", "lower"], where)}
    if name == "treat_as_blank":
        p = _params(op, ["values"], {"ci": True}, where)
        values = _strlist(p["values"], "values", where, max_items=MAX_BLANK_VALUES, non_empty=True)
        return {"op": name, "values": sorted(set(values)), "ci": _bool(p["ci"], "ci", where)}
    if name in ("strip_prefix", "strip_suffix"):
        p = _params(op, ["text"], {"ci": False}, where)
        return {"op": name, "text": _lit(p["text"], "text", where, allow_empty=False),
                "ci": _bool(p["ci"], "ci", where)}
    if name == "split":
        p = _params(op, ["sep", "index"], {"trim": True}, where)
        index = _int(p["index"], "index", where, -MAX_LITERAL, MAX_LITERAL)
        if index == 0:
            raise RecipeError("unknown_operator", "index is 1-based (negative counts from the end)", where)
        return {"op": name, "sep": _lit(p["sep"], "sep", where, allow_empty=False), "index": index,
                "trim": _bool(p["trim"], "trim", where)}
    if name == "join":
        p = _params(op, ["parts"], {"sep": "", "skip_blank": True}, where)
        if not isinstance(p["parts"], list | tuple) or not p["parts"]:
            raise RecipeError("unknown_operator", "parts must be a non-empty list", where)
        if len(p["parts"]) > MAX_JOIN_PARTS:
            raise RecipeError("out_of_bounds", f"more than {MAX_JOIN_PARTS} parts", where)
        return {"op": name, "parts": [_operand(x, where) for x in p["parts"]],
                "sep": _lit(p["sep"], "sep", where), "skip_blank": _bool(p["skip_blank"], "skip_blank", where)}
    if name == "coalesce":
        p = _params(op, ["cols"], {}, where)
        if not isinstance(p["cols"], list | tuple) or not p["cols"]:
            raise RecipeError("unknown_operator", "cols must be a non-empty list", where)
        return {"op": name, "cols": [_operand(x, where, allow=("col", "text", "setup")) for x in p["cols"]]}
    if name == "regex_extract":
        p = _params(op, ["pattern"], {"ci": False, "on_miss": "fail"}, where)
        pattern = _lit(p["pattern"], "pattern", where, allow_empty=False)
        _check_pattern(pattern, where)
        return {"op": name, "pattern": pattern, "ci": _bool(p["ci"], "ci", where),
                "on_miss": _choice(p["on_miss"], "on_miss", ["fail", "missing"], where)}
    if name == "truncate":
        p = _params(op, ["max"], {}, where)
        return {"op": name, "max": _int(p["max"], "max", where, 1, 10_000)}
    if name == "date_parse":
        p = _params(op, ["patterns"], {"yy_pivot": None}, where)
        patterns = sorted(set(_strlist(p["patterns"], "patterns", where, max_items=MAX_DATE_PATTERNS,
                                       non_empty=True)))
        needs_pivot = False
        for pat in patterns:
            needs_pivot = _date_pattern_tokens(pat, where) or needs_pivot
        pivot = p["yy_pivot"]
        if pivot is None and needs_pivot:
            raise RecipeError("unknown_operator", "a YY pattern needs an explicit yy_pivot (no clock is read)", where)
        if pivot is not None:
            pivot = _int(pivot, "yy_pivot", where, 0, 99)
        return {"op": name, "patterns": patterns, "yy_pivot": pivot}
    if name == "number_parse":
        return {"op": name, **_number_params({k: v for k, v in op.items() if k != "op"}, where)}
    if name == "negate":
        _params(op, [], {}, where)
        return {"op": name}
    if name == "debit_credit":
        p = _params(op, ["debit", "credit", "number"], {"both_sides": "net"}, where)
        if not isinstance(p["number"], Mapping):
            raise RecipeError("wrong_type", "number must hold number_parse parameters", where)
        return {"op": name, "debit": _operand(p["debit"], where, allow=("col",)),
                "credit": _operand(p["credit"], where, allow=("col",)),
                "number": _number_params(p["number"], where),
                "both_sides": _choice(p["both_sides"], "both_sides", ["net", "fail"], where)}
    if name == "crosswalk":
        return _normalize_crosswalk(op, where)
    if name == "when":
        p = _params(op, ["test", "then", "else"], {}, where)
        t = p["test"]
        if not isinstance(t, Mapping) or not set(t) <= {"left", "cmp", "right"} or "cmp" not in t:
            raise RecipeError("unknown_operator", "test is {left, cmp, right}", where)
        cmp = _choice(t["cmp"], "cmp", _CMPS, where)
        left = _operand(t.get("left", "value"), where, allow=("col", "value"))
        right: Any = None
        if cmp in ("blank", "not_blank"):
            if "right" in t:
                raise RecipeError("unknown_operator", f"{cmp} takes no right side", where)
        elif cmp in ("in", "not_in"):
            right = sorted(set(_strlist(t.get("right"), "right", where, non_empty=True)))
        else:
            right = _lit(t.get("right"), "right", where)
        return {"op": name, "test": {"left": left, "cmp": cmp, "right": right}, "then": _branch(p["then"], where),
                "else": _branch(p["else"], where)}
    p = _params(op, ["value"], {}, where)  # default
    return {"op": name, "value": _operand(p["value"], where, allow=("text", "setup"))}


def _normalize_crosswalk(op: Mapping[str, Any], where: str) -> dict[str, Any]:
    p = _params(op, [], {"pairs": None, "table": None, "output": None, "records": None, "match_on": None,
                         "take": None, "match": "casefold", "on_miss": "fail"}, where)
    kinds = [k for k in ("pairs", "table", "records") if p[k] is not None]
    if len(kinds) != 1:
        raise RecipeError("unknown_operator", "a crosswalk names exactly one of pairs, table or records", where)
    match = _choice(p["match"], "match", ["exact", "casefold"], where)
    on_miss = _choice(p["on_miss"], "on_miss", ["fail", "missing"], where)
    kind = kinds[0]
    if kind == "pairs":
        pairs = p["pairs"]
        if not isinstance(pairs, Mapping) or not pairs:
            raise RecipeError("unknown_operator", "pairs must be a non-empty mapping", where)
        if len(pairs) > MAX_CROSSWALK_PAIRS:
            raise RecipeError("out_of_bounds", f"more than {MAX_CROSSWALK_PAIRS} pairs", where)
        clean = {_lit(k, "pair key", where, allow_empty=False): _lit(v, "pair value", where)
                 for k, v in pairs.items()}
        source: dict[str, Any] = {"pairs": clean}
    elif kind == "table":
        source = {"table": _lit(p["table"], "table", where, allow_empty=False),
                  "output": _lit(p["output"], "output", where, allow_empty=False)}
    else:
        source = {"records": _lit(p["records"], "records", where, allow_empty=False),
                  "match_on": _choice(p["match_on"], "match_on", ["id", "key", "name"], where),
                  "take": _choice(p["take"], "take", ["id", "name"], where)}
    return {"op": "crosswalk", "source": source, "match": match, "on_miss": on_miss}


_CLASS_ESCAPES = "dws.-[]\\"
_ATOM_ESCAPES = "dws.-[]()\\"
_BRACE = re.compile(r"\{(\d{1,2})(?:,(\d{1,2}))?\}")


def _check_pattern(p: str, where: str) -> None:
    """Refuse (`unknown_operator`) any pattern outside the safe subset: literals, `\\d \\w \\s \\. \\- \\[ \\] \\( \\)
    \\\\`, classes, anchors, `? * + {m} {m,n}` (n <= 50), exactly one unquantified capturing group; no alternation,
    backreference, lookaround, lazy or possessive quantifier, inline flag, named group or `.`."""

    def bad(why: str) -> RecipeError:
        return RecipeError("unknown_operator", f"pattern outside the safe subset: {why}", where)

    if len(p) > MAX_REGEX_LEN:
        raise RecipeError("out_of_bounds", f"pattern longer than {MAX_REGEX_LEN} characters", where)
    i, n = 0, len(p)
    groups = 0
    in_group = False
    group_atoms = 0
    quant = 0
    unbounded = 0
    quantifiable = False
    while i < n:
        c = p[i]
        if c == "\\":
            if i + 1 >= n or p[i + 1] not in _ATOM_ESCAPES:
                raise bad("unsupported escape")
            i += 2
            quantifiable = True
            group_atoms += in_group
        elif c == "[":
            j = i + 1
            if j < n and p[j] == "^":
                j += 1
            start = j
            while j < n and p[j] != "]":
                if p[j] == "\\":
                    if j + 1 >= n or p[j + 1] not in _CLASS_ESCAPES:
                        raise bad("unsupported escape in class")
                    j += 2
                elif p[j] == "[":
                    raise bad("nested class")
                else:
                    j += 1
            if j >= n or j == start:
                raise bad("unterminated or empty class")
            i = j + 1
            quantifiable = True
            group_atoms += in_group
        elif c == "(":
            if in_group or groups:
                raise bad("exactly one capturing group, not nested")
            in_group = True
            groups = 1
            group_atoms = 0
            quantifiable = False
            i += 1
        elif c == ")":
            if not in_group or group_atoms == 0:
                raise bad("empty or unopened group")
            in_group = False
            quantifiable = False
            i += 1
        elif c in "^$":
            quantifiable = False
            i += 1
        elif c in "|.":
            raise bad("alternation and `.` are not allowed")
        elif c in "?*+":
            if not quantifiable:
                raise bad("quantifier without an atom (lazy, possessive and group quantifiers are refused)")
            quant += 1
            unbounded += c != "?"
            quantifiable = False
            i += 1
        elif c == "{":
            m = _BRACE.match(p, i)
            if m is None or not quantifiable:
                raise bad("bad quantifier")
            lo = int(m[1])
            hi = int(m[2]) if m[2] is not None else lo
            if lo > hi or hi > MAX_REGEX_REPEAT:
                raise bad(f"repeat counts must satisfy m <= n <= {MAX_REGEX_REPEAT}")
            quant += 1
            quantifiable = False
            i = m.end()
        elif c in "]})":
            raise bad("unbalanced bracket")
        else:
            quantifiable = True
            group_atoms += in_group
            i += 1
    if in_group:
        raise bad("unterminated group")
    if groups != 1:
        raise bad("exactly one capturing group is required")
    if quant > MAX_REGEX_QUANTIFIERS:
        raise RecipeError("out_of_bounds", f"more than {MAX_REGEX_QUANTIFIERS} quantifiers", where)
    if unbounded > MAX_REGEX_UNBOUNDED:
        raise RecipeError("out_of_bounds", f"more than {MAX_REGEX_UNBOUNDED} unbounded quantifiers", where)
    try:
        re.compile(p, re.ASCII)
    except re.error as exc:
        raise bad(str(exc)) from None


def _date_pattern_tokens(pattern: str, where: str) -> bool:
    """Check one pattern has exactly one year, one month and one day token. True when it uses `YY`."""
    toks = [t for kind, t in _tokenize_pattern(pattern) if kind == "tok"]
    years = [t for t in toks if t in ("YYYY", "YY")]
    months = [t for t in toks if t in ("MM", "M", "Mon")]
    days = [t for t in toks if t in ("DD", "D")]
    if len(years) != 1 or len(months) != 1 or len(days) != 1:
        raise RecipeError("unknown_operator", f"date pattern {pattern!r} needs one year, one month and one day token",
                          where)
    return years[0] == "YY"


def _cols_in(x: Any) -> list[Mapping[str, Any]]:
    """The column operands an op names (counted against the per-field bound)."""
    if isinstance(x, Mapping):
        return ([x] if "col" in x else []) + [c for k, v in x.items() if k != "col" for c in _cols_in(v)]
    if isinstance(x, list):
        return [c for v in x for c in _cols_in(v)]
    return []


_TEXT_IN = ("text", "number", "date")


def _type_step(nop: Mapping[str, Any], t: str, rule: FieldRule, where: str) -> str:
    """The output type of one op given the input type; `wrong_type` when the op cannot take it."""
    name = nop["op"]

    def need(accepted: Sequence[str]) -> None:
        if t not in accepted:
            raise RecipeError("wrong_type", f"{name} cannot take a {t} here (it needs {' or '.join(accepted)})", where)

    if name in ("trim", "case", "treat_as_blank", "strip_prefix", "strip_suffix", "split", "regex_extract",
                "truncate"):
        need(_TEXT_IN)
        if name == "truncate" and (rule.is_id or rule.is_key):
            raise RecipeError("looser_than_control", "an ID or key is never truncated (id_too_long is raised "
                              "instead)", where)
        return "text"
    if name == "join":
        return "text"
    if name in ("coalesce", "default"):
        if name == "default":
            _default_typed(nop["value"], t, where)
        return t
    if name == "date_parse":
        need(("text", "date"))
        return "date"
    if name == "number_parse":
        need(("text", "number"))
        return "number"
    if name == "negate":
        need(("number",))
        return "number"
    if name == "debit_credit":
        return "number"
    if name == "crosswalk":
        need(("text",))
        return "text"
    # when
    test = nop["test"]
    if test["cmp"] in _ORDER_CMPS:
        if "value" not in test["left"]:
            raise RecipeError("wrong_type", "an order comparison reads the current value only", where)
        need(("number", "date"))
        right = test["right"]
        ok = (_PLAIN_DECIMAL.fullmatch(right) if t == "number" else _ISO_DATE.fullmatch(right))
        if ok is None:
            raise RecipeError("wrong_type", f"the right side must be canonical {t} text", where)
        if t == "date":
            try:
                date.fromisoformat(right)
            except ValueError:
                raise RecipeError("wrong_type", "the right side is not a real date", where) from None
    kinds: set[str] = set()
    for br in (nop["then"], nop["else"]):
        if "keep" in br:
            kinds.add(t)
        elif "blank" not in br:
            kinds.add("text")
    if len(kinds) > 1:
        raise RecipeError("wrong_type", "the two branches give different types", where)
    return kinds.pop() if kinds else t


def _default_typed(operand: Mapping[str, Any], t: str, where: str) -> None:
    """A literal default must be canonical for the field's type, as `supply` is."""
    if "text" not in operand:
        return
    lit = operand["text"]
    if t == "number" and not _PLAIN_DECIMAL.fullmatch(lit):
        raise RecipeError("wrong_type", "a number default is a plain decimal with `.`", where)
    if t == "date":
        try:
            if not _ISO_DATE.fullmatch(lit):
                raise ValueError(lit)
            date.fromisoformat(lit)
        except ValueError:
            raise RecipeError("wrong_type", "a date default is YYYY-MM-DD", where) from None


def _prepare(nop: Mapping[str, Any], ctx: Context, where: str) -> tuple[Any, str | None]:
    """(what the op needs at run time, the pin folded into its hash or None)."""
    name = nop["op"]
    if name == "regex_extract":
        return re.compile(nop["pattern"], re.ASCII | (re.IGNORECASE if nop["ci"] else 0)), None
    if name == "date_parse":
        return tuple(_compile_date_pattern(p) for p in nop["patterns"]), None
    if name != "crosswalk":
        return None, None
    src = nop["source"]
    fold = (lambda s: s.casefold()) if nop["match"] == "casefold" else (lambda s: s)
    index: dict[str, set[str]] = {}
    pin = None
    if "pairs" in src:
        for k, v in src["pairs"].items():
            index.setdefault(fold(k.strip()), set()).add(v)
        if any(len(v) > 1 for v in index.values()):
            raise RecipeError("unknown_operator", "two pairs give different results for the same key", where)
    elif "table" in src:
        outputs = ctx.tables.get(src["table"])
        if outputs is None or src["output"] not in outputs:
            raise RecipeError("unknown_reference", f"table {src['table']!r} output {src['output']!r} is not known",
                              where)
        for k, v in outputs[src["output"]].items():
            if v != "":  # the catch-all row's "" means no match
                index.setdefault(fold(str(k).strip()), set()).add(str(v))
        pin = ctx.table_checksums.get(src["table"], "")
    else:
        recs = ctx.records.get(src["records"])
        if recs is None:
            raise RecipeError("unknown_reference", f"record type {src['records']!r} is not known", where)
        for rec in recs:
            key, out = str(rec.get(src["match_on"], "")).strip(), str(rec.get(src["take"], ""))
            if key and out:
                index.setdefault(fold(key), set()).add(out)
        pin = ctx.records_checksums.get(src["records"], "")
    return index, pin


def _source_step(step: Mapping[str, Any], where: str) -> tuple[dict[str, Any], list[Any]]:
    kinds = [k for k in SOURCES if k in step]
    unknown = sorted(set(step) - {*SOURCES, "ops", "note"})
    if unknown:
        raise RecipeError("unknown_operator", f"unknown key {unknown[0]!r}", where)
    if len(kinds) != 1:
        raise RecipeError("unknown_operator", "a field has exactly one source: from, supply or missing", where)
    kind = kinds[0]
    ops = step.get("ops", [])
    if not isinstance(ops, list | tuple):
        raise RecipeError("wrong_type", "ops must be a list", where)
    if kind != "from" and ops:
        raise RecipeError("wrong_type", f"a {kind} source takes no ops", where)
    raw = step[kind]
    if kind == "from":
        return {"from": _header_ref(raw, where)}, list(ops)
    if not isinstance(raw, Mapping) or set(raw) != {"value" if kind == "supply" else "reason"}:
        raise RecipeError("unknown_operator", f"{kind} is {{{'value' if kind == 'supply' else 'reason'}: text}}", where)
    key = next(iter(raw))
    return {kind: {key: _lit(raw[key], key, where, allow_empty=False)}}, list(ops)


def _supplied_value(text: str, rule: FieldRule, where: str) -> Any:
    """A supplied value is canonical for the field's kind; it is never re-read with a guessed format."""
    if rule.kind == "date":
        if not _ISO_DATE.fullmatch(text):
            raise RecipeError("wrong_type", "a supplied date is YYYY-MM-DD", where)
        try:
            return date.fromisoformat(text)
        except ValueError:
            raise RecipeError("wrong_type", "a supplied date is not a real date", where) from None
    if rule.kind == "amount":
        if not _PLAIN_DECIMAL.fullmatch(text):
            raise RecipeError("wrong_type", "a supplied amount is a plain decimal with `.`", where)
        return to_decimal(text)
    return text


def compile_field(name: str, step: Mapping[str, Any], rule: FieldRule | None = None,
                  context: Context | None = None) -> CompiledField:
    """Check one field step (types, bounds, references) and prepare it. Raises RecipeError."""
    rule = rule or rule_for(name)
    ctx = context or _EMPTY_CONTEXT
    if not isinstance(step, Mapping):
        raise RecipeError("unknown_operator", "a field step is a mapping", name)
    note = step.get("note", "")
    if not isinstance(note, str) or len(note) > MAX_NOTE:
        raise RecipeError("out_of_bounds", f"note is text of at most {MAX_NOTE} characters", name)
    source, raw_ops = _source_step(step, name)
    if len(raw_ops) > MAX_OPS:
        raise RecipeError("out_of_bounds", f"more than {MAX_OPS} ops", name)
    t = "text"
    nops: list[dict[str, Any]] = []
    types: list[str] = []
    prepared: list[Any] = []
    hashed: list[dict[str, Any]] = []
    columns = 0
    for n, raw in enumerate(raw_ops, 1):
        nop = _normalize(raw, name)
        at = f"{name}, op {n}: {nop['op']}"
        columns += len(_cols_in(nop))
        types.append(t)
        t = _type_step(nop, t, rule, at)
        prep, pin = _prepare(nop, ctx, at)
        nops.append(nop)
        prepared.append(prep)
        hashed.append({**nop, "pin": pin} if pin is not None else dict(nop))
    if columns > MAX_COLUMN_OPERANDS:
        raise RecipeError("out_of_bounds", f"more than {MAX_COLUMN_OPERANDS} column operands", name)
    supplied = None
    if "supply" in source:
        supplied = _supplied_value(source["supply"]["value"], rule, name)
        t = {"date": "date", "amount": "number"}.get(rule.kind, "text")
    if "missing" not in source:
        want = {"date": "date", "amount": "number", "text": "text"}[rule.kind]
        if t != want and not (want == "text" and t == "number"):
            raise RecipeError("wrong_type", f"the field needs {want} but the pipeline gives {t}", name)
    fh = _sha({"source": source, "ops": hashed})
    return CompiledField(name, source, tuple(nops), tuple(types), rule, fh, tuple(prepared), supplied)


def compile_recipe(recipe: Mapping[str, Any], rules: Mapping[str, FieldRule] | None = None,
                   context: Context | None = None) -> CompiledRecipe:
    """Check every field of a recipe and hash it. `recipe_hash` covers schema_version, id, sponsor and the
    fields' sources and ops; approvals, reasons and notes are left out."""
    fields_raw = recipe.get("fields")
    if not isinstance(fields_raw, Mapping):
        raise RecipeError("unknown_operator", "a recipe has a `fields` mapping", "recipe")
    rules = rules or {}
    fields = {n: compile_field(n, s, rules.get(n), context) for n, s in fields_raw.items()}
    body = {"schema_version": recipe.get("schema_version"), "id": recipe.get("id"),
            "sponsor": recipe.get("sponsor"),
            "fields": {n: {"source": f.source, "field_hash": f.field_hash} for n, f in fields.items()}}
    return CompiledRecipe(_sha(body), fields)


def field_hash(name: str, step: Mapping[str, Any], rule: FieldRule | None = None,
               context: Context | None = None) -> str:
    """`sha256(canonical({source, ops}))`; the note is not part of it."""
    return compile_field(name, step, rule, context).field_hash


def recipe_hash(recipe: Mapping[str, Any], rules: Mapping[str, FieldRule] | None = None,
                context: Context | None = None) -> str:
    return compile_recipe(recipe, rules, context).recipe_hash
