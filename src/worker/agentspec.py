"""The agent adapter's side of the one engine (migration M18, topology T4).

The Deep Agent builds a staged table with tools; the engine is driven by a `JobSpec` (sources, a recipe of closed
steps, the human's choices). This module is the only translation between the two on the agent path, and it only
copies: `agent_to_spec(ctx)` reads what the tools recorded (the spec chain of a staged table, the check the
person's file went through, the account picks a person confirmed at a gate) and writes the same thing as a
`JobSpec`. It decides nothing about a row.

* `agent_to_spec` raises `NotTranslatable` for a chain the closed vocabulary cannot say (a conflict decision, a
  value supplied only to the check, steps out of the engine's order); the caller says so on the record and keeps
  today's writer for that one upload.
* `agent_ports` wires the engine's ports over the run context's own stores. The engine's own record (`eval.started`,
  `stage.done`, `write_upload` with the tie-out, ...) is its own hash-chained log, `<run>/engine/engine_audit.jsonl`
  (`engine_audit`), written before anything it records happens. The run's audit log keeps exactly the entries it had
  before the switch: the golden lock reads that sequence, byte for byte.
* `AgentWorld` defers the engine's commit: the counter, the existing-records store and the saved workflow move
  after the tool's own `write_upload` record is written, as they do today.
* `released_by` names the person whose gate decision released the write, or raises: both writers ask it first.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from worker import persist
from worker.audit import AuditLog
from worker.context import RunContext
from worker.engine import Choice, JobSpec, PinSet, Recipe, SourceRef
from worker.hashing import sha256_file
from worker.jobstate import FILE as STATE_FILE
from worker.ports import FileReader, FileStore, Ports, ProductionSkills, StoreWorld

_STAGE_RANK = {"read": 0, "map": 1, "convert": 2}
_READ_STEPS = {"lift", "lift_blocks", "flatten_compact", "lift_gl_lines", "join", "distinct"}
_MAP_STEPS = {"map_accounts", "resolve_references"}


class NotTranslatable(Exception):
    """The staged table was built in a way the engine's closed steps do not say. Nothing is guessed."""


# ---- who approved ----------------------------------------------------------------------------------------
def person(name: Any) -> str:
    """The name the engine's approval carries. `local` is the web's unauthenticated default reviewer (demo-grade
    authentication, ticket 07): a person at the keyboard, recorded as `local reviewer` because the bare word is a
    reserved system name. A blank is left blank and agent and reserved names are left alone: whoever reads the
    name refuses them, there is no default person."""
    who = " ".join(str(name or "").split())
    return "local reviewer" if who.lower() == "local" else who


def _gate_reviewer(entry: Mapping[str, Any]) -> str:
    return person((entry.get("inputs") or {}).get("reviewer"))


class UnapprovedWrite(ValueError):
    """The run's record holds no approval by a person that releases this write."""


def released_by(entries: Sequence[Mapping[str, Any]]) -> str:
    """The person whose latest gate decision (`gate.approve` / `gate.edit`) released the write; none, a rejection or
    a non-person raises `UnapprovedWrite`."""
    from worker.contracts import is_agent_principal
    from worker.improver import reserved_name

    for e in reversed(entries):
        action = e.get("action")
        if action == "gate.reject":
            raise UnapprovedWrite("no_approval: the last gate decision was a rejection; nothing is written.")
        if action in ("gate.approve", "gate.edit"):
            by = _gate_reviewer(e)
            if is_agent_principal(by) or reserved_name(by):
                raise UnapprovedWrite(f"agent_approver: {by!r} is not a person; only a named person approves an "
                                      "upload.")
            return by
    raise UnapprovedWrite("no_approval: writing the upload needs an approval from a person; the run's record holds "
                          "no gate decision.")


def _confirmations(entries: Sequence[Mapping[str, Any]]) -> dict[str, tuple[str, str]]:
    """account key -> (reviewer, reason) of its latest `account.confirmed` record: the person who decided it is
    the reviewer of the gate decision just before it."""
    out: dict[str, tuple[str, str]] = {}
    who = ""
    for e in entries:
        action = e.get("action")
        if action in ("gate.approve", "gate.edit"):
            who = _gate_reviewer(e)
        elif action == "account.confirmed":
            key = str((e.get("inputs") or {}).get("key", ""))
            out[key] = (who, str(e.get("rationale") or "").strip() or "confirmed at the gate")
    return out


# ---- the job's name and its saved state --------------------------------------------------------------------
def job_name(ctx: RunContext) -> str:
    return ctx.out_dir.parent.name if ctx.out_dir.name == "out" else ctx.out_dir.name


def engine_store_dir(ctx: RunContext) -> Path:
    base = ctx.out_dir.parent if ctx.out_dir.name == "out" else ctx.out_dir
    return base / "engine"


def _saved(ctx: RunContext) -> dict[str, Any]:
    path = Path(ctx.out_dir) / STATE_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


# ---- the chain -----------------------------------------------------------------------------------------
@dataclass
class _Plan:
    ctx: RunContext
    sources: list[SourceRef]
    paths: list[Path]
    steps: list[dict[str, Any]]
    n_read: int = 0
    rank: int = 0
    default_gp: str = ""
    template: str = ""

    def ref(self, path: str, sheet: str | None, header_row: int | None) -> int:
        p = Path(path)
        roots = [Path(r).resolve() for r in self.ctx.inbox_roots]
        name = str(p)
        for root in roots:
            try:
                name = p.resolve().relative_to(root).as_posix()
                break
            except ValueError:
                continue
        ref = SourceRef("file", name, sheet, header_row, sha256_file(p) if p.is_file() else "")
        if ref in self.sources:
            return self.sources.index(ref)
        self.sources.append(ref)
        self.paths.append(p.resolve())
        return len(self.sources) - 1

    def add(self, step: dict[str, Any]) -> None:
        kind = step["step"]
        stage = "read" if kind in _READ_STEPS else "map" if kind in _MAP_STEPS else "convert"
        rank = _STAGE_RANK[stage]
        if rank < self.rank:
            raise NotTranslatable(f"{kind} comes after a {('read', 'map', 'convert')[self.rank]}-stage step; the "
                                  "engine runs its stages in the order read, map, convert")
        self.rank = rank
        if stage == "read":
            self.n_read += 1
        self.steps.append(step)


def _first_sheet_name(path: Path) -> str | None:
    from worker.staging import _first_sheet

    try:
        return _first_sheet(path)
    except Exception:  # noqa: BLE001  a file that cannot be opened is the engine's refusal to make
        return None


def _emit(node: Mapping[str, Any], plan: _Plan) -> int:
    """Append the steps that build `node`, in the order they ran; return the read-step index of its table."""
    kind = node.get("kind")
    if kind == "lift":
        idx = plan.ref(node["path"], node["sheet"], node.get("header_row"))
        step: dict[str, Any] = {"step": "lift", "source": idx, "columns": dict(node["columns"])}
        if node.get("header_row") is not None:
            step["header_row"] = node["header_row"]
        step["first_row"] = node["first_row"]
        if node.get("last_row") is not None:
            step["last_row"] = node["last_row"]
        if node.get("constants"):
            step["constants"] = dict(node["constants"])
        plan.add(step)
    elif kind == "lift_blocks":
        idx = plan.ref(node["path"], node["sheet"], None)
        step = {"step": "lift_blocks", "source": idx, "columns": dict(node["columns"]),
                "blocks": [list(b) for b in node["blocks"]]}
        if node.get("constants"):
            step["constants"] = dict(node["constants"])
        plan.add(step)
    elif kind == "flatten_compact":
        idx = plan.ref(node["path"], node["sheet"], None)
        step = {"step": "flatten_compact", "source": idx, "levels": dict(node["levels"]),
                "first_row": node["first_row"]}
        if node.get("last_row") is not None:
            step["last_row"] = node["last_row"]
        if node.get("column", "A") != "A":
            step["column"] = node["column"]
        plan.add(step)
    elif kind == "gl_lines":
        path = Path(node["path"])
        first = _first_sheet_name(path)
        sheet = None if node.get("sheet") in (None, "", first) else str(node["sheet"])
        idx = plan.ref(node["path"], sheet, None)
        step = {"step": "lift_gl_lines", "source": idx, "fund_id": node.get("fund_id", ""),
                "fund_name": node.get("fund_name", ""), "offsets": bool(node.get("offsets", False))}
        if node.get("columns"):
            step["columns"] = dict(node["columns"])
        if int(node.get("header_row") or 1) != 1:
            step["header_row"] = int(node["header_row"])
        if node.get("gp_id") and node["gp_id"] != plan.default_gp:
            step["gp_id"] = node["gp_id"]
        plan.add(step)
    elif kind == "join":
        left = _emit_read(node["left"], plan)
        right = _emit_read(node["right"], plan)
        other = node.get("right_template")
        if other and other != plan.template:  # the engine lifts every leaf as the job's record type
            raise NotTranslatable(f"a join of a {other} table into a {plan.template} table")
        plan.add({"step": "join", "left": f"t{left}", "right": f"t{right}", "left_key": node["left_key"],
                  "right_key": node["right_key"], "take": list(node["take"]), "how": node.get("how", "left")})
    elif kind == "distinct":
        _emit_read(node["of"], plan)
        plan.add({"step": "distinct", "key_fields": list(node["key_fields"])})
    elif kind == "set_by_key":
        _emit(node["of"], plan)
        via = node.get("via")
        args = dict(node.get("args") or {})
        if via == "map_accounts":
            if any(s["step"] == "map_accounts" for s in plan.steps):
                raise NotTranslatable("two map_accounts steps")
            defaults = {"account_field": "Account_Name", "type_field": "Transaction_Type",
                        "target_field": "Account_Number", "account_type_field": ""}
            plan.add({"step": "map_accounts", **{k: v for k, v in args.items() if defaults.get(k) != v}})
        elif via == "resolve_references":
            plan.add({"step": "resolve_references", "field": args["field"], "record_type": args["record_type"]})
        elif via == "confirm_account_mappings":
            if not any(s["step"] == "map_accounts" for s in plan.steps):
                raise NotTranslatable("accounts were confirmed on a table that was not mapped")
            if plan.rank > _STAGE_RANK["map"]:  # the engine applies the picks with the map stage
                raise NotTranslatable("accounts were confirmed after a convert-stage step ran")
        else:
            raise NotTranslatable(f"set_by_key via {via!r}")
    elif kind == "edit_rows":
        _emit(node["of"], plan)
        if node.get("via") != "match_existing":
            raise NotTranslatable(f"row edits via {node.get('via')!r} are the person's decisions on single rows; "
                                  "the closed steps do not carry them")
        plan.add({"step": "match_existing"})
    elif kind == "assign_ids":
        _emit(node["of"], plan)
        step = {"step": "assign_ids", "id_field": node["field"]}
        if node.get("source_key_field"):
            step["source_key_field"] = node["source_key_field"]
        plan.add(step)
    else:
        raise NotTranslatable(f"a staged table built by {kind!r}")
    return plan.n_read - 1


def _emit_read(node: Mapping[str, Any], plan: _Plan) -> int:
    if node.get("kind") not in ("lift", "lift_blocks", "flatten_compact", "join", "distinct"):
        raise NotTranslatable(f"a join over a table built by {node.get('kind')!r}")
    return _emit(node, plan)


def _checks(plan: _Plan, ctx: RunContext, meta: Mapping[str, Any]) -> None:
    """The data-quality check the stage's source file went through, as the check step of the recipe."""
    source = meta.get("source_file")
    if not source:
        return
    report = ctx.quality_reports.get(str(source))
    call = (report or {}).get("_call")
    if not call:
        return
    path = Path(str(source)).resolve()
    idx = next((i for i, p in enumerate(plan.paths) if p == path), None)
    if idx is None:
        raise NotTranslatable("the check ran on a file the staged table was not read from")
    ref = plan.sources[idx]
    step: dict[str, Any] = {"step": "check_data_quality", "source": idx, "record_type": call["record_type"],
                            "columns": dict(call["columns"])}
    if call.get("sheet") and call["sheet"] != ref.sheet:
        step["sheet"] = call["sheet"]
    if int(call.get("header_row") or 1) != int(ref.header_row or 1):
        step["header_row"] = int(call["header_row"])
    if call.get("generated"):
        step["generated"] = list(call["generated"])
    plan.steps.append(step)


def agent_to_spec(
    ctx: RunContext,
    stage_id: str = "",
    *,
    template: str = "",
    drop_duplicates: bool = False,
    duplicate_key: Sequence[str] | None = None,
    state: Any = None,
    entries: Sequence[Mapping[str, Any]] | None = None,
) -> JobSpec:
    """The `JobSpec` the engine evaluates for a staged table of this run. Pure copy of what the tools recorded.

    `stage_id` defaults to the latest staged table of the run. `state` is the tools' live `JobState`; without it
    the run's saved `job_state.json` is read. Raises `NotTranslatable` when the table was built in a way the
    closed steps cannot say.
    """
    saved = _saved(ctx)
    specs: Mapping[str, Any] = state.specs if state is not None else saved.get("specs", {})
    supplied = state.supplied if state is not None else None
    if supplied is not None:
        if supplied.rows or supplied.all:
            raise NotTranslatable("values were supplied to the check only; the engine fills a cell or checks it, "
                                  "not both")
    elif (saved.get("supplied") or {}).get("rows") or (saved.get("supplied") or {}).get("all"):
        raise NotTranslatable("values were supplied to the check only")
    if not stage_id:
        ids = sorted(specs, key=lambda s: int(s[1:]) if s[1:].isdigit() else 0)
        if not ids:
            raise NotTranslatable("no staged table was built by tools in this run")
        stage_id = ids[-1]
    node = specs.get(stage_id)
    if node is None:
        raise NotTranslatable(f"stage {stage_id} has no recorded build; it was not made by this run's tools")
    meta = ctx.stage.meta(stage_id)
    template = template or str(meta["template"])
    plan = _Plan(ctx, [], [], [], default_gp=ctx.gp_id, template=template)
    _emit(node, plan)
    if not plan.steps or not any(s["step"] in _READ_STEPS for s in plan.steps):
        raise NotTranslatable("the table has no step that reads a source")
    _checks(plan, ctx, meta)
    if drop_duplicates:
        plan.steps.append({"step": "write", "drop_duplicates": True, "duplicate_key": list(duplicate_key or [])})
    choices = _account_choices(ctx, meta, entries)
    return JobSpec(job_name(ctx), ctx.sponsor, template, None, tuple(plan.sources), PinSet(),
                   Recipe.of(*plan.steps), tuple(choices), {"gp_id": ctx.gp_id})


def _account_choices(ctx: RunContext, meta: Mapping[str, Any], entries: Sequence[Mapping[str, Any]] | None) -> list[Choice]:
    info = meta.get("account_mapping") or {}
    picks = [x for x in info.get("keys", []) if x.get("decided_by") == "human"]
    if not picks:
        return []
    seen = list(ctx.audit.read()) if entries is None else list(entries)
    who = _confirmations(seen)
    out: list[Choice] = []
    for x in picks:
        by, reason = who.get(str(x["key"]), ("", "confirmed at the gate"))
        if not by:
            raise UnapprovedWrite(f"no_approval: account key {x['key']!r} was confirmed with no named person's gate "
                                  "decision before it; nothing is guessed.")
        out.append(Choice("map_account", f"key:{x['key']}", str(x["account"]), "job", by, reason))
    return sorted(out, key=lambda c: c.subject)


# ---- a unit named by a file, not by a staged table -----------------------------------------------------------
GL_TYPES = ("GL", "GL Fund", "GL LP")


def file_to_spec(
    ctx: RunContext,
    path: Path,
    record_type: str,
    *,
    sheet: str = "",
    header_row: int = 1,
    first_row: int = 0,
    columns: Mapping[str, str] | None = None,
    fund_id: str = "",
    fund_name: str = "",
    offsets: bool = False,
    constants: Mapping[str, str] | None = None,
) -> JobSpec:
    """The `JobSpec` of one unit named by a file: what `run_job` evaluates when no table was staged. A GL file is
    read as lines, its accounts mapped by the skill, and checked through `columns` (Account, Deal, Journal,
    Debit, Credit as the file's header text). Any other type is lifted through `columns` ({field: column letter})
    with GP_ID the run's, then checked as the template says."""
    plan = _Plan(ctx, [], [], [], default_gp=ctx.gp_id)
    template = "GL" if record_type in GL_TYPES else record_type
    if template not in ctx.stage.templates:
        raise ValueError(f"unknown record type {record_type!r}; known: {', '.join(sorted(ctx.stage.templates))}")
    cols = {str(k): str(v) for k, v in (columns or {}).items()}
    if template == "GL":
        idx = plan.ref(str(path), sheet or None, None)
        step: dict[str, Any] = {"step": "lift_gl_lines", "source": idx, "fund_id": fund_id, "fund_name": fund_name,
                                "offsets": bool(offsets or record_type == "GL LP")}
        if header_row != 1:
            step["header_row"] = header_row
        plan.add(step)
        plan.add({"step": "map_accounts"})
        if cols:
            plan.steps.append({"step": "check_data_quality", "source": idx, "record_type": "GL", "columns": cols})
    else:
        if not cols:
            raise ValueError("columns ({field: column letter}) say where each field comes from")
        idx = plan.ref(str(path), sheet or None, header_row)
        lift: dict[str, Any] = {"step": "lift", "source": idx, "columns": cols, "header_row": header_row,
                                "first_row": first_row or header_row + 1}
        consts = {"GP_ID": ctx.gp_id, **dict(constants or {})}
        lift["constants"] = consts
        plan.add(lift)
    return JobSpec(job_name(ctx), ctx.sponsor, template, None, tuple(plan.sources), PinSet(),
                   Recipe.of(*plan.steps), (), {"gp_id": ctx.gp_id})


# ---- the engine's ports over this run's stores ---------------------------------------------------------------
class AgentWorld(StoreWorld):
    """The run's stores. `commit` only remembers what the engine would move: the tool moves the ID counter, the
    existing-records store and the saved workflow after its own `write_upload` record is written."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.deferred: list[dict[str, Any]] = []

    def commit(self, job_id: str, issued: Sequence[str], records: Sequence[Mapping[str, Any]], **kw: Any) -> None:
        self.deferred.append({"job_id": job_id, "issued": list(issued), "records": len(records), **kw})


def engine_audit(ctx: RunContext) -> AuditLog:
    """The engine's own log for this run: hash-chained and append-only like the run's, next to its store."""
    return persist.audit(engine_store_dir(ctx) / "engine_audit.jsonl")


def agent_ports(ctx: RunContext) -> Ports:
    world = AgentWorld(ctx.sponsor, registry=ctx.registry, id_root=ctx.id_root, accounts_root=ctx.accounts_root,
                       templates=ctx.stage.templates,
                       recipes_root=ctx.id_root.parent / "sponsors" if ctx.id_root is not None else None)
    return Ports(world=world, reader=FileReader(ctx.inbox_roots), audit=engine_audit(ctx),
                 store=FileStore(engine_store_dir(ctx)), out_dir=Path(ctx.out_dir),
                 skills=ProductionSkills(ctx.sponsor, accounts_root=ctx.accounts_root), ranker=None, max_retries=0)


def same_rows(staged: Any, table: Sequence[Mapping[str, Any]], ignore: Collection[str] = ()) -> str:
    """"" when the engine's table is the staged table row for row (minus `ignore`d recipe fields), else the first
    difference in plain words."""
    mine = iter(staged)
    for n, row in enumerate(table):
        got = next(mine, None)
        if got is None:
            return f"the engine has {n} row(s), the staged table {len(table)}"
        left = {k: v for k, v in got.items() if k not in ignore}
        right = {k: v for k, v in row.items() if k not in ignore}
        if left != right:
            diff = sorted(k for k in {*left, *right} if left.get(k) != right.get(k))
            return f"row {n + 1} differs in {', '.join(diff[:5])}"
    if next(mine, None) is not None:
        return f"the engine has more rows than the staged table's {len(table)}"
    return ""
