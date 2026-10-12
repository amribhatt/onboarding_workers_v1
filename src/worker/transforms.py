"""Recipe transforms: the closed vocabulary of 17 ops that turn one source column into one target field.

Pure functions over values: no I/O, no clock, no locale, no randomness. A recipe is plain data; code checks it
(`transform_check`), runs it row by row (`transform_core`), hashes it and explains it in fixed sentences (here). The
model never writes code or an explanation.

* Sources: `from` (a column), `supply` (one value on every row, typed by a person), `missing` (declared absent).
* A row's outcome is ok, MISSING (the source had nothing) or FAILED (a closed failure code; never retried with
  another reading). Day-first versus month-first and the decimal mark are never guessed: a value that reads two
  ways fails `ambiguous_date`, and the option sets built here always say `recommended: None`.
* Numbers are `Decimal` and go through `worker.money`. Nothing is rounded.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from worker.transform_check import (  # noqa: F401
    SETUP_KEYS,
    _cols_in,
    _normalize,
    _prepare,
    _source_step,
    compile_field,
    compile_recipe,
    field_hash,
    recipe_hash,
)
from worker.transform_core import (  # noqa: F401
    _EMPTY_CONTEXT,
    FAILURE_CODES,
    FAILURE_SENTENCES,
    MAX_EXAMPLE_VALUE,
    MAX_EXAMPLES,
    OP_EXAMPLES,
    OPS,
    CompiledField,
    CompiledRecipe,
    Context,
    FieldRule,
    RecipeError,
    Row,
    _columns,
    _Fail,
    _Run,
    _run_op,
    _text,
    apply_field,
    canonical,
    decimal_text,
    enter,
    rule_for,
)


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _q(s: str) -> str:
    return f"«{s}»"


def _operand_words(o: Mapping[str, Any]) -> str:
    if "col" in o:
        n = o["occurrence"]
        return f"column {_q(o['col'])}" + (f" ({_ordinal(n)} with that name)" if n else "")
    if "text" in o:
        return _q(o["text"])
    if "setup" in o:
        return f"setup {o['setup']}"
    return "the current value"


_CMP_WORDS = {"eq": "equals", "ne": "does not equal", "in": "is one of", "not_in": "is not one of",
              "starts_with": "starts with", "blank": "is blank", "not_blank": "is not blank", "lt": "is below",
              "le": "is at most", "gt": "is above", "ge": "is at least"}


def _branch_words(b: Mapping[str, Any]) -> str:
    if "keep" in b:
        return "the value is kept"
    if "blank" in b:
        return "it is left blank"
    return f"it is set to {_operand_words(b)}"


def explain_op(op: Mapping[str, Any], context: Context | None = None) -> str:
    """The fixed sentence for one op, filled from its parameters by code (the model never writes it)."""
    n = _normalize(op, "op")
    name = n["op"]
    ctx = context or _EMPTY_CONTEXT
    if name == "trim":
        return "Spaces trimmed" + (" and repeated inner spaces made single" if n["inner"] == "collapse" else "") + "."
    if name == "case":
        return f"Made {n['to']} case."
    if name == "treat_as_blank":
        return f"{_q(', '.join(n['values']))} treated as no value."
    if name == "strip_prefix":
        return f"Leading {_q(n['text'])} removed."
    if name == "strip_suffix":
        return f"Trailing {_q(n['text'])} removed."
    if name == "split":
        return f"Part {n['index']} of the value split at {_q(n['sep'])}."
    if name == "join":
        return (f"Built as {', '.join(_operand_words(p) for p in n['parts'])} joined with "
                f"{_q(n['sep'])}.")
    if name == "coalesce":
        return ("The first of the current value, then " + ", then ".join(_operand_words(c) for c in n["cols"])
                + " that has a value.")
    if name == "regex_extract":
        return f"The part matching {_q(n['pattern'])}" + (" (any case)" if n["ci"] else "") + "."
    if name == "truncate":
        return f"Cut to {n['max']} characters (each cut row is flagged)."
    if name == "date_parse":
        pivot = f", two-digit years below {n['yy_pivot']} as 20YY" if n["yy_pivot"] is not None else ""
        return (f"Read as a date written {' or '.join(n['patterns'])}{pivot}; a value that reads two ways is "
                "asked, not guessed.")
    if name == "number_parse":
        out = f"Read as a number with {_q(n['decimal'])} for decimals"
        if n["thousands"]:
            out += f", {_q(''.join(n['thousands']))} between thousands"
        if "brackets" in n["negative"]:
            out += ", (brackets) as negative"
        if "trailing_minus" in n["negative"]:
            out += ", a trailing minus as negative"
        if n["currency"]:
            out += f", ignoring {', '.join(n['currency'])}"
        if n["percent"] == "as_fraction":
            out += ", percent as a fraction"
        elif n["percent"] == "as_number":
            out += ", a percent sign dropped and the number kept as written"
        return out + "."
    if name == "negate":
        return "Sign reversed."
    if name == "debit_credit":
        return (f"Debit {_q(n['debit']['col'])} minus credit {_q(n['credit']['col'])}; a blank side counts as zero"
                + ("; a line with both sides is refused" if n["both_sides"] == "fail" else "") + ".")
    if name == "crosswalk":
        s = n["source"]
        if "pairs" in s:
            where = f"the sponsor's list of {len(s['pairs'])} pairs"
        elif "table" in s:
            where = f"table {s['table']}"
        else:
            where = f"existing {s['records']} by {s['match_on']}"
        ci = ", ignoring case" if n["match"] == "casefold" else ""
        miss = "is left blank" if n["on_miss"] == "missing" else "is refused"
        return f"Looked up in {where}{ci}; a value not found {miss}."
    if name == "when":
        t = n["test"]
        left = "the value" if "value" in t["left"] else _operand_words(t["left"])
        r = t["right"]
        right = " " + (", ".join(_q(x) for x in r) if isinstance(r, list) else _q(r)) if r is not None else ""
        return (f"If {left} {_CMP_WORDS[t['cmp']]}{right}, then {_branch_words(n['then'])}; otherwise "
                f"{_branch_words(n['else'])}.")
    return (f"Where blank, set to {_q(n['value'].get('text', n['value'].get('setup', '')))} (supplied by "
            f"{ctx.approver}; each such row is flagged).")


def explain_field(step: Mapping[str, Any], context: Context | None = None) -> str:
    """The source sentence, then each op sentence joined by "; then "."""
    source, ops = _source_step(step, "field")
    if "from" in source:
        ref = source["from"]
        head = f"Taken from column {_q(ref['header'])}" + (
            f", the {_ordinal(ref['occurrence'])} one with that name" if ref["occurrence"] else "")
    elif "supply" in source:
        approver = (context or _EMPTY_CONTEXT).approver
        head = f"Set to {_q(source['supply']['value'])} on every row, as supplied by {approver}; not from the source"
    else:
        head = f"Left blank on purpose: {source['missing']['reason']}. Reported as missing on every row"
    return "; then ".join(p.rstrip(".") for p in [head, *(explain_op(o, context) for o in ops)]) + "."


@dataclass
class Verdict:
    """How many rows of a field conform to the target, and the first few that do not."""

    field: str
    field_hash: str
    total: int = 0
    conform: int = 0
    missing: int = 0
    failed: dict[str, int] = field(default_factory=dict)
    flagged: dict[str, int] = field(default_factory=dict)
    examples: list[dict[str, Any]] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """A stored recipe re-applies without a question only when every row conforms."""
        return self.total > 0 and self.conform == self.total


def _cut(x: Any) -> str | None:
    return None if x is None else _text(x)[:MAX_EXAMPLE_VALUE]


def _preview(op: Mapping[str, Any], values: Sequence[Any]) -> dict[str, Any]:
    nop = _normalize(op, "option")
    prep, _ = _prepare(nop, _EMPTY_CONTEXT, "option")
    run = _Run(Row("", (), ()), {}, _EMPTY_CONTEXT)
    conform = total = 0
    failed: dict[str, int] = {}
    for raw in values:
        try:
            v = enter(raw)
            if v is None:
                continue
            total += 1
            _run_op(nop, prep, "text", v, run)
            conform += 1
        except _Fail as f:
            failed[f.code] = failed.get(f.code, 0) + 1
    return {"conform": conform, "total": total, "failed": dict(sorted(failed.items()))}


def date_options(values: Sequence[Any], *, day_first: Sequence[str], month_first: Sequence[str],
                 yy_pivot: int | None = None) -> dict[str, Any]:
    """A day-first and a month-first reading side by side, each with its preview over the values. Code never
    recommends one: `recommended` is always None, even when the evidence points one way. The evidence is shown."""
    options = []
    for oid, patterns in (("day_first", day_first), ("month_first", month_first)):
        op: dict[str, Any] = {"op": "date_parse", "patterns": list(patterns)}
        if yy_pivot is not None:
            op["yy_pivot"] = yy_pivot
        options.append({"id": oid, "op": _normalize(op, "option"), "preview": _preview(op, values)})
    first = second = 0
    for raw in values:
        m = re.match(r"^\s*(\d{1,2})\D+(\d{1,2})\D+\d{2,4}\s*$", raw) if isinstance(raw, str) else None
        if m:
            first += int(m[1]) > 12
            second += int(m[2]) > 12
    return {"options": options, "recommended": None,
            "evidence": {"first_number_above_12": first, "second_number_above_12": second}}
