"""A request reads the same database state once, not once per caller (the PostgreSQL console took 11 s for a state
view because one list of sponsors read every record of every type per type).

The memo is per request, and a state change committed in the process ends it, so a read after a write is never old.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event

from worker.db.core import Db, read_scope
from worker.db.stores import SqlAccountStore, SqlRegistry, SqlWorld


@contextmanager
def counted(db: Db) -> Iterator[list[str]]:
    seen: list[str] = []

    def hook(conn: object, cur: object, statement: str, *_a: object) -> None:
        seen.append(statement)

    event.listen(db.engine, "before_cursor_execute", hook)
    try:
        yield seen
    finally:
        event.remove(db.engine, "before_cursor_execute", hook)


def _rec(n: int) -> dict[str, str]:
    return {"key": f"INV-{n}", "name": f"Investor {n}"}


@pytest.fixture
def sponsor(db: Db) -> str:
    name = f"sp-{uuid.uuid4().hex[:8]}"
    reg = SqlRegistry(db, name)
    reg.add("Investors", [_rec(1), _rec(2)], source="app batch", batch="b1")
    reg.add("Vendors", [_rec(3)], source="app batch", batch="b1")
    return name


def test_every_template_of_a_sponsor_is_one_query_inside_a_request(db: Db, sponsor: str) -> None:
    reg = SqlRegistry(db, sponsor)
    with counted(db) as q, read_scope():
        got = [len(reg.entries(t)) for t in ("Investors", "Vendors", "Funds", "Investors", "Vendors")]
    assert got == [2, 1, 0, 2, 1]
    assert len([s for s in q if "registry_entries" in s]) == 1


def test_outside_a_request_nothing_is_remembered(db: Db, sponsor: str) -> None:
    reg = SqlRegistry(db, sponsor)
    with counted(db) as q:
        reg.entries("Investors")
        reg.entries("Investors")
    assert len([s for s in q if "registry_entries" in s]) == 2


def test_counts_of_every_sponsor_are_one_query(db: Db, sponsor: str) -> None:
    other = f"sp-{uuid.uuid4().hex[:8]}"
    SqlRegistry(db, other).add("Investors", [_rec(9)], source="app batch", batch="b2")
    with counted(db) as q, read_scope():
        mine, theirs = SqlRegistry(db, sponsor).counts(), SqlRegistry(db, other).counts()
    assert mine == {"Investors": 2, "Vendors": 1} and theirs == {"Investors": 1}
    assert len([s for s in q if "registry_entries" in s]) == 1


def test_a_write_in_the_request_ends_what_was_remembered(db: Db, sponsor: str) -> None:
    reg = SqlRegistry(db, sponsor)
    with read_scope():
        assert len(reg.entries("Investors")) == 2
        reg.add("Investors", [_rec(4)], source="app batch", batch="b3")
        assert len(reg.entries("Investors")) == 3
        assert reg.counts()["Investors"] == 3


def test_a_write_from_another_thread_is_seen_by_the_open_request(db: Db, sponsor: str) -> None:
    import threading

    reg = SqlRegistry(db, sponsor)
    with read_scope():
        assert len(reg.entries("Investors")) == 2
        t = threading.Thread(target=lambda: SqlRegistry(db, sponsor).add(
            "Investors", [_rec(5)], source="app batch", batch="b4"))
        t.start()
        t.join()
        assert len(reg.entries("Investors")) == 3


def test_a_rolled_back_change_does_not_end_the_memo_and_changes_nothing(db: Db, sponsor: str) -> None:
    reg = SqlRegistry(db, sponsor)
    with read_scope():
        reg.entries("Investors")
        with pytest.raises(RuntimeError), db.begin() as conn:
            conn.execute(sa.text("select 1"))
            raise RuntimeError("fail")
        assert len(reg.entries("Investors")) == 2


def test_returned_lists_are_the_callers_own(db: Db, sponsor: str) -> None:
    reg = SqlRegistry(db, sponsor)
    with read_scope():
        first = reg.entries("Investors")
        first[0]["name"] = "changed"
        first.append({})
        assert [e["name"] for e in reg.entries("Investors")] == ["Investor 1", "Investor 2"]


def test_account_and_counter_reads_are_remembered_too(db: Db) -> None:
    name = f"sp-{uuid.uuid4().hex[:8]}"
    SqlAccountStore(db, name).add("k|a|b", "1000", source="t", reason="r")
    world = SqlWorld(db, name)
    store = SqlAccountStore(db, name)
    with counted(db) as q, read_scope():
        for _ in range(3):
            assert store.entries()["k|a|b"]["account"] == "1000"
            world.counter("Investors")
            world.id_scheme("Investors")
    assert len([s for s in q if "account_mappings" in s]) == 1
    assert len([s for s in q if "id_counters" in s]) == 1


def test_the_console_state_view_reads_the_database_a_few_times_not_once_per_type(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from worker.db.backend import db_for
    from worker.web import create_app

    url = f"sqlite:///{(tmp_path / 'ow.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    d = Db(url)
    d.create_all()
    d.dispose()
    app = create_app(tmp_path / "state", environ={"WORKER_MODEL_PROVIDER": "demo", "DATABASE_URL": url,
                                                  "WORKER_ENV_FILE": str(tmp_path / "t.env")})
    http = TestClient(app)
    for a in ("start|Sponsor A|Investors", "ask_set|cov|all", "ask_sample"):
        assert http.post("/api/console/act", json={"action": a}).status_code == 200
    with counted(db_for(url)) as q:
        assert http.get("/api/console/state").status_code == 200
    reads = [s for s in q if "registry_entries" in s]
    assert len(reads) <= 2, f"{len(reads)} registry reads for one state view"
    assert len(q) <= 12, f"{len(q)} statements for one state view"
