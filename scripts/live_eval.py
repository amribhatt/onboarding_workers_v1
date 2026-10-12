"""Live evaluation (marked live: calls a real model, never run by pytest).

Runs the agent on tests/fixtures/set7 and set8 with --auto-approve, writes outputs and a score table
under out/eval/. Needs a model key in the environment, in .env, or in the file named by WORKER_ENV_FILE.

    uv run python scripts/live_eval.py [--out out/eval] [--sets set7,set8] [--nudges 2]

The agent stops at its per-message tool budget and asks the person; `--nudges N` sends up to N plain "continue"
messages while an asked-for upload file is not written (0 is the one-message run). Gates are unchanged:
--auto-approve still rejects an open ERR, a rule, a supplied value and an ID scheme. The ID decision is a named
person's: the setup records the sponsor's standard numbering (the template's own ID format) as `eval-setup` before
the run, the way the tests do, so the model never makes it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from worker import persist
from worker import record_facts as RF
from worker.agent import new_thread_id
from worker.cli import _build_session, build_parser, load_env
from worker.inbox import inbox_message
from worker.score import format_table, score

GP_ID = "GP-001"
SPONSOR = "sponsor-a"
ASKED = ("Investors", "Commitments", "GL")
NUDGE = ("Go ahead and continue until the upload files are written. Do not change the source files; leave what you "
         "cannot fill blank and report it as MISSING.")


def run_set(set_dir: Path, out_dir: Path, state_dir: Path, nudges: int = 0) -> int:
    """Run one set; returns the number of nudges sent."""
    sources = sorted(p for p in set_dir.glob("*.xlsx") if p.is_file())
    if not sources:
        raise SystemExit(f"no source workbooks in {set_dir}")
    args = build_parser().parse_args(
        [
            "run",
            str(sources[0]),
            "--sponsor",
            SPONSOR,
            "--gp-id",
            GP_ID,
            "--out",
            str(out_dir),
            "--state-dir",
            str(state_dir),
            "--auto-approve",
        ]
    )
    session = _build_session(
        args, load_env(), None, None, input, sys.stdout, set_dir, GP_ID, out_dir, new_thread_id()
    )
    for rt in RF.id_decision_types():  # the named person's ID decision, made at setup and audited in this run
        spec = session.ctx.stage.templates.get(rt)
        if spec is not None:
            persist.id_scheme(state_dir / "ids", SPONSOR, rt, spec.id_generation).change(
                dict(spec.id_generation), by="eval-setup", reason="the sponsor's standard numbering (evaluation setup)",
                record=session.ctx.audit.record)
    names = ", ".join(p.name for p in sources)
    session.send(
        f"Build me the Ledgerline sheets (Investors, Commitments and GL) from these files: {names}. "
        f"Sponsor {SPONSOR}, GP_ID {GP_ID}.\n" + inbox_message([set_dir])
    )
    sent = 0
    while sent < nudges and not all(any(out_dir.glob(f"{t}_Upload.*")) for t in ASKED):
        sent += 1
        session.send(NUDGE)
    session.finish(state_dir)
    return sent


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="out/eval")
    p.add_argument("--sets", default="set7,set8")
    p.add_argument("--nudges", type=int, default=0, help="plain 'continue' messages while no upload is written")
    args = p.parse_args(argv)
    base = Path(args.out)
    tables = []
    for name in [s.strip() for s in args.sets.split(",") if s.strip()]:
        set_dir = ROOT / "tests" / "fixtures" / name
        out_dir = base / name
        if out_dir.exists() and any(out_dir.iterdir()):
            raise SystemExit(f"{out_dir} is not empty; remove it or pick another --out")
        sent = run_set(set_dir, out_dir, base / "state" / name, args.nudges)
        tables.append(f"== {name} (nudges sent: {sent})\n{format_table(score(out_dir, set_dir / 'answer_key'))}")
    text = "\n\n".join(tables)
    base.mkdir(parents=True, exist_ok=True)
    (base / "score.txt").write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
