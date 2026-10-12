"""The graph is a derived view: the SQL views answer the questions by default, and Neo4j (a projection, switched
on with WORKER_GRAPH=on) must give the same answers. The driver is imported only inside `worker.graph`."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from worker.db.core import Db
from worker.db.stores import SqlAccountStore, SqlWorld
from worker.graph import answers, enabled
from worker.graph.sql import SqlAnswers, fingerprint

SRC = Path(__file__).resolve().parents[1] / "src" / "worker"
SCHEME = {"mode": "sequential", "prefix": "INV-", "width": 0, "start": 1}


class _Spec:
    id_generation = SCHEME
    required = frozenset()


def _sponsor() -> str:
    return f"sp-{uuid.uuid4().hex[:8]}"


def _commit(world: SqlWorld, job: str, sources: list[str]) -> list[str]:
    key = uuid.uuid4().hex
    ids = world.reserve_ids("Investors", len(sources), key)
    world.commit(job, ids, [{"key": i, "name": f"Name of {s}", "source_key": s} for i, s in zip(ids, sources,
                                                                                               strict=True)],
                 record_type="Investors", key=key, batch=job)
    return ids


def test_the_toggle_defaults_to_off_and_off_means_the_sql_views(db: Db) -> None:
    assert not enabled({}) and not enabled({"WORKER_GRAPH": "off"}) and enabled({"WORKER_GRAPH": "ON"})
    assert isinstance(answers(db, {}), SqlAnswers)


def test_lineage_answers_which_source_id_became_which_target_id(db: Db) -> None:
    sponsor = _sponsor()
    world = SqlWorld(db, sponsor, templates={"Investors": _Spec()})
    ids = _commit(world, "job-1", ["HS-1", "HS-2"])
    other = SqlWorld(db, _sponsor(), templates={"Investors": _Spec()})
    _commit(other, "job-x", ["HS-1"])
    got = SqlAnswers(db).lineage(sponsor)
    assert [(r["source_key"], r["key"], r["batch"]) for r in got] == [("HS-1", ids[0], "job-1"),
                                                                      ("HS-2", ids[1], "job-1")]
    assert SqlAnswers(db).lineage(sponsor, source_key="HS-2")[0]["key"] == ids[1]
    assert SqlAnswers(db).lineage(sponsor, key=ids[0])[0]["source_key"] == "HS-1"
    assert SqlAnswers(db).lineage(sponsor, template="Funds") == []


def test_a_record_registered_without_an_id_has_no_lineage(db: Db) -> None:
    sponsor = _sponsor()
    world = SqlWorld(db, sponsor, templates={"Investors": _Spec()})
    world.registry.add("Investors", [{"key": "", "name": "No Id Ltd"}], source="outside export", batch="b")
    assert SqlAnswers(db).lineage(sponsor) == []


def test_the_current_account_is_the_latest_mapping(db: Db) -> None:
    store = SqlAccountStore(db, _sponsor())
    store.add("cash|", "1000", source="job-1", reason="first")
    store.add("rent|", "6100", source="job-1", reason="first")
    store.add("cash|", "1010", source="job-2", reason="corrected")
    assert SqlAnswers(db).accounts(store.name) == {"cash|": "1010", "rent|": "6100"}


def test_the_fingerprint_does_not_depend_on_order() -> None:
    a = [("s", "Investors", "HS-1", "INV-1"), ("s", "Investors", "HS-2", "INV-2")]
    assert fingerprint(a, [("st", "k", "1")]) == fingerprint(list(reversed(a)), [("st", "k", "1")])
    assert fingerprint(a, []) != fingerprint(a[:1], [])


def test_only_the_graph_package_imports_the_neo4j_driver() -> None:
    offenders = [str(p.relative_to(SRC)) for p in SRC.rglob("*.py")
                 if "graph" not in p.relative_to(SRC).parts[:1]
                 and any(line.strip().startswith(("import neo4j", "from neo4j")) for line in
                         p.read_text(encoding="utf-8").splitlines())]
    assert offenders == []


# ---- against a real Neo4j (integration) --------------------------------------------------------------------
@pytest.mark.integration
def test_the_projection_matches_the_views_and_rebuilds(db: Db) -> None:
    uri = os.environ.get("WORKER_TEST_NEO4J_URI", "")
    if not uri:
        pytest.skip("WORKER_TEST_NEO4J_URI is not set")
    from worker.graph.neo import GraphAnswers, Projector, connect

    env = {"NEO4J_URI": uri, "NEO4J_USER": "neo4j", "NEO4J_PASSWORD": os.environ.get("WORKER_TEST_NEO4J_PASSWORD", "")}
    driver = connect(env)
    sponsor = _sponsor()
    world = SqlWorld(db, sponsor, templates={"Investors": _Spec()})
    projector = Projector(db, driver)
    projector.rebuild()
    ids = _commit(world, "job-1", ["HS-1", "HS-2"])
    SqlAccountStore(db, sponsor).add("cash|", "1000", source="job-1", reason="first")
    projector.project()
    sql = SqlAnswers(db)
    assert projector.fingerprint() == sql.fingerprint()
    assert GraphAnswers(driver).lineage(sponsor) == sql.lineage(sponsor)
    ids2 = _commit(world, "job-2", ["HS-3"])  # incremental
    SqlAccountStore(db, sponsor).add("cash|", "1010", source="job-2", reason="corrected")
    projector.project()
    assert projector.fingerprint() == sql.fingerprint()
    assert [r["key"] for r in GraphAnswers(driver).lineage(sponsor)] == [*ids, *ids2]
    assert GraphAnswers(driver).accounts(f"{sponsor}") == {"cash|": "1010"} == sql.accounts(sponsor)
    before = projector.fingerprint()
    projector.rebuild()  # disposable: rebuilt, not restored
    assert projector.fingerprint() == before == sql.fingerprint()
    driver.close()
