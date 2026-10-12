"""The Mapper (target-first binding per field, verified on the real rows; behind plan_problems) and the Key
Resolver (three voters through resolve.vote, on the `small` role)."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import BaseModel, ConfigDict

from worker import role_runner as rr
from worker import setrecipe, transforms
from worker.contracts import (
    Binding,
    KeyDecisions,
    MappingPlan,
    Option,
    Preview,
    ProfileColumn,
    Question,
    Result,
    SourceProfile,
    Subject,
)
from worker.pipeline import plan_problems
from worker.resolve import batch_size, chunks, vote
from worker.templates import TemplateSpec


class _Bindings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bindings: list[Binding]


class _MapperEnvelope(rr.Envelope[_Bindings]):
    alternatives: dict[str, list[Binding]] | None = None

SETUP_FIELDS = ("GP_ID",)  # fields that come from the job setup, never from a column


MAPPER_INSTRUCTIONS = """You are the Mapper. Work target first: for each listed target field, say how it gets its
value from the source columns, using the field's type, format, allowed values and the column samples.
Actions: "map" (a column as it is, give col_id), "transform" (a column plus steps, give col_id and recipe),
"supply" (only for a setup field: value_from "setup", never a value), "missing" (no column holds it).
A transform step is {"op": name, "args": {...}}: copy a step from transform_ops, which lists every op allowed
with its parameter names; use only those names (the values shown are examples). Never choose between day-first and
month-first, or a decimal mark, on your own: propose both readings as alternatives.
JSON: {"summary": text, "confidence": number, "output": {"bindings": [{"field", "action", "col_id"?, "recipe"?,
"value_from"?, "reason", "confidence"}]}, "alternatives": {field: [binding, ...]}} with at most 3 bindings per field
counting the main one."""


def field_rule(name: str, spec: TemplateSpec) -> transforms.FieldRule:
    return replace(transforms.rule_for(name), required=name in spec.required)


def _format(rule: transforms.FieldRule) -> str:
    return {"date": "a date, written YYYY-MM-DD", "amount": "a plain decimal number"}.get(rule.kind, "text")


def _ops(binding: Binding) -> list[dict[str, Any]]:
    return [{"op": t.op, **dict(t.args)} for t in binding.recipe]


def order_choice(ops: Sequence[Mapping[str, Any]]) -> bool:
    """True when a step commits to day-first or month-first (both numeric, year not first). Never recommended."""
    for op in ops:
        if op.get("op") != "date_parse":
            continue
        for pat in op.get("patterns") or []:
            p = str(pat)
            if "D" in p and re.search(r"M(?!on)", p) and not p.startswith("Y"):
                return True
    return False


def _header_refs(columns: Sequence[ProfileColumn]) -> dict[str, Any]:
    counts = Counter(c.header for c in columns)
    seen: Counter[str] = Counter()
    refs: dict[str, Any] = {}
    for c in columns:
        seen[c.header] += 1
        refs[c.col_id] = c.header if counts[c.header] == 1 else {"header": c.header, "occurrence": seen[c.header]}
    return refs


def _verdict(name: str, ops: Sequence[Mapping[str, Any]], header_ref: Any, rule: transforms.FieldRule,
             rows: Sequence[transforms.Row]) -> transforms.Verdict:
    step = {"from": header_ref, "ops": list(ops)}
    return setrecipe.verify({"fields": {name: step}}, rows, {name: rule})[name]


def _person_choice(payload: dict[str, Any], verdict: transforms.Verdict, what: str) -> Preview:
    """An order or decimal-mark choice: the counts go to the payload as evidence and the preview stays empty, so
    code can never recommend it."""
    payload["person_choice"] = what
    payload["evidence"] = {"conform": verdict.conform, "total": verdict.total}
    return Preview(conform=0, total=0)


def _option(name: str, action: str, col_id: str | None, header_ref: Any, ops: Sequence[Mapping[str, Any]],
            preview: Preview, payload_extra: Mapping[str, Any] | None = None, note: str = "") -> Option:
    payload: dict[str, Any] = {"field": name, "col_id": col_id, "header": header_ref, "recipe": list(ops),
                               **dict(payload_extra or {})}
    return Option(option_id=rr.option_id(name, action, col_id, list(ops)), action=action, proposed_by="mapper",
                  preview=preview, payload=payload, note=note)


def missing_option(name: str) -> Option:
    return Option(option_id=rr.option_id(name, "declare_missing"), action="declare_missing", proposed_by="mapper",
                  preview=Preview(conform=0, total=0), payload={"field": name},
                  note="No column holds this field; it stays blank and is reported as MISSING.")


def binding_options(
    name: str,
    rule: transforms.FieldRule,
    header: str,
    col_id: str,
    values: Sequence[Any],
    *,
    first_row: int = 2,
    src: str = "",
) -> list[Option]:
    """Code's ways to fill field `name` from one column (at most 3) plus declaring it MISSING. Each preview is
    computed on the real values; a day/month order or a decimal mark is a person's choice and is never
    recommended."""
    rows = [transforms.Row(f"{src}{first_row + i}", (header,), (v,)) for i, v in enumerate(values)]
    texts = [v for v in values if isinstance(v, str) and v.strip()]
    tries: list[tuple[list[dict[str, Any]], str | None]] = [([], None)]
    if rule.kind == "date":
        tries.append(([{"op": "date_parse", "patterns": ["YYYY-MM-DD"]}], None))
        seps = Counter(m.group(1) for v in texts if (m := re.match(r"^\s*\d{1,2}([/.\-])\d{1,2}\1\d{4}\s*$", v)))
        if seps:
            sep = seps.most_common(1)[0][0]
            tries.append(([{"op": "date_parse", "patterns": [f"D{sep}M{sep}YYYY"]}], "day and month order"))
            tries.append(([{"op": "date_parse", "patterns": [f"M{sep}D{sep}YYYY"]}], "day and month order"))
    elif rule.kind == "amount":
        neg = ["minus", "brackets"]
        tries.append(([{"op": "number_parse", "decimal": ".", "thousands": [","], "negative": neg}], "decimal mark"))
        tries.append(([{"op": "number_parse", "decimal": ",", "thousands": ["."], "negative": neg}], "decimal mark"))
    elif texts and sum(" - " in v for v in texts) * 2 >= len(texts):
        tries.append(([{"op": "split", "sep": " - ", "index": 1}], None))
        tries.append(([{"op": "split", "sep": " - ", "index": 2}], None))
    built: list[tuple[list[dict[str, Any]], transforms.Verdict, str | None]] = []
    for ops, choice in tries:
        try:
            verdict = _verdict(name, ops, header, rule, rows)
        except transforms.RecipeError:
            continue
        built.append((ops, verdict, choice))
    # A decimal mark is the person's choice only when both readings read every row; otherwise the counts decide.
    # A day and month order is always the person's choice.
    by_choice = Counter(c for _, v, c in built if c is not None and v.clean)
    options: list[Option] = []
    for ops, verdict, choice in built:
        if ops and choice != "day and month order" and not verdict.conform:
            continue  # a reading that fits no row is not offered
        extra: dict[str, Any] = {}
        if choice == "day and month order" or (choice is not None and by_choice[choice] > 1):
            preview = _person_choice(extra, verdict, choice)
        else:
            preview = Preview(conform=verdict.conform, total=verdict.total, sample_ref=f"{src}{col_id}" or None)
        options.append(_option(name, "transform" if ops else "map", col_id, header, ops, preview, extra))
        if len(options) == rr.MAX_FIELD_OPTIONS:
            break
    return [*options, missing_option(name)]


def _binding_problems(
    b: Binding, spec: TemplateSpec, refs: Mapping[str, Any]
) -> list[str]:
    if b.field not in spec.fields:
        return [f"invented binding: {b.field} is not a field of {spec.name}"]
    problems: list[str] = []
    if b.action in ("map", "transform") and b.col_id not in refs:
        problems.append(f"invented binding: column {b.col_id} is not in the sheet ({b.field})")
    if b.action == "supply" and b.field not in SETUP_FIELDS:
        problems.append(f"invented constant: {b.field} does not come from setup")
    for t in b.recipe:
        if t.op not in transforms.OPS:
            problems.append(f"{b.field}: {t.op} is outside the closed transform vocabulary")
        elif t.op in ("default", "when") and touches_literal(t.args):
            problems.append(f"invented constant: {b.field} would write a value the source does not hold")
    if b.action in ("map", "transform") and not problems:
        try:
            transforms.compile_field(b.field, {"from": refs[b.col_id], "ops": _ops(b)}, field_rule(b.field, spec))
        except transforms.RecipeError as exc:
            problems.append(f"{b.field}: {exc.code}: {exc.message}")
    return problems


def touches_literal(args: Mapping[str, Any]) -> bool:
    """True when a `default` or `when` step carries a typed literal value (`{"text": ...}`): an invented constant."""
    def walk(v: Any) -> bool:
        if isinstance(v, Mapping):
            return "text" in v or any(walk(x) for k, x in v.items() if k != "test")
        if isinstance(v, list | tuple):
            return any(walk(x) for x in v)
        return False

    return walk(dict(args))


def map_fields(
    runner: rr.RoleRunner,
    *,
    unit_id: str,
    spec: TemplateSpec,
    profile: SourceProfile,
    rows: Sequence[transforms.Row],
) -> rr.RoleOutcome:
    """Target-first plan for `spec` over a profiled sheet. `rows` carry the profile's headers in column order.

    Code binds every field whose header names it and reads cleanly, and every setup field; the Mapper is asked only
    about the rest (0 model calls for a known layout). Its bindings are verified on the real rows: a clean one is
    kept, anything else (not clean, a day/month order, a required field left missing) is a Question with up to 3
    options plus declaring MISSING."""
    refs = _header_refs(profile.columns)
    by_norm: dict[str, ProfileColumn] = {}
    for col in profile.columns:
        by_norm.setdefault(rr.norm(col.header), col)
    accepted: dict[str, Binding] = {}
    open_fields: list[str] = []
    for name in spec.fields:
        if name in SETUP_FIELDS:
            accepted[name] = Binding(field=name, action="supply", value_from="setup", confidence=1.0,
                                     reason="comes from the job setup")
            continue
        col = by_norm.get(rr.norm(name))
        if col is not None:
            try:
                clean = _verdict(name, [], refs[col.col_id], field_rule(name, spec), rows).clean
            except transforms.RecipeError:
                clean = False
            if clean:
                accepted[name] = Binding(field=name, action="map", col_id=col.col_id, confidence=1.0,
                                         reason="the header names the field and every row reads")
                continue
        open_fields.append(name)
    if not open_fields:
        bound = tuple(accepted[f] for f in spec.fields if f in accepted)
        plan = MappingPlan(plan_id=f"plan-{rr.sha([spec.name, [b.to_dict() for b in bound]])[:16]}",
                           record_type=spec.name, bindings=bound)
        return rr.RoleOutcome("code", plan)

    samples: dict[str, list[str]] = {}
    for idx, col in enumerate(profile.columns):
        seen: list[str] = []
        for row in rows:
            v = rr.cell_text(row.cells[idx]) if idx < len(row.cells) else ""
            if v and v not in seen:
                seen.append(v)
            if len(seen) == 5:
                break
        samples[col.col_id] = seen
    target = []
    for name in open_fields:
        rule = field_rule(name, spec)
        target.append({
            "field": name, "type": rule.kind, "required": rule.required, "format": _format(rule),
            "max_len": rule.max_len, "allowed_values": sorted(rule.reference)[:20] if rule.reference else None,
        })
    payload = {
        "record_type": spec.name,
        "fields": target,
        "columns": [{"col_id": c.col_id, "header": c.header, "sample_ref": c.sample_ref,
                     "samples": samples[c.col_id]} for c in profile.columns],
        "transform_ops": [{"op": op, "args": transforms.OP_EXAMPLES[op]} for op in transforms.OPS],
        "setup_fields": list(SETUP_FIELDS),
        "rows": len(rows),
    }
    brief = rr.make_brief("mapper", unit_id, f"Bind the open fields of {spec.name}", payload)
    plan_id = f"plan-{rr.sha([spec.name, profile.profile_id, open_fields])[:16]}"

    def build(out: _Bindings) -> MappingPlan:
        return MappingPlan(plan_id=plan_id, record_type=spec.name, bindings=tuple(out.bindings))

    def verify(result: Result, env: _MapperEnvelope) -> list[str]:
        plan: MappingPlan = result.output
        problems: list[str] = []
        alternatives = env.alternatives or {}
        for name, items in alternatives.items():
            main = [b for b in plan.bindings if b.field == name]
            if len(main) + len(items) > rr.MAX_FIELD_OPTIONS:
                problems.append(f"more than {rr.MAX_FIELD_OPTIONS} proposals for {name}")
        everything = [*plan.bindings, *(b for items in alternatives.values() for b in items)]
        for b in everything:
            problems.extend(_binding_problems(b, spec, refs))
        bad = rr.unsupported_numbers([b.reason for b in everything], payload)
        if bad:
            problems.append(f"a reason states numbers that are not in the data: {', '.join(bad[:5])}")
        columns = {f: b.col_id for f, b in accepted.items() if b.col_id}
        columns.update({b.field: b.col_id for b in plan.bindings if b.col_id and b.field in open_fields})
        constants = {f: "setup" for f, b in accepted.items() if b.action == "supply"}
        constants.update({b.field: "setup" for b in plan.bindings if b.action == "supply" and b.field in open_fields})
        for msg in plan_problems({"columns": columns, "constants": constants}, spec, SETUP_FIELDS):
            if not msg.startswith("required fields with no source"):
                problems.append(msg)
        return list(dict.fromkeys(problems))

    attempt = runner.call(brief, payload, instructions=MAPPER_INSTRUCTIONS, build=build, verify=verify,
                          facts=payload, envelope=_MapperEnvelope)
    alternatives = (attempt.env.alternatives or {}) if attempt.env is not None else {}
    questions: list[Question] = []
    if attempt.result is None:
        for name in open_fields:
            questions.append(_field_question(unit_id, name, [], spec, refs, rows))
    else:
        plan: MappingPlan = attempt.result.output
        for name in open_fields:
            choices = [b for b in plan.bindings if b.field == name] + alternatives.get(name, [])
            rule = field_rule(name, spec)
            first = choices[0] if choices else None
            if first is not None and first.action == "supply" or first is not None and first.action == "missing" and not rule.required:
                accepted[name] = first
            elif first is None and not rule.required:
                accepted[name] = Binding(field=name, action="missing", confidence=1.0,
                                         reason="no column holds this optional field; it is reported as MISSING")
            elif (first is not None and first.action in ("map", "transform") and not order_choice(_ops(first))
                  and _verdict(name, _ops(first), refs[first.col_id], rule, rows).clean):
                accepted[name] = first
            else:
                questions.append(_field_question(unit_id, name, choices, spec, refs, rows))
    final_plan = MappingPlan(plan_id=plan_id, record_type=spec.name,
                             bindings=tuple(accepted[f] for f in spec.fields if f in accepted))
    return rr.outcome(brief, attempt, final_plan, questions, final_plan)


def _field_question(
    unit_id: str, name: str, choices: Sequence[Binding], spec: TemplateSpec, refs: Mapping[str, Any],
    rows: Sequence[transforms.Row],
) -> Question:
    """A field the plan cannot settle: the Mapper's proposals with previews computed by code, plus MISSING."""
    rule = field_rule(name, spec)
    options: list[Option] = []
    seen: set[str] = set()
    for b in choices:
        if b.action not in ("map", "transform"):
            continue
        ops = _ops(b)
        key = rr.canon([b.col_id, ops])
        if key in seen:
            continue
        seen.add(key)
        verdict = _verdict(name, ops, refs[b.col_id], rule, rows)
        extra: dict[str, Any] = {}
        if order_choice(ops):
            preview = _person_choice(extra, verdict, "day and month order")
        else:
            preview = Preview(conform=verdict.conform, total=verdict.total)
        options.append(_option(name, b.action, b.col_id, refs[b.col_id], ops, preview, extra, note=b.reason))
        if len(options) == rr.MAX_FIELD_OPTIONS:
            break
    options.append(missing_option(name))
    kind = "unknown_format" if any(o.payload.get("person_choice") for o in options) else "unknown_field"
    subject = Subject(kind="field", ref=name)
    return Question.build(question_id=rr.question_id(unit_id, kind, subject.to_dict()), unit_id=unit_id, kind=kind,
                          subject=[subject], options=options, blocking=("convert",))


KEY_INSTRUCTIONS = """You are the Key Resolver. For each item pick one of its candidate ids, or null when none fits.
Pick only from that item's candidates; never invent an id. You do not see rows.
JSON: {"summary": text, "confidence": number, "output": {"picks": {key: candidate id or null}}}"""


VOTERS = 3


class _Picks(BaseModel):
    model_config = ConfigDict(extra="forbid")

    picks: dict[str, str | None]


@dataclass(frozen=True)
class KeyItem:
    """One distinct key, its candidate ids (code produced them) and the words the model may see."""

    key: str
    candidates: tuple[str, ...]
    text: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class KeyOutcome:
    decided: dict[str, str]
    questions: tuple[Question, ...]
    by_lookup: int = 0
    results: list[Result] = field(default_factory=list)  # every verified voter Result
    abstained: int = 0  # voters whose answer failed verification twice


def resolve_keys(
    runner: rr.RoleRunner,
    *,
    unit_id: str,
    items: Sequence[KeyItem],
    lookup: Mapping[str, str] | None = None,
) -> KeyOutcome:
    """Lookup first, then the leftovers in batches, each batch voted on `VOTERS` times. A key is decided only when
    a majority agree on a candidate; every other key is a `disagreement` Question."""
    known = dict(lookup or {})
    by_key = {i.key: i for i in items}
    decided: dict[str, str] = {}
    by_lookup = 0
    leftovers: list[str] = []
    for item in items:
        if item.key in known and known[item.key] in item.candidates:
            decided[item.key] = known[item.key]
            by_lookup += 1
        else:
            leftovers.append(item.key)
    outcome = KeyOutcome(decided, (), by_lookup)
    human: list[str] = []
    for batch in chunks(list(dict.fromkeys(leftovers)), batch_size()):
        batch_id = f"k-{rr.sha(batch)[:16]}"
        payload = {"batch_id": batch_id, "items": [
            {"key": k, "candidates": list(by_key[k].candidates), **dict(by_key[k].text)} for k in batch
        ]}
        voter = iter(range(1, VOTERS + 1))

        def decide(keys: list[Any], batch_id: str = batch_id, payload: dict[str, Any] = payload,
                   voter: Any = voter) -> dict[str, Any]:
            n = next(voter)
            brief = rr.make_brief("key_resolver", unit_id, f"Pick a candidate for each key (voter {n} of {VOTERS})",
                               payload)

            def build(out: _Picks) -> KeyDecisions:
                return KeyDecisions(batch_id=batch_id, picks=out.picks, voter=n)

            def verify(result: Result, env: Any) -> list[str]:
                problems = []
                for key, pick in result.output.picks.items():
                    if key not in by_key or key not in keys:
                        problems.append(f"invented key: {key!r} is not in this batch")
                    elif pick is not None and pick not in by_key[key].candidates:
                        problems.append(f"pick outside the candidates for {key!r}: {pick!r}")
                return problems

            attempt = runner.call(brief, payload, instructions=KEY_INSTRUCTIONS, build=build, verify=verify,
                                  facts=payload, envelope=rr.Envelope[_Picks])
            if attempt.result is None:
                outcome.abstained += 1
                return {}
            outcome.results.append(attempt.result)
            return {k: v for k, v in attempt.result.output.picks.items() if v is not None}

        agreed, undecided = vote(decide, batch, VOTERS)
        for key, pick in agreed.items():
            if pick in by_key[key].candidates:  # defence in depth: never a pick outside the candidates
                decided[key] = pick
            else:
                undecided.append(key)
        human.extend(undecided)
    questions = []
    for key in human:
        subject = Subject(kind="key", ref=key)
        options = [
            Option(option_id=rr.option_id(key, c), action="pick", proposed_by="key_resolver",
                   preview=Preview(conform=0, total=0), payload={"key": key, "candidate": c})
            for c in by_key[key].candidates[:4]
        ] or [Option(option_id=rr.option_id(key, "skip"), action="declare_missing", proposed_by="key_resolver",
                     preview=Preview(conform=0, total=0), payload={"key": key}, note="No candidate exists.")]
        questions.append(Question.build(question_id=rr.question_id(unit_id, "disagreement", key), unit_id=unit_id,
                                        kind="disagreement", subject=[subject], options=options, blocking=("map",)))
    outcome.questions = tuple(questions)
    return outcome
