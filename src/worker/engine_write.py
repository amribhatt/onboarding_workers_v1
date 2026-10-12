"""The write stage: stream the upload, the replay record, audit, rename, commit (`_Run` mixin)."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from worker import accuracy as AC
from worker import engine_console as EC
from worker import replay as RP
from worker import review as review_mod
from worker.audit import AuditError
from worker.engine_types import (
    Approval,
    Evaluation,
    Finding,
    Refused,
    StageFailed,
    TieOut,
    Totals,
    findings_digest,
    is_blank,
    plain,
    sha,
)
from worker.guard import PathRefused
from worker.hashing import sha256_file
from worker.money import cents
from worker.pipeline import STAGES
from worker.registry import row_key
from worker.setops import RowTable
from worker.spool import read_json, write_json
from worker.staging import SRC, write_rows


def _finding_fact(f: Finding) -> dict[str, Any]:
    return {"id": f.id, "rule_id": f.rule.rule_id, "skill": f.rule.skill, "pin": f.rule.pin, "control": f.rule.control,
            "severity": f.severity, "family": f.family, "field": f.field, "count": f.count,
            "ranges_text": f.ranges_text, "message": f.message}


def _tie_dict(t: TieOut | None) -> dict[str, Any]:
    """The tie-out as the replay record keeps it (amounts as text)."""
    return {} if t is None else {k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(t).items()}


def result_components(ev: Evaluation, rendered: Mapping[str, Any],
                      decisions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The components of a result, as the replay record stores them under `expected` and as a replay recomputes
    them: one function for both sides. Findings carry their `RuleRef` pin; the IDs and the decisions are a count and
    a digest (a record never holds rows)."""
    ids = [str(i) for i in rendered.get("ids_issued") or []]
    return {
        "output": dict(rendered.get("output") or {}),
        "findings": [_finding_fact(f) for f in ev.findings],
        "tie_out": dict(rendered.get("tie_out") or {}),
        "missing": [{"field": m.field, "rows": m.rows, "reason": m.reason} for m in ev.missing],
        "tally": {k: int(v) for k, v in ev.tally.items()},
        "ids_issued": {"count": len(ids), "sha256": RP.digest(ids)},
        "decisions": {"count": len(decisions), "sha256": RP.digest(list(decisions))},
    }


class WriteStage:
    def _verify_approval(self, findings: Sequence[Finding]) -> None:
        a = self.approval
        assert a is not None
        if a.eval_key != self.eval_key:
            raise Refused("stale_approval", "this approval was given for a different evaluation; the job changed "
                          "after the person looked. Show them the current result and ask again.")
        now = findings_digest(findings)
        if a.findings_digest != now:
            raise Refused("stale_approval", "the findings are not the ones the person approved; the data changed "
                          "after they looked. Show them the current findings and ask again.")
        if self.world_changed:
            raise Refused("stale_approval", "the sponsor's records changed since this evaluation was checked; run "
                          "it again and ask for the approval again.")
        errs = [f for f in findings if f.severity == "ERR"]
        if errs:
            raise Refused("open_err", f"{len(errs)} ERR finding(s) are open (e.g. {errs[0].rule.rule_id}: "
                          f"{errs[0].message[:100]}). Resolve them; the engine does not write over an ERR.")
        ack = set(self._acknowledged_ids())
        open_warn = [f for f in findings if f.severity == "WARN" and f.id not in ack]
        if open_warn:
            raise Refused("no_approval", f"{len(open_warn)} WARN finding(s) are not acknowledged in the approval "
                          f"(e.g. {open_warn[0].rule.rule_id}). A person acknowledges each with a reason.")

    def _write(self) -> None:
        ev0 = self._build(reached="check")
        wkey = self._stage_key("write")
        a = self.approval
        assert a is not None
        # a sealed write of this evaluation is returned, not written again (no `_v2`, the counter moved once)
        for prev in [wkey, *(self._index.get("writes") or [])]:
            done = read_json(self.ports.store, f"st/{prev}/meta")
            if done and Path(done["path"]).is_file() and sha256_file(Path(done["path"])) == done["sha256"]:
                if a.eval_key != self.eval_key:
                    raise Refused("stale_approval", "this approval was given for a different evaluation.")
                self.reused.append("write")
                self.P["write"] = done
                self.keys["write"] = prev
                return
        self._verify_approval(ev0.findings)
        payload = self._produce_write(wkey, ev0)
        self.P["write"] = payload
        self.keys["write"] = wkey
        self._index["writes"].append(wkey)
        self._index["stages"]["write"] = wkey
        self._save_index()

    def _upload_rows(self, cv: Mapping[str, Any]) -> Iterable[dict[str, Any]]:
        """The rows the upload holds, in order: the table (duplicates dropped when the write step says so), or a
        console job's template rows."""
        wstep = self._first_step("write") or {}
        if wstep.get("drop_duplicates") and self.console is None:
            if not wstep.get("duplicate_key"):
                raise ValueError("drop_duplicates needs a duplicate_key")
            drop = RowTable.from_batches(self._row_batches(cv)).later_duplicates(wstep["duplicate_key"])
            return (r for r in self._rows_of(cv) if r[SRC] not in drop)
        return self._rows_of(cv)

    def _stream(self, cv: Mapping[str, Any], part_dir: Path, stem: str
                ) -> tuple[Path, int, Totals, RP.ContentHasher]:
        """Write the upload rows into `part_dir` through `staging.write_rows`, counting the tie-out and feeding the
        content digest on the way (the file is never read back)."""
        tspec = self.templates[self.template]
        fields_t = list(tspec.fields)
        rows_iter = self._upload_rows(cv)
        is_gl = self.template == "GL"
        acc = Totals()
        hasher = RP.ContentHasher(self.template)
        hasher.row(fields_t)

        def counted() -> Iterator[dict[str, Any]]:
            for r in rows_iter:
                acc.rows += 1
                if is_gl and not is_blank(r.get("Total_Amount")):
                    acc.add(r["Total_Amount"])
                if is_gl and (self.console is not None or r.get("Journal_ID") not in (None, "")):
                    acc.journals.add(str(r.get("Journal_ID", "")))  # a console file counts a blank journal too
                hasher.row([r.get(f) for f in fields_t])
                yield r

        try:
            part_path, n = write_rows(self.template, tspec.fields, counted(), part_dir, tspec.output, stem=stem)
        except ValueError as exc:
            shutil.rmtree(part_dir, ignore_errors=True)
            if "more rows than one Excel sheet" in str(exc):
                raise Refused("too_large", str(exc)) from exc
            raise Refused("schema_invalid", str(exc)) from exc
        return part_path, n, acc, hasher

    def _file_totals(self, n: int, acc: Totals) -> AC.Check:
        """The rows and totals of the file just written, against the totals the check stage showed the person."""
        tie = self.P["check"].get("tie") or {}
        want: dict[str, Decimal | int] = {"rows": int(self.P["check"]["rows"])}
        got: dict[str, Decimal | int] = {"rows": n}
        if self.template == "GL" and tie.get("net") is not None:
            for k, v in (("debits", acc.debits), ("credits", acc.credits), ("net", acc.net)):
                want[k] = Decimal(str(tie[k]))
                got[k] = cents(v)
        return AC.control_totals(want, got, "the file written against the check")

    def _tie(self, cv: Mapping[str, Any], n: int, acc: Totals) -> TieOut:
        is_gl = self.template == "GL"
        if self.console is not None:
            counts = self.P["check"]["counts"]
            skipped, read_n = int(counts["skip"]), int(counts["read"])
            ties = n == acc.rows
        else:
            meta = cv["meta"]
            skipped = len(self._skipped_rows(meta))
            read_n = int(meta.get("input_rows") or n)
            recon = review_mod.reconciliation(meta, int(cv["rows"]), n)
            ties = bool(recon.get("balanced", True)) and n == acc.rows
        return TieOut(read_n, n, skipped, ties, cents(acc.debits) if is_gl else None,
                      cents(acc.credits) if is_gl else None, cents(acc.net) if is_gl else None,
                      len(acc.journals) if is_gl else None)

    @staticmethod
    def _output_facts(part_path: Path, hasher: RP.ContentHasher, digest: str) -> dict[str, Any]:
        fmt = "csv" if part_path.suffix.lower() == ".csv" else "xlsx"
        return {"name": part_path.name, "format": fmt, "content_sha256": hasher.hexdigest(), "bytes_sha256": digest}

    def _stem(self) -> str:
        wstep = self._first_step("write") or {}
        return str(wstep.get("stem") or f"{self.template}_Upload")

    def _render(self) -> dict[str, Any]:
        """Replay's would-be upload: the same rows through the same writer into the replay's own folder, its facts
        taken, then removed. Nothing is approved, renamed or committed."""
        cv = self._table_payload()
        stem = self._stem()
        part_dir = Path(self.ports.out_dir) / f".{stem}.{self.eval_key[:10]}.replay"
        shutil.rmtree(part_dir, ignore_errors=True)
        try:
            part_path, n, acc, hasher = self._stream(cv, part_dir, stem)
            facts = self._output_facts(part_path, hasher, sha256_file(part_path))
        finally:
            shutil.rmtree(part_dir, ignore_errors=True)
        return {"output": facts, "tie_out": _tie_dict(self._tie(cv, n, acc)),
                "ids_issued": list(cv.get("ids_issued") or []), "rows": n}

    def _produce_write(self, wkey: str, ev0: Evaluation) -> dict[str, Any]:
        cv = self._table_payload()
        stem = self._stem()
        out_dir = Path(self.ports.out_dir)
        part_dir = out_dir / f".{stem}.{self.eval_key[:10]}.part"
        shutil.rmtree(part_dir, ignore_errors=True)
        part_path, n, acc, hasher = self._stream(cv, part_dir, stem)
        digest = sha256_file(part_path)
        if self.console is not None:
            counts = self.P["check"]["counts"]
            if n != int(counts["out"]):
                shutil.rmtree(part_dir, ignore_errors=True)
                raise Refused("schema_invalid", f"the file would hold {n} rows but the check counted "
                                                f"{counts['out']} written; nothing was written.")
        tie = self._tie(cv, n, acc)
        try:  # the file about to be kept must be what was checked: failed proofs, or totals that moved, stop it
            AC.require([AC.Check(**{**c, "facts": c.get("facts", {})}) for c in
                        (self.P["check"].get("console") or {}).get("accuracy", []) if not c["ok"]])
            AC.require([self._file_totals(n, acc), AC.Check(
                "conservation", tie.ties, f"{tie.rows_read:,} rows read, {n:,} written, {tie.rows_skipped:,} held: "
                "the rows are not all accounted for")])
        except AC.AccuracyError as exc:
            shutil.rmtree(part_dir, ignore_errors=True)
            raise Refused("accuracy_failed", f"nothing was written: {exc}") from exc
        final = out_dir / part_path.name
        k = 1
        while final.exists():
            k += 1
            final = out_dir / f"{stem}_v{k}{part_path.suffix}"
        issued = list(cv.get("ids_issued") or [])
        a = self.approval
        assert a is not None
        try:
            # the replay record first: if it cannot be written, neither is the upload (audit before effect)
            record = self._write_record(ev0, a, {"output": self._output_facts(part_path, hasher, digest),
                                                 "tie_out": _tie_dict(tie), "ids_issued": issued})
            self._audit("write_upload", f"approved by {a.by}: {len(ev0.findings)} finding(s) shown, "
                        f"{len(a.acknowledged)} acknowledged", {
                            "eval_key": self.eval_key, "job": self.spec.job_id, "sponsor": self.spec.sponsor,
                            "record_type": self.spec.record_type, "template": self.template,
                            "approval_id": a.approval_id, "approved_by": a.by, "approved_at": a.at,
                            "acknowledged": list(a.acknowledged)},
                        {"file": final.name, "sha256": digest, "rows": n, "tie_out": _tie_dict(tie),
                         "ids_issued": issued[:200], "ids_issued_count": len(issued),
                         "pins": plain(self.pins), "manifest": self._manifest().as_record(),
                         "replay_record": record})
        except Refused:
            shutil.rmtree(part_dir, ignore_errors=True)  # no record, so the file must not stand
            raise
        try:
            os.replace(part_path, final)
            self._commit(cv, issued, a)
        except Exception as exc:
            final.unlink(missing_ok=True)
            shutil.rmtree(part_dir, ignore_errors=True)
            try:
                self.ports.audit.record("write_upload.failed", f"the upload was not kept: {exc}"[:300],
                                        {"eval_key": self.eval_key, "file": final.name}, {"removed": True})
            except AuditError:
                pass
            raise StageFailed("write", f"{type(exc).__name__}: {exc}"[:300]) from exc
        shutil.rmtree(part_dir, ignore_errors=True)
        payload = {"path": str(final), "sha256": digest, "rows": n, "ids_issued": issued, "approval_id": a.approval_id,
                   "tie": _tie_dict(tie), "replay_record": record}
        write_json(self.ports.store, f"st/{wkey}/meta", payload)
        return payload

    # ---- the replay record ----
    def _replay_root(self) -> Path:
        """Where the records live: `WORKER_REPLAY_ROOT`; else next to the engine's store folder (`<state>/replay`
        for the production ports, `<run>/replay` for the agent's run); else next to the output folder."""
        env = os.environ.get("WORKER_REPLAY_ROOT", "").strip()
        if env:
            return Path(env)
        root = getattr(self.ports.store, "root", None)
        if root is not None:
            return Path(root).parent / "replay"
        return Path(self.ports.out_dir).parent / "replay"

    def _record_inputs(self) -> list[dict[str, Any]]:
        """Each source by reference and sha256, never a copy: the path under the reader's root that holds it."""
        roots = [Path(r).resolve() for r in (getattr(self.ports.reader, "roots", None) or [])]
        out: list[dict[str, Any]] = []
        for s in self.spec.sources:
            if s.kind != "file":
                out.append({"kind": s.kind, "ref": s.ref, "sha256": s.ref if s.kind == "derived" else "",
                            "bytes": None})
                continue
            item: dict[str, Any] = {"kind": "file", "ref": s.ref, "root": None, "sha256": s.fingerprint,
                                    "bytes": None, "sheet": s.sheet, "header_row": s.header_row}
            try:
                p = Path(self.ports.reader.path(s)).resolve()
            except (PathRefused, OSError):
                p = None
            if p is not None and p.is_file():
                item["bytes"] = p.stat().st_size
                for r in roots:
                    if r == p.parent or r in p.parents:
                        item["ref"], item["root"] = p.relative_to(r).as_posix(), str(r)
                        break
            out.append(item)
        return out

    def _record_facts(self) -> dict[str, Any]:
        """For a console static job: the source's header texts and, per plan field, the column the plan proposed,
        the column the person chose and the column the approved job used (what `SynonymReplay` replays)."""
        if self.console is None or self.console.get("kind") == "gl":
            return {}
        headers = [str(h) for h in (self.P["read"]["meta"].get("header") or [])]
        colmap = EC.parse_choices(self.spec.choices).colmap
        sponsor = str(self.console.get("sponsor", self.spec.sponsor))
        fields_: dict[str, Any] = {}
        for f in self.console.get("plan") or []:
            name, src = str(f[0]), str(f[1] or "")
            if src == "generated" or src.startswith(("constant", "(")):
                continue
            chosen = colmap.get(name)
            used = EC.detect_header(name, chosen if chosen is not None else src, headers, sponsor)
            fields_[name] = {"default": src, "chosen": chosen, "used": used}
        return {"headers": headers, "fields": fields_}

    def _handoffs(self) -> list[dict[str, Any]]:
        """`handoff.returned` records of this job on the engine's log (when the log can be read): the roles' Results
        by unit, role, inputs and attempt, with the model and prompt checksum."""
        read = getattr(self.ports.audit, "read", None)
        if read is None:
            return []
        try:
            entries = read()
        except (OSError, ValueError, AuditError):
            return []
        out: list[dict[str, Any]] = []
        for e in entries:
            i = e.get("inputs") or {}
            if e.get("action") != "handoff.returned" or i.get("unit_id") != self.spec.job_id:
                continue
            out.append({"unit_id": i.get("unit_id"), "role": i.get("role"),
                        "input_checksums": list(i.get("input_checksums") or []),
                        "occurrence": int((e.get("outputs") or {}).get("attempt") or 1), "model_id": i.get("model_id"),
                        "prompt_checksum": i.get("prompt_checksum"), "output": None,
                        "output_checksum": i.get("output_checksum")})
        return out

    def _write_record(self, ev0: Evaluation, a: Approval, rendered: Mapping[str, Any]) -> dict[str, str]:
        """The replay record of this sealed write (ticket 04 R2). Raises `Refused("audit_unavailable")` when it
        cannot be written: the upload is then refused, by the same rule as the audit."""
        spec = self.spec
        root = self._replay_root()
        try:
            blobs = RP.BlobStore(root / "blobs")
            order = [s for s in STAGES if s in self.P and s != "write"]
            handoffs = self._handoffs()
            results = [*self.tape.results_tape(order), *handoffs]
            decisions = self.tape.decisions(order)
            models = dict(self.pins.models)
            prompts: dict[str, str] = {}
            ranker = self.ports.ranker
            if ranker is not None and getattr(ranker, "prompt_checksum", ""):
                prompts["small"] = str(ranker.prompt_checksum)
            for h in handoffs:
                if h.get("role"):
                    models.setdefault(str(h["role"]), str(h.get("model_id") or ""))
                    prompts.setdefault(str(h["role"]), str(h.get("prompt_checksum") or ""))
            choices = [{"kind": c.kind, "subject": c.subject, "value": plain(c.value), "scope": c.scope, "by": c.by,
                        "reason": c.reason} for c in spec.choices]
            recipe = {"checksum": (spec.recipe.checksum or sha(list(spec.recipe.bindings))),
                      "bindings": plain(list(spec.recipe.bindings))} if spec.recipe else None
            pct = RP.holdout_pct_in_force(self.pins.skills)
            manifest = {**self._manifest().as_record(), "models": models, "prompts": prompts,
                        "spec": {"job_id": spec.job_id, "sponsor": spec.sponsor, "entity": spec.entity,
                                 "context": {k: v for k, v in spec.context.items() if k != "trace_id"}},
                        "skill_trees": RP.skill_trees(blobs, self.pins.skills, spec.sponsor),
                        "holdout_pct": pct, "tape_complete": not self.tape.missing,
                        "tape_missing": list(self.tape.missing)}
            record = RP.ReplayRecord(
                job_id=RP.safe_name(spec.job_id), sponsor=RP.safe_name(spec.sponsor),
                record_type=str(spec.record_type), entity=str(spec.entity or ""),
                holdout=RP.assign_holdout(spec.sponsor, spec.job_id, pct) if pct is not None else False,
                signed_off={"by": a.by, "at": a.at, "approval_id": a.approval_id},
                manifest=manifest, inputs=self._record_inputs(),
                blobs={"recipe": {"checksum": recipe["checksum"], "blob": blobs.put_plain(recipe)} if recipe else None,
                       "choices": {"checksum": sha("choices", list(spec.choices)), "blob": blobs.put_plain(choices)},
                       "world_tape": blobs.put_plain(self.tape.world_tape(order)),
                       "results_tape": blobs.put_plain(results), "decisions": blobs.put_plain(decisions)},
                approval={"approval_id": a.approval_id, "by": a.by, "eval_key": a.eval_key,
                          "findings_digest": a.findings_digest, "acknowledged": list(a.acknowledged)},
                expected=result_components(ev0, rendered, decisions), facts=self._record_facts())
            _path, checksum = RP.save_record(root, record)
        except (RP.ReplayError, OSError, ValueError, TypeError) as exc:
            raise Refused("audit_unavailable", f"the replay record of this upload could not be written, so nothing "
                          f"was written ({exc}). Make the replay folder writable and approve again.") from exc
        self.replay_ref = {"ref": record.ref, "record_sha256": checksum}
        return dict(self.replay_ref)


    def _table_payload(self) -> dict[str, Any]:
        """The rows to write: the converted table, or for a console job the template rows its check made."""
        cv: dict[str, Any] = self.P["convert"]
        if self.console is None:
            return cv
        console = (self.P.get("check") or {}).get("console") or {}
        return {**cv, "spools": [console["table"]], "rows": console["written"],
                "ids_issued": list(console.get("issued") or [])}

    def _commit(self, cv: Mapping[str, Any], issued: list[str], a: Approval) -> None:
        """The counter, the existing-records store and the account store move, once, after the upload is there."""
        spec_t = self.templates[self.template]
        resv_field = None
        for step in self._steps("convert"):
            if step["step"] == "assign_ids":
                resv_field = step.get("source_key_field")
        records: list[dict[str, Any]] = []
        if spec_t.key and self.console is None:  # a console job's own state is moved by the console (its `finish`)
            generated = bool(issued)
            for r in self._rows_of(cv):
                src_key = str(r.get(resv_field, "")).strip() if resv_field else ""
                records.append({
                    "key": row_key(r, spec_t.key),
                    "name": str(r.get(spec_t.name_field, "")) if spec_t.name_field else "",
                    "source_key": src_key,
                    "key_kind": "name" if generated and not src_key else "source id",
                    "fund": str(r.get("Fund_ID", "")),
                })
        self.ports.world.commit(self.spec.job_id, issued, records, record_type=self.template, key=self.eval_key,
                                accounts=self.P["map"].get("accounts_confirmed") or {}, batch=self.spec.job_id,
                                reason=f"approved by {a.by} ({a.approval_id})")

