"""Upload template specs (read from the template files) and per-field coverage reports."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Any

import openpyxl

from worker import rulefiles
from worker.rule_target import DEDUP_REASONS, ID_MODES

_NAMES = ("Investors", "Commitments", "GL", "Affiliates", "Vendors", "Investments")
DEDUP_POLICIES = ("exclude", "keep")
# A reason a record may already be onboarded under, and what is done by default: left out. The reasons are the
# loader's; a target file's `dedup` block changes the action per reason.
_DEDUP_DEFAULT_ACTION = "exclude"


def _default_dedup() -> dict[str, str]:
    return dict.fromkeys(DEDUP_REASONS, _DEDUP_DEFAULT_ACTION)


@dataclass(frozen=True)
class TemplateSpec:
    name: str
    fields: tuple[str, ...]
    required: tuple[str, ...]
    output: str = "xlsx"  # "xlsx" or "csv": the file type the target system imports
    id_max_len: int | None = None  # longest allowed value of an *_ID field: the target file's max_length
    key: tuple[str, ...] = ()  # fields that together identify a record (one ID, or a composite)
    name_field: str = ""  # the record's display name, used to catch the same entity under another ID
    # How a row that is already onboarded is treated, per reason: "exclude" (leave it out) or "keep".
    dedup: Mapping[str, str] = field(default_factory=_default_dedup)
    # Names of the deterministic checks that run on staged rows (see worker.checks.CHECKS); empty = all.
    checks: tuple[str, ...] = ()
    # How the target ID is built: {"mode": "source"|"sequential"|"custom", "prefix", "width", "start", "pattern"}.
    id_generation: Mapping[str, Any] = field(default_factory=lambda: {"mode": "source"})
    # References that must already exist: ((field, record type), ...), e.g. (("Investor_ID", "Investors"),).
    depends: tuple[tuple[str, str], ...] = ()
    # Level of each check for this record type: {"id_too_long": "ERR", "gp_id_differs": "WARN", ...}.
    levels: Mapping[str, str] = field(default_factory=dict)
    description: str = ""


def templates_dir() -> Path:
    override = os.environ.get("WORKER_TEMPLATES_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "templates"


def _read_headers(path: Path) -> tuple[str, ...]:
    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        row = next(wb.worksheets[0].iter_rows(min_row=1, max_row=1, values_only=True))
    finally:
        wb.close()
    return tuple(str(v).strip() for v in row if v is not None and str(v).strip())


_SUFFIX = "_Upload_Template.xlsx"


def _dedup(name: str, given: Mapping[str, str] | None) -> dict[str, str]:
    policy = _default_dedup()
    for reason, action in (given or {}).items():
        if reason not in policy or action not in DEDUP_POLICIES:
            raise ValueError(f"template {name}: dedup {reason!r}: {action!r} is not one of {DEDUP_POLICIES}")
        policy[reason] = action
    return policy


def _id_generation(name: str, given: Mapping[str, Any] | None) -> dict[str, Any]:
    cfg: dict[str, Any] = {"mode": "source", **(given or {})}
    if cfg["mode"] not in ID_MODES:
        raise ValueError(f"template {name}: id_generation.mode must be one of {ID_MODES}")
    if cfg["mode"] == "custom" and not cfg.get("pattern"):
        raise ValueError(f"template {name}: id_generation.mode 'custom' needs a pattern")
    return cfg


def _levels(name: str, given: Mapping[str, str] | None) -> dict[str, str]:
    out = {}
    for code, level in (given or {}).items():
        if level not in ("ERR", "WARN"):
            raise ValueError(f"template {name}: level of {code!r} must be ERR or WARN, not {level!r}")
        out[code] = level
    return out


def _rules_dir() -> Path:
    """Where the target files are read from when the specs are built: the templates folder, or the bundled one when
    the folder holds no target file (a test's or a sponsor's folder with only workbooks in it)."""
    own = templates_dir()
    if any(own.glob(f"*{_TARGET_SUFFIX}")):
        return own
    return Path(__file__).resolve().parents[2] / "templates"


def _org_header(rules_dir: Path, name: str) -> tuple[str, ...]:
    path = rules_dir / f"{name}{_SUFFIX}"
    return _read_headers(path) if path.is_file() else ()


def load_templates(directory: Path | None = None) -> dict[str, TemplateSpec]:
    """Every `<Name>_Upload_Template.xlsx` in the templates folder is a target format the firm can choose.

    The six standard ones come first; a new target format is a new file, no code. The fields that identify a
    record, its name field, the required fields and the dependencies are the record type's `*.target.yaml`; a format
    with no target file has none of them (its first two fields are required).
    """
    base = directory or templates_dir()
    found = {p.name[: -len(_SUFFIX)]: p for p in base.glob(f"*{_SUFFIX}")}
    ordered = [n for n in _NAMES if n in found] + sorted(n for n in found if n not in _NAMES)
    rules_dir = _rules_dir()
    known = record_types(rules_dir)
    headers = _target_headers(rules_dir, known)
    specs: dict[str, TemplateSpec] = {}
    for name in ordered:
        fields = _read_headers(found[name])
        rules = _load_target(rules_dir, name, known, headers) if name in known else None
        required = tuple(rules.required) if rules is not None else fields[:2]
        if rules is not None and required == _org_header(rules_dir, name)[:2]:
            required = fields[:2]  # the target file says "the first two header fields": this template's own
        unknown = [f for f in required if f not in fields]
        if unknown:
            raise ValueError(f"template {name}: required fields not in the template header: {unknown}")
        specs[name] = TemplateSpec(
            name=name,
            fields=fields,
            required=required,
            output="xlsx",
            id_max_len=rules.max_length if rules is not None else None,
            key=tuple(rules.key) if rules is not None else (),
            name_field=rules.name_field if rules is not None else "",
            dedup=_dedup(name, None),
            checks=(),
            id_generation=_id_generation(name, None),
            depends=tuple(rules.depends) if rules is not None else (),
            levels=_levels(name, None),
            description="",
        )
    return specs


SPONSOR_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
SOURCE_ORG, SOURCE_SPONSOR, SOURCE_JOB = "organisation", "sponsor override", "job override"
_SETTINGS = (
    "fields", "required", "output", "id_max_len", "key", "name_field", "dedup", "checks", "id_generation",
    "description",
)


def sponsors_dir() -> Path:
    return templates_dir() / "sponsors"


def list_sponsors() -> list[str]:
    """Sponsor overlays: a folder under templates/sponsors/ holding that sponsor's own target formats."""
    base = sponsors_dir()
    return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []


def templates_for(sponsor: str = "") -> dict[str, TemplateSpec]:
    """The target formats a sponsor onboards into: the organisation defaults, overlaid by the sponsor's own files.

    A sponsor file with the same record type replaces the default one; a new name adds a format. Everything the
    sponsor does not define keeps the default. A sponsor never sees another sponsor's overlay. A sponsor without
    an overlay folder gets the defaults; a malformed name is refused.
    """
    if not sponsor:
        return dict(TEMPLATES)
    if not SPONSOR_RE.match(sponsor):
        raise ValueError(f"unknown sponsor {sponsor!r}: use lowercase letters, digits, '-' or '_'")
    merged = dict(TEMPLATES)
    if sponsor in list_sponsors():
        merged.update(load_templates(sponsors_dir() / sponsor))
    return merged


def standards_dir() -> Path:
    """The all-sponsor standards (Golden chart of accounts, transaction type mapping).

    A `WORKER_TEMPLATES_DIR` that has no `standards` folder falls back to the bundled one.
    """
    own = templates_dir() / "standards"
    return own if own.is_dir() else Path(__file__).resolve().parents[2] / "templates" / "standards"


def sponsor_slug(sponsor: str) -> str:
    """`Sponsor A` -> `sponsor-a`: the folder name templates, state and audits use for a sponsor."""
    slug = re.sub(r"[^a-z0-9]+", "-", sponsor.lower()).strip("-")
    if not SPONSOR_RE.match(slug):
        raise ValueError(f"cannot use {sponsor!r} as a sponsor name")
    return slug


def template_of(record_type: str) -> str:
    """The template a console record type is built from: both GL types use the one `GL` template."""
    from worker import record_facts

    return "GL" if record_facts.is_gl(record_type) else record_type


def spec_of(sponsor: str, record_type: str) -> TemplateSpec:
    """The sponsor's effective template for a console record type (`sponsor` is its name, `Sponsor A`)."""
    return templates_for(sponsor_slug(sponsor))[template_of(record_type)]


def file_stem(j: Mapping[str, Any]) -> str:
    """The file name (without extension) a console job's upload file has."""
    from worker import record_facts

    suffix = "" if j["scope"] == "common" else ("_" + str(len(j["ents"])) + "_entities") if j["scope"] == "batch" \
        else "_" + j["fund"].replace(" ", "_")
    return f"{record_facts.template_stem(j['type'])}_{j['sp'].replace(' ', '_')}{suffix}{'_LP' if j['type'] == 'GL LP' else ''}"


def file_name(j: Mapping[str, Any]) -> str:
    return f"{file_stem(j)}.{spec_of(j['sp'], j['type']).output}"


def template_file(sponsor: str, record_type: str) -> Path | None:
    """The template file a sponsor's job uses for a record type: its own overlay file, else the default."""
    name = f"{record_type}{_SUFFIX}"
    if sponsor and SPONSOR_RE.match(sponsor):
        own = sponsors_dir() / sponsor / name
        if own.is_file():
            return own
    default = templates_dir() / name
    return default if default.is_file() else None


def sponsor_levels(sponsor: str) -> dict[str, str]:
    """A sponsor's own check levels (`sponsors/<sponsor>/levels.json`); they override the organisation's."""
    if not sponsor or not SPONSOR_RE.match(sponsor):
        return {}
    path = sponsors_dir() / sponsor / "levels.json"
    if not path.is_file():
        return {}
    return _levels(f"sponsor {sponsor}", json.loads(path.read_text(encoding="utf-8")))


def spec_from_file(path: Path, base: TemplateSpec) -> TemplateSpec:
    """A template uploaded for one job: its header is the field list; every other setting comes from `base`."""
    fields = _read_headers(path)
    if not fields:
        raise ValueError(f"template file {path.name} has no header row")
    required = tuple(f for f in base.required if f in fields) or fields[:2]
    return replace(base, fields=fields, required=required)


def effective_settings(
    sponsor: str, record_type: str, job_template: Path | None = None
) -> dict[str, dict[str, Any]]:
    """Every setting of one record type with its value and where it comes from.

    Source is `organisation` (the default template), `sponsor override` (the sponsor's overlay changes it) or
    `job override` (a template uploaded for this job only). Job beats sponsor beats organisation.
    """
    if record_type not in TEMPLATES:
        raise ValueError(f"unknown record type {record_type!r}; known: {', '.join(TEMPLATES)}")
    org = TEMPLATES[record_type]
    spec = templates_for(sponsor).get(record_type, org)
    job = spec_from_file(job_template, spec) if job_template is not None else None
    out: dict[str, dict[str, Any]] = {}
    for name in _SETTINGS:
        value = getattr(spec, name)
        source = SOURCE_SPONSOR if value != getattr(org, name) else SOURCE_ORG
        if job is not None and getattr(job, name) != value:
            value, source = getattr(job, name), SOURCE_JOB
        out[name] = {"value": value, "source": source}
    return out


_TARGET_SUFFIX = ".target.yaml"


@dataclass(frozen=True)
class TargetRules:
    """The rules of one record type, read from `templates/<RecordType>.target.yaml` through rulefiles strict mode."""

    record_type: str
    template: str  # template file name, same folder
    output: str
    name_field: str
    required: tuple[str, ...]
    key: tuple[str, ...]
    max_length: int | None  # control: longest allowed value of an *_ID field
    id_generation: Mapping[str, Any]  # {"mode": ..., "prefix"?, "width"?, "start"?, "pattern"?}
    id_tokens: tuple[str, ...]  # the tokens a custom pattern may use here (empty: no custom pattern)
    namecode_length: int  # how many characters of a name the NAMECODE token takes (0: no NAMECODE)
    depends: tuple[tuple[str, str], ...]
    dedup: Mapping[str, str]
    checks: tuple[str, ...]
    levels: Mapping[str, str]
    description: str = ""
    membership: Mapping[str, Any] = field(default_factory=dict)  # the file's `membership` block, as written

    @property
    def id_prefix(self) -> str:
        return str(self.id_generation.get("prefix", ""))

    @property
    def id_pad(self) -> int:
        return int(self.id_generation.get("width", 0))


def record_types(base: Path | None = None) -> tuple[str, ...]:
    """The record types that have a target file: the six standard ones first, then any other, sorted."""
    base = base or templates_dir()
    found = {p.name[: -len(_TARGET_SUFFIX)] for p in base.glob(f"*{_TARGET_SUFFIX}")}
    return tuple([n for n in _NAMES if n in found] + sorted(found - set(_NAMES)))


def _target_headers(base: Path, known: tuple[str, ...]) -> dict[str, frozenset[str]]:
    return {n: frozenset(_read_headers(base / f"{n}{_SUFFIX}")) for n in known if (base / f"{n}{_SUFFIX}").is_file()}


def target(record_type: str) -> TargetRules:
    """The target rules of one record type. An unknown record type is refused; a malformed file raises SchemaError."""
    known = record_types()
    if record_type not in known:
        raise ValueError(f"unknown record type {record_type!r}; known: {', '.join(known)}")
    base = templates_dir()
    return _load_target(base, record_type, known, _target_headers(base, known))


def _load_target(base: Path, record_type: str, known: tuple[str, ...],
                 headers: Mapping[str, frozenset[str]]) -> TargetRules:
    ctx = rulefiles.Context(record_types=frozenset(known), target_fields=dict(headers))
    loaded = rulefiles.load_file(base / f"{record_type}{_TARGET_SUFFIX}", context=ctx)
    d, c = loaded.data, loaded.data["controls"]
    fmt = c.get("id_formats") or {}
    return TargetRules(
        record_type=record_type,
        template=str(d["template"]),
        output=str(d["output"]),
        name_field=str(d.get("name_field", "")),
        required=tuple(c["required"]),
        key=tuple(c["key"]),
        max_length=fmt.get("max_length"),
        id_generation={"mode": "source", **(fmt.get("generation") or {})},
        id_tokens=tuple(fmt.get("tokens") or ()),
        namecode_length=int(fmt.get("namecode_length") or 0),
        depends=tuple((str(x["field"]), str(x["record_type"])) for x in c.get("depends") or ()),
        dedup={**_default_dedup(), **(c.get("dedup") or {})},
        checks=tuple(c.get("checks") or ()),
        levels=dict(c.get("levels") or {}),
        description=str(d.get("description", "")),
        membership=dict(loaded.controls.get("membership") or {}),
    )


TEMPLATES: dict[str, TemplateSpec] = load_templates()


@dataclass(frozen=True)
class Membership:
    """The sample world's lists, read from the `membership` blocks of the target files."""

    types: tuple[str, ...]  # every listed name, in onboarding order
    static: tuple[str, ...]  # the names that are static records
    gen: tuple[str, ...]  # always get a new ID from the sponsor counter
    idgen: Mapping[str, str]  # name -> ID field generated when blank
    uniq: tuple[str, ...]  # unique for the sponsor, whichever entity they arrive with
    regt: tuple[str, ...]  # held by the existing-records store
    next: Mapping[str, str]  # name -> the name that follows it
    record: Mapping[str, str]  # name -> the record type (target file) it is built from


def membership() -> Membership:
    """Read every target file's `membership` and join them. The order is the chain of `next`: the first name is the
    one nobody follows. A name listed twice, a `next` that names no listed name, a chain that does not reach every
    name, or a file with no block raises ValueError."""
    owner: dict[str, str] = {}
    blocks: dict[str, Mapping[str, Any]] = {}
    for rt in record_types():
        m = target(rt).membership
        if not m:
            raise ValueError(f"target file {rt} has no membership block")
        blocks[rt] = m
        for name in m["types"]:
            if name in owner:
                raise ValueError(f"{name!r} is listed by both {owner[name]} and {rt}")
            owner[name] = rt
    follow: dict[str, str] = {}
    for rt, m in blocks.items():
        names = list(m["types"])
        follow.update(pairwise(names))
        if m.get("next") is not None:
            if m["next"] not in owner:
                raise ValueError(f"{rt}: next {m['next']!r} is not a listed name")
            follow[names[-1]] = m["next"]
    heads = [n for n in owner if n not in follow.values()]
    if len(heads) != 1:
        raise ValueError(f"the onboarding order has {len(heads)} starting names: {heads}")
    order = [heads[0]]
    while order[-1] in follow:
        if follow[order[-1]] in order:
            raise ValueError(f"the onboarding order loops at {follow[order[-1]]!r}")
        order.append(follow[order[-1]])
    if set(order) != set(owner):
        raise ValueError(f"names outside the onboarding order: {sorted(set(owner) - set(order))}")

    def flagged(flag: str) -> tuple[str, ...]:
        return tuple(n for n in order if blocks[owner[n]].get(flag))

    return Membership(
        types=tuple(order), static=flagged("static"), gen=flagged("new_id_always"),
        idgen={n: str(blocks[owner[n]]["id_when_blank"]) for n in order if blocks[owner[n]].get("id_when_blank")},
        uniq=flagged("unique_per_sponsor"), regt=flagged("existing_store"), next=dict(follow), record=dict(owner),
    )


class Status(StrEnum):
    MATCHED = "MATCHED"
    CHECK_VALUES = "CHECK_VALUES"
    ASSUMED = "ASSUMED"
    MISSING = "MISSING"


@dataclass(frozen=True)
class FieldCoverage:
    status: Status
    source: str | None = None
    note: str | None = None
    filled: int = 0
    total: int = 0


@dataclass(frozen=True)
class CoverageReport:
    template: str
    fields: dict[str, FieldCoverage] = field(default_factory=dict)
    rows: int = 0
    required: tuple[str, ...] = ()

    def missing(self) -> list[str]:
        return [n for n, c in self.fields.items() if c.status is Status.MISSING]

    def missing_required(self) -> list[str]:
        required = self.required or TEMPLATES[self.template].required
        return [n for n in self.missing() if n in required]

    def to_markdown(self) -> str:
        lines = [f"# Coverage: {self.template} ({self.rows} rows)", ""]
        for name in self.missing():
            lines.append(f"- **{name.upper()}**: MISSING")
        for name, cov in self.fields.items():
            if cov.status is Status.MISSING:
                continue
            parts = [f"- {name}: {cov.status.value}"]
            if cov.source:
                parts.append(f"from {cov.source}")
            if cov.filled < cov.total:
                parts.append(f"filled {cov.filled}/{cov.total}")
            text = " ".join(parts)
            if cov.note:
                text += f" ({cov.note})"
            lines.append(text)
        gaps = self.missing_required()
        verdict = "yes" if not gaps else "no"
        suffix = f" (missing required: {', '.join(gaps)})" if gaps else ""
        lines += ["", f"can build: {verdict}{suffix}"]
        return "\n".join(lines)


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def build_coverage(
    template: str,
    staged: Mapping[str, list],
    sources: Mapping[str, str],
    assumed: Mapping[str, str],
    value_checks: Mapping[str, str],
    specs: Mapping[str, TemplateSpec] | None = None,
) -> CoverageReport:
    spec = (specs or TEMPLATES)[template]
    rows = max((len(v) for v in staged.values()), default=0)
    fields: dict[str, FieldCoverage] = {}
    for name in spec.fields:
        values = staged.get(name, [])
        filled = sum(1 for v in values if not _is_blank(v))
        has_source = name in sources
        if not has_source and name not in assumed:
            fields[name] = FieldCoverage(Status.MISSING, None, None, 0, rows)
        elif not has_source:
            fields[name] = FieldCoverage(Status.ASSUMED, None, assumed[name], filled, rows)
        elif name in value_checks:
            fields[name] = FieldCoverage(Status.CHECK_VALUES, sources[name], value_checks[name], filled, rows)
        else:
            fields[name] = FieldCoverage(Status.MATCHED, sources[name], None, filled, rows)
    return CoverageReport(template=template, fields=fields, rows=rows, required=spec.required)
