"""The one engine: `evaluate(spec, ports, approval=None, stop_after="check") -> Evaluation`.

A job is a `JobSpec` (sources, pins, the recipe, the human's choices). The engine runs the stages read, map,
convert, check, write, each a generator over the one before, composing today's functions and rewriting none of
them: `staging` readers and `Stage`, `transforms`, `quality.scan`, `checks` and `review`, `accounts.map_keys`,
`idgen`, `money`, `recon`, `staging.write_rows`. The model never retypes a number or an ID; a ranker only proposes.

Ordering (the audit rule: record first, refuse if it cannot be written):

1. load and validate the pins (`skill.load_refused`, or Refused);
2. audit `eval.started`, before any source is read;
3. each stage: run, audit `stage.done`, seal;
4. write: stream to `.<stem>.part/`, write the replay record (`worker.replay`: the manifest, the inputs by sha256,
   the choices, the approval, the expected component digests and the two tapes; a record that cannot be written
   refuses the upload), audit `write_upload` (tie-out, file hash, approval, pins, the manifest naming the record's
   checksum), rename, then `World.commit` moves the ID counter, the existing-records store and the account store.

Every World answer and every agent Result is put on a tape as the stages run (`replay.RecordingWorld`,
`replay.RecordingRanker`), stored beside each sealed stage, so a write that reuses the sealed check still records
what that check was told. `replay_evaluate` is the same evaluation for replay: to the check stage, the would-be
upload rendered by the same writer into the replay's own folder, nothing approved or committed.

The same `eval_key` returns the sealed stages (`eval.reused`) and, after a sealed write, the file it made.
An `Approval` is built by the gate, never here; the engine only verifies it.

Two severity scales and both `required_blank` levels (WARN in `checks`, ERR in `check-data-quality`) stay separate
namespaces: a finding's `RuleRef.rule_id` is `checks.<code>` or `check-data-quality.<rule>`. They are not unified.
Where the parts live: `engine_types` (the spec, the result and the errors), `engine_read`, `engine_convert`
(map and convert), `engine_check` (check, reconcile), `engine_console_stages` (the console family's read, convert and
check), `engine_write` (write, the replay record, commit) and `engine_findings` (a finding's rule, family and options).
Each stage module is a mixin of `_Run` below; this module holds the run itself: pins, the recipe, the stage cache, the
result.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from worker import engine_console as EC
from worker import record_facts as RF
from worker import replay as RP
from worker import transforms as tf
from worker.audit import AuditError
from worker.contracts import is_agent_principal
from worker.engine_check import CheckStage, shape_findings
from worker.engine_stages import RowStages
from worker.engine_types import (
    CHOICE_KINDS,
    ENGINE_VERSION,
    STEP_STAGE,
    Approval,
    Choice,
    Evaluation,
    Finding,
    JobSpec,
    Manifest,
    Missing,
    PinSet,
    Produced,
    Recipe,
    Refused,
    RuleRef,
    SourceRef,
    StageFailed,
    TieOut,
    canonical,
    plain,
    sha,
)
from worker.engine_write import WriteStage, result_components
from worker.guard import PathRefused
from worker.improver import reserved_name
from worker.pipeline import STAGES
from worker.ports import LoadedRules, Ports, Progress, _answer_key
from worker.rulefiles import SchemaError
from worker.setops import BATCH_ROWS
from worker.spool import read, read_json, write_json
from worker.templates import TemplateSpec, sponsors_dir, templates_dir, templates_for

# the engine's public names; the types live in `engine_types`, `result_components` with the write that records it
__all__ = [
    "CHOICE_KINDS", "ENGINE_VERSION", "Approval", "Choice", "Evaluation", "Finding", "JobSpec", "PinSet", "Recipe",
    "Refused", "RuleRef", "SourceRef", "StageFailed", "canonical", "evaluate", "replay_evaluate",
    "result_components", "sha",
]


def evaluate(spec: JobSpec, ports: Ports, *, approval: Approval | None = None,
             stop_after: str = "check") -> Evaluation:
    """Evaluate this job under these pinned skills. See the module docstring. Raises `Refused` or `StageFailed`;
    everything else is a finding in the result."""
    return _Run(spec, ports, approval, stop_after).run()


def replay_evaluate(spec: JobSpec, ports: Ports) -> tuple[Evaluation, dict[str, Any], list[dict[str, Any]]]:
    """The evaluation replay runs: to the check stage, then the would-be upload rendered by the same writer into
    `ports.out_dir` (the replay's own temporary folder) and removed again. Returns the evaluation, the rendered
    output facts (output, tie-out, IDs) and the decisions it audited. No Approval, no write stage, no commit, no
    record."""
    run = _Run(spec, ports, None, "check", replaying=True)
    ev = run.run()
    if run.rendered is None:
        raise Refused("schema_invalid", "the job did not reach the check stage, so there is nothing to compare.")
    return ev, run.rendered, run.tape.decisions([s for s in STAGES if s in run.P])


class _Run(RowStages, CheckStage, WriteStage):
    def __init__(self, spec: JobSpec, ports: Ports, approval: Approval | None, stop_after: str, *,
                 replaying: bool = False) -> None:
        self.spec0 = spec
        self.spec = spec
        # every World answer and every agent Result goes on the tape (worker.replay); `commit` goes straight through
        self.tape = RP.TapeSink()
        self.replaying = replaying
        self.rendered: dict[str, Any] | None = None
        self.replay_ref: dict[str, str] = {}
        self.ports = replace(
            ports, world=RP.RecordingWorld(ports.world, self.tape),
            ranker=RP.RecordingRanker(ports.ranker, self.tape, spec.job_id) if ports.ranker is not None else None)
        self.approval = approval
        self.stop_after = stop_after
        self.seqs: list[int] = []
        self.reused: list[str] = []
        self.keys: dict[str, str] = {}
        self.P: dict[str, dict[str, Any]] = {}
        self.rules: LoadedRules | None = None
        self.pins = spec.pins
        self.eval_key = ""
        self.world_digest = ""
        self.world_changed = False
        self.template = ("GL" if spec.record_type.startswith("GL") else spec.record_type) if spec.record_type else ""
        self.templates: Mapping[str, TemplateSpec] = {}
        self.tmp_dirs: list[str] = []
        self.source_facts: tuple[Mapping[str, Any], ...] = ()
        self.profile: Any = None
        self.console: dict[str, Any] | None = None  # the `console_check` step, when the job is a console job
        self._world0 = ports.world  # as given, before the replay tape wraps it

    def _audit(self, action: str, why: str, inputs: dict[str, Any], outputs: dict[str, Any] | None = None) -> None:
        try:
            entry = self.ports.audit.record(action, why, inputs, outputs or {})
        except AuditError as exc:
            raise Refused("audit_unavailable", f"the record of {action} could not be written, so nothing was "
                          f"done ({exc}). Restore the audit log and run it again.") from exc
        self.seqs.append(int(entry.get("seq", 0)))
        self.tape.decision(action, why, inputs, outputs)

    def _audit_many(self, action: str, items: list[tuple[str, dict[str, Any], dict[str, Any]]]) -> None:
        if not items:
            return
        try:
            entries = self.ports.audit.record_many(action, items)  # type: ignore[arg-type]
        except AuditError as exc:
            raise Refused("audit_unavailable", f"the record of {action} could not be written, so nothing was "
                          f"done ({exc}). Restore the audit log and run it again.") from exc
        self.seqs.extend(int(e.get("seq", 0)) for e in entries)
        for why, inputs, outputs in items:
            self.tape.decision(action, why, inputs, outputs)

    def _progress(self, stage: str, rows: int, note: str = "") -> None:
        if self.ports.progress is not None:
            self.ports.progress(Progress(stage, rows, note))

    def run(self) -> Evaluation:
        try:
            return self._run()
        except Refused as r:
            self._refused(r)
            raise
        finally:
            for d in self.tmp_dirs:
                shutil.rmtree(d, ignore_errors=True)

    def _refused(self, r: Refused) -> None:
        if r.code == "audit_unavailable":
            return
        try:
            self.ports.audit.record("eval.refused", r.sentence[:300], {
                "eval_key": self.eval_key or self.spec0.eval_key(), "job": self.spec0.job_id, "code": r.code},
                {"refused": True})
        except (AuditError, OSError, ValueError):
            return  # the refusal stands even when it cannot be recorded

    def _mkdtemp(self) -> str:
        d = tempfile.mkdtemp(prefix="engine-")
        self.tmp_dirs.append(d)
        return d

    def _run(self) -> Evaluation:
        spec = self.spec0
        if self.stop_after not in STAGES:
            raise Refused("schema_invalid", f"stop_after must be one of {', '.join(STAGES)}, not {self.stop_after!r}")
        self._precheck()
        rules = self._load_rules()
        self.rules = rules
        pins = PinSet(
            skills=dict(rules.skills), target=self._target_checksum(), controls=rules.controls,
            golden_coa=rules.golden_coa, engine=ENGINE_VERSION, schema_versions=dict(rules.schema_versions),
            models=self._models())
        self._verify_pins(spec.pins, pins)
        self.pins = pins
        self.spec = self._with_sponsor_recipe(replace(spec, pins=pins))
        self.eval_key = self.spec.eval_key()  # until the sources are fingerprinted (below)
        self._compile_recipe()
        self._audit("eval.started", f"evaluating {spec.job_id} for {spec.sponsor}", {
            "spec_key": self.eval_key, "job": spec.job_id, "sponsor": spec.sponsor, "record_type": spec.record_type,
            "stop_after": self.stop_after, "approval": self.approval.approval_id if self.approval else None,
            "sources": [{"kind": s.kind, "ref": s.ref, "sheet": s.sheet, "fingerprint": s.fingerprint}
                        for s in spec.sources],
            "trace_id": spec.context.get("trace_id")}, {"pins": plain(pins)})
        fingerprinted = self._check_sources()
        # the identity of a job includes the bytes it read: a source given without a fingerprint is pinned now
        self.spec = replace(self.spec, sources=fingerprinted)
        self.eval_key = self.spec.eval_key()
        self._open_index()
        ev = self._stages()
        if self.replaying and ev.reached == "check":
            self.rendered = self._render()
        if self.reused:
            self._audit("eval.reused", "the same job again: sealed stages returned, not recomputed",
                        {"eval_key": self.eval_key, "stages": list(self.reused)}, {})
            ev = replace(ev, reused=tuple(self.reused), audit_seqs=tuple(self.seqs))
        return ev

    def _with_sponsor_recipe(self, spec: JobSpec) -> JobSpec:
        """A job that carries no sponsor recipe of its own gets the one its world holds (`World.field_recipe`, which
        returns a ready `transform` step with its pin, or nothing): the recipe becomes data of the spec, so the
        eval_key, the stage keys, the manifest and the replay record all say which recipe was in force. A console
        job already carries its own (its adapter wrote it), and a world without the method changes nothing."""
        if spec.recipe is None or spec.record_type is None:
            return spec
        steps = spec.recipe.bindings
        if any(b.get("step") == "console_check" or (b.get("step") == "transform" and b.get("field_recipe"))
               for b in steps):
            return spec
        fetch = getattr(self._world0, "field_recipe", None)
        step = fetch(str(spec.record_type)) if callable(fetch) else None
        if not step:
            return spec
        return replace(spec, recipe=Recipe.of(step, *steps))

    def _precheck(self) -> None:
        spec = self.spec0
        for c in spec.choices:
            if c.kind not in CHOICE_KINDS:
                raise Refused("schema_invalid", f"choice kind {c.kind!r} is not one of {', '.join(CHOICE_KINDS)}")
            if is_agent_principal(c.by) or reserved_name(c.by):
                raise Refused("agent_approver", f"{c.by!r} is not a person; only a person records a choice. "
                              "A named person must make it.")
            if not str(c.reason).strip():
                raise Refused("schema_invalid", f"a choice ({c.kind} {c.subject}) needs a reason")
        for s in spec.sources:
            if s.kind == "file" and _answer_key(s.ref):
                raise Refused("path_refused", "answer keys are not readable by the worker")
        if self.stop_after == "write":
            a = self.approval
            if a is None:
                raise Refused("no_approval", "writing the upload needs an approval from a person; the gate "
                              "makes one, the engine cannot.")
            if is_agent_principal(a.by) or reserved_name(a.by):
                raise Refused("agent_approver", f"{a.by!r} is not a person; only a named person approves an upload.")
        if spec.record_type is None and self.stop_after != "read":
            raise Refused("schema_invalid", "no record type is given; only stop_after='read' (profiling) runs "
                          "without one")
        if spec.record_type is not None or spec.sponsor:
            try:
                self.templates = templates_for(spec.sponsor)
            except ValueError as exc:
                raise Refused("schema_invalid", str(exc)) from exc
        if spec.record_type is not None and self.template not in self.templates:
            raise Refused("schema_invalid", f"unknown record type {spec.record_type!r}; known: "
                          f"{', '.join(sorted(self.templates))}")

    def _load_rules(self) -> LoadedRules:
        try:
            return self.ports.skills(self.spec0.pins)
        except SchemaError as exc:
            codes = set(getattr(exc, "codes", ()) or ())
            lines = list(getattr(exc, "lines", ()) or [str(exc)])
            try:  # the refused pin is recorded first; if it cannot be, the refusal stands all the same
                self.ports.audit.record(
                    "skill.load_refused", f"{len(lines)} problem(s) in the rule files; the job does not start",
                    {"problems": lines[:50], "codes": sorted(codes)}, {"started": False})
            except AuditError:
                pass
            shown = "; ".join(lines[:3])
            if codes & {"out_of_bounds", "looser_than_control"}:
                raise Refused("control_out_of_bounds", f"a control is outside its bounds or looser than its floor "
                              f"({shown}). A controller fixes the controls file; the job does not start.") from exc
            raise Refused("schema_invalid", f"a rule file is refused ({shown}). The job does not start.") from exc
        except (OSError, ValueError) as exc:
            raise Refused("schema_invalid", f"the skills could not be loaded ({exc}).") from exc

    def _target_checksum(self) -> str:
        if not self.template:
            return ""
        h = hashlib.sha256()
        files = [templates_dir() / f"{self.template}.target.yaml",
                 templates_dir() / f"{self.template}_Upload_Template.xlsx"]
        root = sponsors_dir() / self.spec0.sponsor if self.spec0.sponsor else None
        if root is not None and root.is_dir():
            files += [root / f"{self.template}.target.yaml", root / f"{self.template}_Upload_Template.xlsx"]
        for p in files:
            if p.is_file():
                h.update(p.name.encode() + b"\0" + p.read_bytes().replace(b"\r\n", b"\n") + b"\0")
        return h.hexdigest()[:12]

    def _models(self) -> dict[str, str]:
        r = self.ports.ranker
        if r is None:
            return {}
        model = getattr(r, "model", "") or os.environ.get("WORKER_MODEL_SMALL") or os.environ.get("WORKER_MODEL")
        return {"small": model or "unnamed"}

    def _verify_pins(self, given: PinSet, now: PinSet) -> None:
        for name, checksum in given.skills.items():
            if now.skills.get(name) != checksum:
                raise Refused("schema_invalid", f"the job is pinned to {name} {checksum} but the skill on disk is "
                              f"{now.skills.get(name)}. A change was promoted since the job started; start the job "
                              "again under the new version.")
        if given.controls and given.controls != now.controls:
            raise Refused("control_out_of_bounds", "the controls changed since the job was pinned; a controller's "
                          "change needs the job to be started again.")
        if given.target and given.target != now.target:
            raise Refused("schema_invalid", "the target rules changed since the job was pinned; start it again.")
        if given.golden_coa and given.golden_coa != now.golden_coa:
            raise Refused("schema_invalid", "the Golden chart of accounts changed since the job was pinned.")

    def _steps(self, stage: str) -> list[dict[str, Any]]:
        if self.spec.recipe is None:
            return []
        return [dict(b) for b in self.spec.recipe.bindings if STEP_STAGE.get(str(b.get("step"))) == stage]

    def _first_step(self, name: str) -> dict[str, Any] | None:
        bindings = self.spec.recipe.bindings if self.spec.recipe else ()
        return next((dict(b) for b in bindings if b.get("step") == name), None)

    def _compile_recipe(self) -> None:
        r = self.spec.recipe
        if r is None:
            return
        if r.checksum and r.checksum != sha(list(r.bindings)):
            raise Refused("schema_invalid", "the recipe's checksum does not match its steps; it was changed after "
                          "it was approved.")
        self.console = self._first_step("console_check")
        if self.console is not None and self._first_step("console_read") is None:
            raise Refused("schema_invalid", "a console job needs a console_read step as well as console_check.")
        for b in r.bindings:
            name = str(b.get("step"))
            if name not in STEP_STAGE:
                raise Refused("schema_invalid", f"step {name!r} is not one the engine knows: "
                              f"{', '.join(sorted(STEP_STAGE))}")
            if name == "transform":
                try:
                    tf.compile_recipe(b["recipe"], None, self._transform_context(records=bool(b.get("field_recipe"))))
                except tf.RecipeError as exc:
                    raise self._recipe_refusal(exc) from exc
        for c in self.spec.choices:
            if c.kind == "transform":
                try:
                    tf.compile_field(c.subject.split(":", 1)[-1], c.value, None,
                                     self._transform_context(records=self.console is not None))
                except tf.RecipeError as exc:
                    raise self._recipe_refusal(exc) from exc

    @staticmethod
    def _recipe_refusal(exc: Any) -> Refused:
        if exc.code == "unknown_operator":
            return Refused("unknown_transform", f"{exc.message} ({exc.where}). The transform vocabulary is closed; "
                           "an engineer adds an operation, a recipe cannot.")
        if exc.code in ("out_of_bounds", "looser_than_control"):
            return Refused("control_out_of_bounds", f"{exc.message} ({exc.where}).")
        return Refused("schema_invalid", f"{exc.message} ({exc.where}).")

    def _transform_context(self, records: bool = False) -> Any:
        setup = {k: str(self.spec.context[k]) for k in tf.SETUP_KEYS if k in self.spec.context}
        if not records:
            return tf.Context(setup=setup)
        # the sponsor's existing records, for a crosswalk that looks a value up in them (never fuzzy)
        known: dict[str, list[dict[str, str]]] = {}
        for t in RF.stored_types():
            known[t] = [{k: str(v) for k, v in x.items() if k in ("id", "key", "name")}
                        for x in EC.known_view(self.ports.world.store(t))]
        return tf.Context(setup=setup, records=known,
                          records_checksums={t: sha("records", v) for t, v in known.items()})

    def _source(self, step: Mapping[str, Any]) -> SourceRef:
        idx = int(step.get("source", 0))
        try:
            return self.spec.sources[idx]
        except IndexError:
            raise Refused("schema_invalid", f"step {step.get('step')} names source {idx}, but the job has "
                          f"{len(self.spec.sources)}") from None

    def _check_sources(self) -> tuple[SourceRef, ...]:
        out: list[SourceRef] = []
        for ref in self.spec.sources:
            if ref.kind == "evaluation":
                if read_json(self.ports.store, f"eval/{ref.ref}") is None:
                    raise Refused("source_changed", f"evaluation {ref.ref[:12]} is not in the store; run it first.")
                out.append(ref)
                continue
            try:
                got = self.ports.reader.fingerprint(ref)
            except PathRefused as exc:
                raise Refused("path_refused", str(exc)) from exc
            except OSError as exc:
                raise Refused("path_refused", f"{ref.ref} cannot be read ({exc}).") from exc
            if ref.fingerprint and got != ref.fingerprint:
                raise Refused("source_changed", f"{ref.ref} is not the file that was looked at: its fingerprint is "
                              f"now {got[:12]}, it was {ref.fingerprint[:12]}. Open the job again from the new file.")
            out.append(replace(ref, fingerprint=got))
        return tuple(out)

    # the index: which stage keys this eval_key used
    def _open_index(self) -> None:
        store = self.ports.store
        idx = read_json(store, f"eval/{self.eval_key}") or {}
        tspec = self.templates.get(self.template)
        types = sorted({t for t in [self.template, *(d for _f, d in (tspec.depends if tspec else ()))] if t})
        now = self.ports.world.fingerprint(types)
        sealed_check = bool((idx.get("stages") or {}).get("check"))
        if idx and idx.get("world") != now and not sealed_check:
            idx = {}
        self.world_digest = idx.get("world") or now
        self.world_changed = bool(idx) and idx.get("world") != now
        self._index = {"world": self.world_digest, "stages": dict(idx.get("stages") or {}),
                       "writes": list(idx.get("writes") or [])}

    def _save_index(self) -> None:
        write_json(self.ports.store, f"eval/{self.eval_key}", self._index)

    def _stage_key(self, stage: str) -> str:
        approval = self.approval if stage == "write" else None
        profiled = self.stop_after == "read" and stage == "read"
        return sha("st", self.spec.stage_key(stage, approval), self.world_digest, profiled)

    def _stage(self, stage: str, produce: Any) -> dict[str, Any]:
        store = self.ports.store
        key = self._index["stages"].get(stage) or self._stage_key(stage)
        got = read_json(store, f"st/{key}/meta")
        if got is not None:
            self.reused.append(stage)
            self.keys[stage] = key
            self._index["stages"][stage] = key
            self._save_index()  # a job made only of sealed stages is still this eval_key's, by name
            self.P[stage] = got
            self.tape.load(stage, read_json(store, f"tape/{key}"))  # what the sealed stage was told
            return got
        key = self._stage_key(stage)
        attempt = 0
        while True:
            started = time.perf_counter()
            self.tape.begin(stage)
            try:
                payload = produce(key)
                break
            except (Refused, AuditError, RP.TapeMiss):  # a tape miss is a replay difference, never retried
                raise
            except Exception as exc:
                attempt += 1
                self._audit("stage.failed", f"{stage} failed on attempt {attempt}",
                            {"eval_key": self.eval_key, "stage": stage, "attempt": attempt},
                            {"error": f"{type(exc).__name__}: {exc}"[:300]})
                if attempt > self.ports.max_retries:
                    raise StageFailed(stage, f"{type(exc).__name__}: {exc}"[:300]) from exc
        write_json(store, f"tape/{key}", self.tape.end(stage))  # beside the stage, before it is sealed
        self._audit("stage.done", f"{stage} done", {
            "eval_key": self.eval_key, "stage": stage, "record_type": self.spec.record_type,
            "sponsor": self.spec.sponsor, "stage_key": key},
            {"seconds": round(time.perf_counter() - started, 4), "rows": int(payload.get("rows", 0)),
             "model_calls": int(payload.get("model_calls", 0)), "retries": attempt})
        write_json(store, f"st/{key}/meta", payload)
        self.keys[stage] = key
        self._index["stages"][stage] = key
        self._save_index()
        self.P[stage] = payload
        self._progress(stage, int(payload.get("rows", 0)))
        return payload

    def _stages(self) -> Evaluation:
        last = STAGES.index(self.stop_after)
        reached = ""
        if self.spec.recipe is None and self.stop_after == "read":
            self._profile()
            return self._build(reached="read")
        if self.spec.recipe is None or not self._steps("read"):
            raise Refused("schema_invalid", "the job has no recipe with a step that reads the source.")
        if self.spec.record_type is None:
            raise Refused("schema_invalid", "no record type is given.")
        produce = {"read": self._produce_read, "map": self._produce_map, "convert": self._produce_convert,
                   "check": self._produce_check}
        for stage in STAGES[: last + 1]:
            if stage == "write":
                self._write()
            else:
                self._stage(stage, produce[stage])
                if stage == "read" and self.stop_after == "read" and not self.source_facts:
                    self._profile()
            reached = stage
        return self._build(reached=reached)

    def _path(self, ref: SourceRef) -> Path:
        try:
            return self.ports.reader.path(ref)
        except PathRefused as exc:
            raise Refused("path_refused", str(exc)) from exc

    def _rows_of(self, payload: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
        for k in payload["spools"]:
            yield from read(self.ports.store, k)

    def _row_batches(self, payload: Mapping[str, Any]) -> Iterator[list[dict[str, Any]]]:
        """The spooled rows in batches the set-based checks load into a table."""
        batch: list[dict[str, Any]] = []
        for row in self._rows_of(payload):
            batch.append(row)
            if len(batch) >= BATCH_ROWS:
                yield batch
                batch = []
        if batch:
            yield batch

    def _acknowledged_ids(self) -> list[str]:
        ids: list[str] = []
        if self.approval is not None:
            ids.extend(self.approval.acknowledged)
        ids.extend(c.subject.split(":", 1)[-1] for c in self.spec.choices if c.kind == "accept_flag")
        return ids

    def _manifest(self) -> Manifest:
        spec = self.spec
        return Manifest(
            eval_key=self.eval_key, pins=self.pins, sources=spec.sources,
            recipe_checksum=(spec.recipe.checksum or sha(list(spec.recipe.bindings))) if spec.recipe else None,
            choices_checksum=sha("choices", list(spec.choices)),
            model=(self.pins.models.get("small") if self.pins.models else None),
            spool_checksum=sha([(k, self.P[k].get("digest") or self.P[k].get("outcomes_digest"))
                                for k in ("read", "convert", "check") if k in self.P]),
            stage_keys=dict(self.keys),
            prompts=({"small": str(self.ports.ranker.prompt_checksum)}  # type: ignore[union-attr]
                     if getattr(self.ports.ranker, "prompt_checksum", "") else {}),
            replay_record=dict(self.replay_ref or (self.P.get("write") or {}).get("replay_record") or {}),
            field_recipes=self._field_steps()[1],
            derived_sources=self._derived_sources())

    def _derived_sources(self) -> tuple[Mapping[str, Any], ...]:
        """What each `derived` source came from (artifact, approval, hashes), when the reader can say."""
        lookup = getattr(self.ports.reader, "derivation", None)
        found = [lookup(s) for s in self.spec.sources if s.kind == "derived"] if lookup else []
        return tuple(d for d in found if d)

    def _build(self, reached: str) -> Evaluation:
        rd = self.P.get("read")
        ck = self.P.get("check")
        wr = self.P.get("write")
        findings: tuple[Finding, ...] = ()
        missing: tuple[Missing, ...] = ()
        tie: TieOut | None = None
        tally: dict[str, int] = {}
        header: tuple[str, ...] = tuple(self.templates[self.template].fields) if self.template else ()
        outcome_key = ""
        table_keys: tuple[str, ...] = ()
        if rd is not None:
            tally = {"read": int(rd["meta"].get("input_rows") or rd["rows"]), "out": 0, "warn": 0, "err": 0,
                     "skip": 0}
        cv = self.P.get("convert")
        if cv is not None:
            table_keys = tuple(cv["spools"])
        if ck is not None:
            ack_ids = set(self._acknowledged_ids())
            findings = shape_findings(self.rules, [
                {**f, "acknowledged": "acknowledged" if f["severity"] == "WARN" and f["id"] in ack_ids else None}
                for f in ck["findings"]])
            missing = tuple(Missing(m["field"], int(m["rows"]), m["reason"]) for m in ck["missing"])
            tie = TieOut.of(ck["tie"])
            tally = dict(ck["counts"])
            outcome_key = ck["outcomes"]
            if ck.get("console"):  # the console family writes its template rows beside the outcomes
                table_keys = (ck["console"]["table"],)
        produced = None
        if wr is not None:
            produced = Produced(Path(wr["path"]), wr["sha256"], int(wr["rows"]), tuple(wr["ids_issued"]),
                                wr["approval_id"])
            tie = TieOut.of(wr["tie"])
        if reached in ("check", "write"):
            blocked = any(f.severity == "ERR" for f in findings)
            outcome = "needs_human" if blocked else "complete"
        else:
            outcome = "stopped"
        manifest = self._manifest()
        proposals: tuple[Mapping[str, Any], ...] = ()
        if self.P.get("map") and self.P["map"].get("account_mapping"):
            proposals = tuple({"key": x["key"], "account": x["account"], "tier": x["tier"], "action": x["action"],
                               "evidence": x["evidence"], "candidates": x["candidates"][:3]}
                              for x in self.P["map"]["account_mapping"]["keys"]
                              if x["action"] in ("suggest", "needs_you"))
        return Evaluation(
            outcome=outcome, reached=reached, tally=tally, findings=findings, missing=missing, tie_out=tie,
            proposals=proposals, manifest=manifest, produced=produced, audit_seqs=tuple(self.seqs),
            profile=self.profile, header=header, source_facts=self.source_facts,
            reconcile=(ck or {}).get("reconcile"), reconciliation=(ck or {}).get("reconciliation"),
            reused=tuple(self.reused), facts=dict((ck or {}).get("console") or {}),
            _store=self.ports.store, _table_keys=table_keys, _outcome_key=outcome_key)
