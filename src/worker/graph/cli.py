"""`onboarding-worker graph ...`: keep the Neo4j projection in step with the database, or ask a question of either
store. Needs `DATABASE_URL`; Neo4j settings are `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD(_FILE)`."""

from __future__ import annotations

import argparse
import json
import signal
import threading
from collections.abc import Mapping
from typing import TextIO

from worker.db.backend import database_url, db_for


class GraphCliError(Exception):
    """A usage problem, reported as a plain sentence."""


def add_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser("graph", help="Neo4j projection (WORKER_GRAPH=on): project, rebuild, check; lineage queries")
    cmds = p.add_subparsers(dest="graph_command", required=True)
    s = cmds.add_parser("project", help="bring the graph up to the database")
    s.add_argument("--follow", action="store_true", help="repeat every WORKER_GRAPH_INTERVAL_SECONDS (default 30)")
    cmds.add_parser("rebuild", help="delete the graph and project it again from the database")
    cmds.add_parser("fingerprint", help="compare the graph with the SQL views; exit 1 when they differ")
    s = cmds.add_parser("lineage", help="which source IDs became which target IDs (graph when on, else SQL views)")
    s.add_argument("--sponsor", required=True)
    s.add_argument("--template", default="")
    s.add_argument("--source-key", default="")
    s.add_argument("--key", default="")


def run(args: argparse.Namespace, env: Mapping[str, str], out: TextIO) -> int:
    url = database_url(env)
    if not url:
        raise GraphCliError("DATABASE_URL is not set")
    db = db_for(url)
    if args.graph_command == "lineage":
        from worker.graph import answers

        found = answers(db, env).lineage(args.sponsor, template=args.template, source_key=args.source_key,
                                         key=args.key)
        print(json.dumps(found, indent=2), file=out)
        return 0
    from worker.graph.neo import Projector, connect
    from worker.graph.sql import SqlAnswers

    projector = Projector(db, connect(env))
    if args.graph_command == "rebuild":
        print(f"Graph rebuilt: {projector.rebuild()}", file=out)
        return 0
    if args.graph_command == "fingerprint":
        graph, views = projector.fingerprint(), SqlAnswers(db).fingerprint()
        print(f"graph {graph}\nviews {views}\n{'in step' if graph == views else 'DIFFERENT'}", file=out)
        return 0 if graph == views else 1
    if not args.follow:
        print(f"Graph projected: {projector.project()}", file=out)
        return 0
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    interval = float(env.get("WORKER_GRAPH_INTERVAL_SECONDS") or 30)
    while not stop.is_set():
        try:
            projector.project()
        except Exception as exc:  # noqa: BLE001  a graph that is down is retried; it is never a reason to stop
            print(f"graph not projected: {exc}", file=out, flush=True)
        stop.wait(interval)
    return 0
