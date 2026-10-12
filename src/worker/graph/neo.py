"""The Neo4j projection: built from the database's rows, rebuilt at will, never authoritative.

Model (natural keys, all `MERGE`, so applying a batch twice changes nothing):

    (:Sponsor {sponsor})-[:HAS]->(:Record {sponsor, template, key, name})
    (:SourceId {sponsor, template, value})-[:BECAME {job, batch, source}]->(:Record)
    (:SourceAccount {store, key})-[:MAPS_TO {source, reason}]->(:Account {number})

The cursor (`:ProjectionCursor`) is written in the same transaction as its batch. Row ids are not guaranteed to
commit in order, so each run re-reads a window below the cursor; the merge makes that harmless.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from neo4j import Driver, GraphDatabase

from worker.db.core import Db
from worker.graph.sql import account_rows, fingerprint, ident, lineage_rows

BATCH = 5000
OVERLAP = 1000

REGISTRY_CYPHER = """
UNWIND $rows AS r
MERGE (sp:Sponsor {sponsor: r.sponsor})
MERGE (rec:Record {sponsor: r.sponsor, template: r.template, key: r.key})
  SET rec.name = r.name
MERGE (sp)-[:HAS]->(rec)
WITH r, rec WHERE r.source_key <> ''
MERGE (src:SourceId {sponsor: r.sponsor, template: r.template, value: r.source_key})
MERGE (src)-[b:BECAME]->(rec)
  SET b.job = r.job, b.batch = r.batch, b.source = r.source
"""

ACCOUNT_CYPHER = """
UNWIND $rows AS r
MERGE (sa:SourceAccount {store: r.store, key: r.key})
WITH sa, r
OPTIONAL MATCH (sa)-[old:MAPS_TO]->(x:Account) WHERE x.number <> r.account
DELETE old
WITH DISTINCT sa, r
MERGE (a:Account {number: r.account})
MERGE (sa)-[m:MAPS_TO]->(a)
  SET m.source = r.source, m.reason = r.reason
"""

CURSOR_CYPHER = "MERGE (c:ProjectionCursor {name: $name}) SET c.id = $id"


def registry_params(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The `$rows` of REGISTRY_CYPHER: pure, so a test needs no server."""
    return {"rows": [{"sponsor": r["sponsor"], "template": r["template"], "key": ident(r), "name": r["name"],
                      "source_key": r["source_key"], "job": r["job"], "batch": r["batch"], "source": r["source"]}
                     for r in rows]}


def account_params(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"rows": [{k: r[k] for k in ("store", "key", "account", "source", "reason")} for r in rows]}


def connect(env: Mapping[str, str]) -> Driver:
    password = env.get("NEO4J_PASSWORD", "")
    if not password and env.get("NEO4J_PASSWORD_FILE"):
        password = Path(env["NEO4J_PASSWORD_FILE"]).read_text(encoding="utf-8").strip()
    return GraphDatabase.driver(env.get("NEO4J_URI") or "bolt://localhost:7687",
                                auth=(env.get("NEO4J_USER") or "neo4j", password))


class Projector:
    def __init__(self, db: Db, driver: Driver) -> None:
        self.db = db
        self.driver = driver

    # -- cursor
    def _cursor(self, name: str) -> int:
        with self.driver.session() as s:
            rec = s.run("MATCH (c:ProjectionCursor {name: $name}) RETURN c.id AS id", name=name).single()
        return int(rec["id"]) if rec else 0

    def _apply(self, cypher: str, params: dict[str, Any], name: str, last_id: int) -> None:
        def work(tx: Any) -> None:
            tx.run(cypher, **params)
            tx.run(CURSOR_CYPHER, name=name, id=last_id)

        with self.driver.session() as s:
            s.execute_write(work)

    def constraints(self) -> None:
        with self.driver.session() as s:
            for label, props in (("Sponsor", "sponsor"), ("SourceAccount", "store, key"), ("Account", "number"),
                                 ("ProjectionCursor", "name"), ("Record", "sponsor, template, key"),
                                 ("SourceId", "sponsor, template, value")):
                cols = ", ".join(f"n.{p.strip()}" for p in props.split(","))
                s.run(f"CREATE CONSTRAINT {label.lower()}_key IF NOT EXISTS FOR (n:{label}) REQUIRE ({cols}) IS UNIQUE")

    # -- projection
    def project(self) -> dict[str, int]:
        """Bring the graph up to the database. Returns how many rows each part applied."""
        self.constraints()
        done = {"registry": 0, "accounts": 0}
        for name, rows_of, cypher, params_of in (
            ("registry", lineage_rows, REGISTRY_CYPHER, registry_params),
            ("accounts", account_rows, ACCOUNT_CYPHER, account_params),
        ):
            after = max(0, self._cursor(name) - OVERLAP)
            while True:
                rows = rows_of(self.db, after, BATCH)
                if not rows:
                    break
                after = int(rows[-1]["id"])
                self._apply(cypher, params_of(rows), name, after)
                done[name] += len(rows)
        return done

    def rebuild(self) -> dict[str, int]:
        """Delete everything in the graph and project from the start (the graph is never restored from a backup)."""
        with self.driver.session() as s:
            s.run("MATCH (n) CALL { WITH n DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS")
        return self.project()

    def fingerprint(self) -> str:
        """The same digest `SqlAnswers.fingerprint` computes from the views: equal when the graph is in step."""
        with self.driver.session() as s:
            lineage = [(r["sp"], r["t"], r["v"], r["k"]) for r in s.run(
                "MATCH (s:SourceId)-[:BECAME]->(r:Record) "
                "RETURN s.sponsor AS sp, s.template AS t, s.value AS v, r.key AS k")]
            accounts = [(r["st"], r["k"], r["a"]) for r in s.run(
                "MATCH (sa:SourceAccount)-[:MAPS_TO]->(a:Account) RETURN sa.store AS st, sa.key AS k, a.number AS a")]
        return fingerprint(lineage, accounts)


class GraphAnswers:
    def __init__(self, driver: Driver) -> None:
        self.driver = driver

    def lineage(self, sponsor: str, *, template: str = "", source_key: str = "", key: str = "") -> list[dict[str, Any]]:
        query = ("MATCH (s:SourceId {sponsor: $sponsor})-[b:BECAME]->(r:Record) "
                 "WHERE ($template = '' OR s.template = $template) AND ($source_key = '' OR s.value = $source_key) "
                 "AND ($key = '' OR r.key = $key) "
                 "RETURN s.template AS template, s.value AS source_key, r.key AS key, r.name AS name, "
                 "b.job AS job, b.batch AS batch ORDER BY template, source_key, key")
        with self.driver.session() as s:
            return [dict(r) for r in s.run(query, sponsor=sponsor, template=template, source_key=source_key,
                                           key=key)]

    def accounts(self, store: str) -> dict[str, str]:
        with self.driver.session() as s:
            return {r["k"]: r["a"] for r in s.run(
                "MATCH (sa:SourceAccount {store: $store})-[:MAPS_TO]->(a:Account) RETURN sa.key AS k, a.number AS a",
                store=store)}
