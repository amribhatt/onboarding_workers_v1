"""One contract suite for the SQL adapters, on SQLite in memory and (integration) on PostgreSQL.

Each case states a rule of the file-backed class it replaces, or a rule the files could not keep (concurrency).
"""

from __future__ import annotations

import json
import threading
import uuid
from typing import Any

import pytest
import sqlalchemy as sa

from worker.audit import AuditError
from worker.db import schema as S
from worker.db.core import Db, SqlAuditLog
from worker.db.documents import SqlFieldRecipes, SqlImprovementStore, SqlRecipeStore, SqlSponsorMemory
from worker.db.stores import SqlAccountStore, SqlRegistry, SqlStore, SqlVersions, SqlWorld
from worker.recipes import RecipeChangeRefused
from worker.spool import Spool
from worker.spool import read as spool_read

SCHEME = {"mode": "sequential", "prefix": "INV-", "width": 0, "start": 1}


class _Spec:
    id_generation = SCHEME
    required = frozenset({"Investor_Name"})


def _sponsor() -> str:
    return f"sp-{uuid.uuid4().hex[:8]}"


def _world(db: Db, sponsor: str | None = None) -> SqlWorld:
    return SqlWorld(db, sponsor or _sponsor(), templates={"Investors": _Spec()})


def _key() -> str:
    return uuid.uuid4().hex


# ---- the audit log ---------------------------------------------------------------------------------------
def test_audit_appends_chain_and_verifies(db: Db) -> None:
    log = SqlAuditLog(db, f"runs/{_key()}/audit.jsonl")
    first = log.record("a", "because", {"x": 1})
    second = log.record("b", "why", {"y": 2}, {"z": 3})
    assert (first["seq"], second["seq"]) == (1, 2)
    assert second["prev_hash"] == first["hash"]
    assert log.verify() == (True, 2)
    assert [e["action"] for e in log.read()] == ["a", "b"]


def test_audit_refuses_an_empty_reason_and_writes_nothing(db: Db) -> None:
    log = SqlAuditLog(db, f"runs/{_key()}/audit.jsonl")
    with pytest.raises(AuditError):
        log.record("a", "  ")
    with pytest.raises(AuditError):
        log.record_many("a", [("ok", None, None), ("", None, None)])
    assert log.read() == []


def test_audit_streams_are_independent(db: Db) -> None:
    a, b = SqlAuditLog(db, f"s/{_key()}"), SqlAuditLog(db, f"s/{_key()}")
    a.record("a", "r")
    a.record("a", "r")
    b.record("b", "r")
    assert (a.verify(), b.verify()) == ((True, 2), (True, 1))


def test_audit_chain_holds_under_concurrent_appends(db: Db) -> None:
    stream = f"hot/{_key()}"
    errors: list[Exception] = []

    def work(n: int) -> None:
        try:
            log = SqlAuditLog(db, stream)
            for i in range(10):
                log.record("tick", f"thread {n} entry {i}", {"n": n, "i": i})
        except Exception as exc:  # noqa: BLE001 - the test reports whatever a thread hit
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert SqlAuditLog(db, stream).verify() == (True, 60)


def test_audit_read_verifies_columns_against_the_stored_text(db: Db) -> None:
    """A changed column (not only a changed body) is found: the stored text is what the hash covers."""
    stream = f"c/{_key()}"
    log = SqlAuditLog(db, stream)
    log.record("a", "r")
    log.record("b", "r")
    if db.dialect != "sqlite":
        pytest.skip("on PostgreSQL the triggers refuse the edit; test_db_immutability.py covers it")
    with db.begin() as conn:
        conn.execute(sa.text("DROP TRIGGER audit_no_update"))
        conn.execute(sa.text("UPDATE audit_log SET action = 'zzz' WHERE stream = :s AND seq = 2"), {"s": stream})
    checked, problem = log.first_break()
    assert checked == 1 and problem is not None and problem["seq"] is None


# ---- registry and accounts -------------------------------------------------------------------------------
def test_registry_deduplicates_case_insensitively_and_by_kind(db: Db) -> None:
    reg = SqlRegistry(db, _sponsor())
    n = reg.add("Investors", [{"key": "A-1", "name": "One"}, {"key": "a-1", "name": "Again"},
                              {"key": "", "name": "No Id"}], source="app batch", batch="b1")
    assert n == 2
    assert reg.add("Investors", [{"key": "A-1", "name": "x"}, {"key": "", "name": "no id"}],
                   source="outside export", batch="b2") == 0
    assert [e["key"] for e in reg.entries("Investors")] == ["A-1", ""]
    assert reg.counts() == {"Investors": 2}
    assert reg.provenance("Investors") == {"app batch": 2}


def test_registry_refuses_an_unknown_source(db: Db) -> None:
    with pytest.raises(ValueError):
        SqlRegistry(db, _sponsor()).add("Investors", [{"key": "A", "name": "a"}], source="guess", batch="b")


def test_one_sponsor_never_sees_another(db: Db) -> None:
    a, b = _world(db), _world(db)
    a.registry.add("Investors", [{"key": "X-1", "name": "x"}], source="quick entry", batch="b")
    SqlAccountStore(db, a.sponsor).add("k", "1000", source="s", reason="r")
    assert b.store("Investors") == [] and b.account_entries() == {}
    assert list(a.account_entries()) == ["k"]


def test_account_store_needs_a_reason_and_the_last_entry_wins(db: Db) -> None:
    store = SqlAccountStore(db, _sponsor())
    with pytest.raises(ValueError):
        store.add("k", "1000", source="s", reason=" ")
    store.add("k", "1000", source="s", reason="first")
    store.add("k", "2000", source="s", reason="second")
    assert store.entries()["k"]["account"] == "2000"


# ---- versions --------------------------------------------------------------------------------------------
def test_versions_publish_and_rollback_bring_back_the_content_exactly(db: Db) -> None:
    v = SqlVersions(db, f"golden-{_key()}", lambda: {"base": 1})
    assert (v.version, v.current(), v.next_version()) == (0, {"base": 1}, 1)
    assert v.publish({"a": 1}, "first") == 1
    assert v.publish({"a": 2, "ü": "ñ"}, "second") == 2
    assert v.current() == {"a": 2, "ü": "ñ"}
    assert v.rollback() == 1 and v.current() == {"a": 1}
    assert v.at(2) == {"a": 2, "ü": "ñ"}  # nothing was deleted
    assert v.rollback() == 0 and v.current() == {"base": 1}
    with pytest.raises(ValueError):
        v.rollback()
    with pytest.raises(ValueError):
        v.publish({}, " ")


# ---- the world: IDs --------------------------------------------------------------------------------------
def test_same_key_gets_the_same_ids_and_the_counter_moves_at_commit(db: Db) -> None:
    w = _world(db)
    key = _key()
    ids = w.reserve_ids("Investors", 3, key)
    assert ids == ["INV-1", "INV-2", "INV-3"]
    assert w.reserve_ids("Investors", 3, key) == ids
    assert w.counter("Investors") == 1
    w.commit("job-1", ids, [{"key": i, "name": i} for i in ids], record_type="Investors", key=key)
    assert w.counter("Investors") == 4
    assert [e["key"] for e in w.store("Investors")] == ids


def test_ids_skip_taken_and_registered_and_never_reissue(db: Db) -> None:
    w = _world(db)
    w.registry.add("Investors", [{"key": "INV-2", "name": "two"}], source="outside export", batch="b")
    assert w.reserve_ids("Investors", 2, _key(), taken=["INV-1"]) == ["INV-3", "INV-4"]


def test_commit_is_idempotent_and_never_moves_backwards(db: Db) -> None:
    w = _world(db)
    key = _key()
    ids = w.reserve_ids("Investors", 2, key)
    w.commit("job-1", ids, [{"key": i, "name": i} for i in ids], record_type="Investors", key=key)
    w.commit("job-1", ids, [{"key": i, "name": i} for i in ids], record_type="Investors", key=key)
    assert w.counter("Investors") == 3 and len(w.store("Investors")) == 2
    with db.begin() as conn:
        n = conn.execute(sa.select(sa.func.count()).select_from(S.world_commits)
                         .where(S.world_commits.c.sponsor == w.sponsor)).scalar_one()
    assert n == 1


def test_concurrent_jobs_get_disjoint_ids(db: Db) -> None:
    """The bug the files could not fix: two jobs of one sponsor and record type at once took the same IDs."""
    sponsor = _sponsor()
    got: list[list[str]] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def job() -> None:
        try:
            ids = SqlWorld(db, sponsor, templates={"Investors": _Spec()}).reserve_ids("Investors", 5, _key())
            with lock:
                got.append(ids)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=job) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    flat = [i for ids in got for i in ids]
    assert len(flat) == 40 and len(set(flat)) == 40


def test_a_failed_commit_leaves_nothing_behind(db: Db) -> None:
    w = _world(db)
    key = _key()
    ids = w.reserve_ids("Investors", 2, key)
    bad = [{"key": ids[0], "name": "ok"}, {"key": ids[1], "name": "bad", "key_kind": "nonsense"}]
    with pytest.raises(ValueError):
        w.commit("job-1", ids, bad, record_type="Investors", key=key, accounts={"k": "1000"})
    assert w.counter("Investors") == 1 and w.store("Investors") == [] and w.account_entries() == {}
    w.commit("job-1", ids, [{"key": i, "name": i} for i in ids], record_type="Investors", key=key)  # can be retried
    assert w.counter("Investors") == 3


def test_commit_writes_account_mappings_with_the_job_as_source(db: Db) -> None:
    w = _world(db)
    w.commit("job-9", [], [], accounts={"cash|": "1000"}, reason="approved by a person")
    entry = w.account_entries()["cash|"]
    assert (entry["account"], entry["source"], entry["reason"]) == ("1000", "approved in job-9",
                                                                    "approved by a person")


def test_reserving_without_a_scheme_for_a_source_mode_type_is_refused(db: Db) -> None:
    w = SqlWorld(db, _sponsor(), templates={})
    with pytest.raises(ValueError):
        w.reserve_ids("Funds", 1, _key())


def test_required_and_fingerprint(db: Db) -> None:
    w = _world(db)
    assert w.required("Investors", "Investor_Name", False) is True
    assert w.required("Funds", "x", True) is True
    before = w.fingerprint(["Investors"])
    w.registry.add("Investors", [{"key": "A", "name": "a"}], source="quick entry", batch="b")
    assert w.fingerprint(["Investors"]) != before


# ---- the store and the spool -----------------------------------------------------------------------------
def test_store_appends_lines_drops_a_partial_line_and_seals(db: Db) -> None:
    st = SqlStore(db, prefix=f"{_key()}/")
    assert st.get("a/b") is None and st.sealed("a/b") is None
    st.append("a/b", b"one\ntwo\n")
    st.append("a/b", b"three\npart")
    assert list(st.lines("a/b")) == [b"one", b"two", b"three"]
    assert list(st.lines("a/b", skip=2)) == [b"three"]
    assert st.get("a/b") == b"one\ntwo\nthree\npart"
    st.seal("a/b", "d1")
    assert st.sealed("a/b") == "d1"
    st.truncate("a/b", 1)
    assert list(st.lines("a/b")) == [b"one"] and st.sealed("a/b") is None
    st.reset("a/b")
    assert st.get("a/b") is None


def test_store_refuses_a_bad_key(db: Db) -> None:
    with pytest.raises(ValueError):
        SqlStore(db).append("../x", b"")


def test_the_spool_pages_back_exactly_what_it_wrote(db: Db) -> None:
    st = SqlStore(db, prefix=f"{_key()}/")
    spool = Spool(st, "rows", chunk=3)
    rows = [{"n": i, "name": f"row {i}"} for i in range(10)]
    spool.extend(rows)
    digest = spool.seal()
    assert st.sealed("rows") == digest
    assert list(spool_read(st, "rows")) == rows
    assert list(spool_read(st, "rows", skip=8)) == rows[8:]


# ---- memory, recipes, cards ------------------------------------------------------------------------------
def test_memory_rule_lifecycle(db: Db) -> None:
    m = SqlSponsorMemory(db, _sponsor())
    rid = m.propose("Net amounts only", "seen twice")
    assert rid == "R1" and list(m.pending()) == ["R1"] and m.rules_text() == ""
    m.approve(rid)
    assert m.pending() == {} and m.active_ids() == {"R1"}
    assert "R1: Net amounts only" in m.rules_text()
    second = m.propose("Use sheet 2", "e")
    m.reject(second, "not general")
    assert m.pending() == {}
    with pytest.raises(KeyError):
        m.approve("R9")


def test_memory_of_one_sponsor_is_not_another_sponsors(db: Db) -> None:
    a, b = SqlSponsorMemory(db, _sponsor()), SqlSponsorMemory(db, _sponsor())
    a.approve(a.propose("rule", "e"))
    assert b.approved() == {} and b.pending() == {}


def test_legacy_recipe_store_versions(db: Db) -> None:
    r = SqlRecipeStore(db, _sponsor())
    assert r.load("Investors") is None and r.versions("Investors") == []
    variant = {"spec": {"a": 1}, "post": [], "layout": [], "name": "v"}
    v1, stored, new = r.save_variant("Investors", "do it", variant)
    assert (v1, new, stored["variant_id"]) == (1, True, "var1")
    assert r.save_variant("Investors", "do it", variant)[2] is False
    v2, _, new2 = r.save_variant("Investors", "do it", {**variant, "spec": {"a": 2}})
    assert (v2, new2) == (2, True)
    assert r.versions("Investors") == [1, 2] and len(r.variants("Investors")) == 2
    assert r.load("Investors", 1)["variants"][0]["spec"] == {"a": 1}


def _plan(record_type: str, text: str, pin_before: str | None) -> dict[str, Any]:
    from worker.recipes import _pin_of

    return {"record_type": record_type, "field": "Investor_Name", "old": None, "new": {"from": "B"}, "text": text,
            "changed": True, "pin_before": pin_before, "pin_after": _pin_of(text),
            "verify": {"conform": 1, "total": 1, "missing": 0, "failed": {}}}


def test_field_recipe_commit_audits_first_then_stores_and_moves_the_pin(db: Db) -> None:
    sponsor = _sponsor()
    fr = SqlFieldRecipes(db, sponsor)
    audit = SqlAuditLog(db, f"jobs/{_key()}/audit.jsonl")
    assert fr.text("Investors") is None and fr.pin("Investors") is None
    out = fr.commit(_plan("Investors", "fields: one\n", None), "fb-1", audit)
    assert out["written"] is True and fr.text("Investors") == "fields: one\n"
    assert fr.pin("Investors") == out["pin_after"]
    assert [e["action"] for e in audit.read()] == ["recipe.changed"]
    assert [e["action"] for e in fr.history("Investors").read()] == ["recipe.changed"]
    with pytest.raises(RecipeChangeRefused):  # planned against a pin that has since moved
        fr.commit(_plan("Investors", "fields: two\n", None), "fb-2", audit)
    other = SqlFieldRecipes(db, _sponsor())
    assert other.text("Investors") is None


def test_field_recipe_removal_keeps_every_version(db: Db) -> None:
    fr = SqlFieldRecipes(db, _sponsor())
    fr._write("Investors", "a: 1\n")
    fr._write("Investors", "a: 2\n")
    fr._write("Investors", None)
    assert fr.text("Investors") is None
    fr._write("Investors", "a: 3\n")
    assert fr.text("Investors") == "a: 3\n"
    with db.begin() as conn:
        n = conn.execute(sa.select(sa.func.count()).select_from(S.field_recipes)
                         .where(S.field_recipes.c.sponsor == fr.sponsor)).scalar_one()
    assert n == 3


def test_cards_save_load_and_filter(db: Db) -> None:
    store = SqlImprovementStore(db)
    other, cid = f"imp-{_key()[:8]}", f"imp-{_key()[:8]}"
    store.save({"id": other, "status": "blocked", "x": 1})
    store.save({"id": cid, "status": "blocked"})
    store.save({"id": cid, "status": "ready"})
    assert store.load(cid)["status"] == "ready"
    assert [c["id"] for c in store.cards("ready")] == [cid]
    assert {c["id"] for c in store.cards()} == {other, cid}
    with pytest.raises(FileNotFoundError):
        store.load("missing")
    with pytest.raises(ValueError):
        store.load("../x")


def test_documents_survive_json_round_trip_of_odd_text(db: Db) -> None:
    m = SqlSponsorMemory(db, _sponsor())
    m.approve(m.propose("Zażółć gęślą jaźń – “quotes”", "evidence: ü"))
    assert "Zażółć" in json.dumps(m.approved(), ensure_ascii=False)
