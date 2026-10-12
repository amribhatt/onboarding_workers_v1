"""Terminal interface: give the agent a file, talk to it, approve or correct its gated actions."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TextIO

from dotenv import dotenv_values
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.sqlite import SqliteSaver

from worker import persist
from worker.agent import (
    PROVIDERS,
    GateRefused,
    build_agent,
    check_approval,
    decide,
    model_for,
    new_thread_id,
    pending_gate,
    start,
)
from worker.audit import AuditLog
from worker.context import Budget, Limits, RunContext
from worker.inbox import inbox_message
from worker.resolve import model_ranker
from worker.staging import Stage
from worker.templates import templates_for
from worker.transcript import IMAGE_TYPES, image_block, last_coverage, last_say

InputFn = Callable[[str], str]
_DEFAULT_STATE = os.environ.get("WORKER_STATE_DIR", ".worker_state")
MAX_ARG_CHARS = 100


class CliError(Exception):
    """A one-line, user-facing failure."""


def load_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """`.env` in the working directory (or the file named by WORKER_ENV_FILE) under the real environment."""
    environ = os.environ if environ is None else environ
    path = Path(environ.get("WORKER_ENV_FILE") or ".env")
    merged: dict[str, str] = {}
    if path.is_file():
        merged.update({k: v for k, v in dotenv_values(path).items() if v is not None})
    merged.update(environ)
    return merged


def _short(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = " ".join(text.split())
    return text if len(text) <= MAX_ARG_CHARS else text[: MAX_ARG_CHARS - 3] + "..."


class Session:
    """One conversation: context, agent, thread and the approve / correct loop."""

    def __init__(
        self,
        ctx: RunContext,
        agent: Any,
        thread_id: str,
        input_fn: InputFn,
        out: TextIO,
        auto_approve: bool = False,
    ) -> None:
        self.ctx, self.agent, self.thread_id = ctx, agent, thread_id
        self.input_fn, self.out, self.auto_approve = input_fn, out, auto_approve

    def say(self, text: str = "") -> None:
        """Print a line. A character the terminal cannot show (a model's emoji on a Windows cp1252 console) is
        replaced, never fatal: a reply is not worth losing the run over."""
        try:
            print(text, file=self.out)
        except UnicodeEncodeError:
            enc = getattr(self.out, "encoding", None) or "ascii"
            print(text.encode(enc, errors="replace").decode(enc, errors="replace"), file=self.out)

    def send(self, message: str, attachments: list[dict[str, Any]] | None = None) -> None:
        result = start(self.agent, self.ctx, message, self.thread_id, attachments)
        self._drive(result)

    def _drive(self, result: dict[str, Any]) -> None:
        while True:
            said = last_say(result)
            if said:
                self.say(f"agent> {said}")
            gates = pending_gate(result)
            if not gates:
                return
            result = self._gate(result, gates)

    def _gate(self, result: dict[str, Any], gates: list[dict[str, Any]]) -> dict[str, Any]:
        names = [g.get("name", "?") for g in gates]
        self.say("")
        self.say(f"== Needs your decision: {', '.join(names)}")
        for gate in gates:
            self.say(f"  {gate.get('name', '?')}")
            for key, value in (gate.get("args") or {}).items():
                self.say(f"    {key}: {_short(value)}")
        coverage = last_coverage(result)
        if coverage:
            self.say("  Coverage so far:")
            for line in coverage.splitlines()[:25]:
                self.say(f"    {line}")
        refusal = self._refusal(gates)
        if refusal is not None and not self.auto_approve:
            self.say(f"  {refusal}")
        if self.auto_approve:
            ack: dict[str, str] = {}
            if refusal is not None and refusal.review["open_err"]:
                self.say("  --auto-approve: the code refuses this approval (ERR open); rejecting.")
                decision = {"type": "reject", "message": str(refusal)}
            elif "propose_rule" in names:
                self.say("  --auto-approve: rules are never auto-approved; rejecting this proposal.")
                decision = {"type": "reject", "message": "auto-approve never approves a rule; a human must."}
            elif "supply_value" in names:  # a supplied value is a person's; no person is here (never invent)
                self.say("  --auto-approve: a value is only supplied by a human; rejecting.")
                decision = {"type": "reject", "message": (
                    "auto-approve never supplies a value: no human gave it. Leave the field blank, report it as "
                    "MISSING and continue with the rest.")}
            elif "set_id_scheme" in names:  # the ID decision is a named person's (idgen.decision_refusal)
                self.say("  --auto-approve: the ID decision is a person's; rejecting.")
                decision = {"type": "reject", "message": (
                    "auto-approve never decides an ID scheme: a named person does. Use the scheme already decided "
                    "for this sponsor; if none is decided, say so and leave the write for a person.")}
            else:
                self.say("  --auto-approve: approved without a human.")
                decision = {"type": "approve"}
                if refusal is not None:  # WARN only: acknowledged by the evaluation switch, on the record
                    note = "auto-approve (evaluation): warning not reviewed by a human"
                    ack = dict.fromkeys(refusal.review["unacknowledged_warn"], note)
            return decide(
                self.agent, self.ctx, self.thread_id, decision, "auto-approve (evaluation)",
                {"reviewer": "auto-approve"}, ack,
            )
        answer = self.input_fn("[a]pprove / [r]eject / or type feedback in plain words> ").strip()
        low = answer.lower()
        if low in {"a", "approve", "y", "yes"}:
            try:
                check_approval(self.ctx, gates)
            except GateRefused as exc:
                if exc.review["open_err"]:  # ERR cannot be approved past: send it back to be resolved
                    decision = {"type": "reject", "message": str(exc)}
                    return decide(self.agent, self.ctx, self.thread_id, decision, "approval refused: ERR open")
                reason = self.input_fn("Reason to acknowledge the warning(s) above> ").strip()
                ack = {fid: reason for fid in exc.review["unacknowledged_warn"]} if reason else {}
                try:
                    check_approval(self.ctx, gates, ack)
                except GateRefused:
                    decision = {"type": "reject", "message": "Warnings not acknowledged with a reason."}
                    return decide(self.agent, self.ctx, self.thread_id, decision, "no acknowledgement")
                return decide(
                    self.agent, self.ctx, self.thread_id, {"type": "approve"}, "approved with acknowledgement",
                    {"reviewer": "cli"}, ack,
                )
            decision = {"type": "approve"}
        elif low in {"r", "reject", "n", "no", ""}:
            decision = {"type": "reject", "message": "Rejected by the human without comment."}
        else:
            decision = {"type": "reject", "message": answer}
        # the person at the terminal is the reviewer on the record; the upload's Approval is built from this name
        return decide(self.agent, self.ctx, self.thread_id, decision, meta={"reviewer": "cli"})

    def _refusal(self, gates: list[dict[str, Any]]) -> GateRefused | None:
        try:
            check_approval(self.ctx, gates)
        except GateRefused as exc:
            return exc
        return None

    def finish(self, state_dir: Path) -> int:
        files = sorted(p for p in self.ctx.out_dir.glob("*_Upload.*") if p.is_file())
        self.say("")
        self.say("Output files:" if files else "Output files: none written.")
        for p in files:
            self.say(f"  {p}")
        ok, n = self.ctx.audit.verify()
        self.say(f"Audit file: {self.ctx.audit.path}")
        self.say(f"Audit verification: {'OK' if ok else 'FAILED'} ({n} entries)")
        self.say(f"Continue with: python -m worker.cli chat --sponsor {self.ctx.sponsor}")
        return 0 if ok else 1


def _state_file(state_dir: Path, sponsor: str) -> Path:
    return state_dir / sponsor / "session.json"


def _build_session(
    args: argparse.Namespace,
    env: Mapping[str, str],
    model: BaseChatModel | None,
    saver: Any,
    input_fn: InputFn,
    out: TextIO,
    inbox: Path,
    gp_id: str,
    out_dir: Path,
    thread_id: str,
) -> Session:
    state_dir = Path(args.state_dir)
    persist.use_state_dir(state_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ctx = RunContext(
        sponsor=args.sponsor,
        gp_id=gp_id,
        inbox_roots=[inbox],
        out_dir=out_dir,
        audit=persist.audit(out_dir / "audit.jsonl"),
        stage=Stage(out_dir, templates_for(args.sponsor)),
        id_root=state_dir / "ids",
        accounts_root=state_dir / "accounts",
        feedback=persist.feedback(state_dir / "feedback", args.sponsor),
        registry=persist.registry(state_dir, args.sponsor),
        recipes=persist.recipe_store(state_dir / "recipes", args.sponsor),
        memory=persist.memory(state_dir / "memory", args.sponsor),
        budget=Budget.from_env(env),
        limits=Limits.from_env(env),
        env=dict(env),
    )
    if env.get("WORKER_RANK_ACCOUNTS", "").strip().lower() in ("1", "true", "yes"):
        ctx.ranker = model_ranker(budget=ctx.budget, audit=ctx.audit, env=env)  # role `small`
    if model is None:  # the Deep Agent runs on the `exec` role; --model overrides it
        override = {"WORKER_MODEL": args.model, "WORKER_MODEL_EXEC": args.model} if args.model else {}
        model = model_for("exec", {**env, **override})
    (state_dir / args.sponsor).mkdir(parents=True, exist_ok=True)
    if saver is None:
        db = state_dir / args.sponsor / "checkpoints.sqlite"
        saver = SqliteSaver(sqlite3.connect(str(db), check_same_thread=False))
    agent = build_agent(ctx, model, saver)
    _state_file(state_dir, args.sponsor).write_text(
        json.dumps(
            {"thread_id": thread_id, "gp_id": gp_id, "out": str(out_dir), "inbox": str(inbox)}, indent=2
        ),
        encoding="utf-8",
    )
    return Session(ctx, agent, thread_id, input_fn, out, getattr(args, "auto_approve", False))


def cmd_run(
    args: argparse.Namespace, env: Mapping[str, str], model: Any, saver: Any, input_fn: InputFn, out: TextIO
) -> int:
    path = Path(args.file)
    if not path.is_file():
        raise CliError(f"file not found: {path}")
    path = path.resolve()
    if args.auto_approve:
        print(
            "WARNING: --auto-approve is for scripted evaluation only; no human checks the upload.", file=out
        )
    attachments = [image_block(path)] if path.suffix.lower() in IMAGE_TYPES else None
    session = _build_session(
        args, env, model, saver, input_fn, out, path.parent, args.gp_id, Path(args.out), new_thread_id()
    )
    session.send(
        f"Build me the Ledgerline sheet from {path.name}. Sponsor {args.sponsor}, GP_ID {args.gp_id}.\n"
        + inbox_message([path.parent]),
        attachments,
    )
    return session.finish(Path(args.state_dir))


def cmd_chat(
    args: argparse.Namespace, env: Mapping[str, str], model: Any, saver: Any, input_fn: InputFn, out: TextIO
) -> int:
    state_dir = Path(args.state_dir)
    saved: dict[str, str] = {}
    sf = _state_file(state_dir, args.sponsor)
    if sf.is_file():
        saved = json.loads(sf.read_text(encoding="utf-8"))
    else:
        print(f"Note: no earlier session for {args.sponsor}; starting a new conversation.", file=out)
    if args.auto_approve:
        print(
            "WARNING: --auto-approve is for scripted evaluation only; no human checks the upload.", file=out
        )
    inbox = Path(args.inbox or saved.get("inbox") or ".")
    out_dir = Path(args.out or saved.get("out") or "out")
    gp_id = args.gp_id or saved.get("gp_id") or ""
    session = _build_session(
        args,
        env,
        model,
        saver,
        input_fn,
        out,
        inbox,
        gp_id,
        out_dir,
        saved.get("thread_id") or new_thread_id(),
    )
    session.say("Chat. Type a message; an empty line or /quit ends.")
    while True:
        try:
            line = input_fn("you> ").strip()
        except EOFError:
            break
        if not line or line == "/quit":
            break
        session.send(line)
    return session.finish(state_dir)


def cmd_serve(
    args: argparse.Namespace, env: Mapping[str, str], model: Any, saver: Any, input_fn: InputFn, out: TextIO
) -> int:
    from worker.web import serve

    if (env.get("WORKER_REQUIRE_TOKEN") or "") == "1" and not env.get("WORKER_APPROVER_TOKEN"):
        raise CliError("WORKER_REQUIRE_TOKEN=1 and WORKER_APPROVER_TOKEN is not set: approvals would be open")
    print(f"Onboarding Worker console: http://{args.host}:{args.port}  (Ctrl+C to stop)", file=out)
    if args.host not in ("127.0.0.1", "localhost", "::1") and not env.get("WORKER_APPROVER_TOKEN"):
        print(
            "WARNING: listening beyond localhost without WORKER_APPROVER_TOKEN; anyone who can reach this "
            "port can approve uploads.",
            file=out,
        )
    serve(args.host, args.port, args.state_dir)
    return 0


def cmd_demo(
    args: argparse.Namespace, env: Mapping[str, str], model: Any, saver: Any, input_fn: InputFn, out: TextIO
) -> int:
    from worker.demo import seed_inbox

    state = Path(args.state_dir)
    names = seed_inbox(state / "inbox")
    os.environ["WORKER_MODEL_PROVIDER"] = "demo"
    print(f"Seeded {len(names)} sample workbook(s) into {state / 'inbox'}.", file=out)
    print("Offline demo model: build the sheet from Investor_Roster_side_by_side.xlsx.", file=out)
    if not args.no_open:
        import threading
        import webbrowser

        threading.Timer(1.5, webbrowser.open, args=[f"http://{args.host}:{args.port}"]).start()
    return cmd_serve(args, env, model, saver, input_fn, out)


def cmd_models(
    args: argparse.Namespace, env: Mapping[str, str], model: Any, saver: Any, input_fn: InputFn, out: TextIO
) -> int:
    import httpx

    from worker import models as catalog

    try:
        found = catalog.free_models(refresh=True)
    except httpx.HTTPError as exc:
        raise CliError(f"could not reach OpenRouter: {exc}") from exc
    usable = [m for m in found if m["tools"]]
    print(f"{len(usable)} free models can call tools ({len(found) - len(usable)} cannot):", file=out)
    for m in usable:
        tags = ", ".join(t for t, on in (("vision", m["vision"]), ("router", m["router"])) if on)
        star = "*" if m["preferred"] else " "
        print(f" {star} {m['id']:<52} {m['context'] // 1000:>5}k  {tags}", file=out)
    print("(* = seen working with this project; probe before relying on one)", file=out)
    for mid in args.probe:
        result = catalog.probe(mid, env)
        verdict = "OK" if result["ok"] else f"FAILED: {result.get('error', '')}"
        print(f"probe {mid}: {verdict} ({result['seconds']}s)", file=out)
    return 0


def cmd_doctor(
    args: argparse.Namespace, env: Mapping[str, str], model: Any, saver: Any, input_fn: InputFn, out: TextIO
) -> int:
    from worker import __version__
    from worker.templates import TEMPLATES

    problems = 0

    def check(ok: bool, label: str, detail: str = "", fatal: bool = True) -> None:
        nonlocal problems
        mark = "ok  " if ok else ("FAIL" if fatal else "warn")
        print(f"[{mark}] {label}{': ' + detail if detail else ''}", file=out)
        if not ok and fatal:
            problems += 1

    print(f"onboarding-worker {__version__} on Python {sys.version.split()[0]}", file=out)
    check(
        sys.version_info[:2] == (3, 12),
        "Python 3.12",
        f"found {sys.version_info.major}.{sys.version_info.minor}",
    )
    check(len(TEMPLATES) == 6, "upload templates", f"{len(TEMPLATES)} loaded: {', '.join(TEMPLATES)}")
    provider = (env.get("WORKER_MODEL_PROVIDER") or "openai").lower()
    keyname = PROVIDERS[provider].key_env if provider in PROVIDERS else ""
    check(provider in PROVIDERS, "provider", provider)
    if keyname:
        check(bool(env.get(keyname)), f"{keyname} set", "" if env.get(keyname) else "add it to .env")
    check(
        bool(env.get("WORKER_MODEL")) or provider == "demo",
        "WORKER_MODEL",
        env.get("WORKER_MODEL", "(default)"),
        False,
    )
    state = Path(args.state_dir)
    try:
        state.mkdir(parents=True, exist_ok=True)
        probe = state / ".doctor"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        check(True, "state dir writable", str(state.resolve()))
    except OSError as exc:
        check(False, "state dir writable", str(exc))
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401

        check(True, "web dependencies")
    except ImportError as exc:
        check(False, "web dependencies", str(exc))
    if args.live:
        from worker import models as catalog

        result = (
            catalog.probe(env.get("WORKER_MODEL", ""), env)
            if provider != "demo"
            else {"ok": True, "seconds": 0}
        )
        check(
            bool(result["ok"]), "model answers with a tool call", result.get("error", f"{result['seconds']}s")
        )
    print("All good." if not problems else f"{problems} problem(s) found.", file=out)
    return 1 if problems else 0


def cmd_verify(args: argparse.Namespace, env: Any, model: Any, saver: Any, input_fn: Any, out: TextIO) -> int:
    path = Path(args.audit)
    if not path.is_file():
        raise CliError(f"no audit file at {path}")
    checked, problem = AuditLog(path).first_break()
    if problem is None:
        print(f"Audit verification: OK ({checked} entries)", file=out)
        return 0
    print(
        f"Audit verification: BROKEN at line {problem['line']} (seq {problem['seq']}): {problem['reason']}; "
        f"{checked} entries before it verified.",
        file=out,
    )
    return 1


def cmd_metrics(args: argparse.Namespace, env: Any, model: Any, saver: Any, input_fn: Any, out: TextIO) -> int:
    from worker.metrics import harness_metrics, latency

    report = harness_metrics if args.report == "harness" else latency
    print(json.dumps(report(Path(args.state_dir)), indent=2), file=out)
    return 0


def cmd_db(args: argparse.Namespace, env: Any, model: Any, saver: Any, input_fn: Any, out: TextIO) -> int:
    from worker.db.cli import DbCliError, run

    try:
        return run(args, env, out)
    except DbCliError as exc:
        raise CliError(str(exc)) from exc


def cmd_graph(args: argparse.Namespace, env: Any, model: Any, saver: Any, input_fn: Any, out: TextIO) -> int:
    from worker.graph.cli import GraphCliError, run

    try:
        return run(args, env, out)
    except GraphCliError as exc:
        raise CliError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="worker.cli", description="Onboarding knowledge worker")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("run", "chat"):
        s = sub.add_parser(name)
        if name == "run":
            s.add_argument("file")
            s.add_argument("--sponsor", required=True)
            s.add_argument("--gp-id", required=True)
            s.add_argument("--out", default="out")
        else:
            s.add_argument("--sponsor", required=True)
            s.add_argument("--gp-id", default="")
            s.add_argument("--out", default="")
            s.add_argument("--inbox", default="")
        s.add_argument("--model", default="")
        s.add_argument("--state-dir", default=_DEFAULT_STATE)
        s.add_argument("--auto-approve", action="store_true")
    for name in ("serve", "demo"):
        s = sub.add_parser(
            name, help="web console" if name == "serve" else "web console with an offline sample"
        )
        s.add_argument("--host", default="127.0.0.1")
        s.add_argument("--port", type=int, default=8765)
        s.add_argument("--state-dir", default=_DEFAULT_STATE)
        if name == "demo":
            s.add_argument("--no-open", action="store_true", help="do not open a browser")
    s = sub.add_parser("verify", help="re-check the hash chain of an audit file and report the first break")
    s.add_argument("audit", help="path to audit.jsonl")
    s = sub.add_parser("metrics", help="latency p50/p95 from the audit files of finished jobs")
    s.add_argument("report", choices=["latency", "harness"])
    s.add_argument("--state-dir", default=_DEFAULT_STATE)
    s = sub.add_parser("models", help="list free OpenRouter models that can call tools")
    s.add_argument("--probe", action="append", default=[], metavar="MODEL", help="test a model (uses quota)")
    from worker.db.cli import add_parser as add_db_parser
    from worker.graph.cli import add_parser as add_graph_parser

    add_db_parser(sub)
    add_graph_parser(sub)
    s = sub.add_parser("doctor", help="check the installation and configuration")
    s.add_argument("--state-dir", default=_DEFAULT_STATE)
    s.add_argument("--live", action="store_true", help="also test the configured model (uses quota)")
    return p


def main(
    argv: list[str] | None = None,
    *,
    input_fn: InputFn = input,
    out: TextIO | None = None,
    model: BaseChatModel | None = None,
    saver: Any = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    if out is None:
        for stream in (sys.stdout, sys.stderr):  # a model's punctuation must never crash a legacy console
            try:
                stream.reconfigure(errors="replace")  # type: ignore[union-attr]
            except (AttributeError, ValueError):
                pass
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    try:
        env = load_env(environ)
        handler = {
            "run": cmd_run,
            "chat": cmd_chat,
            "serve": cmd_serve,
            "demo": cmd_demo,
            "models": cmd_models,
            "doctor": cmd_doctor,
            "verify": cmd_verify,
            "metrics": cmd_metrics,
            "db": cmd_db,
            "graph": cmd_graph,
        }[args.command]
        return handler(args, env, model, saver, input_fn, out)
    except CliError as exc:
        print(f"error: {exc}", file=out)
        return 2
    except EOFError:
        print("error: input closed; the run is left paused at its gate.", file=out)
        return 1


if __name__ == "__main__":
    sys.exit(main())
