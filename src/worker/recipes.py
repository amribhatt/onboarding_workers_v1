"""Saved workflows: a recipe per sponsor and record type, with one entry per source variant.

A recipe is what a human already approved, kept as data so it can be run again by code, with no model:

    <root>/<sponsor>/<RecordType>/v<N>.json   (machine) and v<N>.md (human-readable), one new version per change

Each variant holds the deterministic mapping (the spec chain `worker.memory` also replays), the header text of
every source column it reads (the layout), the constants, and the steps that follow the mapping (check against
existing records, issue IDs). A new file is matched against the variants: `exact` (same sheet, header row and
header text at the same columns), `close` (the headers are there but moved or renamed: a proposal with a diff
to confirm) or `none`. Human decisions about single rows (excluded, corrected) are never part of a recipe.

The recipe file v-next (`FieldRecipes`, below) holds one ticket-02 field step per target field, written only from a
human's field decision, audited before the file, undone by appending (never deleting).
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from worker import workbook as wb
from worker.audit import AuditLog
from worker.fsutil import atomic_write_text
from worker.staging import _col, _norm

_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,59}$")
_LEAF_KINDS = ("lift", "lift_blocks", "flatten_compact", "gl_lines")
CLOSE_CUTOFF = 0.8


def _letter(index: int) -> str:
    out = ""
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def split_spec(spec: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(mapping spec, steps after it). Row-level human decisions are dropped; checks and ID issue are kept as steps."""
    post: list[dict[str, Any]] = []
    node = dict(spec)
    while node["kind"] in ("assign_ids", "edit_rows", "set_by_key"):
        if node["kind"] == "set_by_key":
            if node.get("via") == "map_accounts":
                post.insert(0, {"step": "map_accounts", **node["args"]})
            elif node.get("via") == "resolve_references":
                post.insert(0, {"step": "resolve_references", **node["args"]})
        elif node["kind"] == "assign_ids":
            post.insert(0, {"step": "assign_ids", "field": node["field"],
                            "source_key_field": node.get("source_key_field")})
        elif node.get("via") == "match_existing":
            post.insert(0, {"step": "match_existing"})
        node = dict(node["of"])
    return node, post


def leaves(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The file-reading nodes of a spec chain, left first (mutable references into `spec`)."""
    kind = spec["kind"]
    if kind in _LEAF_KINDS:
        return [spec]  # type: ignore[list-item]
    if kind == "join":
        return leaves(spec["left"]) + leaves(spec["right"])
    return leaves(spec["of"])


def _header_texts(path: str | Path, leaf: Mapping[str, Any]) -> dict[str, str]:
    """{field: header text} of the columns a leaf reads, taken from its (first) header row."""
    if leaf["kind"] == "flatten_compact":
        return {}
    if leaf["kind"] == "gl_lines":  # the columns are named by header text, so the header row is the evidence
        from worker.quality import Source

        found = set(Source(path, leaf.get("sheet") or None, int(leaf["header_row"])).headers)
        return {part: (name if name in found else "") for part, name in leaf["columns"].items()}
    sh = wb._sheet(path, leaf["sheet"])
    header_row = leaf["header_row"] if leaf["kind"] == "lift" else (leaf["blocks"][0][0] if leaf["blocks"] else None)
    if not header_row:
        return {}
    out = {}
    for fld, letter in leaf["columns"].items():
        cell = sh.get(int(header_row), _col(letter))
        out[fld] = "" if cell is None or cell[0] is None else str(cell[0]).strip()
    return out


def describe(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Layout of every leaf: file basename, sheet, header row, columns and the header text found there."""
    out = []
    for leaf in leaves(spec):
        out.append(
            {
                "file": Path(leaf["path"]).name,
                "kind": leaf["kind"],
                "sheet": leaf["sheet"],
                "header_row": leaf.get("header_row"),
                "columns": dict(leaf.get("columns") or leaf.get("levels") or {}),
                "headers": _header_texts(leaf["path"], leaf),
            }
        )
    return out


def portable(spec: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of `spec` whose file-reading nodes name their file without its folder."""
    out = copy.deepcopy(dict(spec))
    for leaf in leaves(out):
        leaf["path"] = Path(leaf["path"]).name
    return out


def bind(spec: Mapping[str, Any], paths: Mapping[str, str], mapping: Mapping[str, str] | None = None,
         constants: Mapping[str, str] | None = None) -> dict[str, Any]:
    """A copy of `spec` reading the files in `paths` ({stored file name: path}), with column letters and constants
    overridden where given. A file name with no path is an error: nothing is guessed."""
    bound = copy.deepcopy(dict(spec))
    for leaf in leaves(bound):
        name = Path(leaf["path"]).name
        if name not in paths:
            raise ValueError(f"this recipe also reads {name!r}; give its path in `paths`")
        leaf["path"] = str(paths[name])
        if mapping and leaf["kind"] in ("lift", "lift_blocks"):
            for fld, letter in mapping.items():
                if fld in leaf["columns"]:
                    leaf["columns"][fld] = letter
        if constants is not None and "constants" in leaf:
            leaf["constants"] = {**leaf["constants"], **constants}
    return bound


def match_layout(stored: Sequence[Mapping[str, Any]], leaves_now: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Compare stored layouts (`describe`) with the same leaves read from a new file.

    `leaves_now[i]` is {"sheet", "header_row_texts": {letter: text}} for the new file's leaf i.
    Returns {"match": "exact" | "close" | "none", "mapping": {field: letter}, "diff": [...], "missing": [...]}.
    """
    mapping: dict[str, str] = {}
    diff: list[dict[str, Any]] = []
    missing: list[str] = []
    total = same = 0
    for old, now in zip(stored, leaves_now, strict=False):
        by_text = {_norm(t): l for l, t in now["header_row_texts"].items() if str(t).strip()}
        for fld, text in old["headers"].items():
            total += 1
            letter = old["columns"][fld]
            if not text:
                missing.append(fld)
                continue
            key = _norm(text)
            if now["sheet"] == old["sheet"] and now["header_row_texts"].get(letter, "") and \
                    _norm(now["header_row_texts"][letter]) == key:
                same += 1
                mapping[fld] = letter
                continue
            if key in by_text:
                mapping[fld] = by_text[key]
                diff.append({"field": fld, "old_header": text, "new_header": text, "old_column": letter,
                             "new_column": by_text[key], "how": "moved"})
                continue
            close = difflib.get_close_matches(key, list(by_text), n=1, cutoff=CLOSE_CUTOFF)
            if close:
                mapping[fld] = by_text[close[0]]
                diff.append({"field": fld, "old_header": text, "new_header": now["header_row_texts"][by_text[close[0]]],
                             "old_column": letter, "new_column": by_text[close[0]], "how": "renamed"})
                continue
            missing.append(fld)
    if total and same == total and not missing:
        return {"match": "exact", "mapping": mapping, "diff": [], "missing": []}
    found = total - len(missing)
    if total and found / total >= 0.5:
        return {"match": "close", "mapping": mapping, "diff": diff, "missing": missing}
    return {"match": "none", "mapping": {}, "diff": [], "missing": missing}


def header_row_texts(path: str | Path, leaf: Mapping[str, Any]) -> dict[str, Any]:
    """The new file's header row for one stored leaf: {"sheet", "header_row_texts": {letter: text}}."""
    sheets = wb.list_sheets(path)["sheets"]
    name = leaf["sheet"] if any(s["name"] == leaf["sheet"] for s in sheets) else sheets[0]["name"]
    sh = wb._sheet(path, name)
    row = int(leaf.get("header_row") or 1)
    texts = {}
    for col in range(1, sh.max_column + 1):
        cell = sh.get(row, col)
        if cell is not None and cell[0] is not None and str(cell[0]).strip():
            texts[_letter(col)] = str(cell[0]).strip()
    return {"sheet": name, "header_row_texts": texts}


class RecipeStore:
    def __init__(self, root: str | Path, sponsor: str) -> None:
        if not _SAFE.match(sponsor):
            raise ValueError(f"bad sponsor name {sponsor!r}")
        self.sponsor = sponsor
        self.root = Path(root) / sponsor

    def _dir(self, record_type: str) -> Path:
        if not _SAFE.match(record_type):
            raise ValueError(f"bad record type {record_type!r}")
        return self.root / record_type

    def versions(self, record_type: str) -> list[int]:
        d = self._dir(record_type)
        if not d.is_dir():
            return []
        return sorted(int(m.group(1)) for p in d.glob("v*.json") if (m := re.fullmatch(r"v(\d+)\.json", p.name)))

    def load(self, record_type: str, version: int | None = None) -> dict[str, Any] | None:
        have = self.versions(record_type)
        if not have:
            return None
        n = version or have[-1]
        return json.loads((self._dir(record_type) / f"v{n}.json").read_text(encoding="utf-8"))

    def variants(self, record_type: str) -> list[dict[str, Any]]:
        recipe = self.load(record_type)
        return list(recipe["variants"]) if recipe else []

    def save_variant(
        self, record_type: str, instruction: str, variant: dict[str, Any]
    ) -> tuple[int, dict[str, Any], bool]:
        """Add `variant` unless an identical one is stored. Returns (version, stored variant, was_new)."""
        current = self.load(record_type)
        existing = list(current["variants"]) if current else []
        for v in existing:
            if v["spec"] == variant["spec"] and v["post"] == variant["post"] and v["layout"] == variant["layout"]:
                return current["version"], v, False  # type: ignore[index]
        n = len(existing) + 1
        variant = {**variant, "variant_id": f"var{n}", "created_at": datetime.now(UTC).isoformat()}
        version = (current["version"] if current else 0) + 1
        recipe = {
            "sponsor": self.sponsor,
            "record_type": record_type,
            "version": version,
            "instruction": instruction.strip(),
            "variants": [*existing, variant],
        }
        self._put(record_type, version, recipe)
        return version, variant, True

    def _put(self, record_type: str, version: int, recipe: dict[str, Any]) -> None:
        d = self._dir(record_type)
        atomic_write_text(d / f"v{version}.json", json.dumps(recipe, indent=2, ensure_ascii=False))
        atomic_write_text(d / f"v{version}.md", _markdown(recipe))


def _markdown(recipe: Mapping[str, Any]) -> str:
    lines = [f"# {recipe['record_type']} recipe for {recipe['sponsor']} (version {recipe['version']})", ""]
    if recipe.get("instruction"):
        lines += [f"Instruction: {recipe['instruction']}", ""]
    for v in recipe["variants"]:
        lines += [f"## Variant {v['variant_id']}: {v.get('name', '')}".rstrip(": "), ""]
        for leaf in v["layout"]:
            lines.append(f"- file `{leaf['file']}`, sheet `{leaf['sheet']}`, header row {leaf['header_row']}")
            for fld, letter in leaf["columns"].items():
                header = leaf["headers"].get(fld, "")
                lines.append(f"  - {fld} <- column {letter}" + (f" (\"{header}\")" if header else ""))
        if v.get("constants"):
            lines.append("- constants: " + ", ".join(f"{k}={val}" for k, val in v["constants"].items()))
        for step in v["post"]:
            lines.append(f"- then: {step['step']}" + (f" on {step['field']}" if step.get("field") else ""))
        lines.append("")
    return "\n".join(lines)


# ==== field recipes (recipe file v-next, ticket 02 field steps; ticket 05 D2a, D5) =============================
#
#     <root>/<sponsor>/recipes/<RecordType>.yaml          the recipe (kind `recipe`, validated by `rulefiles`)
#     <root>/<sponsor>/recipes/<RecordType>.history.jsonl  append-only, hash-chained: every change, undo, migration
#
# A step is one source (`from`, `supply`, `missing`) plus ordered ops from the closed vocabulary. Every change writes
# `recipe.changed` to the audit before the file; an undo writes `recipe.reverted` first and appends an undo record,
# nothing is deleted. The `rulefiles` recipe schema has no per-step `feedback` link yet, so the link from a step to
# the decision that made it lives in the history log (exact undo needs it).

RECIPE_FILE_VERSION = 1
_RECORD = re.compile(r"^[A-Z][A-Za-z]{0,40}$")


class RecipeChangeRefused(ValueError):
    """A recipe change that code refuses (schema, scope, replace without saying so, an undo that no longer fits)."""


def _pin_of(text: str | bytes | None) -> str | None:
    if text is None:
        return None
    raw = text.encode("utf-8") if isinstance(text, str) else text
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _dump(doc: Mapping[str, Any]) -> str:
    import yaml

    return yaml.safe_dump(dict(doc), sort_keys=False, allow_unicode=True, width=120)


def step_from_option(option: Mapping[str, Any], header: str, reason: str = "") -> dict[str, Any]:
    """The field step a chosen option stands for (closed actions only). The header comes from the payload or the
    decision's source; nothing is guessed."""
    action = option.get("action")
    payload = option.get("payload") or {}
    if action in ("map", "transform"):
        ops = list(payload.get("ops", payload.get("transforms")) or [])
        src = payload.get("from") or header
        if not src:
            raise RecipeChangeRefused("a mapped field names its source header")
        return {"from": src, **({"ops": ops} if ops else {})}
    if action == "supply":
        if "value" not in payload or str(payload["value"]).strip() == "":
            raise RecipeChangeRefused("a supplied field carries the value you typed")
        return {"supply": {"value": str(payload["value"])}}
    if action == "declare_missing":
        return {"missing": {"reason": reason or "declared missing: the source has no such field"}}
    raise RecipeChangeRefused(f"a {action!r} answer is not a field step")


class FieldRecipes:
    """One sponsor's field recipes. Reads go through the strict loader; writes are audited first."""

    def __init__(self, root: str | Path, sponsor: str, *, record_types: Iterable[str] | None = None,
                 target_fields: Mapping[str, Iterable[str]] | None = None) -> None:
        from worker.rule_recipe import SPONSOR_RE

        if not SPONSOR_RE.match(sponsor):
            raise ValueError(f"bad sponsor name {sponsor!r}")
        self.sponsor = sponsor
        self.dir = Path(root) / sponsor / "recipes"
        self._record_types = frozenset(record_types) if record_types is not None else None
        self._target_fields = ({k: frozenset(v) for k, v in target_fields.items()}
                               if target_fields is not None else None)

    # ---- paths, reads and pins --------------------------------------------------------------------------------
    def path(self, record_type: str) -> Path:
        if not _RECORD.match(record_type):
            raise ValueError(f"bad record type {record_type!r}")
        return self.dir / f"{record_type}.yaml"

    def history(self, record_type: str) -> AuditLog:
        return AuditLog(self.dir / f"{record_type}.history.jsonl")

    def _rel(self, record_type: str) -> str:
        return f"templates/sponsors/{self.sponsor}/recipes/{record_type}.yaml"

    def validate(self, record_type: str, text: str) -> dict[str, Any]:
        """Load `text` as this sponsor's recipe for `record_type` through `rulefiles` (kind `recipe`) and type-check
        every step through `transforms`. Raises RecipeChangeRefused listing the problems."""
        from worker import rulefiles
        from worker import transforms as T

        ctx = rulefiles.Context(record_types=self._record_types, target_fields=self._target_fields,
                                sponsor=self.sponsor)
        try:
            loaded = rulefiles.load_text(self._rel(record_type), text, context=ctx)
            T.compile_recipe(loaded.data)
        except (rulefiles.SchemaError, T.RecipeError) as exc:
            raise RecipeChangeRefused(str(exc)) from exc
        return dict(loaded.data)

    def text(self, record_type: str) -> str | None:
        p = self.path(record_type)
        return p.read_bytes().decode("utf-8") if p.exists() else None

    def load(self, record_type: str) -> dict[str, Any] | None:
        text = self.text(record_type)
        return None if text is None else self.validate(record_type, text)

    def pin(self, record_type: str) -> str | None:
        """The recipe pin a job records at its start (null when there is none). A change moves the next job's pin."""
        return _pin_of(self.text(record_type))

    def engine_step(self, record_type: str) -> dict[str, Any] | None:
        """The ready `transform` step for the engine's convert stage (what a `World.field_recipe` returns), or None
        when this sponsor has no recipe for the record type."""
        doc = self.load(record_type)
        return None if doc is None else engine_step(doc, self.pin(record_type), record_type)

    def _write(self, record_type: str, text: str | None) -> None:
        p = self.path(record_type)
        if text is None:
            p.unlink(missing_ok=True)
            return
        atomic_write_text(p, text)

    # ---- a decision becomes a step ------------------------------------------------------------------------------
    def plan(self, rec: Mapping[str, Any], rows: Sequence[Any], *, replace: bool = False,
             target_rules: Mapping[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
        """Build, validate and verify (on every row) the change one sponsor-scope decision makes. Writes nothing."""
        from worker import learnindex as L
        from worker import setrecipe as SR

        if rec.get("kind") != "field_decision":
            raise RecipeChangeRefused("only a field decision changes a recipe")
        if rec.get("scope") != "sponsor":
            raise RecipeChangeRefused("a job-scope answer is recorded as feedback only; the recipe does not change")
        if rec.get("sponsor") != self.sponsor:
            raise RecipeChangeRefused("a decision changes only its own sponsor's recipe")
        if not (rec.get("principal") or {}).get("name"):
            raise RecipeChangeRefused("a recipe changes only on a human's answer")
        rt, fld = str(rec["record_type"]), str(rec["target_field"])
        step = step_from_option(L.chosen_option(rec), str((rec.get("source") or {}).get("header", "")),
                                str(rec.get("reason") or ""))
        before_text = self.text(rt)
        current = self.validate(rt, before_text) if before_text is not None else None
        old = (current or {}).get("fields", {}).get(fld)
        if old is not None and old != step and not replace:
            raise RecipeChangeRefused(f"{fld} already has a step in this recipe; replacing it needs an explicit "
                                      "replace")
        doc: dict[str, Any] = {"schema_version": RECIPE_FILE_VERSION, "kind": "recipe", "id": rt,
                               "sponsor": self.sponsor,
                               "fields": {**((current or {}).get("fields") or {}), fld: step},
                               "approved_by": rec["principal"]["name"],
                               "approved_at": str(rec.get("decided_at") or datetime.now(UTC).isoformat()),
                               "reason": f"decided in job {rec['job']}"}
        text = _dump(doc)
        self.validate(rt, text)
        verdict = SR.verify({"fields": {fld: step}}, rows, target_rules, context)[fld]
        changed = old != step
        return {"record_type": rt, "field": fld, "old": old, "new": step, "text": text, "changed": changed,
                "pin_before": _pin_of(before_text), "pin_after": _pin_of(text) if changed else _pin_of(before_text),
                "verify": {"conform": verdict.conform, "total": verdict.total, "missing": verdict.missing,
                           "failed": dict(verdict.failed)}}

    def commit(self, plan: Mapping[str, Any], feedback_id: str, audit: AuditLog) -> dict[str, Any]:
        """`recipe.changed` goes to the audit first; only then is the file written and the history appended. If the
        record cannot be written, the recipe stays byte for byte as it was."""
        rt = plan["record_type"]
        if not plan["changed"]:
            return {**plan, "written": False}
        if self.pin(rt) != plan["pin_before"]:
            raise RecipeChangeRefused("the recipe changed since this change was planned; plan it again")
        facts = {"feedback_id": feedback_id, "sponsor": self.sponsor, "record_type": rt, "field": plan["field"],
                 "old": plan["old"], "new": plan["new"], "pin_before": plan["pin_before"],
                 "pin_after": plan["pin_after"], "verify": plan["verify"]}
        audit.record("recipe.changed", f"{plan['field']} decided by {feedback_id}", facts)
        self._write(rt, plan["text"])
        self.history(rt).record("recipe.changed", f"{plan['field']} decided by {feedback_id}", facts)
        return {**plan, "written": True}

    # ---- undo -----------------------------------------------------------------------------------------------------
    def revert(self, record_type: str, feedback_id: str, *, principal: str, reason: str, audit: AuditLog,
               feedback_store: Any = None) -> dict[str, Any]:
        """Undo the step one decision made: `recipe.reverted` to the audit first, then the step before it is
        restored (or the field is dropped), then an undo record is appended to the history and, when given, to the
        sponsor's feedback log. Refused when a newer decision has replaced that step since."""
        from worker.contracts import is_agent_principal

        if not principal.strip() or is_agent_principal(principal):
            raise RecipeChangeRefused("an undo is a person's decision")
        if not reason.strip():
            raise RecipeChangeRefused("an undo needs a reason")
        log = self.history(record_type).read()
        undone = {e["inputs"].get("feedback_id") for e in log if e["action"] == "recipe.reverted"}
        change = next((e["inputs"] for e in reversed(log) if e["action"] == "recipe.changed"
                       and e["inputs"].get("feedback_id") == feedback_id), None)
        if change is None or feedback_id in undone:
            raise RecipeChangeRefused(f"{feedback_id} made no change to this recipe that is still in force")
        current = self.load(record_type) or {"fields": {}}
        fld = change["field"]
        if current["fields"].get(fld) != change["new"]:
            raise RecipeChangeRefused(f"{fld} was changed again after {feedback_id}; undo the newer decision first")
        fields = dict(current["fields"])
        if change["old"] is None:
            fields.pop(fld)
        else:
            fields[fld] = change["old"]
        text = None
        if fields:
            text = _dump({**current, "fields": fields, "approved_by": principal.strip(),
                          "approved_at": datetime.now(UTC).isoformat(), "reason": f"undo of {feedback_id}"})
            self.validate(record_type, text)
        facts = {"feedback_id": feedback_id, "sponsor": self.sponsor, "record_type": record_type, "field": fld,
                 "removed": change["new"], "restored": change["old"], "pin_before": self.pin(record_type),
                 "pin_after": _pin_of(text), "by": principal.strip()}
        audit.record("recipe.reverted", reason.strip(), facts)
        self._write(record_type, text)
        self.history(record_type).record("recipe.reverted", reason.strip(), facts)
        if feedback_store is not None:
            feedback_store.add_undo(feedback_id, principal, reason)
        return facts


def decide_field(audit: AuditLog, feedback_store: Any, recipes: FieldRecipes | None, rows: Sequence[Any], *,
                 replace: bool = False, target_rules: Mapping[str, Any] | None = None, context: Any = None,
                 **decision: Any) -> dict[str, Any]:
    """A human's answer, end to end: the record is built and checked; at sponsor scope the recipe change is planned
    and verified on every row; the record goes to the job's audit and then the sponsor's log; then `recipe.changed`
    is audited and the file written. The current job uses the answer at once; the new pin applies from the next
    job. Returns {"record", "recipe"}."""
    from worker import feedback as F

    rec = F.make_field_decision(**decision)
    plan = None
    if rec["scope"] == "sponsor":
        if recipes is None:
            raise RecipeChangeRefused("a sponsor-scope answer needs the sponsor's recipes")
        plan = recipes.plan(rec, rows, replace=replace, target_rules=target_rules, context=context)
    saved = F.record(audit, feedback_store, kind=F.FIELD_DECISION,
                     **{**decision, "recipe_after": plan["pin_after"] if plan else None})
    result = recipes.commit(plan, saved["feedback_id"], audit) if (plan and recipes is not None) else None
    return {"record": saved, "recipe": result}


def engine_step(doc: Mapping[str, Any], pin: str | None, record_type: str) -> dict[str, Any]:
    """The `transform` step the engine's convert stage applies for a sponsor's field recipe: the recipe's fields as
    data (so the job's eval_key, stage keys, manifest and replay record all carry them) and the recipe pin."""
    return {"step": "transform",
            "recipe": {"schema_version": doc.get("schema_version"), "id": doc.get("id"),
                       "sponsor": doc.get("sponsor"), "fields": copy.deepcopy(dict(doc.get("fields") or {}))},
            "field_recipe": {"pin": pin, "record_type": record_type, "approved_by": doc.get("approved_by")}}
