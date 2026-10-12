"""`onboarding-worker db ...`: migrate, verify the chains, write and check anchors. Run as their own services in the
compose file (`migrate` once; `verifier` and `anchor` with `--follow`)."""

from __future__ import annotations

import argparse
import signal
import threading
from collections.abc import Mapping
from typing import TextIO

from worker.db import anchor as A
from worker.db import verify as V
from worker.db.backend import database_url, db_for
from worker.db.migrate import current, head, upgrade


class DbCliError(Exception):
    """A usage problem (no database configured, no sink); reported as a plain sentence."""


def add_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser("db", help="database: migrate, verify the audit chains, anchor their heads")
    cmds = p.add_subparsers(dest="db_command", required=True)
    cmds.add_parser("migrate", help="apply the schema migrations (needs the owner's credentials)")
    s = cmds.add_parser("verify", help="re-check every audit chain (and the anchors, when a sink is set)")
    s.add_argument("--follow", action="store_true", help="keep checking: incremental often, full now and then")
    s.add_argument("--anchor-dir", default="", help="compare with the anchors in this directory")
    s.add_argument("--restore-drill", action="store_true",
                   help="a restored backup may end before the latest anchor: report that, fail on anything else")
    s = cmds.add_parser("anchor", help="write the head of every stream to a sink outside the database")
    s.add_argument("--anchor-dir", default="", help="the sink directory (or WORKER_ANCHOR_DIR)")
    s.add_argument("--follow", action="store_true", help="every WORKER_ANCHOR_INTERVAL_SECONDS (default 900)")


def _db(env: Mapping[str, str]):  # type: ignore[no-untyped-def]
    url = database_url(env)
    if not url:
        raise DbCliError("DATABASE_URL is not set")
    return db_for(url)


def _sink(args: argparse.Namespace, env: Mapping[str, str]) -> A.DirectorySink | None:
    directory = args.anchor_dir or env.get("WORKER_ANCHOR_DIR", "")
    return A.DirectorySink(directory) if directory else None


def _stop_on_signal() -> threading.Event:
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    return stop


def run(args: argparse.Namespace, env: Mapping[str, str], out: TextIO) -> int:
    db = _db(env)
    if args.db_command == "migrate":
        upgrade(db)
        print(f"Database at revision {current(db)} (this build ships {head()}).", file=out)
        return 0
    sink = _sink(args, env)
    interval_key = "WORKER_ANCHOR_INTERVAL_SECONDS" if args.db_command == "anchor" else "WORKER_VERIFY_INTERVAL_SECONDS"
    fallback = A.DEFAULT_INTERVAL_SECONDS if args.db_command == "anchor" else 300
    interval = float(env.get(interval_key) or fallback)
    if args.db_command == "anchor":
        if sink is None:
            raise DbCliError("an anchor needs a sink: --anchor-dir or WORKER_ANCHOR_DIR")
        if not args.follow:
            doc = A.write(db, sink)
            print(f"Anchor {doc['number']} written: {len(doc['streams'])} stream heads, root {doc['merkle_root']}.",
                  file=out)
            return 0
        A.follow(db, sink, interval=interval, stop=_stop_on_signal(),
                 on_error=lambda exc: print(f"anchor not written: {exc}", file=out, flush=True))
        return 0
    anchors = (lambda: A.check(db, sink, max_age_seconds=2 * A.DEFAULT_INTERVAL_SECONDS)) if sink else None
    if args.follow:
        V.follow(db, interval=interval, full_every=12, stop=_stop_on_signal(), check_anchors=anchors,
                 on_report=lambda r: None if r.ok else print(f"AUDIT PROBLEM: {r.summary()}", file=out, flush=True))
        return 0
    report = V.verify_streams(db)
    if anchors is not None:
        report.anchor_problems = anchors()
    if args.restore_drill:  # an older backup than the anchor is the expected finding of a drill
        behind = [p for p in report.anchor_problems if p["code"] == "behind_anchor"]
        report.anchor_problems = [p for p in report.anchor_problems if p["code"] not in ("behind_anchor",
                                                                                         "anchor_stale")]
        if behind:
            print(f"The backup ends before the latest anchor: {len(behind)} stream head(s) are newer than the "
                  f"dump (up to {max(p['entries_behind'] for p in behind)} entries). That is the work since the "
                  "backup, not a rewrite.", file=out)
    summary = report.summary()
    print(f"Audit verification: {'OK' if report.ok else 'BROKEN'} ({summary['entries']} entries in "
          f"{summary['streams']} streams)", file=out)
    for b in summary["broken"]:
        print(f"  {b['stream']}: seq {b.get('seq')}: {b.get('reason')}", file=out)
    for p in summary["anchor_problems"]:
        print(f"  anchor: {p}", file=out)
    return 0 if report.ok else 1
