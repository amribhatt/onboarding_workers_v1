"""The seam of the engine: the only ways `worker.engine.evaluate` touches the world.

Each port is a small `Protocol` with a production implementation (files, the sponsor's stores, the audit log, the
strict skill loader) and a fake (in memory, scriptable to fail; the larger ones are `tests/port_fakes.py`). The engine never opens a path, a store
or a log by any other route.

* `World`    the sponsor's records, account knowledge, ID counters; `commit` is the only way they move.
* `Reader`   a source file as a stream of rows, its fingerprint and a readable local path.
* `AuditSink` where every decision is recorded first; a sink that cannot write makes the engine refuse.
* `Store`    checkpoints and the spool: append-only bytes under a key, sealed when a stage is done.
* `Ranker`   optional model ranking of account candidates; it proposes, code decides.
* skills     the pinned rule files, loaded and validated, with their checksums.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from worker import persist
from worker.audit import AuditError
from worker.fsutil import atomic_write_text
from worker.guard import PathRefused, check_path
from worker.hashing import sha256_file
from worker.idgen import Reservation

if TYPE_CHECKING:
    from worker.engine import PinSet, SourceRef


# ---- the ports -------------------------------------------------------------------------------------------
class World(Protocol):
    """What the sponsor already has, and the one place it changes (`commit`)."""

    sponsor: str

    def store(self, record_type: str) -> Sequence[Mapping[str, Any]]: ...
    def required(self, record_type: str, field_name: str, default: bool) -> bool: ...
    def known_account(self, source_account: str, transaction_type: str) -> str: ...
    def account_entries(self) -> Mapping[str, Mapping[str, Any]]: ...
    def golden_accounts(self) -> Mapping[str, Mapping[str, Any]]: ...
    def id_scheme(self, record_type: str) -> Mapping[str, Any] | None: ...
    def counter(self, record_type: str) -> int: ...
    def reserve_ids(self, record_type: str, n: int, key: str, taken: Sequence[str] = ()) -> Sequence[str]: ...
    def fingerprint(self, record_types: Sequence[str]) -> str: ...
    def commit(self, job_id: str, issued: Sequence[str], records: Sequence[Mapping[str, Any]], *,
               record_type: str = "", key: str = "", accounts: Mapping[str, str] | None = None,
               batch: str = "", reason: str = "") -> None: ...


class Reader(Protocol):
    def fingerprint(self, ref: SourceRef) -> str: ...
    def path(self, ref: SourceRef) -> Path: ...
    def rows(self, ref: SourceRef) -> Iterator[tuple[int, list[Any]]]: ...


class AuditSink(Protocol):
    def record(self, action: str, rationale: str, inputs: dict | None = None, outputs: dict | None = None) -> dict: ...
    def record_many(self, action: str, items: list[tuple[str, dict | None, dict | None]]) -> list[dict]: ...


class Store(Protocol):
    def get(self, key: str) -> bytes | None: ...
    def append(self, key: str, chunk: bytes) -> None: ...
    def lines(self, key: str, skip: int = 0) -> Iterator[bytes]: ...
    def reset(self, key: str) -> None: ...
    def truncate(self, key: str, keep_lines: int) -> None: ...
    def seal(self, key: str, digest: str) -> None: ...
    def sealed(self, key: str) -> str | None: ...


class Ranker(Protocol):
    def rank(self, keys: Sequence[Any]) -> Mapping[str, str]: ...


@dataclass(frozen=True)
class Progress:
    """What the UI and `RunManager` see while a job runs."""

    stage: str
    rows: int
    note: str = ""


@dataclass(frozen=True)
class LoadedRules:
    """The pinned rule files as loaded and validated: checksums (sponsor overlay folded in) and what the stages
    need to run. `limits` holds the engine's own controls (`max_keys`: unset = no cap)."""

    skills: Mapping[str, str]
    controls: str
    schema_versions: Mapping[str, int]
    golden_coa: str
    quality_rules: tuple[Any, ...] = ()
    rule_index: Mapping[str, int] = field(default_factory=dict)
    controls_floor: Mapping[str, Any] = field(default_factory=dict)
    cascade: Any = None
    coa: Any = None
    types: Any = None
    limits: Mapping[str, Any] = field(default_factory=dict)
    stamps: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Ports:
    world: World
    reader: Reader
    audit: AuditSink
    store: Store
    out_dir: Path
    skills: Any  # Callable[[PinSet], LoadedRules]
    ranker: Ranker | None = None
    progress: Any = None  # Callable[[Progress], None] | None
    max_retries: int = 0


# ---- store: files ----------------------------------------------------------------------------------------
_KEY_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=-]{0,120}$")


def _parts(key: str) -> list[str]:
    parts = key.split("/")
    if not parts or any(not _KEY_PART.match(p) for p in parts) or any(p.endswith(".sealed") for p in parts):
        raise ValueError(f"bad store key {key!r}")
    return parts


class FileStore:
    """Files under one folder: `<root>/<key>` holds the bytes, `<root>/<key>.sealed` holds the digest of a done
    key. Written with append and flushed per chunk; a half-written last line is never read back."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _p(self, key: str) -> Path:
        return self.root.joinpath(*_parts(key))

    def get(self, key: str) -> bytes | None:
        p = self._p(key)
        return p.read_bytes() if p.is_file() else None

    def append(self, key: str, chunk: bytes) -> None:
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "ab") as fh:
            fh.write(chunk)
            fh.flush()

    def lines(self, key: str, skip: int = 0) -> Iterator[bytes]:
        p = self._p(key)
        if not p.is_file():
            return
        with open(p, "rb") as fh:
            for n, raw in enumerate(fh):
                if not raw.endswith(b"\n"):
                    return  # a partial last line is not a record
                if n >= skip:
                    yield raw[:-1]

    def reset(self, key: str) -> None:
        p = self._p(key)
        p.unlink(missing_ok=True)
        Path(str(p) + ".sealed").unlink(missing_ok=True)

    def truncate(self, key: str, keep_lines: int) -> None:
        p = self._p(key)
        if not p.is_file():
            return
        kept = 0
        with open(p, "r+b") as fh:
            end = 0
            for raw in fh:
                if kept >= keep_lines or not raw.endswith(b"\n"):
                    break
                kept += 1
                end += len(raw)
            fh.truncate(end)
        Path(str(p) + ".sealed").unlink(missing_ok=True)

    def seal(self, key: str, digest: str) -> None:
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.is_file():
            p.write_bytes(b"")
        atomic_write_text(str(p) + ".sealed", digest)

    def sealed(self, key: str) -> str | None:
        p = Path(str(self._p(key)) + ".sealed")
        return p.read_text(encoding="utf-8") if p.is_file() else None


class MemoryStore:
    """The same, in memory (the fake)."""

    def __init__(self) -> None:
        self.data: dict[str, bytearray] = {}
        self.seals: dict[str, str] = {}

    def get(self, key: str) -> bytes | None:
        return bytes(self.data[key]) if key in self.data else None

    def append(self, key: str, chunk: bytes) -> None:
        self.data.setdefault(key, bytearray()).extend(chunk)

    def lines(self, key: str, skip: int = 0) -> Iterator[bytes]:
        raw = bytes(self.data.get(key, b""))
        if not raw:
            return
        parts = raw.split(b"\n")
        complete = parts[:-1]  # the tail after the last newline is a partial line (or empty)
        yield from complete[skip:]

    def reset(self, key: str) -> None:
        self.data.pop(key, None)
        self.seals.pop(key, None)

    def truncate(self, key: str, keep_lines: int) -> None:
        raw = bytes(self.data.get(key, b""))
        parts = raw.split(b"\n")[:-1][:keep_lines]
        self.data[key] = bytearray(b"".join(p + b"\n" for p in parts))
        self.seals.pop(key, None)

    def seal(self, key: str, digest: str) -> None:
        self.data.setdefault(key, bytearray())
        self.seals[key] = digest

    def sealed(self, key: str) -> str | None:
        return self.seals.get(key)


# ---- reader: files ---------------------------------------------------------------------------------------
def _answer_key(text: str) -> bool:
    parts = re.split(r"[\\/]", text)
    return any(p.lower() == "answer_key" for p in parts) or text.lower().endswith(".answer.json")


class FileReader:
    """Source files under the inbox roots, through the path guard (`worker.guard`): outside the roots, or an
    answer key, is `PathRefused`."""

    def __init__(self, roots: Sequence[str | Path]) -> None:
        self.roots = [Path(r) for r in roots]

    def path(self, ref: SourceRef) -> Path:
        if ref.kind != "file":
            raise PathRefused(f"{ref.ref!r} is an evaluation, not a file")
        if _answer_key(ref.ref):
            raise PathRefused("answer keys are not readable by the worker")
        p = Path(ref.ref)
        if p.is_absolute():
            return check_path(p, self.roots)
        for root in self.roots:
            if (root / p).exists():
                return check_path(root / p, self.roots)
        if p.exists():  # a path relative to the working directory that lies under a root (a relative state dir)
            return check_path(p, self.roots)
        if self.roots:
            check_path(self.roots[0] / p, self.roots)  # a refusal wins over "not found"
        raise PathRefused(f"no file named {ref.ref!r} in the inbox")

    def fingerprint(self, ref: SourceRef) -> str:
        return sha256_file(self.path(ref))

    def rows(self, ref: SourceRef) -> Iterator[tuple[int, list[Any]]]:
        from worker.quality import Source

        yield from Source(self.path(ref), ref.sheet or None, ref.header_row or 1).rows()


# ---- audit: the fake (the production sink is `audit.AuditLog` as it stands) --------------------------------
class ListAudit:
    """An in-memory audit sink that can be made to fail: `fail_all`, or `fail_on` (a set of actions that raise
    `AuditError` when recorded). It numbers its entries like the log does."""

    def __init__(self, *, fail_all: bool = False, fail_on: Sequence[str] = ()) -> None:
        self.entries: list[dict[str, Any]] = []
        self.fail_all = fail_all
        self.fail_on = set(fail_on)

    def record(self, action: str, rationale: str, inputs: dict | None = None, outputs: dict | None = None) -> dict:
        return self.record_many(action, [(rationale, inputs, outputs)])[0]

    def record_many(self, action: str, items: list[tuple[str, dict | None, dict | None]]) -> list[dict]:
        if self.fail_all or action in self.fail_on:
            raise AuditError(f"the audit sink cannot record {action}")
        out = []
        for rationale, inputs, outputs in items:
            if not str(rationale).strip():
                raise AuditError(f"a reason is required for {action}; the action is refused")
            entry = {"seq": len(self.entries) + 1, "action": action, "rationale": rationale,
                     "inputs": json.loads(json.dumps(inputs or {}, default=str)),
                     "outputs": json.loads(json.dumps(outputs or {}, default=str))}
            self.entries.append(entry)
            out.append(entry)
        return out

    def actions(self) -> list[str]:
        return [e["action"] for e in self.entries]


# ---- world: the sponsor's stores -------------------------------------------------------------------------


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")).hexdigest()


class StoreWorld:
    """The sponsor's stores as the console and the agent share them: `Registry` (existing records), `IdScheme`
    (counters), `AccountStore` and the versioned Golden mappings. Reserved IDs are kept per key in a small
    ledger, so the same job gets the same IDs again and the counter moves once, at `commit`."""

    def __init__(self, sponsor: str, *, registry: Any = None, id_root: str | Path | None = None,
                 accounts_root: str | Path | None = None, templates: Mapping[str, Any] | None = None,
                 ledger_root: str | Path | None = None, recipes_root: str | Path | None = None) -> None:
        self.sponsor = sponsor
        self.recipes_root = Path(recipes_root) if recipes_root is not None else None
        self.registry = registry
        self.id_root = Path(id_root) if id_root is not None else None
        self.accounts_root = Path(accounts_root) if accounts_root is not None else None
        self.templates = templates
        base = Path(ledger_root) if ledger_root is not None else (
            self.id_root / "_reservations" / sponsor if self.id_root is not None else None)
        self.ledger = base

    # -- reads
    def store(self, record_type: str) -> list[dict[str, Any]]:
        return self.registry.entries(record_type) if self.registry is not None else []

    def required(self, record_type: str, field_name: str, default: bool) -> bool:
        spec = (self.templates or {}).get(record_type)
        return field_name in spec.required if spec is not None else default

    def field_recipe(self, record_type: str) -> dict[str, Any] | None:
        """The `transform` step (with its pin) of the sponsor's field recipe for this record type, or None. A recipe
        file that no longer validates raises `RecipeChangeRefused`: a damaged recipe is loud, never skipped."""
        if self.recipes_root is None:
            return None
        templates = self.templates or {}
        recipes = persist.field_recipes(self.recipes_root, self.sponsor, record_types=list(templates) or None,
                                        target_fields={t: s.fields for t, s in templates.items()} or None)
        return recipes.engine_step(record_type)

    def account_entries(self) -> dict[str, dict[str, Any]]:
        return persist.accounts(self.accounts_root, self.sponsor).entries() if self.accounts_root else {}

    def golden_accounts(self) -> dict[str, Any]:
        return persist.golden(self.accounts_root).current() if self.accounts_root else {}

    def known_account(self, source_account: str, transaction_type: str) -> str:
        from worker.accounts import key_of

        key = key_of("", source_account, transaction_type)
        hit = self.account_entries().get(key) or self.golden_accounts().get(key)
        return str(hit["account"]) if hit else ""

    def _ids(self, record_type: str) -> Any:
        spec = (self.templates or {}).get(record_type)
        return persist.id_scheme(self.id_root, self.sponsor, record_type, spec.id_generation if spec else None)

    def id_scheme(self, record_type: str) -> dict[str, Any] | None:
        return self._ids(record_type).scheme if self.id_root is not None else None

    def counter(self, record_type: str) -> int:
        """The number the next generated ID of this record type takes."""
        return int(self._ids(record_type).counter) if self.id_root is not None else 1

    def fingerprint(self, record_types: Sequence[str]) -> str:
        facts: dict[str, Any] = {"records": {}, "counters": {}}
        for rt in sorted(set(record_types)):
            facts["records"][rt] = [[e.get("key"), e.get("name"), e.get("source_key")] for e in self.store(rt)]
            if self.id_root is not None:
                facts["counters"][rt] = [self.id_scheme(rt), self.counter(rt)]
        facts["accounts"] = self.account_entries()
        facts["golden"] = self.golden_accounts()
        return _digest(facts)

    # -- the ledger of reserved IDs
    def _ledger_path(self, key: str) -> Path | None:
        if self.ledger is None:
            return None
        if not re.fullmatch(r"[0-9a-f]{8,64}", key):
            raise ValueError(f"bad reservation key {key!r}")
        return self.ledger / f"{key}.json"

    def reserve_ids(self, record_type: str, n: int, key: str, taken: Sequence[str] = ()) -> list[str]:
        if self.id_root is None:
            raise ValueError("no ID counter is configured for this run")
        path = self._ledger_path(key)
        assert path is not None
        if path.is_file():
            doc = json.loads(path.read_text(encoding="utf-8"))
            if doc["record_type"] == record_type and len(doc["ids"]) >= n:
                return list(doc["ids"][:n])
        scheme = self._ids(record_type)
        used = set(taken) | {str(e["key"]) for e in self.store(record_type) if e.get("key")}
        rsv = scheme.reserve(used)
        first, scheme_doc = rsv.first, dict(rsv.scheme)
        ids = [rsv.take() for _ in range(n)]
        atomic_write_text(path, json.dumps({"record_type": record_type, "ids": ids, "scheme": scheme_doc,
                                            "first": first, "taken": sorted(used)}))
        return ids

    # -- the one place things move
    def commit(self, job_id: str, issued: Sequence[str], records: Sequence[Mapping[str, Any]], *,
               record_type: str = "", key: str = "", accounts: Mapping[str, str] | None = None,
               batch: str = "", reason: str = "") -> None:
        marker = self._ledger_path(key) if key else None
        done = marker.with_suffix(".committed") if marker is not None else None
        if done is not None and done.is_file():
            return  # the same job again: the counter and the stores already moved
        if marker is not None and marker.is_file() and issued and self.id_root is not None:
            doc = json.loads(marker.read_text(encoding="utf-8"))
            rsv = Reservation(doc["scheme"], int(doc["first"]), doc["taken"])
            for _ in issued:
                rsv.take()
            self._ids(doc["record_type"]).commit(rsv)
        if self.registry is not None and record_type and records:
            self.registry.add(record_type, list(records), source="app batch", batch=batch or job_id)
        if accounts and self.accounts_root is not None:
            store = persist.accounts(self.accounts_root, self.sponsor)
            for k, account in accounts.items():
                store.add(k, account, source=f"approved in {job_id}", reason=reason or "approved with the upload")
        if done is not None:
            done.parent.mkdir(parents=True, exist_ok=True)
            done.write_text("committed", encoding="utf-8")


# ---- skills: the strict loader ---------------------------------------------------------------------------


class ProductionSkills:
    """Loads and validates every skill through the strict loader and returns their checksums with the sponsor's
    overlay files folded in. A refused file raises `rulefiles.SchemaError`; nothing is half-loaded."""

    def __init__(self, sponsor: str, *, accounts_root: str | Path | None = None,
                 env: Mapping[str, str] | None = None, golden_version: Any = None) -> None:
        self.sponsor = sponsor
        self.accounts_root = Path(accounts_root) if accounts_root is not None else None
        self.env = env if env is not None else os.environ
        self.golden_version = golden_version  # a callable: the Golden mappings' version, when not file-backed

    def __call__(self, pins: PinSet | None = None) -> LoadedRules:
        from worker import quality
        from worker import rulefiles as rf
        from worker import skills as sk
        from worker.accounts import Coa, TypeMap
        from worker.templates import SPONSOR_RE, sponsors_dir, standards_dir

        found = sk.load_skills()
        sponsor_root = None
        if self.sponsor and SPONSOR_RE.match(self.sponsor):
            candidate = sponsors_dir() / self.sponsor
            sponsor_root = candidate if candidate.is_dir() else None
        checksums = {name: s.content_hash(sponsor_root) for name, s in sorted(found.items())}
        versions: dict[str, int] = {}
        controls = hashlib.sha256()
        floors: dict[str, Any] = {}
        for name, s in sorted(found.items()):
            pin = s.pin
            controls.update(f"{name}:{pin.controls_hash}\n".encode())
            for kind, v in pin.schema_versions.items():
                versions[kind] = max(versions.get(kind, 0), int(v))
            if s.controls():
                floors[name] = s.controls()
        qrules: tuple[Any, ...] = ()
        index: dict[str, int] = {}
        if "check-data-quality" in found:
            qrules = tuple(quality.load_rules())
            loaded = rf.load_file(found["check-data-quality"].path / "checks.yaml").data
            index = {str(r["id"]): i for i, r in enumerate(loaded["rules"]) if "id" in r}
        cascade = found["map-accounts"].table("coa-cascade", sponsor_root) if "map-accounts" in found else None
        standards = standards_dir()
        coa = Coa.load(standards / "Golden_COA.csv") if (standards / "Golden_COA.csv").is_file() else None
        types = TypeMap.load(standards / "Transaction_Type_Mapping.csv") \
            if (standards / "Transaction_Type_Mapping.csv").is_file() else None
        if self.golden_version is not None:
            version = int(self.golden_version())
        else:
            version = persist.golden(self.accounts_root).version if self.accounts_root is not None else 0
        chart = sha256_file(standards / "Golden_COA.csv")[:12] if coa is not None else ""
        limits: dict[str, Any] = {}
        raw = (self.env.get("WORKER_ENGINE_MAX_KEYS") or "").strip()
        if raw:
            limits["max_keys"] = int(raw)
        return LoadedRules(
            skills=checksums, controls=controls.hexdigest()[:12], schema_versions=versions,
            golden_coa=f"v{version}:{chart}", quality_rules=qrules, rule_index=index, controls_floor=floors,
            cascade=cascade, coa=coa, types=types, limits=limits,
            stamps={name: s.stamp for name, s in found.items()},
        )
