"""Per-sponsor memory: approved rules (AGENTS.md), pending proposals and approved cases for replay.

Layout under `<root>/<sponsor>/`: `AGENTS.md` (approved rules), `pending.json` (proposed / rejected rules) and
`cases/<id>.json` (one per approved upload). Only the human gate may lead to `approve`; a sponsor object never
reads another sponsor's folder.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from worker.fsutil import atomic_write_text
from worker.hashing import sha256_file
from worker.staging import Stage

if TYPE_CHECKING:
    from worker.context import RunContext

_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ID = re.compile(r"^R(\d+)$")
_HEADING = re.compile(r"^## Rule (\S+)\s*$", re.MULTILINE)
RULE_ID_PATTERN = re.compile(r"\bR\d+\b")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def rows_digest(rows: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode("utf-8")).hexdigest()


class SponsorMemory:
    def __init__(self, root: Path | str, sponsor: str) -> None:
        if not _SAFE.match(sponsor):
            raise ValueError(f"unsafe sponsor name {sponsor!r}")
        self.sponsor = sponsor
        self.dir = Path(root) / sponsor
        self.agents_path = self.dir / "AGENTS.md"
        self.pending_path = self.dir / "pending.json"
        self.cases_dir = self.dir / "cases"

    # ---- storage ---------------------------------------------------------------------------
    def _load_pending(self) -> dict[str, dict[str, Any]]:
        if not self.pending_path.exists():
            return {}
        return json.loads(self.pending_path.read_text(encoding="utf-8"))

    def _save_pending(self, data: dict[str, dict[str, Any]]) -> None:
        atomic_write_text(self.pending_path, json.dumps(data, indent=2, ensure_ascii=False))

    def _approved(self) -> dict[str, dict[str, str]]:
        """Rule id -> fields, in file order."""
        if not self.agents_path.exists():
            return {}
        raw = self.agents_path.read_text(encoding="utf-8")
        parts = _HEADING.split(raw)
        out: dict[str, dict[str, str]] = {}
        for rid, body in zip(parts[1::2], parts[2::2], strict=True):
            fields: dict[str, str] = {}
            for line in body.splitlines():
                if line.startswith("- ") and ": " in line:
                    key, _, value = line[2:].partition(": ")
                    fields[key.strip()] = value.strip()
            out[rid] = fields
        return out

    def _write_approved(self, rules: dict[str, dict[str, str]]) -> None:
        blocks = [f"# Approved rules for {self.sponsor}\n"]
        for rid, f in rules.items():
            lines = [f"## Rule {rid}"] + [f"- {k}: {v}" for k, v in f.items()]
            blocks.append("\n".join(lines) + "\n")
        atomic_write_text(self.agents_path, "\n".join(blocks))

    def _next_id(self) -> str:
        nums = [
            int(m.group(1)) for rid in [*self._load_pending(), *self._approved()] if (m := _ID.match(rid))
        ]
        return f"R{max(nums, default=0) + 1}"

    # ---- rules -----------------------------------------------------------------------------
    def propose(self, text: str, evidence: str) -> str:
        """Record a proposal as pending only; it is not applied until approved."""
        if not _one_line(text):
            raise ValueError("a rule needs text")
        rid = self._next_id()
        pending = self._load_pending()
        pending[rid] = {
            "text": _one_line(text),
            "evidence": _one_line(evidence),
            "status": "pending",
            "proposed_at": _now(),
        }
        self._save_pending(pending)
        return rid

    def pending(self) -> dict[str, dict[str, Any]]:
        return {k: v for k, v in self._load_pending().items() if v.get("status") == "pending"}

    def approve(self, rule_id: str) -> None:
        pending = self._load_pending()
        item = pending.get(rule_id)
        if item is None or item.get("status") != "pending":
            raise KeyError(f"no pending rule {rule_id!r}")
        rules = self._approved()
        rules[rule_id] = {
            "status": "active",
            "text": item["text"],
            "evidence": item["evidence"],
            "approved_at": _now(),
        }
        self._write_approved(rules)
        del pending[rule_id]
        self._save_pending(pending)

    def reject(self, rule_id: str, reason: str) -> None:
        pending = self._load_pending()
        item = pending.get(rule_id)
        if item is None or item.get("status") != "pending":
            raise KeyError(f"no pending rule {rule_id!r}")
        item.update(status="rejected", reason=_one_line(reason), rejected_at=_now())
        self._save_pending(pending)

    def approved(self) -> dict[str, dict[str, str]]:
        """Every approved rule by id, with its fields, for display."""
        return self._approved()

    def active_ids(self) -> set[str]:
        return {rid for rid, f in self._approved().items() if f.get("status") == "active"}

    def rules_text(self) -> str:
        """Active rules for the prompt, one line each; empty when there are none."""
        lines = [
            f"- {rid}: {f.get('text', '')} (evidence: {f.get('evidence', '')})"
            for rid, f in self._approved().items()
            if f.get("status") == "active"
        ]
        return "\n".join(lines)

    # ---- cases -----------------------------------------------------------------------------
    def save_case(
        self,
        template: str,
        spec: dict[str, Any],
        sources: dict[str, str],
        constants: dict[str, str],
        output: Path,
        rows: list[dict[str, Any]],
        corrections: list[dict[str, Any]] | None = None,
    ) -> str:
        out_sha = sha256_file(output)
        case_id = f"{template}-{out_sha[:12]}"
        record = {
            "case_id": case_id,
            "sponsor": self.sponsor,
            "template": template,
            "created_at": _now(),
            "input": {"path": spec["path"], "sha256": sha256_file(spec["path"])},
            "output": {"file": output.name, "sha256": out_sha},
            "mapping": {"sources": sources, "constants": constants},
            "spec": spec,
            "rows": len(rows),
            "rows_sha256": rows_digest(rows),
            "corrections": list(corrections or []),  # what the human changed on the way, with reasons
        }
        self._put_case(case_id, record)
        return case_id

    def _put_case(self, case_id: str, record: dict[str, Any]) -> None:
        atomic_write_text(self.cases_dir / f"{case_id}.json",
                          json.dumps(record, indent=2, ensure_ascii=False, default=str))

    def cases(self) -> list[dict[str, Any]]:
        if not self.cases_dir.exists():
            return []
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.cases_dir.glob("*.json"))]


def apply_spec(stage: Stage, template: str, spec: dict[str, Any]) -> str:
    """Run a (bound) spec chain on `stage`; returns the new stage id. Code only, no model."""
    return _relift_sid(stage, template, spec)


def _relift(stage: Stage, template: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    return stage.rows(_relift_sid(stage, template, spec))


def _relift_sid(stage: Stage, template: str, spec: dict[str, Any]) -> str:
    kind = spec["kind"]
    if kind == "join":
        left = _relift_sid(stage, template, spec["left"])
        right = _relift_sid(stage, spec["right_template"], spec["right"])
        return stage.join(left, right, spec["left_key"], spec["right_key"], spec["take"], spec["how"])
    if kind == "assign_ids":
        from worker.idgen import Reservation

        rsv = Reservation(spec["scheme"], spec["first"], [])
        sid, _ = stage.assign_ids(
            _relift_sid(stage, template, spec["of"]), spec["field"], rsv.take, spec.get("source_key_field")
        )
        return sid
    if kind == "set_by_key":
        sid, _ = stage.set_by_key(
            _relift_sid(stage, template, spec["of"]),
            spec["key_fields"],
            {tuple(k): v for k, v in spec["items"]},
            only_blank=spec.get("via") not in ("confirm_account_mappings", "resolve_references"),
        )
        return sid
    if kind == "edit_rows":
        return stage.edit_rows(
            _relift_sid(stage, template, spec["of"]), spec.get("drop", ()), spec.get("edits")
        )
    if kind == "distinct":
        return stage.distinct(_relift_sid(stage, template, spec["of"]), spec["key_fields"])
    if kind == "lift":
        sid = stage.lift(
            spec["path"],
            spec["sheet"],
            spec["columns"],
            spec["header_row"],
            spec["first_row"],
            spec["last_row"],
            spec["constants"],
            template=template,
        )
    elif kind == "lift_blocks":
        blocks = [(b[0], int(b[1] or 1), b[2]) for b in spec["blocks"]]
        sid = stage.lift_blocks(
            spec["path"], spec["sheet"], spec["columns"], blocks, spec["constants"], template=template
        )
    elif kind == "gl_lines":
        sid = stage.lift_gl_lines(
            spec["path"], spec["sheet"], spec["columns"], spec["header_row"], gp_id=spec["gp_id"],
            fund_id=spec["fund_id"], fund_name=spec["fund_name"],
            investors={k: (v[0], v[1]) for k, v in spec["investors"].items()}, offsets=spec["offsets"],
            template=template,
        )
    elif kind == "flatten_compact":
        sid = stage.flatten_compact(
            spec["path"],
            spec["sheet"],
            {int(k): v for k, v in spec["levels"].items()},
            spec["first_row"],
            template=template,
            last_row=spec["last_row"],
            column=spec["column"],
        )
    else:
        raise ValueError(f"unknown case kind {kind!r}")
    return sid


def replay(ctx: RunContext) -> list[dict[str, Any]]:
    """Re-run the deterministic mapping of every approved case; report which outputs would change."""
    mem = ctx.memory
    if mem is None or mem.sponsor != ctx.sponsor:
        return []
    results: list[dict[str, Any]] = []
    for case in mem.cases():
        item: dict[str, Any] = {"case_id": case["case_id"], "template": case["template"], "changed": False}
        spec = case["spec"]
        path = Path(spec["path"])
        if not path.exists():
            item.update(changed=True, reason="input file missing")
            results.append(item)
            continue
        input_changed = sha256_file(path) != case["input"]["sha256"]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                digest = rows_digest(_relift(Stage(tmp), case["template"], spec))
        except Exception as exc:  # noqa: BLE001  a mapping that no longer runs is a change
            item.update(changed=True, reason=f"mapping fails: {type(exc).__name__}: {exc}"[:200])
            results.append(item)
            continue
        if digest != case["rows_sha256"]:
            item.update(
                changed=True, reason="input file changed" if input_changed else "mapping output differs"
            )
        elif input_changed:
            item["note"] = "input file bytes changed; mapped rows are identical"
        results.append(item)
    return results
