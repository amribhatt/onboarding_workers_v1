"""The sponsor's stores over SQLAlchemy Core, behind the same surfaces the file classes have.

Every write is ONE transaction that begins with its audit row (`journal`), so on PostgreSQL the database refuses the
effect when the audit row is missing, and a crash leaves neither or both. No ORM objects per record: Core
`insert()` / `select()` only.

* `SqlRegistry`, `SqlAccountStore`, `SqlVersions`: the file `Registry`, `AccountStore` and `Versions`.
* `SqlWorld`: the engine's `World` port, with a real lock on ID reservation (no two concurrent jobs of one sponsor
  and record type can take the same ID) and an atomic, idempotent `commit`.
* `SqlStore`: the engine's `Store` port (checkpoints and the spool) as ordered chunks.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from worker.accounts import _SAFE as _ACCOUNT_SAFE
from worker.accounts import Versions, key_of
from worker.db import schema as S
from worker.db.core import Db, in_read_scope, insert_ignore, journal, remembered
from worker.idgen import IdScheme, Reservation, next_number
from worker.ports import _parts
from worker.registry import _SAFE as _REGISTRY_SAFE
from worker.registry import DEFAULT_SPONSOR, entry_key, fresh_entries

_RESERVATION_KEY = re.compile(r"^[0-9a-f]{8,64}$")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _scheme_of(text: str) -> dict[str, Any]:
    """The scheme in an `id_counters.scheme` text; the person's decision rides in it under `decided`."""
    return {k: v for k, v in json.loads(text).items() if k != "decided"}


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


# ---- the registry --------------------------------------------------------------------------------------
class SqlRegistry:
    """What a sponsor has already onboarded. A unique key makes a duplicate impossible even across processes."""

    def __init__(self, db: Db, sponsor: str = "") -> None:
        self.db = db
        self.sponsor = sponsor or DEFAULT_SPONSOR
        if not _REGISTRY_SAFE.match(self.sponsor):
            raise ValueError(f"bad sponsor name {sponsor!r}")

    def _template(self, template: str) -> str:
        if not _REGISTRY_SAFE.match(template):
            raise ValueError(f"bad template name {template!r}")
        return template

    def entries(self, template: str) -> list[dict[str, Any]]:
        tpl = self._template(template)
        t = S.registry_entries

        def load(only: str | None) -> dict[str, tuple[str, ...]]:
            q = sa.select(t.c.template, t.c.doc).where(t.c.sponsor == self.sponsor).order_by(t.c.id)
            if only is not None:
                q = q.where(t.c.template == only)
            out: dict[str, list[str]] = {}
            with self.db.begin() as conn:
                for r in conn.execute(q):
                    out.setdefault(r.template, []).append(r.doc)
            return {k: tuple(v) for k, v in out.items()}

        if in_read_scope():  # one query answers every template of the sponsor for the rest of the request
            docs = remembered((id(self.db), "registry", self.sponsor), lambda: load(None)).get(tpl, ())
        else:
            docs = load(tpl).get(tpl, ())
        return [json.loads(d) for d in docs]

    def add(self, template: str, records: Sequence[Mapping[str, Any]], *, source: str, batch: str,
            fund: str = "") -> int:
        with self.db.begin() as conn:
            return self.add_in(conn, template, records, source=source, batch=batch, fund=fund)

    def add_in(self, conn: Connection, template: str, records: Sequence[Mapping[str, Any]], *, source: str,
               batch: str, fund: str = "") -> int:
        """`add` inside a transaction the caller owns (`SqlWorld.commit`)."""
        t = S.registry_entries
        tpl = self._template(template)
        known = {(r.key_kind, r.dedupe) for r in conn.execute(
            sa.select(t.c.key_kind, t.c.dedupe).where(t.c.sponsor == self.sponsor, t.c.template == tpl))}
        fresh = fresh_entries(known, records, source=source, batch=batch, fund=fund)
        if fresh:
            journal(conn, f"state/{self.sponsor}/registry", "registry.added",
                    f"{len(fresh)} {tpl} records registered from {source}",
                    {"sponsor": self.sponsor, "template": tpl, "batch": batch, "count": len(fresh)})
            conn.execute(insert_ignore(self.db.dialect, t), [
                {"sponsor": self.sponsor, "template": tpl, "key_kind": e["key_kind"],
                 "dedupe": entry_key(e)[1], "doc": _dump(e)} for e in fresh])
        return len(fresh)

    def provenance(self, template: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self.entries(template):
            label = e["source"] + (f" · {e['fund']}" if e.get("fund") else "")
            counts[label] = counts.get(label, 0) + 1
        return counts

    def counts(self) -> dict[str, int]:
        t = S.registry_entries

        def load(only: str | None) -> dict[str, dict[str, int]]:
            q = sa.select(t.c.sponsor, t.c.template, sa.func.count()).group_by(t.c.sponsor, t.c.template)
            if only is not None:
                q = q.where(t.c.sponsor == only)
            out: dict[str, dict[str, int]] = {}
            with self.db.begin() as conn:
                for sponsor, template, n in conn.execute(q.order_by(t.c.sponsor, t.c.template)):
                    out.setdefault(sponsor, {})[template] = int(n)
            return out

        if in_read_scope():  # every sponsor at once: a view that lists sponsors asks once, not once per sponsor
            return dict(remembered((id(self.db), "registry.counts"), lambda: load(None)).get(self.sponsor, {}))
        return dict(load(self.sponsor).get(self.sponsor, {}))


# ---- account mappings and versions ---------------------------------------------------------------------
class SqlAccountStore:
    """Approved mappings, append-only: the sponsor's own store (`sponsor`) or the Golden one (`None`)."""

    def __init__(self, db: Db, sponsor: str | None) -> None:
        name = "_golden" if sponsor is None else sponsor
        if not _ACCOUNT_SAFE.match(name):
            raise ValueError(f"bad store name {name!r}")
        self.db = db
        self.name = name

    def entries(self) -> dict[str, dict[str, Any]]:
        t = S.account_mappings

        def load() -> tuple[str, ...]:
            with self.db.begin() as conn:
                return tuple(r.doc for r in conn.execute(
                    sa.select(t.c.doc).where(t.c.store == self.name).order_by(t.c.id)))

        out: dict[str, dict[str, Any]] = {}
        for doc in remembered((id(self.db), "accounts", self.name), load):
            e = json.loads(doc)
            out[e["key"]] = e  # a later entry for a key replaces the earlier one
        return out

    def add(self, key: str, account: str, *, source: str, reason: str) -> None:
        with self.db.begin() as conn:
            self.add_in(conn, key, account, source=source, reason=reason)

    def add_in(self, conn: Connection, key: str, account: str, *, source: str, reason: str) -> None:
        if not reason.strip():
            raise ValueError("a reason is required to store a mapping")
        entry = {"key": key, "account": account, "source": source, "reason": reason.strip(),
                 "added": time.strftime("%Y-%m-%dT%H:%M:%S")}
        journal(conn, f"state/{self.name}/accounts", "account.mapped", reason.strip(),
                {"store": self.name, "key": key}, {"account": account})
        conn.execute(S.account_mappings.insert().values(store=self.name, key=key, doc=_dump(entry)))


class SqlVersions(Versions):
    """A versioned document: immutable snapshots and a pointer. Two publishers cannot take one number (primary
    key); a rollback moves the pointer and deletes nothing."""

    def __init__(self, db: Db, name: str, initial: Callable[[], dict[str, Any]] | None = None) -> None:
        self.db = db
        self.name = name
        self._initial = initial or (dict)

    @property
    def version(self) -> int:
        t = S.version_pointer

        def load() -> int:
            with self.db.begin() as conn:
                row = conn.execute(sa.select(t.c.version).where(t.c.name == self.name)).first()
            return int(row[0]) if row else 0

        return int(remembered((id(self.db), "version", self.name), load))

    def _latest(self) -> int:
        t = S.versions
        with self.db.begin() as conn:
            return int(conn.execute(sa.select(sa.func.coalesce(sa.func.max(t.c.version), 0))
                                    .where(t.c.name == self.name)).scalar_one())

    def at(self, version: int) -> dict[str, Any]:
        if version == 0:
            return dict(self._initial())
        t = S.versions

        def load() -> str | None:
            with self.db.begin() as conn:
                row = conn.execute(sa.select(t.c.content).where(t.c.name == self.name,
                                                               t.c.version == version)).first()
            return None if row is None else str(row[0])

        content = remembered((id(self.db), "version.at", self.name, version), load)
        if content is None:
            raise FileNotFoundError(f"no version {version} of {self.name}")
        return json.loads(content)

    def publish(self, content: Mapping[str, Any], note: str) -> int:
        if not note.strip():
            raise ValueError("a note is required to publish a version")
        with self.db.begin() as conn:
            t = S.versions
            n = int(conn.execute(sa.select(sa.func.coalesce(sa.func.max(t.c.version), 0))
                                 .where(t.c.name == self.name)).scalar_one()) + 1
            journal(conn, f"state/versions/{self.name}", "version.published", note.strip(),
                    {"name": self.name, "version": n})
            conn.execute(t.insert().values(name=self.name, version=n, note=note.strip(), published=_now(),
                                           content=_dump(dict(content))))
            self._point(conn, n)
        return n

    def rollback(self) -> int:
        with self.db.begin() as conn:
            t = S.version_pointer
            row = conn.execute(sa.select(t.c.version).where(t.c.name == self.name)).first()
            n = int(row[0]) if row else 0
            if n == 0:
                raise ValueError("there is no earlier version to go back to")
            journal(conn, f"state/versions/{self.name}", "version.rolled_back", f"back to version {n - 1}",
                    {"name": self.name, "from": n, "to": n - 1})
            self._point(conn, n - 1)
        return n - 1

    def _point(self, conn: Connection, version: int) -> None:
        t = S.version_pointer
        if conn.execute(t.update().where(t.c.name == self.name).values(version=version)).rowcount == 0:
            conn.execute(t.insert().values(name=self.name, version=version))


class SqlIdScheme(IdScheme):
    """One sponsor's counter for one record type: the `IdScheme` surface over `id_counters`. The counter moves
    at `commit` and never backwards, in a transaction that also moves the high-water mark `SqlWorld` reserves from."""

    def __init__(self, db: Db, sponsor: str, record_type: str, default: Mapping[str, Any] | None = None) -> None:
        self._names(sponsor, record_type, default)
        self.db = db

    def _row(self, conn: Connection, *, lock: bool = False) -> Any:
        t = S.id_counters
        q = sa.select(t).where(t.c.sponsor == self.sponsor, t.c.record_type == self.record_type)
        return conn.execute(q.with_for_update() if lock else q).first()

    def _data(self, row: Any) -> dict[str, Any]:
        if row is None:
            return {"scheme": self._default, "counter": self._default["start"]}
        return {"scheme": _scheme_of(row.scheme), "counter": int(row.counter), "held": int(row.high_water),
                "decided": json.loads(row.scheme).get("decided")}  # `held`: numbers live reservations hold

    def _load(self) -> dict[str, Any]:
        def load() -> Any:
            with self.db.begin() as conn:
                return self._row(conn)

        return self._data(remembered((id(self.db), "id_counter", self.sponsor, self.record_type), load))

    def _put(self, conn: Connection, scheme: Mapping[str, Any], counter: int,
             decided: Mapping[str, Any] | None = None) -> None:
        t = S.id_counters
        row = self._row(conn, lock=True)
        if decided:
            scheme = {**scheme, "decided": dict(decided)}
        if row is None:
            conn.execute(t.insert().values(sponsor=self.sponsor, record_type=self.record_type,
                                           scheme=_dump(dict(scheme)), counter=counter, high_water=counter))
        else:
            conn.execute(t.update().where(t.c.sponsor == self.sponsor, t.c.record_type == self.record_type)
                         .values(scheme=_dump(dict(scheme)), counter=counter,
                                 high_water=max(int(row.high_water), counter)))

    def counters(self) -> dict[str, int]:
        with self.db.begin() as conn:
            rows = conn.execute(sa.select(S.id_counters).where(S.id_counters.c.sponsor == self.sponsor)).all()
        return {r.record_type: next_number(self._data(r)) for r in rows}

    def _move(self, to: Callable[[dict[str, Any]], int], action: str) -> int:
        with self.db.begin() as conn:
            data = self._data(self._row(conn, lock=True))
            moved = to(data)
            journal(conn, f"state/{self.sponsor}/ids", action, f"{self.record_type} counter moved to {moved}",
                    {"sponsor": self.sponsor, "record_type": self.record_type, "to": moved})
            self._put(conn, data["scheme"], moved, data.get("decided"))
        return moved

    def _store_change(self, new: dict[str, Any], who: dict[str, Any], audit_new: dict[str, Any], reason: str) -> None:
        with self.db.begin() as conn:
            journal(conn, f"state/{self.sponsor}/ids", "id.scheme_changed", reason, who, {"new": audit_new})
            self._put(conn, new["scheme"], new["counter"], new.get("decided"))


class StateConflict(RuntimeError):
    """The console document was changed by someone else since it was loaded; reload before saving."""


class SqlDocument:
    """One text document with compare-and-set: `save` fails when the stored version is not the one `load` saw."""

    def __init__(self, db: Db, doc_id: int) -> None:
        self.db = db
        self.doc_id = doc_id
        self._version = 0

    def load(self) -> str | None:
        t = S.console_state
        with self.db.begin() as conn:
            row = conn.execute(sa.select(t.c.row_version, t.c.doc).where(t.c.id == self.doc_id)).first()
        self._version = int(row[0]) if row else 0
        return str(row[1]) if row else None

    def save(self, text: str) -> None:
        t = S.console_state
        with self.db.begin() as conn:
            if self._version == 0:
                try:
                    conn.execute(t.insert().values(id=self.doc_id, row_version=1, doc=text))
                except sa.exc.IntegrityError as exc:
                    raise StateConflict("the console state was created elsewhere; reload before saving") from exc
                self._version = 1
                return
            done = conn.execute(t.update().where(t.c.id == self.doc_id, t.c.row_version == self._version)
                                .values(row_version=self._version + 1, doc=text))
            if done.rowcount == 0:
                raise StateConflict("the console state changed elsewhere; reload before saving")
            self._version += 1


def account_store_names(db: Db) -> list[str]:
    t = S.account_mappings
    with db.begin() as conn:
        return [r[0] for r in conn.execute(sa.select(t.c.store).distinct().order_by(t.c.store))]


# ---- the world ------------------------------------------------------------------------------------------
class SqlWorld:
    """The engine's `World` over the database.

    `reserve_ids` takes a row lock on the sponsor's counter for the record type, starts after every number any
    live reservation holds (`high_water`) and records the IDs in `issued_ids`, whose primary key makes a duplicate
    impossible. `commit` is one transaction: counter, registry, accounts, the idempotency marker and its audit row.
    """

    def __init__(self, db: Db, sponsor: str, *, templates: Mapping[str, Any] | None = None) -> None:
        self.db = db
        self.sponsor = sponsor
        self.templates = templates
        self.registry = SqlRegistry(db, sponsor)
        self.accounts = SqlAccountStore(db, sponsor)
        self.golden = SqlVersions(db, "golden", lambda: SqlAccountStore(db, None).entries())

    # -- reads
    def store(self, record_type: str) -> list[dict[str, Any]]:
        return self.registry.entries(record_type)

    def required(self, record_type: str, field_name: str, default: bool) -> bool:
        spec = (self.templates or {}).get(record_type)
        return field_name in spec.required if spec is not None else default

    def account_entries(self) -> dict[str, dict[str, Any]]:
        return self.accounts.entries()

    def golden_accounts(self) -> dict[str, Any]:
        return self.golden.current()

    def known_account(self, source_account: str, transaction_type: str) -> str:
        key = key_of("", source_account, transaction_type)
        hit = self.account_entries().get(key) or self.golden_accounts().get(key)
        return str(hit["account"]) if hit else ""

    def _ids(self, record_type: str) -> SqlIdScheme:
        spec = (self.templates or {}).get(record_type)
        return SqlIdScheme(self.db, self.sponsor, record_type, spec.id_generation if spec else None)

    def id_scheme(self, record_type: str) -> dict[str, Any]:
        return self._ids(record_type).scheme

    def counter(self, record_type: str) -> int:
        """The number the next generated ID of this record type takes (once the live reservations are done)."""
        return next_number({**self._ids(record_type)._load(), "held": 0})

    def fingerprint(self, record_types: Sequence[str]) -> str:
        facts: dict[str, Any] = {"records": {}, "counters": {}}
        for rt in sorted(set(record_types)):
            facts["records"][rt] = [[e.get("key"), e.get("name"), e.get("source_key")] for e in self.store(rt)]
            facts["counters"][rt] = [self.id_scheme(rt), self.counter(rt)]
        facts["accounts"] = self.account_entries()
        facts["golden"] = self.golden_accounts()
        return hashlib.sha256(json.dumps(facts, sort_keys=True, default=str, ensure_ascii=False).encode()
                              ).hexdigest()

    # -- reservation, with a real lock
    def reserve_ids(self, record_type: str, n: int, key: str, taken: Sequence[str] = ()) -> list[str]:
        ids = self._ids(record_type)
        if not _RESERVATION_KEY.match(key):
            raise ValueError(f"bad reservation key {key!r}")
        rsv_t, iss_t, ctr_t = S.id_reservations, S.issued_ids, S.id_counters
        with self.db.begin() as conn:
            have = conn.execute(sa.select(rsv_t).where(rsv_t.c.sponsor == self.sponsor,
                                                       rsv_t.c.reservation_key == key)).first()
            if (have is not None and have.status != "released" and have.record_type == record_type
                    and len(json.loads(have.ids)) >= n):
                return json.loads(have.ids)[:n]
            scheme = ids._default
            journal(conn, f"state/{self.sponsor}/ids", "id.reserving", f"{n} {record_type} IDs asked for",
                    {"sponsor": self.sponsor, "record_type": record_type, "key": key, "n": n})
            conn.execute(insert_ignore(self.db.dialect, ctr_t).values(
                sponsor=self.sponsor, record_type=record_type, scheme=_dump(scheme),
                counter=int(scheme["start"]), high_water=int(scheme["start"])))
            row = ids._row(conn, lock=True)  # the serialisation point
            used = set(taken) | {str(e["key"]) for e in self.registry.entries(record_type) if e.get("key")}
            used |= {r[0] for r in conn.execute(sa.select(iss_t.c.id_norm).where(
                iss_t.c.sponsor == self.sponsor, iss_t.c.record_type == record_type))}
            held = ids._data(row)
            rsv = Reservation(held["scheme"], next_number(held), used)
            ids = [rsv.take() for _ in range(n)]
            journal(conn, f"state/{self.sponsor}/ids", "id.reserved", f"{n} {record_type} IDs reserved",
                    {"sponsor": self.sponsor, "record_type": record_type, "key": key, "first": rsv.first,
                     "next": rsv.next}, {"ids": ids if len(ids) <= 20 else [ids[0], ids[-1]]})
            doc = {"sponsor": self.sponsor, "reservation_key": key, "record_type": record_type, "first": rsv.first,
                   "next": rsv.next, "ids": json.dumps(ids), "taken": json.dumps(sorted(used)),
                   "scheme": _dump(rsv.scheme), "status": "reserved", "created_at": _now(), "closed_at": None}
            if have is None:
                conn.execute(rsv_t.insert().values(**doc))
            else:
                conn.execute(rsv_t.update().where(rsv_t.c.sponsor == self.sponsor,
                                                  rsv_t.c.reservation_key == key).values(**doc))
                conn.execute(iss_t.delete().where(iss_t.c.sponsor == self.sponsor,
                                                  iss_t.c.reservation_key == key))
            conn.execute(iss_t.insert(), [{"sponsor": self.sponsor, "record_type": record_type,
                                           "id_norm": i.lower(), "reservation_key": key} for i in ids])
            conn.execute(ctr_t.update().where(ctr_t.c.sponsor == self.sponsor, ctr_t.c.record_type == record_type)
                         .values(high_water=max(int(row.high_water), rsv.next)))
        return ids

    # -- the one place things move
    def commit(self, job_id: str, issued: Sequence[str], records: Sequence[Mapping[str, Any]], *,
               record_type: str = "", key: str = "", accounts: Mapping[str, str] | None = None,
               batch: str = "", reason: str = "") -> None:
        token = key or job_id
        rsv_t, ctr_t, com_t = S.id_reservations, S.id_counters, S.world_commits
        with self.db.begin() as conn:
            if conn.execute(sa.select(com_t.c.token).where(com_t.c.sponsor == self.sponsor,
                                                          com_t.c.token == token)).first():
                return  # the same job again: the counter and the stores already moved
            journal(conn, f"state/{self.sponsor}/world", "world.committed", reason or "approved with the upload",
                    {"sponsor": self.sponsor, "job": job_id, "record_type": record_type, "key": key,
                     "records": len(records), "ids": len(issued), "accounts": len(accounts or {})})
            res = conn.execute(sa.select(rsv_t).where(rsv_t.c.sponsor == self.sponsor,
                                                      rsv_t.c.reservation_key == key)).first() if key else None
            if res is not None and issued:
                rsv = Reservation(json.loads(res.scheme), int(res.first), json.loads(res.taken))
                for _ in issued:
                    rsv.take()
                row = SqlIdScheme(self.db, self.sponsor, res.record_type)._row(conn, lock=True)
                moved = max(int(row.counter), rsv.next)
                conn.execute(ctr_t.update().where(ctr_t.c.sponsor == self.sponsor,
                                                  ctr_t.c.record_type == res.record_type)
                             .values(counter=moved, high_water=max(int(row.high_water), moved)))
            if res is not None:
                conn.execute(rsv_t.update().where(rsv_t.c.sponsor == self.sponsor,
                                                  rsv_t.c.reservation_key == key)
                             .values(status="committed", closed_at=_now()))
            if record_type and records:
                self.registry.add_in(conn, record_type, list(records), source="app batch", batch=batch or job_id)
            for k, account in (accounts or {}).items():
                self.accounts.add_in(conn, k, account, source=f"approved in {job_id}",
                                     reason=reason or "approved with the upload")
            conn.execute(com_t.insert().values(sponsor=self.sponsor, token=token, job_id=job_id,
                                               committed_at=_now()))


# ---- the engine's store ---------------------------------------------------------------------------------
class SqlStore:
    """`Store`: append-only bytes under a key, in ordered chunks, sealed when a stage is done. A half-written last
    line is never read back (same rule as `FileStore`)."""

    def __init__(self, db: Db, prefix: str = "") -> None:
        self.db = db
        self.prefix = prefix

    def _k(self, key: str) -> str:
        _parts(key)
        return f"{self.prefix}{key}"

    def _after(self, key: str, n: int) -> tuple[int, bytes] | None:
        """The chunk after number `n` (one short transaction per chunk, so a reader never holds one open)."""
        t = S.store_chunks
        with self.db.begin() as conn:
            row = conn.execute(sa.select(t.c.n, t.c.data).where(t.c.key == key, t.c.n > n).order_by(t.c.n)
                               .limit(1)).first()
        return (int(row[0]), bytes(row[1])) if row else None

    def _complete(self, key: str) -> Iterator[bytes]:
        carry, n = b"", -1
        while (nxt := self._after(key, n)) is not None:
            n, data = nxt
            parts = (carry + data).split(b"\n")
            carry = parts.pop()  # the tail after the last newline is a partial line (or empty)
            yield from parts

    def _exists(self, conn: Connection, key: str) -> bool:
        t = S.store_chunks
        return conn.execute(sa.select(t.c.n).where(t.c.key == key).limit(1)).first() is not None

    def get(self, key: str) -> bytes | None:
        k = self._k(key)
        with self.db.begin() as conn:
            if not self._exists(conn, k):
                return None
        out, n = [], -1
        while (nxt := self._after(k, n)) is not None:
            n, data = nxt
            out.append(data)
        return b"".join(out)

    def append(self, key: str, chunk: bytes) -> None:
        k, t = self._k(key), S.store_chunks
        with self.db.begin() as conn:
            n = int(conn.execute(sa.select(sa.func.coalesce(sa.func.max(t.c.n), -1)).where(t.c.key == k))
                    .scalar_one()) + 1
            conn.execute(t.insert().values(key=k, n=n, data=chunk))

    def lines(self, key: str, skip: int = 0) -> Iterator[bytes]:
        for seen, line in enumerate(self._complete(self._k(key))):
            if seen >= skip:
                yield line

    def reset(self, key: str) -> None:
        k = self._k(key)
        with self.db.begin() as conn:
            conn.execute(S.store_chunks.delete().where(S.store_chunks.c.key == k))
            conn.execute(S.store_seals.delete().where(S.store_seals.c.key == k))

    def truncate(self, key: str, keep_lines: int) -> None:
        k, t = self._k(key), S.store_chunks
        with self.db.begin() as conn:
            if not self._exists(conn, k):
                return
        kept = b"".join(line + b"\n" for _, line in zip(range(keep_lines), self._complete(k), strict=False))
        with self.db.begin() as conn:
            conn.execute(t.delete().where(t.c.key == k))
            conn.execute(t.insert().values(key=k, n=0, data=kept))
            conn.execute(S.store_seals.delete().where(S.store_seals.c.key == k))

    def seal(self, key: str, digest: str) -> None:
        k = self._k(key)
        with self.db.begin() as conn:
            if not self._exists(conn, k):
                conn.execute(S.store_chunks.insert().values(key=k, n=0, data=b""))
            conn.execute(S.store_seals.delete().where(S.store_seals.c.key == k))
            conn.execute(S.store_seals.insert().values(key=k, digest=digest))

    def sealed(self, key: str) -> str | None:
        k = self._k(key)
        with self.db.begin() as conn:
            row = conn.execute(sa.select(S.store_seals.c.digest).where(S.store_seals.c.key == k)).first()
        return str(row[0]) if row else None
