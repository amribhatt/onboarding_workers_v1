"""The core of the transform vocabulary: ops, bounds, failure codes, field rules, rows and outcomes, cell values,
the canonical hash, and the row-wise reference implementation of every op (number and date reading, one op on one
value, one field on one row). `worker.setrecipe` gives the same outcomes over whole columns; this is the definition.
Validation lives in `transform_check`; `worker.transforms` is the public entry."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, localcontext
from typing import Any

from worker.money import ZERO, to_decimal
from worker.staging import _clean

# One valid step per op, parameters by name (the values are only examples). A model that proposes a recipe is shown
# these, so it never has to guess a parameter name; `tests/test_transform_op_examples.py` keeps each one valid.
# No example carries a typed literal in `default` or `when` (that is an invented constant for the Mapper).
OP_EXAMPLES: dict[str, dict[str, Any]] = {
    "trim": {"inner": "collapse"},
    "case": {"to": "upper"},
    "treat_as_blank": {"values": ["n/a", "-"]},
    "strip_prefix": {"text": "INV-"},
    "strip_suffix": {"text": " Ltd"},
    "split": {"sep": " - ", "index": 1},
    "join": {"parts": [{"col": "First Name"}, {"col": "Last Name"}], "sep": " "},
    "coalesce": {"cols": [{"col": "Legal Name"}, {"col": "Short Name"}]},
    "regex_extract": {"pattern": "INV-([0-9]+)", "on_miss": "fail"},
    "truncate": {"max": 20},
    "date_parse": {"patterns": ["YYYY-MM-DD"]},
    "number_parse": {"decimal": ".", "thousands": [","], "negative": ["minus", "brackets"]},
    "negate": {},
    "debit_credit": {"debit": {"col": "Debit"}, "credit": {"col": "Credit"}, "number": {"decimal": "."}},
    "crosswalk": {"pairs": {"usd": "USD", "us dollar": "USD"}},
    "when": {"test": {"left": "value", "cmp": "blank"}, "then": {"col": "Short Name"}, "else": "keep"},
    "default": {"value": {"setup": "gp_id"}},
}
OPS: tuple[str, ...] = tuple(OP_EXAMPLES)

LOAD_CODES: tuple[str, ...] = (
    "unknown_operator", "wrong_type", "unknown_reference", "out_of_bounds", "looser_than_control",
)

FAILURE_SENTENCES: dict[str, str] = {
    "no_column": "The column named in the recipe is not in this file.",
    "ambiguous_column": "The column name appears more than once and the recipe does not say which one.",
    "split_no_part": "The value has fewer parts than the recipe asks for.",
    "no_match": "The value does not contain the pattern the recipe looks for.",
    "too_long_to_match": "The value is too long to be searched safely.",
    "not_a_date": "The value is not a date in any of the written forms the recipe states.",
    "ambiguous_date": "The value is a date in more than one of the stated forms, and they give different days.",
    "date_has_time": "The cell holds a time of day, which a date field does not take.",
    "number_not_date": "The cell holds a plain number, and spreadsheet serial numbers are not read as dates.",
    "not_a_number": "The value is not a number in the form the recipe states.",
    "bad_grouping": "The thousands separators in the value are not in groups of three.",
    "percent_not_allowed": "The value carries a percent sign and the recipe does not allow one.",
    "wrong_type": "The cell holds a kind of value this step cannot read.",
    "both_sides": "The line has both a debit and a credit, and the recipe refuses that.",
    "not_in_crosswalk": "The value is not in the list the recipe looks it up in.",
    "ambiguous_match": "The value matches more than one existing record with different results.",
}
FAILURE_CODES: tuple[str, ...] = tuple(FAILURE_SENTENCES)

# Bounds on what a recipe may hold (refused as `out_of_bounds`), and on what is searched or shown.
MAX_REGEX_INPUT = 1000
MAX_EXAMPLE_VALUE = 60
MAX_EXAMPLES = 5

_PLAIN_DECIMAL = re.compile(r"-?\d+(\.\d+)?")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ORDER_CMPS = ("lt", "le", "gt", "ge")


class RecipeError(ValueError):
    """A recipe refused at load time. `code` is one of LOAD_CODES, `where` names the field and op."""

    def __init__(self, code: str, message: str, where: str = "") -> None:
        super().__init__(f"{code}: {message}" + (f" ({where})" if where else ""))
        self.code = code
        self.message = message
        self.where = where


class _Fail(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FieldRule:
    """What the target says about a field (the repo derives it from the name suffix today)."""

    kind: str = "text"  # text | date | amount
    required: bool = False
    is_id: bool = False
    is_key: bool = False
    max_len: int | None = None
    reference: frozenset[str] | None = None


def rule_for(name: str) -> FieldRule:
    """The rule the repo derives from a target field's name: `_Date`, `_Amount`, `_ID` (not `GP_ID`), `_Name`."""
    if name.endswith("_Date"):
        return FieldRule(kind="date")
    if name.endswith("_Amount"):
        return FieldRule(kind="amount")
    if name.endswith("_ID") and name != "GP_ID":
        return FieldRule(is_id=True, max_len=20)
    if name.endswith("_Name"):
        return FieldRule(max_len=100)
    return FieldRule()


@dataclass(frozen=True)
class Context:
    """The pinned things a recipe may read besides its own row: setup values and lookup data.

    `tables[table_id][output]` is {input: result}; `records[record_type]` is a list of {"id", "key", "name"}
    mappings (the sponsor's existing records plus this batch's staged ones). The `*_checksums` fold into hashes.
    """

    setup: Mapping[str, str] = field(default_factory=dict)
    tables: Mapping[str, Mapping[str, Mapping[str, str]]] = field(default_factory=dict)
    table_checksums: Mapping[str, str] = field(default_factory=dict)
    records: Mapping[str, Sequence[Mapping[str, str]]] = field(default_factory=dict)
    records_checksums: Mapping[str, str] = field(default_factory=dict)
    approver: str = "the approving person"


_EMPTY_CONTEXT = Context()


@dataclass(frozen=True)
class Row:
    """One staged source row: its locator, the headers in column order and the cells."""

    src: str
    headers: tuple[str, ...]
    cells: tuple[Any, ...]

    @classmethod
    def of(cls, src: str, values: Mapping[str, Any]) -> Row:
        return cls(src, tuple(values), tuple(values.values()))


@dataclass(frozen=True)
class Outcome:
    """The result of one field on one row. `status` is ok, missing or failed."""

    status: str
    value: Any = None
    code: str | None = None
    flags: frozenset[str] = frozenset()
    source: Any = None


def enter(cell: Any) -> Any:
    """A source cell as a pipeline value: None (blank), str, Decimal, date, datetime (with a time) or bool.

    Text goes through the reader's `staging._clean` (outer whitespace stripped; `n/a`, `-`, `--`, `tbd` blank).
    Numbers become `Decimal` through `money.to_decimal` (a float through its shortest repr)."""
    if cell is None or isinstance(cell, bool):
        return cell
    if isinstance(cell, int | float | Decimal):
        try:
            return to_decimal(cell)
        except ValueError:
            raise _Fail("not_a_number") from None
    if isinstance(cell, datetime):
        return cell.date() if cell.time() == time(0) else cell
    if isinstance(cell, date):
        return cell
    text, _marker = _clean(cell if isinstance(cell, str) else str(cell))
    return text or None


def decimal_text(d: Decimal) -> str:
    """The canonical text of a number: no exponent, no trailing zeros (`1000.0` is `1000`), exact."""
    if d == 0:
        return "0"
    t = format(d, "f")
    return t.rstrip("0").rstrip(".") if "." in t else t


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, Decimal):
        return decimal_text(v)
    if isinstance(v, date):  # a datetime too
        return v.isoformat()
    return str(v)


def _no_negative_zero(d: Decimal) -> Decimal:
    return d.copy_abs() if d == 0 else d


def _stringify(x: Any) -> Any:
    if isinstance(x, bool) or x is None or isinstance(x, str):
        return x
    if isinstance(x, int | float | Decimal):
        return str(x)
    if isinstance(x, Mapping):
        return {str(k): _stringify(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_stringify(v) for v in x]
    raise TypeError(f"not canonical: {type(x).__name__}")


def canonical(x: Any) -> str:
    """Canonical JSON: sorted keys, no extra whitespace, UTF-8 (`ensure_ascii=False`), numbers as strings.
    Literal text is never trimmed or normalised."""
    return json.dumps(_stringify(x), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(x: Any) -> str:
    return hashlib.sha256(canonical(x).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CompiledField:
    """A checked, hashed and prepared field step."""

    name: str
    source: dict[str, Any]
    ops: tuple[dict[str, Any], ...]
    in_types: tuple[str, ...]
    rule: FieldRule
    field_hash: str
    prepared: tuple[Any, ...] = ()
    supplied: Any = None


@dataclass(frozen=True)
class CompiledRecipe:
    recipe_hash: str
    fields: dict[str, CompiledField]


_WS = " \t\r\n "
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_FULL_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
                "november", "december")
_MONTH_NUMBER = {**{m: i + 1 for i, m in enumerate(_MONTHS)}, **{m: i + 1 for i, m in enumerate(_FULL_MONTHS)}}
_THOUSANDS_CHARS = (",", ".", " ", "'", " ")
_DIGITS = "0123456789"


_DATE_TOKENS = ("YYYY", "YY", "MM", "DD", "Mon", "M", "D")


def _tokenize_pattern(pattern: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(pattern):
        for tok in _DATE_TOKENS:
            if pattern.startswith(tok, i):
                out.append(("tok", tok))
                i += len(tok)
                break
        else:
            out.append(("lit", pattern[i]))
            i += 1
    return out


@dataclass(frozen=True)
class _DatePattern:
    regex: re.Pattern[str]
    order: tuple[str, ...]  # token kinds of the capture groups, in order

    def read(self, text: str, pivot: int | None) -> date | None:
        m = self.regex.fullmatch(text)
        if m is None:
            return None
        year = month = day = 0
        for kind, raw in zip(self.order, m.groups(), strict=True):
            if kind == "YYYY":
                year = int(raw)
            elif kind == "YY":
                yy = int(raw)
                year = 2000 + yy if yy < (pivot or 0) else 1900 + yy
            elif kind == "Mon":
                number = _MONTH_NUMBER.get(raw.lower())
                if number is None:
                    return None
                month = number
            elif kind in ("MM", "M"):
                month = int(raw)
            else:
                day = int(raw)
        try:
            return date(year, month, day)
        except ValueError:
            return None


def _compile_date_pattern(pattern: str) -> _DatePattern:
    parts: list[str] = []
    order: list[str] = []
    for kind, tok in _tokenize_pattern(pattern):
        if kind == "lit":
            parts.append(re.escape(tok))
            continue
        order.append(tok)
        parts.append({"YYYY": r"(\d{4})", "YY": r"(\d{2})", "MM": r"(\d{2})", "DD": r"(\d{2})",
                      "M": r"(\d{1,2})", "D": r"(\d{1,2})", "Mon": r"([A-Za-z]{3,9})"}[tok])
    return _DatePattern(re.compile("".join(parts), re.ASCII), tuple(order))


def _group_ok(int_part: str, thousands: Sequence[str]) -> str:
    """Digits of the integer part, or raise `not_a_number` / `bad_grouping`."""
    if int_part == "":
        raise _Fail("not_a_number")
    seps = {c for c in int_part if c not in _DIGITS}
    if not seps:
        return int_part
    if not seps <= set(thousands):
        raise _Fail("not_a_number")
    if len(seps) > 1:
        raise _Fail("bad_grouping")
    sep = next(iter(seps))
    groups = int_part.split(sep)
    if not 1 <= len(groups[0]) <= 3 or any(len(g) != 3 for g in groups[1:]) or "" in groups:
        raise _Fail("bad_grouping")
    return "".join(groups)


def _parse_number(text: str, p: Mapping[str, Any]) -> Decimal:
    """The grammar of vocabulary `number_parse`. Nothing is guessed: a form that is not declared is not read."""
    body = text.strip(_WS)
    negative = False
    pct = False
    sign_done = cur_done = pct_done = False
    for _ in range(4):
        before = body
        if not sign_done:
            if "brackets" in p["negative"] and len(body) > 2 and body[0] == "(" and body[-1] == ")":
                body, negative, sign_done = body[1:-1].strip(_WS), True, True
            elif "trailing_minus" in p["negative"] and len(body) > 1 and body[-1] == "-":
                body, negative, sign_done = body[:-1].strip(_WS), True, True
            elif "minus" in p["negative"] and len(body) > 1 and body[0] == "-":
                body, negative, sign_done = body[1:].strip(_WS), True, True
        if not pct_done and body.endswith("%"):
            if p["percent"] == "reject":
                raise _Fail("percent_not_allowed")
            body, pct, pct_done = body[:-1].strip(_WS), True, True
        if not cur_done:
            for token in sorted(p["currency"], key=lambda c: (-len(c), c)):
                if body.startswith(token):
                    body, cur_done = body[len(token):].strip(_WS), True
                    break
                if body.endswith(token):
                    body, cur_done = body[: -len(token)].strip(_WS), True
                    break
        if body == before:
            break
    if "%" in body:
        raise _Fail("percent_not_allowed" if p["percent"] == "reject" else "not_a_number")
    decimal = p["decimal"]
    if body.count(decimal) > 1:
        raise _Fail("not_a_number")
    int_part, _, frac = body.partition(decimal)
    if decimal in body and (frac == "" or any(c not in _DIGITS for c in frac)):
        raise _Fail("not_a_number")
    digits = _group_ok(int_part, p["thousands"])
    value = to_decimal(f"{digits}.{frac}" if frac else digits)
    if pct and p["percent"] == "as_fraction":
        sign, mantissa, exp = value.as_tuple()
        value = Decimal((sign, mantissa, int(exp) - 2))  # exact: no context rounding
    if negative:
        value = value.copy_negate()
    return _no_negative_zero(value)


def _number_of(v: Any, p: Mapping[str, Any]) -> Decimal | None:
    if v is None:
        return None
    if isinstance(v, bool):
        raise _Fail("wrong_type")
    if isinstance(v, Decimal):
        return v
    if isinstance(v, str):
        return _parse_number(v, p)
    raise _Fail("not_a_number")


def _columns(headers: Sequence[str]) -> dict[str, list[int]]:
    cols: dict[str, list[int]] = {}
    for i, h in enumerate(headers):
        cols.setdefault(" ".join(str(h).casefold().split()), []).append(i)
    return cols


@dataclass
class _Run:
    row: Row
    cols: dict[str, list[int]]
    ctx: Context
    flags: set[str] = field(default_factory=set)

    def raw(self, header: str, occurrence: int | None) -> Any:
        idx = self.cols.get(" ".join(header.casefold().split()))
        if not idx or (occurrence or 1) > len(idx):
            raise _Fail("no_column")
        if occurrence is None and len(idx) > 1:
            raise _Fail("ambiguous_column")
        i = idx[(occurrence or 1) - 1]
        return self.row.cells[i] if i < len(self.row.cells) else None

    def operand(self, o: Mapping[str, Any], value: Any) -> Any:
        if "col" in o:
            return enter(self.raw(o["col"], o["occurrence"]))
        if "text" in o:
            return o["text"]
        if "setup" in o:
            got = self.ctx.setup.get(o["setup"])
            return _clean(got)[0] or None if got is not None else None
        return value


def _run_op(nop: Mapping[str, Any], prep: Any, t: str, v: Any, run: _Run) -> Any:
    name = nop["op"]
    if name == "join":
        items: list[str] = []
        any_value = False
        for part in nop["parts"]:
            x = run.operand(part, v)
            if "text" in part:
                items.append(part["text"])
                continue
            if x is None:
                if not nop["skip_blank"]:
                    items.append("")
                continue
            any_value = True
            items.append(_text(x))
        return nop["sep"].join(items) if any_value else None
    if name == "coalesce":
        if v is not None:
            return v
        for c in nop["cols"]:
            x = run.operand(c, v)
            if x is not None:
                run.flags.add("fallback")
                return _coerce(x, t)
        return None
    if name == "default":
        if v is not None:
            return v
        x = run.operand(nop["value"], v)
        if x is None:
            return None
        run.flags.add("defaulted")
        return _coerce_literal(x, t)
    if name == "when":
        return _run_when(nop, t, v, run)
    if name == "debit_credit":
        p = nop["number"]
        debit = _number_of(run.operand(nop["debit"], v), p)
        credit = _number_of(run.operand(nop["credit"], v), p)
        if debit is None and credit is None:
            return None
        d, c = debit or ZERO, credit or ZERO
        if d != 0 and c != 0 and nop["both_sides"] == "fail":
            raise _Fail("both_sides")
        with localcontext() as lc:
            lc.prec = 100
            return _no_negative_zero(d - c)
    if v is None:
        return None
    if name == "negate":
        if not isinstance(v, Decimal):
            raise _Fail("wrong_type")
        return _no_negative_zero(v.copy_negate())
    if name == "number_parse":
        return _number_of(v, nop)
    if name == "date_parse":
        return _run_date(nop, prep, v)
    return _run_text_op(nop, prep, v, run)


def _coerce(x: Any, t: str) -> Any:
    if t == "text":
        return _text(x)
    if t == "number":
        if isinstance(x, Decimal):
            return x
    elif t == "date" and type(x) is date:
        return x
    raise _Fail("wrong_type")


def _coerce_literal(x: Any, t: str) -> Any:
    s = _text(x)
    if t == "number":
        if not _PLAIN_DECIMAL.fullmatch(s):
            raise _Fail("not_a_number")
        return to_decimal(s)
    if t == "date":
        try:
            if not _ISO_DATE.fullmatch(s):
                raise ValueError(s)
            return date.fromisoformat(s)
        except ValueError:
            raise _Fail("not_a_date") from None
    return s


def _run_date(nop: Mapping[str, Any], prep: tuple[_DatePattern, ...], v: Any) -> date:
    if isinstance(v, datetime):
        raise _Fail("date_has_time")
    if isinstance(v, date):
        return v
    if isinstance(v, Decimal):
        raise _Fail("number_not_date")
    if not isinstance(v, str):
        raise _Fail("not_a_date")
    found = {d for pat in prep if (d := pat.read(v, nop["yy_pivot"])) is not None}
    if not found:
        raise _Fail("not_a_date")
    if len(found) > 1:
        raise _Fail("ambiguous_date")
    return next(iter(found))


def _run_text_op(nop: Mapping[str, Any], prep: Any, v: Any, run: _Run) -> Any:
    name = nop["op"]
    s = _text(v)
    if name == "trim":
        s = re.sub(f"[{_WS}]+", " ", s) if nop["inner"] == "collapse" else s
        return s.strip(_WS) or None
    if name == "case":
        return s.upper() if nop["to"] == "upper" else s.lower()
    if name == "treat_as_blank":
        probe = s.strip(_WS)
        values = [x.strip(_WS) for x in nop["values"]]
        hit = probe.casefold() in {x.casefold() for x in values} if nop["ci"] else probe in values
        return None if hit else s
    if name in ("strip_prefix", "strip_suffix"):
        lit = nop["text"]
        lead = name == "strip_prefix"
        a, b = (s.casefold(), lit.casefold()) if nop["ci"] else (s, lit)
        if lead and a.startswith(b):
            s = s[len(lit):]
        elif not lead and a.endswith(b):
            s = s[: len(s) - len(lit)]
        return s or None
    if name == "split":
        parts = s.split(nop["sep"])
        i = nop["index"]
        if (i > 0 and i > len(parts)) or (i < 0 and -i > len(parts)):
            raise _Fail("split_no_part")
        part = parts[i - 1] if i > 0 else parts[i]
        return (part.strip(_WS) if nop["trim"] else part) or None
    if name == "regex_extract":
        if len(s) > MAX_REGEX_INPUT:
            raise _Fail("too_long_to_match")
        m = prep.search(s)
        if m is None:
            if nop["on_miss"] == "missing":
                return None
            raise _Fail("no_match")
        return m.group(1) or None
    if name == "truncate":
        if len(s) > nop["max"]:
            run.flags.add("truncated")
            return s[: nop["max"]]
        return s
    # crosswalk
    key = s.strip(_WS)
    key = key.casefold() if nop["match"] == "casefold" else key
    hits = prep.get(key)
    if not hits:
        if nop["on_miss"] == "missing":
            return None
        raise _Fail("not_in_crosswalk")
    if len(hits) > 1:
        raise _Fail("ambiguous_match")
    return next(iter(hits)) or None


def _run_when(nop: Mapping[str, Any], t: str, v: Any, run: _Run) -> Any:
    test = nop["test"]
    left = run.operand(test["left"], v)
    cmp, right = test["cmp"], test["right"]
    if cmp == "blank":
        hit = left is None
    elif cmp == "not_blank":
        hit = left is not None
    elif left is None:
        hit = cmp in ("ne", "not_in")
    elif cmp in _ORDER_CMPS:
        bound = to_decimal(right) if t == "number" else date.fromisoformat(right)
        if type(left) is not type(bound):
            raise _Fail("wrong_type")
        hit = {"lt": left < bound, "le": left <= bound, "gt": left > bound, "ge": left >= bound}[cmp]
    else:
        s = _text(left)
        if cmp == "eq":
            hit = s == right
        elif cmp == "ne":
            hit = s != right
        elif cmp == "in":
            hit = s in right
        elif cmp == "not_in":
            hit = s not in right
        else:
            hit = s.startswith(right)
    branch = nop["then"] if hit else nop["else"]
    if "keep" in branch:
        return v
    if "blank" in branch:
        return None
    x = run.operand(branch, v)
    return None if x is None else _text(x)


def apply_field(cf: CompiledField, row: Row, context: Context | None = None,
                cols: dict[str, list[int]] | None = None) -> Outcome:
    """One field on one row: ok, missing or failed. The original cell is kept in `source`, never in the value."""
    ctx = context or _EMPTY_CONTEXT
    run = _Run(row, cols if cols is not None else _columns(row.headers), ctx)
    source: Any = None
    try:
        if "missing" in cf.source:
            return Outcome("missing")
        if "supply" in cf.source:
            value = cf.supplied
        else:
            ref = cf.source["from"]
            source = run.raw(ref["header"], ref["occurrence"])
            value = enter(source)
            for nop, prep, t in zip(cf.ops, cf.prepared, cf.in_types, strict=True):
                value = _run_op(nop, prep, t, value, run)
    except _Fail as f:
        return Outcome("failed", None, f.code, frozenset(run.flags), source)
    flags = frozenset(run.flags)
    if value is None:
        return Outcome("missing", None, None, flags, source)
    if cf.rule.kind == "text" and isinstance(value, Decimal):
        value = decimal_text(value)  # a number at a text field is written as canonical decimal text
    return Outcome("ok", value, None, flags, source)
