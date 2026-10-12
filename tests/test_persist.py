"""`worker.persist`: files by default, the database when `DATABASE_URL` is set, and the same answers either way."""

from __future__ import annotations

from pathlib import Path

import pytest

from worker import persist
from worker.accounts import AccountStore, Versions
from worker.audit import AuditLog
from worker.db.backend import database_url, stream_of
from worker.db.core import Db, SqlAuditLog
from worker.db.stores import SqlAccountStore, SqlIdScheme, SqlRegistry, StateConflict
from worker.feedback import FeedbackStore
from worker.idgen import IdScheme
from worker.registry import Registry

SCHEME = {"mode": "sequential", "prefix": "INV-", "width": 3, "start": 1}


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Db:
    url = f"sqlite:///{(tmp_path / 'ow.db').as_posix()}"
    db = Db(url)
    db.create_all()
    monkeypatch.setenv("DATABASE_URL", url)
    persist.use_state_dir(tmp_path / "state")
    return db


def test_without_a_database_url_every_store_is_the_file_class(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL_FILE", raising=False)
    assert persist._db() is None
    assert type(persist.audit(tmp_path / "a.jsonl")) is AuditLog
    assert type(persist.registry(tmp_path, "sponsor-a")) is Registry
    assert type(persist.feedback(tmp_path, "sponsor-a")) is FeedbackStore
    assert type(persist.accounts(tmp_path, "sponsor-a")) is AccountStore
    assert type(persist.versions(tmp_path / "v")) is Versions
    assert type(persist.id_scheme(tmp_path, "sponsor-a", "Investors", SCHEME)) is IdScheme


def test_with_a_database_url_the_stores_are_the_database_ones(database: Db, tmp_path: Path) -> None:
    assert persist._db() is not None
    assert isinstance(persist.audit(tmp_path / "state" / "runs" / "r1" / "audit.jsonl"), SqlAuditLog)
    assert isinstance(persist.registry(tmp_path, "sponsor-a"), SqlRegistry)
    assert isinstance(persist.accounts(tmp_path, None), SqlAccountStore)
    assert isinstance(persist.id_scheme(tmp_path, "sponsor-a", "Investors", SCHEME), SqlIdScheme)
    log = persist.audit(tmp_path / "state" / "runs" / "r1" / "audit.jsonl")
    assert log.stream == "runs/r1/audit.jsonl"  # named by the path under the state folder
    log.record("x", "because")
    assert SqlAuditLog(database, "runs/r1/audit.jsonl").verify() == (True, 1)


def test_the_database_url_may_come_from_a_mounted_secret(tmp_path: Path) -> None:
    secret = tmp_path / "url.txt"
    secret.write_text("postgresql+psycopg://u:p@h/d\n", encoding="utf-8")
    assert database_url({"DATABASE_URL_FILE": str(secret)}) == "postgresql+psycopg://u:p@h/d"
    assert database_url({"DATABASE_URL": "sqlite://", "DATABASE_URL_FILE": str(secret)}) == "sqlite://"
    assert database_url({}) is None


def test_stream_names_are_posix_paths_under_the_state_folder(tmp_path: Path) -> None:
    state = tmp_path / "state"
    assert stream_of(state / "feedback" / "sponsor-a" / "feedback.jsonl", state) == "feedback/sponsor-a/feedback.jsonl"
    assert ":" not in stream_of(Path("C:/elsewhere/x/audit.jsonl"), state)


def test_the_id_scheme_of_the_database_has_the_file_schemes_rules(database: Db) -> None:
    s = persist.id_scheme(Path("."), "sponsor-a", "Investors", SCHEME)
    assert (s.scheme["prefix"], s.counter) == ("INV-", 1)
    rsv = s.reserve(["INV-002"])
    assert [rsv.take(), rsv.take()] == ["INV-001", "INV-003"]
    assert s.counter == 1  # nothing moves until commit
    assert s.commit(rsv) == 4 and s.counter == 4
    older = s.reserve()
    older.next = 2
    assert s.commit(older) == 4  # never backwards
    seen: list[str] = []
    new = s.change({"prefix": "FUND-"}, 50, by="reviewer-a", reason="the sponsor asked",
                   record=lambda action, why, i, o: seen.append(action))
    assert new["scheme"]["prefix"] == "FUND-" and s.counter == 50 and seen == ["id.scheme_changed"]
    with pytest.raises(ValueError):
        s.change(counter=10, by="reviewer-a", reason=" ", record=lambda *a: None)
    with pytest.raises(ValueError):
        s.change(counter=0, by="reviewer-a", reason="bad", record=lambda *a: None)


def test_the_console_document_refuses_a_stale_writer(database: Db, tmp_path: Path) -> None:
    first = persist.console_document(tmp_path / "console.json")
    second = persist.console_document(tmp_path / "console.json")
    assert first.load() is None and second.load() is None
    first.save('{"v": 1}')
    with pytest.raises(StateConflict):
        second.save('{"v": "lost update"}')  # it had loaded nothing and the row now exists
    first.save('{"v": 2}')
    assert second.load() == '{"v": 2}'
    second.save('{"v": 3}')
    with pytest.raises(StateConflict):
        first.save('{"v": "stale"}')


def test_listing_helpers_find_the_same_things_in_the_database(database: Db, tmp_path: Path) -> None:
    persist.feedback(tmp_path, "sponsor-a").log.record("feedback", "r", {"kind": "x"})
    persist.feedback(tmp_path, "sponsor-b").log.record("feedback", "r", {"kind": "x"})
    assert persist.feedback_sponsors(tmp_path) == ["sponsor-a", "sponsor-b"]
    before = persist.feedback_signature(tmp_path)
    persist.feedback(tmp_path, "sponsor-b").log.record("feedback", "r", {"kind": "y"})
    assert persist.feedback_signature(tmp_path) != before
    persist.accounts(tmp_path, "sponsor-a").add("k", "1000", source="s", reason="r")
    persist.accounts(tmp_path, None).add("g", "2000", source="s", reason="r")
    stores = persist.account_stores(tmp_path)
    assert set(stores) == {"_golden", "sponsor-a"} and stores["sponsor-a"]["k"]["account"] == "1000"
    g = persist.golden(tmp_path)
    assert g.version == 0 and g.current()["g"]["account"] == "2000"
    assert g.publish({"x": 1}, "first") == 1 and g.current() == {"x": 1}
