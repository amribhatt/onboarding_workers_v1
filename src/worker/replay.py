"""Replay: what "the same result" means, the replay record, the blob store, the tapes, and the drivers.

The record layout and the algorithms are in `.scratch/skills-and-one-engine/replay.md` (ticket 04).

* M2 (core): `canonical`, `VOLATILE`, the output digests, `ReplayRecord`, `BlobStore`, `diff`, the held-out set,
  `replay_job` with an injected `run`.
* M19 (wired): the engine writes one record per sealed write (`engine._Run._write_record`), before its
  `write_upload` audit entry, which names the record's checksum; a record that cannot be written refuses the upload.
  The record holds the manifest, the inputs by sha256 reference (never a copy), the human's choices verbatim, the
  approval, the `expected` component digests, the holdout and signed-off flags, and two tapes: every World answer
  (`RecordingWorld` / `TapeWorld`) and every agent Result (`RecordingRanker` / `TapeRanker`). A question that is not
  on a tape is a difference (`TapeMiss`), never a live model call.
* `EngineRun` replays a record through `engine.evaluate` (to the check stage; replay never writes an upload, moves a
  counter or approves); `replay_all` is the engine release check.

Nothing here calls a model, reads an answer key, moves a counter, approves, promotes, or writes an upload file.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePath
from typing import Any

from worker import columnar, hashing, persist
from worker.audit import AuditError, AuditLog
from worker.guard import PathRefused, check_path

SCHEMA_VERSION = 1

# The closed list of keys that never take part in "same". Adding one is an engineer's change with a test.
# M19 adds the identities of a run: they name it, they are not what it decided. The evaluation key, the stage keys
# and the source fingerprint are derived from the pins and the bytes read (the inputs are checked by sha256 on their
# own, `source_gone`), so a candidate pin changes them by construction; an approval id and its time, and the record's
# own checksum, are fresh per approval.
VOLATILE: frozenset[str] = frozenset({
    "ts", "seq", "hash", "prev_hash", "trace_id", "run_id", "thread_id", "seconds", "tokens_in", "tokens_out",
    "created_at", "filed",
    "eval_key", "spec_key", "stage_key", "stage_keys", "fingerprint", "approval_id", "approved_at", "record_sha256",
})
# The audit actions inside an evaluation that are decisions (the `decisions` component). `eval.started`,
# `stage.done` and the write record are bookkeeping of the run, not decisions about the data.
DECISION_ACTIONS: tuple[str, ...] = ("account.mapped", "choice.applied", "row.excluded", "quality.cause")

# The components of a result, in the order the diff reports them.
COMPONENTS: tuple[str, ...] = ("output", "findings", "tie_out", "missing", "tally", "ids_issued", "decisions")

_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA = re.compile(r"^[0-9a-f]{64}$")


class NotBuilt(NotImplementedError):
    """Capability replay (asking live models again) is not built."""


class ReplayError(Exception):
    """A record or blob is malformed or unsafe to use."""


class TapeMiss(Exception):
    """The recorded run has no answer for a question the replay asked. It is a difference, never a live call."""


class CannotReplay(Exception):
    """The record cannot be replayed as it stands (an input is gone, a pin is no longer on disk, it depends on another
    evaluation). Counted as `cannot_replay`, never silently skipped."""


# ---- canonical form -------------------------------------------------------------------------------------------
def number_text(value: Decimal | float) -> str:
    """A number as a normalised fixed-point string; -0 is 0 and 1.50 is 1.5."""
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value))
        if not d.is_finite():
            return str(d)
        text = format(d.normalize(), "f")
    except InvalidOperation:
        return str(value)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _rel(path: PurePath, roots: Sequence[Path]) -> str:
    p = Path(path)
    for root in roots:
        try:
            return p.resolve().relative_to(Path(root).resolve()).as_posix()
        except ValueError:
            continue
    return f"outside-roots:{p.name}"  # a path outside the inbox and the output root is a difference


def normal(x: Any, roots: Sequence[Path] = ()) -> Any:
    """The canonical tree of `x`: volatile keys dropped at any depth, paths root-relative, numbers normalised,
    sets sorted, every sequence kept in order. Strings are data and are never changed."""
    if isinstance(x, Mapping):
        return {str(k): normal(v, roots) for k, v in x.items() if str(k) not in VOLATILE}
    if isinstance(x, (set, frozenset)):
        return sorted((normal(i, roots) for i in x), key=hashing.canonical_strict)
    if isinstance(x, (list, tuple)):
        return [normal(i, roots) for i in x]
    if isinstance(x, bool) or x is None or isinstance(x, str):
        return x
    if isinstance(x, (int, float, Decimal)):
        return number_text(x)
    if isinstance(x, (datetime, date, time)):
        return x.isoformat()
    if isinstance(x, PurePath):
        return _rel(x, roots)
    if isinstance(x, (bytes, bytearray)):
        return "bytes:" + hashlib.sha256(bytes(x)).hexdigest()
    return str(x)


def canonical(x: Any, roots: Sequence[Path] = ()) -> str:
    """Sorted-key, compact, UTF-8 JSON of the canonical tree."""
    return hashing.canonical_strict(normal(x, roots))


def digest(x: Any, roots: Sequence[Path] = ()) -> str:
    return hashlib.sha256(canonical(x, roots).encode("utf-8")).hexdigest()


# ---- output digests -------------------------------------------------------------------------------------------
def _typed(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "b:1" if value else "b:0"
    if isinstance(value, (int, float, Decimal)):
        return "n:" + number_text(value)
    if isinstance(value, (datetime, date)):
        return "d:" + value.isoformat()
    return "s:" + str(value)


class ContentHasher:
    """The content digest fed a row at a time (the engine feeds it the rows it streams into the upload file, so a
    million-line file is never read back). `cells_digest` is the same function over whole sheets."""

    def __init__(self, sheet: str | None = None) -> None:
        self._h = hashlib.sha256()
        self._pending: list[str] = []
        if sheet is not None:
            self.sheet(sheet)

    def sheet(self, name: str) -> None:
        self._pending = []  # blank rows at the end of a sheet do not count
        self._h.update(b"sheet\x00" + str(name).encode("utf-8") + b"\x00")

    def row(self, values: Iterable[Any]) -> None:
        cells = [_typed(v) for v in values]
        while cells and cells[-1] == "":
            cells.pop()
        if not cells:
            self._pending.append("[]")  # held back until a non-blank row follows
            return
        for p in self._pending:
            self._h.update(p.encode("utf-8") + b"\n")
        self._pending = []
        self._h.update(hashing.canonical_strict(cells).encode("utf-8") + b"\n")

    def hexdigest(self) -> str:
        return self._h.hexdigest()


def cells_digest(sheets: Iterable[tuple[str, Iterable[Sequence[Any]]]]) -> str:
    """The content digest: per sheet in order, the sheet name, then each row as typed cells. Trailing empty cells
    and trailing empty rows do not count, so a written file and the rows that were written give the same digest."""
    h = ContentHasher()
    for name, rows in sheets:
        h.sheet(name)
        for row in rows:
            h.row(row)
    return h.hexdigest()


def xlsx_content_digest(path: str | Path) -> str:
    """Typed cells per sheet in order, never file bytes (docProps and zip dates change on every save)."""
    return cells_digest(
        (name, (cells for _n, cells in columnar.rows(path, name))) for name in columnar.sheet_names(path))


def bytes_digest(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def output_facts(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    fmt = "xlsx" if p.suffix.lower() in (".xlsx", ".xlsm") else "csv"
    facts: dict[str, Any] = {"name": p.name, "format": fmt, "bytes_sha256": bytes_digest(p)}
    facts["content_sha256"] = xlsx_content_digest(p) if fmt == "xlsx" else facts["bytes_sha256"]
    return facts


# ---- the record -----------------------------------------------------------------------------------------------
@dataclass
class ReplayRecord:
    """One record per sealed evaluation, stored at `<root>/<sponsor>/<job_id>.json`."""

    job_id: str
    sponsor: str
    record_type: str
    entity: str = ""
    holdout: bool = False
    signed_off: dict[str, Any] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)
    inputs: list[dict[str, Any]] = field(default_factory=list)  # {ref, sha256, bytes, sheet, header_row}
    blobs: dict[str, Any] = field(default_factory=dict)
    approval: dict[str, Any] = field(default_factory=dict)
    expected: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)  # small source facts a replay needs (headers, columns)
    schema_version: int = SCHEMA_VERSION
    kind: str = "replay_record"

    def __post_init__(self) -> None:
        for name, value in (("sponsor", self.sponsor), ("job_id", self.job_id)):
            if not _SAFE.match(str(value)):
                raise ReplayError(f"{name} {value!r} cannot name a record file")

    @property
    def split(self) -> str:
        return f"{self.sponsor}/{self.record_type}"

    @property
    def family(self) -> str:
        return "gl" if self.record_type.upper().startswith("GL") else "static"

    @property
    def result_sha256(self) -> str:
        return result_digest(self.expected)

    def checksum(self) -> str:
        """sha256 over the whole record as stored (every key, the identities too): what the audit entry names."""
        return hashing.sha256_json(asdict(self))

    @property
    def ref(self) -> str:
        return f"{self.sponsor}/{self.job_id}.json"

    def path(self, root: str | Path) -> Path:
        return Path(root) / self.sponsor / f"{self.job_id}.json"

    def save(self, root: str | Path) -> Path:
        """Write the record atomically. An unwritable record raises; the caller refuses the upload."""
        target = self.path(root)
        tmp = target.with_suffix(".json.tmp")
        data = json.dumps(asdict(self), sort_keys=True, indent=1, ensure_ascii=False, default=str) + "\n"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise ReplayError(f"cannot write the replay record: {exc}") from exc
        return target

    @classmethod
    def load(cls, path: str | Path) -> ReplayRecord:
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReplayError(f"cannot read the replay record: {exc}") from exc
        if doc.get("kind") != "replay_record" or doc.get("schema_version") != SCHEMA_VERSION:
            raise ReplayError("not a replay record of a known schema version")
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in doc.items() if k in known})


# ---- blobs ----------------------------------------------------------------------------------------------------
class BlobStore:
    """Content-addressed files at `<root>/sha256/<2>/<62>`; the same bytes are stored once."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, checksum: str) -> Path:
        if not _SHA.match(checksum):
            raise ReplayError("a blob reference is a sha256 hex digest")
        return self.root / "sha256" / checksum[:2] / checksum[2:]

    def put(self, data: bytes) -> str:
        checksum = hashlib.sha256(data).hexdigest()
        target = self._path(checksum)
        if target.is_file():
            return checksum
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        return checksum

    def put_plain(self, obj: Any) -> str:
        """JSON as it is (numbers stay numbers, no key dropped): a tape plays back exactly what was answered."""
        return self.put(hashing.canonical(obj).encode("utf-8"))

    def get(self, checksum: str) -> bytes:
        try:
            data = self._path(checksum).read_bytes()
        except OSError as exc:
            raise ReplayError(f"blob {checksum[:12]} is missing") from exc
        if hashlib.sha256(data).hexdigest() != checksum:
            raise ReplayError(f"blob {checksum[:12]} does not match its checksum")
        return data

    def get_json(self, checksum: str) -> Any:
        return json.loads(self.get(checksum).decode("utf-8"))

    def count(self) -> int:
        base = self.root / "sha256"
        return sum(1 for p in base.rglob("*") if p.is_file() and not p.name.endswith(".tmp")) if base.is_dir() else 0


# ---- comparing ------------------------------------------------------------------------------------------------
def result_digest(components: Mapping[str, Any]) -> str:
    """sha256 over the component digests, so one value stands for the whole result."""
    return digest({name: digest(component_value(name, components.get(name))) for name in COMPONENTS})


def component_value(name: str, value: Any) -> Any:
    """What of a component takes part in "same": findings and missing are sets; everything else keeps its order."""
    if name == "output":
        out = dict(value or {})
        key = "content_sha256" if out.get("format") == "xlsx" else "bytes_sha256"
        return {"format": out.get("format"), key: out.get(key)}
    if name == "findings":
        return sorted((normal(f) for f in (value or [])), key=lambda f: (str(f.get("id", "")), hashing.canonical_strict(f)))
    if name == "missing":
        return sorted((normal(m) for m in (value or [])), key=lambda m: hashing.canonical_strict(m))
    return value if value is not None else ([] if name in ("ids_issued", "decisions") else {})


_WORDS = {
    "output": "the upload file",
    "findings": "the findings",
    "tie_out": "the tie-out",
    "missing": "the list of MISSING fields",
    "tally": "the tally",
    "ids_issued": "the IDs issued",
    "decisions": "the audit decisions",
}


def _first_difference(name: str, was: Any, now: Any) -> str:
    label = _WORDS[name]
    if name == "output":
        return f"{label} differs: the recorded content was {str(was)[:80]} and the replay made {str(now)[:80]}."
    if isinstance(was, list) and isinstance(now, list):
        for i, (a, b) in enumerate(zip(was, now, strict=False)):
            if a != b:
                where = f"item {i + 1}"
                if isinstance(a, dict) and isinstance(b, dict):
                    keys = sorted(set(a) | set(b))
                    k = next((k for k in keys if a.get(k) != b.get(k)), None)
                    if k is not None:
                        ident = a.get("id") or a.get("field") or a.get("action") or where
                        return f"{label} differ at {ident}: {k} was {str(a.get(k))[:80]} and is now {str(b.get(k))[:80]}."
                return f"{label} differ at {where}: {str(a)[:80]} became {str(b)[:80]}."
        if len(was) != len(now):
            return f"{label} differ in count: {len(was)} were recorded and the replay has {len(now)}."
    if isinstance(was, dict) and isinstance(now, dict):
        k = next((k for k in sorted(set(was) | set(now)) if was.get(k) != now.get(k)), None)
        if k is not None:
            return f"{label} differ at {k}: it was {str(was.get(k))[:80]} and is now {str(now.get(k))[:80]}."
    return f"{label} differ: {str(was)[:80]} became {str(now)[:80]}."


@dataclass(frozen=True)
class ReplayDiff:
    job_id: str
    status: str  # identical | different | cannot_replay
    component: str = ""
    first_difference: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def identical(self) -> bool:
        return self.status == "identical"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def diff(expected: Mapping[str, Any], actual: Mapping[str, Any], job_id: str = "") -> ReplayDiff:
    """Compare two results component by component in table order and name the first that differs."""
    for name in COMPONENTS:
        if name not in expected and name not in actual:
            continue
        was = normal(component_value(name, expected.get(name)))
        now = normal(component_value(name, actual.get(name)))
        if was != now:
            return ReplayDiff(job_id, "different", name, _first_difference(name, was, now),
                              {"expected_sha256": digest(was), "actual_sha256": digest(now)})
    return ReplayDiff(job_id, "identical", detail={"result_sha256": result_digest(expected)})


# ---- the held-out set -----------------------------------------------------------------------------------------
def assign_holdout(sponsor: str, job_id: str, holdout_pct: int) -> bool:
    """Fixed at record time: sha256(sponsor|job_id) % 100 < holdout_pct."""
    if not 0 <= int(holdout_pct) <= 100:
        raise ValueError("holdout_pct is a percentage from 0 to 100")
    h = hashlib.sha256(f"{sponsor}|{job_id}".encode()).hexdigest()
    return int(h[:8], 16) % 100 < int(holdout_pct)


def replay_controls(skill: str, skills_root: str | Path | None = None) -> dict[str, Any]:
    """A skill's `controls.yaml` as written (the `replay:` section is what replay reads). {} when the skill or the
    file is absent or unreadable, so the evidence minimum fails closed rather than taking a default."""
    import yaml

    from worker.skills import skills_dir

    if not re.fullmatch(r"[a-z][a-z0-9-]{0,40}", skill):
        return {}
    p = Path(skills_root or skills_dir()) / skill / "controls.yaml"
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except (OSError, ValueError, yaml.YAMLError):
        return {}
    return dict(doc) if isinstance(doc, Mapping) else {}


def holdout_pct_in_force(skills: Iterable[str], skills_root: str | Path | None = None) -> int | None:
    """The held-out percentage a new record is assigned under: the largest `replay.holdout_pct` that the job's pinned
    skills set (holding more out is the stricter evidence). None when no skill sets one: no job is held out, and the
    record says so."""
    found: list[int] = []
    for s in skills:
        section = replay_controls(s, skills_root).get("replay")
        v = section.get("holdout_pct") if isinstance(section, Mapping) else None
        if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100:
            found.append(v)
    return max(found) if found else None


# ---- the tapes ------------------------------------------------------------------------------------------------
# What the engine asks the world. `commit` is never on a tape and never played back.
WORLD_QUESTIONS: tuple[str, ...] = ("store", "required", "known_account", "account_entries", "golden_accounts",
                                    "id_scheme", "counter", "reserve_ids", "fingerprint")
_RAISES: dict[str, type[Exception]] = {"ValueError": ValueError, "KeyError": KeyError, "LookupError": LookupError,
                                       "TypeError": TypeError, "OSError": OSError}


def _plainish(x: Any) -> Any:
    """Plain JSON values of what a port was asked or answered (dataclasses, sets and paths included)."""
    if is_dataclass(x) and not isinstance(x, type):
        return {k: _plainish(v) for k, v in asdict(x).items()}
    if isinstance(x, Mapping):
        return {str(k): _plainish(v) for k, v in x.items()}
    if isinstance(x, (set, frozenset)):
        return sorted((_plainish(v) for v in x), key=lambda v: json.dumps(v, sort_keys=True, default=str))
    if isinstance(x, (list, tuple)):
        return [_plainish(v) for v in x]
    if x is None or isinstance(x, (str, int, float, bool)):
        return x
    if isinstance(x, (Decimal, PurePath)):
        return str(x)
    if hasattr(x, "__dict__"):
        return {k: _plainish(v) for k, v in vars(x).items() if not k.startswith("_")}
    return str(x)


def _plain_dumps(x: Any) -> str:
    return json.dumps(_plainish(x), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _json_copy(x: Any) -> Any:
    return json.loads(_plain_dumps(x))


def _world_key(name: str, args: Sequence[Any], kw: Mapping[str, Any]) -> str:
    """The question as a lookup key. A reservation's key (the evaluation it is for) is not part of it: the same
    rows under a candidate pin ask for the same IDs."""
    a, k = list(args), dict(kw)
    if name == "reserve_ids":
        if len(a) >= 3:
            a.pop(2)
        k.pop("key", None)
    return _plain_dumps([name, a, k])


class _Segment:
    """What one stage (or the run around the stages) asked and decided."""

    def __init__(self) -> None:
        self.world: list[dict[str, Any]] = []
        self.seen: set[str] = set()
        self.results: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []

    def as_dict(self) -> dict[str, Any]:
        return {"world": self.world, "results": self.results, "decisions": self.decisions}

    @classmethod
    def of(cls, doc: Mapping[str, Any]) -> _Segment:
        seg = cls()
        seg.world = [dict(e) for e in doc.get("world") or []]
        seg.seen = {str(e.get("key")) for e in seg.world}
        seg.results = [dict(e) for e in doc.get("results") or []]
        seg.decisions = [dict(e) for e in doc.get("decisions") or []]
        return seg


class TapeSink:
    """The tapes of one evaluation, kept per stage so that a sealed stage, reused by a later call (the write after
    the check), brings its own answers along. The engine stores a stage's tape beside the stage."""

    def __init__(self) -> None:
        self.run = _Segment()
        self.stages: dict[str, _Segment] = {}
        self.current = self.run
        self.missing: list[str] = []  # sealed stages whose tape was not stored (sealed before records existed)

    def begin(self, stage: str) -> None:
        self.current = self.stages[stage] = _Segment()

    def end(self, stage: str) -> dict[str, Any]:
        seg = self.stages.get(stage) or _Segment()
        self.current = self.run
        return seg.as_dict()

    def load(self, stage: str, doc: Mapping[str, Any] | None) -> None:
        if doc is None:
            self.missing.append(stage)
            self.stages[stage] = _Segment()
        else:
            self.stages[stage] = _Segment.of(doc)

    def world(self, name: str, args: Sequence[Any], kw: Mapping[str, Any], *, answer: Any = None,
              raises: BaseException | None = None) -> None:
        key = _world_key(name, args, kw)
        seg = self.current
        if key in seg.seen:
            return
        seg.seen.add(key)
        entry: dict[str, Any] = {"key": key, "q": name}
        if raises is not None:
            entry["raises"] = {"type": type(raises).__name__, "message": str(raises)[:300]}
        else:
            entry["a"] = _json_copy(answer)
        seg.world.append(entry)

    def result(self, entry: Mapping[str, Any]) -> None:
        self.current.results.append(dict(entry))

    def decision(self, action: str, rationale: str, inputs: Any, outputs: Any) -> None:
        if action in DECISION_ACTIONS:
            self.current.decisions.append({"action": action, "rationale": str(rationale),
                                           "inputs": _json_copy(inputs or {}), "outputs": _json_copy(outputs or {})})

    def _segments(self, order: Sequence[str]) -> list[_Segment]:
        return [self.run, *(self.stages[s] for s in order if s in self.stages)]

    def world_tape(self, order: Sequence[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for seg in self._segments(order):
            for e in seg.world:
                if e["key"] not in seen:
                    seen.add(e["key"])
                    out.append(e)
        return out

    def results_tape(self, order: Sequence[str]) -> list[dict[str, Any]]:
        return [r for seg in self._segments(order) for r in seg.results]

    def decisions(self, order: Sequence[str]) -> list[dict[str, Any]]:
        return [d for seg in (*(self.stages[s] for s in order if s in self.stages), self.run) for d in seg.decisions]


class RecordingWorld:
    """The engine's `World`, with every question and its answer put on the tape (each distinct question once per
    stage). Everything else, `commit` included, goes straight through."""

    def __init__(self, inner: Any, sink: TapeSink) -> None:
        self._inner = inner
        self._sink = sink

    @property
    def inner(self) -> Any:
        return self._inner

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)  # AttributeError: the wrapped world does not have it either
        if name not in WORLD_QUESTIONS or not callable(attr):
            return attr
        sink = self._sink

        def asked(*args: Any, **kw: Any) -> Any:
            try:
                answer = attr(*args, **kw)
            except Exception as exc:
                sink.world(name, args, kw, raises=exc)
                raise
            sink.world(name, args, kw, answer=answer)
            return answer

        return asked


class TapeWorld:
    """The world as recorded: every question is answered from the tape, and one that is not on it raises `TapeMiss`
    (a difference, never a look at the live stores). `commit` refuses: replay moves no counter and no store."""

    def __init__(self, entries: Iterable[Mapping[str, Any]], sponsor: str = "") -> None:
        self.sponsor = sponsor
        self._answers = {str(e["key"]): dict(e) for e in entries}
        self.asked: list[str] = []

    def _ask(self, name: str, args: Sequence[Any], kw: Mapping[str, Any]) -> Any:
        key = _world_key(name, args, kw)
        e = self._answers.get(key)
        if e is None:
            shown = _plain_dumps(list(args))[:120]
            raise TapeMiss(f"the world was asked {name}({shown}) and the recorded job never asked that")
        self.asked.append(name)
        if "raises" in e:
            raise _RAISES.get(str(e["raises"].get("type")), ReplayError)(str(e["raises"].get("message", "")))
        return json.loads(json.dumps(e.get("a")))  # a fresh copy: the engine may change what it is given

    def store(self, *a: Any, **k: Any) -> Any:
        return self._ask("store", a, k)

    def required(self, *a: Any, **k: Any) -> Any:
        return self._ask("required", a, k)

    def known_account(self, *a: Any, **k: Any) -> Any:
        return self._ask("known_account", a, k)

    def account_entries(self, *a: Any, **k: Any) -> Any:
        return self._ask("account_entries", a, k)

    def golden_accounts(self, *a: Any, **k: Any) -> Any:
        return self._ask("golden_accounts", a, k)

    def id_scheme(self, *a: Any, **k: Any) -> Any:
        return self._ask("id_scheme", a, k)

    def counter(self, *a: Any, **k: Any) -> Any:
        return self._ask("counter", a, k)

    def reserve_ids(self, *a: Any, **k: Any) -> Any:
        return self._ask("reserve_ids", a, k)

    def fingerprint(self, *a: Any, **k: Any) -> Any:
        return self._ask("fingerprint", a, k)

    def commit(self, *a: Any, **k: Any) -> None:
        raise ReplayError("replay never commits: no counter, store or account moves")


def _input_checksum(keys: Any) -> str:
    return hashlib.sha256(_plain_dumps(list(keys)).encode("utf-8")).hexdigest()


class RecordingRanker:
    """The agents port of the engine (the ranker, role `small`): every Result it returns goes on the results tape,
    keyed (unit_id, role, input_checksums, occurrence). Code verification of the pick stays the engine's."""

    role = "small"

    def __init__(self, inner: Any, sink: TapeSink, unit_id: str) -> None:
        self._inner = inner
        self._sink = sink
        self.unit_id = unit_id
        self.model = getattr(inner, "model", "") or ""
        self.prompt_checksum = getattr(inner, "prompt_checksum", "") or ""
        self._occ: Counter[str] = Counter()

    def rank(self, keys: Sequence[Any]) -> Mapping[str, str]:
        inp = _input_checksum(keys)
        self._occ[inp] += 1
        out = self._inner.rank(keys)
        plain = _json_copy(dict(out))
        self._sink.result({"unit_id": self.unit_id, "role": self.role, "input_checksums": [inp],
                           "occurrence": self._occ[inp], "model_id": self.model or "unnamed",
                           "prompt_checksum": self.prompt_checksum, "output": plain,
                           "output_checksum": hashlib.sha256(_plain_dumps(plain).encode()).hexdigest()})
        return out


class TapeRanker:
    """The recorded Results played back. The prompt is not in the key (a know-how edit replays the recorded answer);
    an answer that is not on the tape is `TapeMiss`. There is no model here."""

    role = "small"

    def __init__(self, entries: Iterable[Mapping[str, Any]], unit_id: str, model: str = "") -> None:
        self.unit_id = unit_id
        self.model = model
        self._by = {(str(e["unit_id"]), str(e["role"]), tuple(e["input_checksums"]), int(e["occurrence"])): e
                    for e in entries}
        self._occ: Counter[str] = Counter()

    def rank(self, keys: Sequence[Any]) -> Mapping[str, str]:
        inp = _input_checksum(keys)
        self._occ[inp] += 1
        e = self._by.get((self.unit_id, self.role, (inp,), self._occ[inp]))
        if e is None:
            raise TapeMiss(f"no recorded answer for unit {self.unit_id}, role {self.role}, input {inp[:12]}, "
                           f"occurrence {self._occ[inp]}")
        return {str(k): str(v) for k, v in dict(e["output"]).items()}


# ---- writing a record ------------------------------------------------------------------------------------------
def default_root() -> Path:
    """Where records live when nothing else says: `WORKER_REPLAY_ROOT`, else `state/replay`."""
    return Path(os.environ.get("WORKER_REPLAY_ROOT") or Path("state") / "replay")


def safe_name(text: str) -> str:
    """A job id or sponsor as a record file name: unsafe characters become `_`."""
    s = re.sub(r"[^A-Za-z0-9._-]", "_", str(text)) or "job"
    return s if _SAFE.match(s) else f"j{s}"


def save_record(root: str | Path, record: ReplayRecord) -> tuple[Path, str]:
    """Write the record under a free name (an earlier record of the same job is evidence: it is never overwritten)
    and list its inputs in `retained.jsonl` (inbox clean-up must keep them). Raises `ReplayError` when either cannot
    be written; the engine then refuses the upload."""
    root = Path(root)
    base, n = record.job_id, 1
    while record.path(root).exists():
        n += 1
        record.job_id = f"{base}-{n}"
    path = record.save(root)
    checksum = record.checksum()
    line = json.dumps({"record": record.ref, "record_sha256": checksum,
                       "inputs": [{"ref": i.get("ref"), "root": i.get("root"), "sha256": i.get("sha256")}
                                  for i in record.inputs]}, sort_keys=True, ensure_ascii=False) + "\n"
    try:
        with open(root / "retained.jsonl", "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise ReplayError(f"cannot list the record's inputs as retained: {exc}") from exc
    return path, checksum


def skill_trees(blobs: BlobStore, skills: Iterable[str], sponsor: str = "") -> dict[str, str]:
    """skill -> the blob of its tree (relative path -> file sha256), the sponsor's overlay files included; each file
    is a blob too, stored once across jobs."""
    from worker.skills import skills_dir
    from worker.templates import SPONSOR_RE, sponsors_dir

    out: dict[str, str] = {}
    base = skills_dir()
    sponsor_root = sponsors_dir() / sponsor if sponsor and SPONSOR_RE.match(sponsor) else None
    for name in sorted(skills):
        folder = base / name
        if not folder.is_dir():
            continue
        tree: dict[str, str] = {}
        for p in sorted(q for q in folder.rglob("*") if q.is_file() and "__pycache__" not in q.parts):
            tree[p.relative_to(folder).as_posix()] = blobs.put(p.read_bytes().replace(b"\r\n", b"\n"))
        if sponsor_root is not None:
            place = sponsor_root / "skills" / name
            if place.is_dir():
                for p in sorted(q for q in place.rglob("*") if q.is_file()):
                    rel = f"sponsor/{place.name}/{p.relative_to(place).as_posix()}"
                    tree[rel] = blobs.put(p.read_bytes().replace(b"\r\n", b"\n"))
        out[name] = blobs.put_plain(tree)
    return out


# ---- the driver -----------------------------------------------------------------------------------------------
Run = Callable[[ReplayRecord, Path], Mapping[str, Any]]


class Differs(Exception):
    """The replay ran and the job came out differently in a way that has no component digest (the engine refused it
    under these pins, for example). A difference, with the component named."""

    def __init__(self, component: str, sentence: str) -> None:
        super().__init__(sentence)
        self.component = component


def _is_answer_key(ref: str) -> bool:
    parts = re.split(r"[\\/]", ref)
    return any(p.lower() == "answer_key" for p in parts) or ref.lower().endswith(".answer.json")


def _refused(record: ReplayRecord) -> str | None:
    base = Path(tempfile.gettempdir()).resolve() / "replay-inbox"
    for item in record.inputs:
        ref = str(item.get("ref", ""))
        if _is_answer_key(ref):
            return f"{ref}: answer keys are not readable by the worker"
        if Path(ref).is_absolute():
            continue  # read under its own recorded root, through the path guard, at replay
        try:
            check_path(base / ref, [base])
        except PathRefused as exc:
            return f"{item.get('ref')}: {exc}"
    return None


def _input_path(item: Mapping[str, Any], inbox_root: Path | None) -> Path | None:
    root = inbox_root if inbox_root is not None else (Path(item["root"]) if item.get("root") else None)
    return None if root is None else root / str(item["ref"])


def _inputs_gone(record: ReplayRecord, inbox_root: Path | None) -> str | None:
    for item in record.inputs:
        if item.get("kind", "file") != "file":
            continue
        path = _input_path(item, inbox_root)
        if path is None:
            return "no inbox root was given, so the inputs cannot be checked"
        if not path.is_file():
            return f"{item['ref']} is no longer there"
        if bytes_digest(path) != item.get("sha256"):
            return f"{item['ref']} has changed since the job was recorded"
    return None


def replay_job(
    record: ReplayRecord,
    run: Run | None = None,
    *,
    mode: str = "regression",
    inbox_root: str | Path | None = None,
    audit: AuditLog | None = None,
    reason: str = "replay of a recorded job",
    root: str | Path | None = None,
) -> ReplayDiff:
    """Recompute a recorded job and compare the result with `record.expected`.

    `run(record, workdir)` returns the components of the result (same shape as `expected`). Without one, the engine
    replays it (`EngineRun` over the blobs under `root`). A tape miss, or the engine refusing the job under its
    pins, is a difference; a missing input or a pin no longer on disk is `cannot_replay`, counted, never skipped.
    Replay writes only inside its own temporary directory, and its own `replay.*` audit entries.
    """
    if mode == "capability":
        raise NotBuilt("capability replay (asking live models again) is not built")
    if mode != "regression":
        raise ValueError(f"unknown replay mode {mode!r}")
    if run is None:
        run = EngineRun(root if root is not None else default_root(), inbox_root=inbox_root)
    refused = _refused(record)
    if refused:
        if audit is not None:
            audit.record("replay.refused", "a record that names an answer key is not replayed",
                         {"job_id": record.job_id}, {"why": refused})
        return ReplayDiff(record.job_id, "cannot_replay", "inputs", f"Replay refused: {refused}.",
                          {"refused": True})
    gone = _inputs_gone(record, Path(inbox_root) if inbox_root is not None else None)
    if gone:
        result = ReplayDiff(record.job_id, "cannot_replay", "inputs", f"source_gone: {gone}.",
                            {"reason": "source_gone"})
    else:
        work = Path(tempfile.mkdtemp(prefix="replay-"))
        try:
            try:
                actual = run(record, work)
            except TapeMiss as exc:
                result = ReplayDiff(record.job_id, "different", "tape", f"tape_miss: {exc}.",
                                    {"reason": "tape_miss"})
            except CannotReplay as exc:
                result = ReplayDiff(record.job_id, "cannot_replay", "replay", f"cannot_replay: {exc}.",
                                    {"reason": str(exc).split(":", 1)[0]})
            except Differs as exc:
                result = ReplayDiff(record.job_id, "different", exc.component, f"{exc}.",
                                    {"reason": exc.component})
            else:
                result = diff(record.expected, actual, record.job_id)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    if audit is not None:
        audit.record("replay.run", reason, {"job_id": record.job_id, "mode": mode},
                     {"status": result.status, "component": result.component,
                      "diff_sha256": digest(result.as_dict())})
    return result


@contextlib.contextmanager
def _skills_at(path: Path | None) -> Iterator[None]:
    """Point the skill loader at a candidate's folder for one replay (not thread-safe: replay is a release tool)."""
    if path is None:
        yield
        return
    old = os.environ.get("WORKER_SKILLS_DIR")
    os.environ["WORKER_SKILLS_DIR"] = str(path)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("WORKER_SKILLS_DIR", None)
        else:
            os.environ["WORKER_SKILLS_DIR"] = old


class _ReplaySkills:
    """The strict loader, with the Golden chart's version taken from the record: the version is the sponsor's
    account store (world state, on the tape), the chart file itself must still be the one pinned."""

    def __init__(self, sponsor: str, golden_coa: str) -> None:
        from worker.ports import ProductionSkills

        self._real = ProductionSkills(sponsor, env={})
        self.golden_coa = golden_coa

    def __call__(self, pins: Any = None) -> Any:
        from dataclasses import replace

        got = self._real(pins)
        if self.golden_coa and got.golden_coa.split(":", 1)[-1] == self.golden_coa.split(":", 1)[-1]:
            got = replace(got, golden_coa=self.golden_coa)
        return got


class EngineRun:
    """`run` for `replay_job`: rebuild the job from its record and evaluate it with `engine.evaluate` to the check
    stage, over the tapes (`TapeWorld`, `TapeRanker`), an in-memory audit and spool, and the would-be upload rendered
    by the same writer into the replay's own temporary folder. No Approval, no write stage, no commit.

    `skills_dir` points the loader at a candidate's skills folder; `swap` names the one skill whose pin the
    candidate replaces (its findings are compared under the recorded pin); `withhold` drops the human choices of
    those kinds (the unaided run)."""

    def __init__(self, root: str | Path, *, inbox_root: str | Path | None = None, skills_dir: str | Path | None = None,
                 swap: str = "", withhold: Sequence[str] = ()) -> None:
        self.root = Path(root)
        self.inbox_root = Path(inbox_root) if inbox_root is not None else None
        self.skills_dir = Path(skills_dir) if skills_dir is not None else None
        self.swap = swap
        self.withhold = tuple(withhold)

    def spec_of(self, record: ReplayRecord) -> tuple[Any, Any]:
        """(JobSpec, Ports) of the record."""
        from worker import engine as E
        from worker.ports import FileReader, ListAudit, MemoryStore, Ports

        blobs = BlobStore(self.root / "blobs")
        b, m = record.blobs, record.manifest
        try:
            recipe = blobs.get_json(b["recipe"]["blob"]) if b.get("recipe") else None
            choices = blobs.get_json(b["choices"]["blob"])
            world = blobs.get_json(b["world_tape"])
            results = blobs.get_json(b["results_tape"])
        except (KeyError, TypeError, ReplayError, ValueError) as exc:
            raise CannotReplay(f"blobs_missing: the record's blobs are not all there ({exc})") from exc
        roots: list[Path] = []
        sources = []
        for item in record.inputs:
            if item.get("kind", "file") != "file":
                raise CannotReplay("depends_on_evaluation: the job read another evaluation, not a file")
            path = _input_path(item, self.inbox_root)
            if path is None:
                raise CannotReplay("source_gone: no inbox root for the inputs")
            root = path.parent if Path(str(item["ref"])).is_absolute() else (
                self.inbox_root if self.inbox_root is not None else Path(item["root"]))
            if root not in roots:
                roots.append(root)
            sources.append(E.SourceRef("file", str(item["ref"]), item.get("sheet"), item.get("header_row"),
                                       str(item.get("sha256") or "")))
        spec_m = m.get("spec") or {}
        pins = m.get("pins") or {}
        skills = {k: v for k, v in (pins.get("skills") or {}).items() if k != self.swap}
        given = E.PinSet(skills=skills, target=str(pins.get("target") or ""),
                         controls="" if self.swap else str(pins.get("controls") or ""),
                         golden_coa=str(pins.get("golden_coa") or ""))
        made = [E.Choice(**c) for c in choices if c.get("kind") not in self.withhold]
        spec = E.JobSpec(
            str(spec_m.get("job_id") or record.job_id), str(spec_m.get("sponsor") or record.sponsor),
            record.record_type, spec_m.get("entity"), tuple(sources), given,
            E.Recipe(str(recipe["checksum"]), tuple(recipe["bindings"])) if recipe else None, tuple(made),
            dict(spec_m.get("context") or {}))
        models = dict(pins.get("models") or {})
        ranker = TapeRanker(results, spec.job_id, models["small"]) if "small" in models else None
        ports = Ports(world=TapeWorld(world, spec.sponsor), reader=FileReader(roots), audit=ListAudit(),
                      store=MemoryStore(), out_dir=Path(), skills=_ReplaySkills(spec.sponsor,
                                                                               str(pins.get("golden_coa") or "")),
                      ranker=ranker)
        return spec, ports

    def __call__(self, record: ReplayRecord, work: Path) -> dict[str, Any]:
        from dataclasses import replace

        from worker import engine as E

        spec, ports = self.spec_of(record)
        ports = replace(ports, out_dir=Path(work))
        try:
            with _skills_at(self.skills_dir):
                ev, rendered, decisions = E.replay_evaluate(spec, ports)
        except E.Refused as exc:
            if exc.code in ("source_changed", "path_refused"):
                raise CannotReplay(f"source_gone: {exc.sentence}") from exc
            if not self.swap and exc.code in ("schema_invalid", "control_out_of_bounds") and (
                    "pinned" in exc.sentence or "changed since" in exc.sentence):
                raise CannotReplay(f"pins_changed: {exc.sentence}") from exc
            raise Differs("refused", f"the engine refused the job: {exc.code}: {exc.sentence}") from exc
        except E.StageFailed as exc:
            raise CannotReplay(f"stage_failed: {exc}") from exc
        out = E.result_components(ev, rendered, decisions)
        if self.swap:
            recorded = str(((record.manifest.get("pins") or {}).get("skills") or {}).get(self.swap, ""))
            for f in out["findings"]:
                if f.get("skill") == self.swap:
                    f["pin"] = recorded  # the same rule under the candidate version is the same finding
        out["_outcome"] = ev.outcome
        return out


# ---- the engine release check ----------------------------------------------------------------------------------
@dataclass
class ReleaseReport:
    engine: str
    total: int = 0
    identical: int = 0
    different: list[dict[str, Any]] = field(default_factory=list)
    accepted: list[dict[str, Any]] = field(default_factory=list)
    cannot_replay: list[dict[str, Any]] = field(default_factory=list)
    invalid_accepted: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """No difference a human has not accepted, and at least one record replayed when any is retained."""
        replayed = self.identical + len(self.accepted)
        return not self.different and (self.total == 0 or replayed > 0)

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "passed": self.passed}


def records_under(root: str | Path) -> Iterator[tuple[Path, ReplayRecord | None]]:
    """Every retained record (`<root>/<sponsor>/<job>.json`); None for one that cannot be read."""
    base = Path(root)
    if not base.is_dir():
        return
    for p in sorted(base.glob("*/*.json")):
        if p.parent.name in ("blobs", "accepted"):
            continue
        try:
            yield p, ReplayRecord.load(p)
        except (ReplayError, TypeError) as exc:
            del exc
            yield p, None


def accepted_differences(root: str | Path, engine: str) -> tuple[list[dict[str, Any]], list[str]]:
    """The differences a human listed for this engine version in `<root>/accepted/<engine>.json`, and why any entry
    is not used. Each entry needs `job_id`, `component`, `new_digest`, a person in `by` and a `reason`."""
    from worker.improver import reserved_name

    if not re.fullmatch(r"[A-Za-z0-9._-]+", engine):
        return [], [f"engine version {engine!r} cannot name a file"]
    path = Path(root) / "accepted" / f"{engine}.json"
    if not path.is_file():
        return [], []
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], [f"{path.name} cannot be read: {exc}"]
    items = doc.get("accepted", doc) if isinstance(doc, Mapping) else doc
    good, bad = [], []
    for i, e in enumerate(items if isinstance(items, list) else []):
        need = ("job_id", "component", "new_digest", "by", "reason")
        if not isinstance(e, Mapping) or any(not str(e.get(k, "")).strip() for k in need):
            bad.append(f"entry {i + 1} needs {', '.join(need)}")
        elif reserved_name(str(e["by"])):
            bad.append(f"entry {i + 1}: {e['by']} is not a person; only a person accepts a difference")
        else:
            good.append(dict(e))
    return good, bad


def replay_all(root: str | Path | None = None, *, engine: str | None = None, run: Run | None = None,
               inbox_root: str | Path | None = None, audit: AuditLog | None = None) -> ReleaseReport:
    """The engine release check: replay every retained record under the engine as it is now, every other pin kept.
    Any difference fails the release unless a human listed it (job, component, new digest, reason) in
    `<root>/accepted/<engine>.json`; an accepted difference is appended to the record's `history` (its `expected` is
    never rewritten). A record that cannot be replayed is counted and named."""
    from worker.engine import ENGINE_VERSION

    base = Path(root) if root is not None else default_root()
    version = engine or ENGINE_VERSION
    report = ReleaseReport(version)
    accepted, report.invalid_accepted = accepted_differences(base, version)
    runner = run or EngineRun(base, inbox_root=inbox_root)
    for path, rec in records_under(base):
        report.total += 1
        if rec is None:
            report.cannot_replay.append({"record": path.name, "reason": "unreadable"})
            continue
        d = replay_job(rec, runner, inbox_root=inbox_root, audit=audit, reason=f"release check of {version}",
                       root=base)
        if d.identical:
            report.identical += 1
            continue
        if d.status == "cannot_replay":
            report.cannot_replay.append({"record": rec.ref, "reason": d.first_difference})
            continue
        new = str(d.detail.get("actual_sha256", ""))
        hit = next((a for a in accepted if a["job_id"] == rec.job_id and a["component"] == d.component
                    and new and a["new_digest"] == new), None)
        if hit is None:
            report.different.append({"record": rec.ref, **d.as_dict()})
            continue
        entry = {"engine": version, "component": d.component, "new_digest": new,
                 "accepted_by": str(hit["by"]).strip(), "reason": str(hit["reason"]).strip()}
        if entry not in rec.history:
            if audit is not None:  # the record of the acceptance comes first
                audit.record("replay.accepted", entry["reason"], {"record": rec.ref, "engine": version},
                             {"component": d.component, "new_digest": new, "by": entry["accepted_by"]})
            rec.history.append(entry)
            rec.save(base)
        report.accepted.append({"record": rec.ref, **entry})
    if audit is not None:
        audit.record("replay.release_check", f"engine {version}: {'passed' if report.passed else 'failed'}",
                     {"engine": version, "root": base.name},
                     {"total": report.total, "identical": report.identical, "different": len(report.different),
                      "accepted": len(report.accepted), "cannot_replay": len(report.cannot_replay),
                      "passed": report.passed})
    return report



def main(argv: Sequence[str] | None = None) -> int:
    """`python -m worker.replay --all [--root state/replay] [--engine engine-2] [--inbox DIR]`: the release check."""
    import argparse

    p = argparse.ArgumentParser(prog="worker.replay", description="Replay every retained record (release check).")
    p.add_argument("--all", action="store_true", required=True)
    p.add_argument("--root", default=None)
    p.add_argument("--engine", default=None)
    p.add_argument("--inbox", default=None)
    a = p.parse_args(argv)
    root = Path(a.root) if a.root else default_root()
    try:
        audit = persist.audit(root / "replay_audit.jsonl")
        report = replay_all(root, engine=a.engine, inbox_root=a.inbox, audit=audit)
    except AuditError as exc:
        print(f"the release check could not be recorded: {exc}")
        return 2
    print(f"engine {report.engine}: {report.total} record(s), {report.identical} identical, "
          f"{len(report.different)} different, {len(report.accepted)} accepted, "
          f"{len(report.cannot_replay)} cannot be replayed: {'PASSED' if report.passed else 'FAILED'}")
    for d in report.different[:20]:
        print(f"  {d['record']}: {d['first_difference']}")
    return 0 if report.passed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
