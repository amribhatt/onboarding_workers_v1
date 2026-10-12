"""Test doubles for the engine's ports: sources, the sponsor's stores and the skill loader held in memory.

The production implementations are in `worker.ports`; these are scriptable (they count, and can be made to fail),
so they live with the tests and not in the package.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from worker.flex.runner import RunResult
from worker.guard import PathRefused
from worker.idgen import Reservation
from worker.ports import (
    AuditSink,
    FileReader,
    FileStore,
    LoadedRules,
    Ports,
    ProductionSkills,
    Ranker,
    StoreWorld,
    _answer_key,
    _digest,
)

if TYPE_CHECKING:
    from worker.engine import PinSet, SourceRef


class MemoryReader:
    """Sources held in memory: name -> rows (a list of cell lists, header first). `path` writes the rows once to a
    CSV in a private temp folder, because the engine's file functions take a path. `pulled` counts the rows
    handed out by `rows`, `paths` the files materialised and `fingerprinted` the fingerprints taken (a test can
    see whether a source was touched, or read again)."""

    def __init__(self, files: Mapping[str, Sequence[Sequence[Any]]]) -> None:
        self.files = {k: [list(r) for r in v] for k, v in files.items()}
        self._dir: tempfile.TemporaryDirectory[str] | None = None
        self._paths: dict[str, Path] = {}
        self.pulled = 0
        self.paths = 0
        self.fingerprinted = 0
        self.fail_on: set[str] = set()

    def _rows(self, ref: SourceRef) -> list[list[Any]]:
        if _answer_key(ref.ref):
            raise PathRefused("answer keys are not readable by the worker")
        if ref.ref not in self.files:
            raise PathRefused(f"no file named {ref.ref!r} in the inbox")
        return self.files[ref.ref]

    def fingerprint(self, ref: SourceRef) -> str:
        self.fingerprinted += 1
        body = json.dumps(self._rows(ref), default=str, sort_keys=True).encode("utf-8")
        return hashlib.sha256(body).hexdigest()

    def rows(self, ref: SourceRef) -> Iterator[tuple[int, list[Any]]]:
        header = ref.header_row or 1
        for n, row in enumerate(self._rows(ref), start=1):
            if n <= header:
                continue
            if any(v is not None and str(v).strip() != "" for v in row):
                self.pulled += 1
                yield n, list(row)

    def path(self, ref: SourceRef) -> Path:
        rows = self._rows(ref)
        if ref.ref in self.fail_on:
            raise OSError(f"cannot read {ref.ref}")
        if ref.ref not in self._paths:
            if self._dir is None:
                self._dir = tempfile.TemporaryDirectory(prefix="engine-mem-")
            name = re.sub(r"[^A-Za-z0-9_.-]", "_", ref.ref)
            p = Path(self._dir.name) / (name if name.lower().endswith(".csv") else name + ".csv")
            with open(p, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                for r in rows:
                    w.writerow(["" if v is None else v for v in r])
            self._paths[ref.ref] = p
            self.paths += 1
        return self._paths[ref.ref]


class MemoryWorld:
    """The same, in memory (the fake). `records` is record type -> entries; `counters` record type -> next number;
    `commits` lists what `commit` was called with; `fail_commit` makes the next commit raise."""

    def __init__(self, sponsor: str = "sponsor-a", *, records: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
                 schemes: Mapping[str, Mapping[str, Any]] | None = None, accounts: Mapping[str, Any] | None = None,
                 golden: Mapping[str, Any] | None = None, required: Mapping[tuple[str, str], bool] | None = None) -> None:
        self.sponsor = sponsor
        self.records: dict[str, list[dict[str, Any]]] = {k: [dict(e) for e in v] for k, v in (records or {}).items()}
        self.schemes: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (schemes or {}).items()}
        self.counters: dict[str, int] = {k: int(v.get("start", 1)) for k, v in self.schemes.items()}
        self.accounts: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (accounts or {}).items()}
        self.golden: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (golden or {}).items()}
        self._required = dict(required or {})
        self.ledger: dict[str, dict[str, Any]] = {}
        self.committed: set[str] = set()
        self.commits: list[dict[str, Any]] = []
        self.fail_commit = False
        self.reservations = 0

    def store(self, record_type: str) -> list[dict[str, Any]]:
        return [dict(e) for e in self.records.get(record_type, [])]

    def required(self, record_type: str, field_name: str, default: bool) -> bool:
        return self._required.get((record_type, field_name), default)

    def account_entries(self) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self.accounts.items()}

    def golden_accounts(self) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self.golden.items()}

    def known_account(self, source_account: str, transaction_type: str) -> str:
        from worker.accounts import key_of

        key = key_of("", source_account, transaction_type)
        hit = self.accounts.get(key) or self.golden.get(key)
        return str(hit["account"]) if hit else ""

    def id_scheme(self, record_type: str) -> dict[str, Any] | None:
        return dict(self.schemes[record_type]) if record_type in self.schemes else None

    def counter(self, record_type: str) -> int:
        return int(self.counters.get(record_type, 1))

    def fingerprint(self, record_types: Sequence[str]) -> str:
        return _digest({"records": {rt: self.records.get(rt, []) for rt in sorted(set(record_types))},
                        "counters": self.counters, "accounts": self.accounts, "golden": self.golden})

    def reserve_ids(self, record_type: str, n: int, key: str, taken: Sequence[str] = ()) -> list[str]:
        if record_type not in self.schemes:
            raise ValueError("no ID counter is configured for this run")
        have = self.ledger.get(key)
        if have is not None and have["record_type"] == record_type and len(have["ids"]) >= n:
            return list(have["ids"][:n])
        used = set(taken) | {str(e["key"]) for e in self.records.get(record_type, []) if e.get("key")}
        scheme = self.schemes[record_type]
        rsv = Reservation(scheme, self.counters[record_type], used)
        ids = [rsv.take() for _ in range(n)]
        self.ledger[key] = {"record_type": record_type, "ids": ids, "scheme": dict(scheme), "first": rsv.first,
                            "taken": sorted(used)}
        self.reservations += 1
        return list(ids)

    def commit(self, job_id: str, issued: Sequence[str], records: Sequence[Mapping[str, Any]], *,
               record_type: str = "", key: str = "", accounts: Mapping[str, str] | None = None,
               batch: str = "", reason: str = "") -> None:
        if self.fail_commit:
            raise OSError("the store cannot be written")
        token = key or job_id
        if token in self.committed:
            return
        self.committed.add(token)
        led = self.ledger.get(key)
        if led is not None and issued:
            rsv = Reservation(led["scheme"], int(led["first"]), led["taken"])
            for _ in issued:
                rsv.take()
            self.counters[led["record_type"]] = max(self.counters[led["record_type"]], rsv.next)
        known = {str(e.get("key")) for e in self.records.get(record_type, [])}
        for r in records:
            if str(r.get("key", "")) not in known:
                self.records.setdefault(record_type, []).append(dict(r))
        for k, a in (accounts or {}).items():
            self.accounts[k] = {"key": k, "account": a, "source": f"approved in {job_id}", "reason": reason}
        self.commits.append({"job_id": job_id, "issued": list(issued), "records": len(records), "key": key,
                             "accounts": dict(accounts or {})})


class FakeSkills:
    """The production loader with scriptable faults: `refuse` raises the given exception on load, `overrides` is
    merged over the limits (for example `{"max_keys": 3}`), `checksums` replaces skill checksums. `calls` counts
    loads."""

    def __init__(self, sponsor: str = "sponsor-a", *, refuse: Exception | None = None,
                 limits: Mapping[str, Any] | None = None, checksums: Mapping[str, str] | None = None,
                 accounts_root: str | Path | None = None) -> None:
        self._real = ProductionSkills(sponsor, accounts_root=accounts_root, env={})
        self.refuse = refuse
        self.limits = dict(limits or {})
        self.checksums = dict(checksums or {})
        self.calls = 0

    def __call__(self, pins: PinSet | None = None) -> LoadedRules:
        self.calls += 1
        if self.refuse is not None:
            raise self.refuse
        from dataclasses import replace

        got = self._real(pins)
        return replace(got, limits={**got.limits, **self.limits}, skills={**got.skills, **self.checksums})


def production_ports(sponsor: str, *, inbox_roots: Sequence[str | Path], out_dir: str | Path, state: str | Path,
                     audit: AuditSink, ranker: Ranker | None = None, progress: Any = None,
                     max_retries: int = 0) -> Ports:
    """The file-backed production ports over one state folder (what the golden tests lock the digests with)."""
    from worker.registry import Registry
    from worker.templates import templates_for

    state = Path(state)
    world = StoreWorld(sponsor, registry=Registry(state, sponsor), id_root=state / "ids",
                       accounts_root=state / "accounts", templates=templates_for(sponsor),
                       recipes_root=state / "sponsors")
    return Ports(world=world, reader=FileReader(inbox_roots), audit=audit, store=FileStore(state / "engine"),
                 out_dir=Path(out_dir), skills=ProductionSkills(sponsor, accounts_root=state / "accounts"),
                 ranker=ranker, progress=progress, max_retries=max_retries)


class FakeRunner:
    """Tests: returns what `respond(kind, program, inputs, seed)` says (bytes, or a RunResult-shaped dict) and
    counts calls. It never starts anything."""

    def __init__(self, respond: Any, image: str = "sha256:" + "0" * 64) -> None:
        self.respond, self.image, self.calls = respond, image, 0

    def run(self, kind: str, program: bytes, inputs: Mapping[str, bytes], seed: int, exec_id: str) -> RunResult:
        self.calls += 1
        out = self.respond(kind, program, inputs, seed)
        if isinstance(out, RunResult):
            return out
        status = "ok" if out is not None else "no_output"
        return RunResult(status, 0, out, "", 0.0, "fake", exec_id)
