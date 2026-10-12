"""Typed messages for hand-offs between the Conductor, the agents and the human.

These are frozen pydantic dataclasses: field types and rules are checked on construction, with a canonical JSON form so
a record can be hashed and a replay can be byte-identical.

Rules the types enforce (topology.md section 4 and 5, issue 09 D3 and D4):

* Every reference is an id the system issued. No contract carries a path or a row.
* A summary is at most 600 characters. Numbers travel as references into engine results.
* A Question has 1 to 4 Options, scopes ``job`` and/or ``sponsor`` (never organisation), and a recommended option
  that code chooses (``choose_recommended``).
* An Option can never offer a control, and an agent can never be the approver or the principal of an Answer.
"""

from __future__ import annotations

import functools
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, field
from typing import Annotated, Any, Self

from pydantic import (
    AfterValidator,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.dataclasses import dataclass as pydantic_dataclass
from pydantic_core import to_jsonable_python

# ----------------------------------------------------------------------------------------------------------------
# Vocabulary

SUMMARY_MAX = 600
MAX_OPTIONS = 4

ROLES = ("coworker", "profiler", "mapper", "key_resolver", "explainer", "drafter", "layout_drafter",
         "interpreter")
STAGES = ("read", "map", "convert", "check", "write")  # = pipeline.STAGES
ARTIFACT_KINDS = ("profile", "stage", "plan", "keys", "recon", "findings")
LAYOUTS = ("flat", "blocks", "compact", "gl_lines")
BINDING_ACTIONS = ("map", "transform", "supply", "missing")
OPTION_ACTIONS = ("map", "transform", "supply", "declare_missing", "pick", "rename_match", "skip_unit")
QUESTION_KINDS = (
    "unknown_field",
    "unknown_format",
    "conflict",
    "low_confidence",
    "disagreement",
    "missing_report",
    "err_no_rule",
)
SCOPES = ("job", "sponsor")  # organisation scope belongs to the improvement loop, never to a Question
RESULT_STATUSES = ("ok", "needs_human", "failed")

HANDOFF_SENT = "handoff.sent"
HANDOFF_RETURNED = "handoff.returned"
HANDOFF_REJECTED = "handoff.rejected"
HANDOFF_EVENTS = (HANDOFF_SENT, HANDOFF_RETURNED, HANDOFF_REJECTED)

# Tools that sit behind interrupt_on (agent.GATED_TOOLS). A Brief may never hand one to an agent.
GATED_TOOLS = frozenset(
    {
        "write_upload",
        "propose_rule",
        "set_id_scheme",
        "import_existing_records",
        "add_existing_record",
        "confirm_account_mappings",
        "supply_value",
        "approve_layout_program",
    }
)
# Roles that return structured output only.
_NO_TOOL_ROLES = frozenset({"key_resolver", "explainer", "drafter", "layout_drafter", "interpreter"})

_AGENT_WORDS = frozenset(
    {"agent", "assistant", "model", "llm", "bot", "system", "worker", "conductor", "gatekeeper", "improver",
     "replayer", "auditor", "writer", "learned"} | set(ROLES)
)
_CONTROL_KEYS = frozenset({"control", "controls", "control_id", "controls_yaml"})

_SKILL_SLICE = re.compile(r"^[A-Za-z0-9_.-]+@[A-Za-z0-9_.:-]+#[A-Za-z0-9_./-]+$")
_SCHEMA_REF = re.compile(r"^[A-Za-z][A-Za-z0-9_]*@[0-9]+$")


# ----------------------------------------------------------------------------------------------------------------
# Field types (a pydantic annotation is the validator)


class ContractError(ValueError):
    """A message failed validation. The offender never reaches the engine."""


def _nonblank(v: str) -> str:
    if not v.strip():
        raise ValueError("must not be blank")
    return v


def _cap(v: str) -> str:
    if len(v) > SUMMARY_MAX:
        raise ValueError(f"is {len(v)} characters; the limit is {SUMMARY_MAX}")
    return v


def _unit(v: float) -> float:
    if not 0.0 <= v <= 1.0:  # also rejects NaN
        raise ValueError("must be between 0 and 1")
    return v


def _unique(v: tuple[str, ...]) -> tuple[str, ...]:
    if len(set(v)) != len(v):
        raise ValueError("holds a duplicate")
    return v


def _slices(v: tuple[str, ...]) -> tuple[str, ...]:
    for item in v:
        if not _SKILL_SLICE.match(item):
            raise ValueError(f"skill slice {item!r} is not skill@checksum#file")
    return v


def _schema_ref(v: str) -> str:
    if not _SCHEMA_REF.match(v):
        raise ValueError("must be name@version")
    return v


def _scopes(v: tuple[str, ...]) -> tuple[str, ...]:
    if not v:
        raise ValueError("a Question offers at least one scope")
    for scope in v:
        if scope not in SCOPES:
            raise ValueError(f"scope {scope!r} is not offered on a Question (job or sponsor only)")
    return v


def _stages(v: tuple[str, ...]) -> tuple[str, ...]:
    for stage in v:
        if stage not in STAGES:
            raise ValueError(f"must be one of {', '.join(STAGES)}; got {stage!r}")
    return v


def _human(v: str) -> str:
    if is_agent_principal(v):
        raise ValueError(f"{v!r} is an agent or system actor; only a human may approve")
    return v


def _json(value: Any, name: str = "value") -> Any:
    """Deep-copy a JSON value, refusing anything that is not plain JSON (tuples become lists)."""
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{name} holds a number that is not finite")
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(k, str) for k in value):
            raise ValueError(f"{name} keys must be text")
        return {k: _json(v, f"{name}.{k}") for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json(v, name) for v in value]
    raise ValueError(f"{name} holds a value that is not plain JSON ({type(value).__name__})")


def lit(*allowed: str) -> Any:
    def check(v: str) -> str:
        if v not in allowed:
            raise ValueError(f"must be one of {', '.join(allowed)}; got {v!r}")
        return v

    return Annotated[str, Field(strict=True), AfterValidator(check)]


Free = Annotated[str, Field(strict=True)]
Str = Annotated[Free, AfterValidator(_nonblank)]
Note = Annotated[Free, AfterValidator(_cap)]
Summary = Annotated[Str, AfterValidator(_cap)]
Unit = Annotated[float, Field(strict=True), AfterValidator(_unit)]
Count = Annotated[int, Field(strict=True, ge=0)]
Strs = tuple[Str, ...]
Uniq = Annotated[Strs, AfterValidator(_unique)]
Human = Annotated[Str, AfterValidator(_human)]
Plain = Annotated[dict[str, Any], AfterValidator(_json)]


def _mentions_control(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(str(k).casefold() in _CONTROL_KEYS or _mentions_control(v) for k, v in value.items())
    if isinstance(value, list | tuple):
        return any(_mentions_control(item) for item in value)
    if isinstance(value, str):
        low = value.casefold()
        return "controls.yaml" in low or low.startswith("controls.")
    return False


def is_agent_principal(name: str) -> bool:
    """True when ``name`` is an agent, a role or a system actor, which may never approve."""
    low = name.strip().casefold()
    tokens = [t for t in re.split(r"[^a-z0-9]+", low) if t]
    joined = "_".join(tokens)
    return not tokens or joined in _AGENT_WORDS or any(t in _AGENT_WORDS for t in tokens)


class UnnamedApprover(ValueError):
    """An approval or a rejection that names no person the system will accept."""


DEMO_REVIEWER = "local"  # the web's name for a call that carries none, while no approver token is configured


def approver_name(raw: str, *, demo: bool) -> str:
    """The name an approval or rejection is recorded under, or `UnnamedApprover`. Self-declared (demo-grade), but
    always a person: agents and reserved names never approve; `local` or blank is the demo reviewer, accepted only
    when `demo` (no approver token) and recorded as `local reviewer`."""
    from worker.improver import reserved_name

    who = " ".join(str(raw or "").split())
    if not who or who.casefold() == DEMO_REVIEWER:
        if demo:
            return f"{DEMO_REVIEWER} reviewer"
        raise UnnamedApprover("name yourself to approve or reject: send your name in X-Reviewer")
    if is_agent_principal(who) or reserved_name(who):
        raise UnnamedApprover(f"{who!r} is not a person; only a named person approves or rejects")
    return who


# ----------------------------------------------------------------------------------------------------------------
# Canonical JSON and the base class


def canonical_json(obj: Any) -> str:
    """Stable text for hashing: sorted keys, no spaces, UTF-8 characters kept, no NaN."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def explain_error(exc: ValidationError) -> str:
    """One sentence per problem, naming the contract and the field."""
    out = []
    for d in exc.errors():
        loc = ".".join(str(p) for p in d["loc"])
        kind, text = d["type"], d["msg"].removeprefix("Value error, ")
        if kind == "missing":
            out.append(f"{exc.title} is missing {loc}")
        elif kind in ("unexpected_keyword_argument", "extra_forbidden"):
            where = f" (at {loc}; it does not belong there)" if "." in loc else ""  # a repair needs the place
            out.append(f"{exc.title} has unknown keys: {loc.rsplit('.', 1)[-1]}{where}")
        else:
            out.append(f"{exc.title}.{loc} {text}" if loc else f"{exc.title}: {text}")
    return "; ".join(out)


class Contract:
    """Mixin: dict and JSON round trip, and a stable content hash."""

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable_python(asdict(self))  # type: ignore[call-overload]

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    def digest(self) -> str:
        """sha256 of the canonical JSON. Equal contracts give equal digests, on any machine."""
        return sha256_hex(self.to_json())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        if not isinstance(data, Mapping):
            raise ContractError(f"{cls.__name__} must be an object")
        return cls(**data)

    @classmethod
    def from_json(cls, text: str) -> Self:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ContractError(f"{cls.__name__}: not JSON ({exc.msg})") from exc
        return cls.from_dict(data)


def contract(cls: type) -> type:
    """A frozen pydantic dataclass: field types validate on construction and on `from_dict`, nested dicts become
    contracts, and any failure is one `ContractError`. Positional use and `dataclasses.replace` keep working."""
    cls = pydantic_dataclass(frozen=True, config=ConfigDict(extra="forbid"))(cls)
    plain = cls.__init__

    @functools.wraps(plain)
    def __init__(self: Any, *args: Any, **kwargs: Any) -> None:
        try:
            plain(self, *args, **kwargs)
        except ValidationError as exc:
            raise ContractError(explain_error(exc)) from None

    cls.__init__ = __init__  # type: ignore[misc]
    return cls


def _derived_id(prefix: str, payload: Mapping[str, Any]) -> str:
    return f"{prefix}-{sha256_hex(canonical_json(payload))[:16]}"


def _derive(obj: Any, name: str, value: str) -> None:
    """Fill a content-derived id; an id supplied by a stored record must match its content."""
    have = getattr(obj, name)
    if have and have != value:
        raise ValueError(f"{name} does not match its content")
    object.__setattr__(obj, name, value)


# ----------------------------------------------------------------------------------------------------------------
# References


@contract
class ArtifactRef(Contract):
    """A pointer to something the system stored: id plus checksum, so a changed artifact is detected."""

    kind: lit(*ARTIFACT_KINDS)  # type: ignore[valid-type]
    id: Str
    checksum: Str


@contract
class Subject(Contract):
    """What a Question is about: an artifact, a target field or a distinct key. Always an issued id or a name."""

    kind: lit("artifact", "field", "key")  # type: ignore[valid-type]
    ref: Str
    checksum: Str | None = None

    @model_validator(mode="after")
    def _only_artifacts(self) -> Self:
        if self.kind != "artifact" and self.checksum is not None:
            raise ValueError("only an artifact subject carries a checksum")
        return self


@contract
class Budget(Contract):
    model_calls: Count
    tool_calls: Count
    tokens: Count
    seconds: Count

    @model_validator(mode="after")
    def _calls_for_tokens(self) -> Self:
        if self.model_calls == 0 and self.tokens > 0:
            raise ValueError("a budget with no model calls cannot allow tokens")
        return self


# ----------------------------------------------------------------------------------------------------------------
# Work: WorkOrder, JobUnit, Brief


@contract
class JobUnit(Contract):
    """(file, sheet, record type, entity) under a set of pins. ``unit_id`` is a hash that includes the pins."""

    file_fp: Str
    sheet: Str
    record_type: Str
    entity: Str | None
    pins: dict[Str, Str]
    depends_on: Uniq = ()
    unit_id: str = ""

    @model_validator(mode="after")
    def _identify(self) -> Self:
        body = {"file_fp": self.file_fp, "sheet": self.sheet, "record_type": self.record_type,
                "entity": self.entity, "pins": self.pins}
        _derive(self, "unit_id", _derived_id("u", body))
        if self.unit_id in self.depends_on:
            raise ValueError("a unit cannot depend on itself")
        return self


@contract
class WorkOrder(Contract):
    """What the human confirmed: the units to run. Pins are the same for every unit and fixed for the order."""

    order_id: Str
    request_text: Free
    units: tuple[JobUnit, ...]
    proposed_by: lit("coworker", "human")  # type: ignore[valid-type]
    confirmed_by: Human | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        units = self.units
        if not units:
            raise ValueError("a WorkOrder needs at least one unit")
        ids = [u.unit_id for u in units]
        if len(set(ids)) != len(ids):
            raise ValueError("WorkOrder holds the same unit twice")
        if len({canonical_json(u.pins) for u in units}) != 1:
            raise ValueError("every unit in an order carries the same pins (fixed at the start of the order)")
        known = set(ids)
        for unit in units:
            missing = [d for d in unit.depends_on if d not in known]
            if missing:
                raise ValueError(f"unit {unit.unit_id} depends on a unit outside the order: {missing[0]}")
        deps = {u.unit_id: u.depends_on for u in units}
        done: set[str] = set()

        def visit(node: str, path: frozenset[str]) -> None:
            if node in path:
                raise ValueError("units depend on each other in a cycle")
            if node not in done:
                for parent in deps[node]:
                    visit(parent, path | {node})
                done.add(node)

        for node in deps:
            visit(node, frozenset())
        return self

    @property
    def confirmed(self) -> bool:
        return self.confirmed_by is not None


@contract
class Brief(Contract):
    """The only thing an agent is given. ``brief_id`` is derived from the content, so a repeat is recognisable."""

    unit_id: Str
    role: lit(*ROLES)  # type: ignore[valid-type]
    objective: Str
    inputs: tuple[ArtifactRef, ...]
    skill_slice: Annotated[Uniq, AfterValidator(_slices)]
    tools: Uniq
    output_schema: Annotated[str, AfterValidator(_schema_ref)]
    budget: Budget
    stop_when: Str
    brief_id: str = ""

    @model_validator(mode="after")
    def _identify(self) -> Self:
        gated = sorted(set(self.tools) & GATED_TOOLS)
        if gated:
            raise ValueError(f"an agent cannot be given a gated tool: {gated[0]}")
        if self.role in _NO_TOOL_ROLES and self.tools:
            raise ValueError(f"role {self.role} returns structured output only and gets no tools")
        body = {"unit_id": self.unit_id, "role": self.role, "objective": self.objective,
                "inputs": [asdict(i) for i in self.inputs], "skill_slice": list(self.skill_slice),
                "tools": list(self.tools), "output_schema": self.output_schema, "budget": asdict(self.budget),
                "stop_when": self.stop_when}
        _derive(self, "brief_id", _derived_id("b", body))
        return self


# ----------------------------------------------------------------------------------------------------------------
# Agent outputs


@contract
class ProfileColumn(Contract):
    col_id: Str
    header: Free
    kind: Str
    sample_ref: Str | None = None


@contract
class RecordTypeCandidate(Contract):
    type: Str
    confidence: Unit
    evidence: Note


@contract
class SourceProfile(Contract):
    """The Profiler's answer to "what is this sheet"."""

    profile_id: Str
    file_fp: Str
    sheet: Str
    header_row: Annotated[int, Field(strict=True, ge=1)]
    layout: lit(*LAYOUTS)  # type: ignore[valid-type]
    columns: tuple[ProfileColumn, ...]
    record_type_candidates: tuple[RecordTypeCandidate, ...] = ()

    @model_validator(mode="after")
    def _unique_columns(self) -> Self:
        if len({c.col_id for c in self.columns}) != len(self.columns):
            raise ValueError("SourceProfile holds a duplicate col_id")
        return self


@contract
class Transform(Contract):
    """One step of the closed transform vocabulary (ticket 02). The op names are checked by transform_check.py."""

    op: Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9_]*$")]
    args: Plain = field(default_factory=dict)


@contract
class Binding(Contract):
    """How one target field gets its value. Never an invented constant: supply names a setup source."""

    field: Str
    action: lit(*BINDING_ACTIONS)  # type: ignore[valid-type]
    reason: Note
    confidence: Unit
    col_id: Str | None = None
    recipe: tuple[Transform, ...] = ()
    value_from: lit("setup") | None = None  # type: ignore[valid-type]

    @field_validator("recipe", mode="before")
    @classmethod
    def _no_recipe(cls, value: Any) -> Any:
        """`null` is "no steps", as for the other optional keys; models write it for an optional key."""
        return () if value is None else value

    @model_validator(mode="after")
    def _shape(self) -> Self:
        needs_col = self.action in ("map", "transform")
        if needs_col and self.col_id is None:
            raise ValueError(f"a {self.action} binding needs a col_id")
        if not needs_col and self.col_id is not None:
            raise ValueError(f"a {self.action} binding must not name a column")
        if self.action == "transform" and not self.recipe:
            raise ValueError("a transform binding needs at least one transform")
        if self.action != "transform" and self.recipe:
            raise ValueError(f"a {self.action} binding must not carry transforms")
        if self.action == "supply" and self.value_from is None:
            raise ValueError("a supply binding must say where the value comes from; it never holds one")
        if self.action != "supply" and self.value_from is not None:
            raise ValueError(f"a {self.action} binding must not name a supply source")
        return self


@contract
class MappingPlan(Contract):
    plan_id: Str
    record_type: Str
    bindings: tuple[Binding, ...]

    @model_validator(mode="after")
    def _once_per_field(self) -> Self:
        names = [b.field for b in self.bindings]
        if len(set(names)) != len(names):
            raise ValueError("MappingPlan binds the same field twice")
        return self


@contract
class KeyDecisions(Contract):
    """One voter's picks for a batch of distinct keys. A pick is a candidate id from the list, or None."""

    batch_id: Str
    picks: dict[str, str | None]
    voter: Annotated[int, Field(strict=True, ge=1, le=3)]


# ----------------------------------------------------------------------------------------------------------------
# Questions and answers


@contract
class Preview(Contract):
    """Computed by code on the real rows: how many conform, out of how many."""

    conform: Count
    total: Count
    sample_ref: Str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.conform > self.total:
            raise ValueError(f"conform must be between 0 and {self.total}")
        return self

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.conform == self.total


@contract
class Option(Contract):
    """One closed-vocabulary way to resolve a Question. It can never be a control."""

    option_id: Str
    action: lit(*OPTION_ACTIONS)  # type: ignore[valid-type]
    proposed_by: lit(*ROLES, "human", "learned")  # type: ignore[valid-type]
    preview: Preview
    payload: Plain = field(default_factory=dict)
    note: Note = ""
    suggestion_id: Str | None = None

    @model_validator(mode="after")
    def _no_control(self) -> Self:
        if _mentions_control(self.payload):
            raise ValueError("an option cannot offer a control; controls change only through a controller")
        return self


# Preference when code picks the recommended option. Lower is preferred.
_ACTION_RANK = {"map": 0, "transform": 1, "pick": 2, "rename_match": 3, "supply": 4}
_PROPOSER_RANK = {"human": 0, "mapper": 1, "learned": 2}


def choose_recommended(options: Sequence[Option]) -> str | None:
    """Code's choice. Only an option whose rows all conform is eligible, and declaring missing or skipping is never
    recommended. Ties go by action, then proposer, then position. Confidence plays no part."""
    ranked = [
        (_ACTION_RANK[o.action], _PROPOSER_RANK.get(o.proposed_by, 3), i, o.option_id)
        for i, o in enumerate(options)
        if o.preview.complete and o.action in _ACTION_RANK
    ]
    return min(ranked)[3] if ranked else None


@contract
class Question(Contract):
    """The ask. Build it with ``Question.build`` so the recommended option is chosen by code."""

    question_id: Str
    unit_id: Str
    kind: lit(*QUESTION_KINDS)  # type: ignore[valid-type]
    subject: tuple[Subject, ...]
    options: tuple[Option, ...]
    recommended: Str | None
    scopes: Annotated[Uniq, AfterValidator(_scopes)]
    blocking: Annotated[Uniq, AfterValidator(_stages)] = ()

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if not self.subject:
            raise ValueError("a Question needs a subject")
        if not 1 <= len(self.options) <= MAX_OPTIONS:
            raise ValueError(f"a Question has 1 to {MAX_OPTIONS} options; got {len(self.options)}")
        ids = [o.option_id for o in self.options]
        if len(set(ids)) != len(ids):
            raise ValueError("a Question holds the same option_id twice")
        if self.recommended != choose_recommended(self.options):
            raise ValueError("the recommended option is chosen by code (choose_recommended), not supplied")
        return self

    @classmethod
    def build(
        cls,
        *,
        question_id: str,
        unit_id: str,
        kind: str,
        subject: Sequence[Subject],
        options: Sequence[Option],
        scopes: Sequence[str] = SCOPES,
        blocking: Sequence[str] = (),
    ) -> Question:
        opts = tuple(options)
        return cls(
            question_id=question_id,
            unit_id=unit_id,
            kind=kind,
            subject=tuple(subject),
            options=opts,
            recommended=choose_recommended(opts) if all(isinstance(o, Option) for o in opts) else None,
            scopes=tuple(scopes),
            blocking=tuple(blocking),
        )

    def option(self, option_id: str) -> Option:
        for opt in self.options:
            if opt.option_id == option_id:
                return opt
        raise ContractError(f"no option {option_id!r} on question {self.question_id}")


@contract
class Answer(Contract):
    """The human's decision. Exactly one of ``option_id`` or ``custom``; the principal is always a human."""

    question_id: Str
    scope: lit(*SCOPES)  # type: ignore[valid-type]
    reason: Free
    principal: Human
    option_id: Str | None = None
    custom: Option | None = None

    @model_validator(mode="after")
    def _one_choice(self) -> Self:
        if (self.option_id is None) == (self.custom is None):
            raise ValueError("an Answer names exactly one of option_id or custom")
        if self.custom is not None:
            if self.custom.proposed_by != "human":
                raise ValueError("a custom option is proposed by the human")
            if not self.reason.strip():
                raise ValueError("a custom option needs a reason")
        return self

    def check_against(self, question: Question) -> None:
        """Raise unless this answer fits the question it answers (id, option, scope)."""
        if self.question_id != question.question_id:
            raise ContractError("this answer is for a different question")
        if self.scope not in question.scopes:
            raise ContractError(f"scope {self.scope!r} is not offered on this question")
        if self.option_id is not None:
            question.option(self.option_id)


@contract
class Explanation(Contract):
    """The Explainer's likely cause. Figures are never in the text: ``cites`` points at engine table rows."""

    cause_id: Str
    likely_cause: Summary
    cites: Strs
    check_no: Annotated[int, Field(strict=True, ge=1)] | None = None
    ask: Question | None = None

    @model_validator(mode="after")
    def _cites(self) -> Self:
        if not self.cites:
            raise ValueError("an Explanation cites at least one engine table row")
        return self


# ----------------------------------------------------------------------------------------------------------------
# Result and Handoff

_OUTPUT_TYPES: dict[str, type[Contract]] = {
    "SourceProfile": SourceProfile,
    "MappingPlan": MappingPlan,
    "KeyDecisions": KeyDecisions,
    "Explanation": Explanation,
}
_ROLE_OUTPUT = {
    "profiler": SourceProfile,
    "mapper": MappingPlan,
    "key_resolver": KeyDecisions,
    "explainer": Explanation,
}


@contract
class Result(Contract):
    """What an agent returns. Code validates the schema, then the content on real rows, before it is used."""

    brief_id: Str
    role: lit(*ROLES)  # type: ignore[valid-type]
    status: lit(*RESULT_STATUSES)  # type: ignore[valid-type]
    summary: Summary
    confidence: Unit
    output: SourceProfile | MappingPlan | KeyDecisions | Explanation | None = None
    questions: tuple[Question, ...] = ()

    @model_validator(mode="after")
    def _agree(self) -> Self:
        expected = _ROLE_OUTPUT.get(self.role)
        if self.output is not None and (expected is None or not isinstance(self.output, expected)):
            raise ValueError(f"role {self.role} does not return {type(self.output).__name__}")
        if self.status == "ok" and expected is not None and self.output is None:
            raise ValueError(f"an ok result from {self.role} carries its {expected.__name__}")
        if self.status == "needs_human" and not self.questions:
            raise ValueError("a needs_human result carries at least one Question")
        if self.status == "failed" and self.output is not None:
            raise ValueError("a failed result carries no output")
        return self

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        if self.output is not None:
            data["output"] = {"type": type(self.output).__name__, "value": data["output"]}
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        if not isinstance(data, Mapping):
            raise ContractError("Result must be an object")
        raw = dict(data)
        tagged = raw.get("output")
        if tagged is not None:
            if not isinstance(tagged, Mapping) or set(tagged) != {"type", "value"}:
                raise ContractError("Result.output must be {type, value}")
            kind = _OUTPUT_TYPES.get(str(tagged["type"]))
            if kind is None:
                raise ContractError(f"Result.output type {tagged['type']!r} is unknown")
            raw["output"] = kind.from_dict(tagged["value"])
        return cls(**raw)


@contract
class Handoff(Contract):
    """The audit record of one hand-off. ``event`` is the audit action name.

    sent: nothing is known about the output yet. returned: schema valid and verified. rejected: schema invalid or
    verification failed. The audit write comes first; if it fails the hand-off does not happen.
    """

    event: lit(*HANDOFF_EVENTS)  # type: ignore[valid-type]
    brief_id: Str
    unit_id: Str
    role: lit(*ROLES)  # type: ignore[valid-type]
    model_id: Str
    prompt_checksum: Str
    skill_pins: Strs
    input_checksums: Strs
    trace_id: Str
    output_checksum: Str | None = None
    schema_valid: StrictBool | None = None
    verify_result: lit("pass", "fail", "not_run") | None = None  # type: ignore[valid-type]
    tokens_in: Count = 0
    tokens_out: Count = 0
    seconds: Annotated[float, Field(strict=True, ge=0, lt=1e9)] = 0.0
    tool_calls: Count = 0

    @model_validator(mode="after")
    def _state(self) -> Self:
        if self.event == HANDOFF_SENT:
            if any(v is not None for v in (self.output_checksum, self.schema_valid, self.verify_result)):
                raise ValueError("a sent hand-off has no output, schema verdict or verification yet")
        elif self.event == HANDOFF_RETURNED:
            if self.output_checksum is None or self.schema_valid is not True or self.verify_result != "pass":
                raise ValueError("a returned hand-off has an output that is schema-valid and verified")
        elif self.schema_valid is not False and self.verify_result != "fail":
            raise ValueError("a rejected hand-off records a schema or verification failure")
        return self

    @property
    def action(self) -> str:
        """The audit action name for ``audit.record``."""
        return self.event


__all__ = [
    "ARTIFACT_KINDS",
    "GATED_TOOLS",
    "HANDOFF_EVENTS",
    "HANDOFF_REJECTED",
    "HANDOFF_RETURNED",
    "HANDOFF_SENT",
    "Answer",
    "ArtifactRef",
    "Binding",
    "Brief",
    "Budget",
    "Contract",
    "ContractError",
    "Explanation",
    "Handoff",
    "JobUnit",
    "KeyDecisions",
    "MappingPlan",
    "Option",
    "Preview",
    "ProfileColumn",
    "Question",
    "RecordTypeCandidate",
    "Result",
    "SourceProfile",
    "Subject",
    "Summary",
    "Transform",
    "Unit",
    "WorkOrder",
    "canonical_json",
    "choose_recommended",
    "explain_error",
    "is_agent_principal",
]
