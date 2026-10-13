"""Web app: a local console to drop a file, watch the agent work, and approve what it proposes.

`create_app` returns a FastAPI app; `serve` runs it with uvicorn. No frontend build: one static page.
Binds to 127.0.0.1 by default. Set WORKER_APPROVER_TOKEN to require a token for every state-changing call.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import smtplib
import tempfile
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field

from worker import __version__
from worker import models as model_catalog
from worker.agent import PROVIDERS
from worker.auth import SESSION_SECONDS, Accounts
from worker.cli import load_env
from worker.console import Console
from worker.contracts import UnnamedApprover, approver_name
from worker.db.core import read_scope
from worker.errors import StatusError
from worker.models import model_for_role
from worker.obs import METRICS, bind, configure_logging
from worker.service import MAX_UPLOAD_BYTES, RunManager, ServiceError
from worker.templates import TEMPLATES

STATIC = Path(__file__).parent / "static"
APPROVING_ACTS = ("approve", "allow_empty", "so", "issue_apply", "issue_undo", "recon_apply", "recon_undo")  # console acts that approve or sign off: they name a person
SECRET_KEYS = {n: p.key_env for n, p in PROVIDERS.items() if p.key_env}
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:/,@+=-]{0,300}$")


class AuthBody(BaseModel):
    name: str = ""
    email: str = ""
    login: str = ""
    code: str = ""
    password: str = ""


class NewRun(BaseModel):
    file: str
    sponsor: str = "sponsor-a"
    gp_id: str = Field(default="GP-001")
    request: str = ""


class Message(BaseModel):
    text: str


class Decision(BaseModel):
    proposal_hash: str
    verdict: str
    feedback: str = ""
    acknowledged: dict[str, str] = {}


class ConsoleAct(BaseModel):
    action: str
    text: str = ""
    context: dict[str, Any] = {}  # where the person is when they say something: view, sponsor, job, group


class IssuePicks(BaseModel):
    """Groups to decide: [{id, option, value?}], whether to save the change as a sponsor rule, an optional note."""

    picks: list[dict[str, Any]] = []
    rule: bool = False
    note: str = ""


class IssueUndo(BaseModel):
    decision: int = 0
    batch: int = 0
    reason: str = ""


class IdgenBody(BaseModel):
    """The ID generation decision: the scheme {mode, prefix, width, start, pattern}, the job it is read on, why."""

    sponsor: str = ""
    record_type: str
    scheme: dict[str, Any] = {}
    job: str = ""
    reason: str = ""
    column: str = ""  # for "use source ID": the column the person says holds the ID
    name_ok: bool = False  # the person means a column of names to be the ID, and says why in `reason`


class ConsoleAnswer(BaseModel):
    """A person's answer to a question about an unknown column or format (see STATUS.md, Migration M24a)."""

    question_id: str
    option_id: str | None = None
    custom: dict[str, Any] | None = None
    scope: str
    reason: str = ""


class FeedbackBody(BaseModel):
    kind: str
    action: str
    reason: str = ""
    shown: str | None = None
    chosen: str | None = None
    scope: str | None = None
    skill: str | None = None
    table_row: str | None = None
    record_type: str | None = None
    lineage: str | None = None
    check: str | None = None
    concept: str | None = None
    outcome: str | None = None


class ImprovementBody(BaseModel):
    key: str
    account: str
    scope: str = "organisation"


class ImprovementDecision(BaseModel):
    verdict: str = "approve"
    reason: str = ""
    proposal_sha256: str = ""  # a control card: the hash of the proposal as the approver saw it


class RecordsAct(BaseModel):
    op: str
    sponsor: str = ""
    type: str = ""
    value: str = ""


class AskJob(BaseModel):
    question: str = ""
    sql: str


class ReshapeAct(BaseModel):
    action: str
    proposal_id: str = ""
    artifact_id: str = ""
    bound: dict[str, str] = {}
    acknowledged: list[str] = []
    scope: str = "job"
    reason: str = ""


class HarnessAct(BaseModel):
    action: str
    card: str = ""
    skill: str = ""
    table: str = ""
    rows: list[dict[str, Any]] | None = None
    scope: str = "sponsor"
    sponsor: str = ""
    surface: str = "golden_mapping"
    reason: str = ""
    controls: dict[str, Any] | None = None  # propose_control: the whole new controls document


class LearningAct(BaseModel):
    action: str
    job: str
    i: int | None = None
    scope: str = ""
    kind: str = ""
    ref: str = ""
    skill: str = ""
    text: str = ""


class Settings(BaseModel):
    provider: str | None = None
    model: str | None = None
    fallbacks: str | None = None
    api_key: str | None = None


class Probe(BaseModel):
    model: str


def _env_file(environ: Mapping[str, str]) -> Path:
    return Path(environ.get("WORKER_ENV_FILE") or ".env")


def _write_env(path: Path, updates: Mapping[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    out.extend(f"{k}={v}" for k, v in updates.items() if k not in seen)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


log = logging.getLogger("worker.web")


def readiness(manager: RunManager, state_dir: Path, environ: Mapping[str, str]) -> dict[str, str]:
    """Ready to take traffic: not draining, the state folder writable, the skills valid, no accepted decision
    waiting to run and, with a database, reachable, migrated and its audit chains not reported broken. Each
    value is "ok" or the reason; Neo4j is not part of this."""
    from worker.db.backend import database_url, db_for
    from worker.db.backend import health as db_health
    from worker.skills import load_skills

    checks = {"draining": "failed: shutting down" if manager.draining else "ok"}
    try:
        with tempfile.NamedTemporaryFile(dir=state_dir, prefix=".ready-"):
            checks["state_dir"] = "ok"
    except OSError as exc:
        checks["state_dir"] = f"not writable: {exc}"
    try:
        load_skills()
        checks["skills"] = "ok"
    except Exception as exc:  # noqa: BLE001  any refusal of the rule files is "not ready"
        checks["skills"] = f"refused: {str(exc)[:120]}"
    waiting = manager.pending_approvals(older_than=120)  # a decision that is just running is not a fault
    checks["approvals"] = "ok" if waiting == 0 else f"{waiting} accepted decision(s) not yet executed"
    url = database_url(environ)
    if url:
        checks.update(db_health(db_for(url)))
    return checks


async def _body(request: Request, *, service: bool = False) -> bytes:
    """The request body, refused with 413 once it is over the upload limit."""
    data = b""
    async for chunk in request.stream():
        data += chunk
        if len(data) > MAX_UPLOAD_BYTES:
            msg = f"file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB"
            raise ServiceError(msg, 413) if service else HTTPException(413, msg)
    return data


def _safe_name(name: str, default: str) -> str:
    return re.sub(r"[^A-Za-z0-9._ -]", "_", Path(name).name).strip(". ") or default


def _warm_chat() -> None:
    from worker import console_chat

    console_chat.warm()


def create_app(
    state_dir: Path | str = ".worker_state",
    *,
    environ: Mapping[str, str] | None = None,
    model_factory: Callable[[], BaseChatModel] | None = None,
    fetch_models: Callable[[], Mapping[str, Any]] | None = None,
) -> FastAPI:
    environ = os.environ if environ is None else environ
    configure_logging(load_env(environ))
    token = environ.get("WORKER_APPROVER_TOKEN", "")
    admins = {a.strip() for a in environ.get("WORKER_ADMINS", "").split(",") if a.strip()}

    def factory() -> BaseChatModel:
        return model_for_role("exec", load_env(environ))

    manager = RunManager(Path(state_dir), model_factory or factory, approver_token=token, admins=admins,
                         budget_env=load_env(environ))
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        manager.close()  # stop taking work, let accepted decisions finish (WORKER_DRAIN_SECONDS)

    app = FastAPI(title="Onboarding Worker", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.manager = manager
    # a basic login (worker.auth): on for `serve`, off for the offline demo and for tests that do not ask for it
    require_login = environ.get("WORKER_REQUIRE_LOGIN", "") not in ("", "0", "false")
    accounts = Accounts(Path(state_dir) / "auth")
    cookie = "ow_session"

    def session_name(request: Request) -> str:
        return accounts.name_of(request.cookies.get(cookie))

    @app.middleware("http")
    async def login_gate(request: Request, call_next: Callable[[Request], Any]) -> Response:
        if require_login and not session_name(request):
            path = request.url.path
            if path.startswith("/api/") and not path.startswith("/api/auth/"):
                return JSONResponse({"detail": "sign in first"}, status_code=401)
            if path in ("/console", "/inbox"):
                return RedirectResponse("/login")
        response: Response = await call_next(request)
        return response

    @app.middleware("http")
    async def observe(request: Request, call_next: Callable[[Request], Any]) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        started = time.perf_counter()
        status = 500
        with bind(request_id=request_id), read_scope():  # a read repeated within the request is made once
            try:
                response: Response = await call_next(request)
                status = response.status_code
            finally:
                route = getattr(request.scope.get("route"), "path", "unmatched")
                seconds = time.perf_counter() - started
                METRICS.inc("http_requests_total", route=route, status=status)
                METRICS.observe("http_request", seconds, route=route)
                if route not in ("/livez", "/readyz", "/metrics"):
                    log.info("request", extra={"method": request.method, "route": route, "status": status,
                                               "ms": round(seconds * 1000, 1)})
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)

    async def _status_error(_: Request, exc: StatusError) -> JSONResponse:
        """A refusal of the console pages: the message, with the status the error carries (409 by default)."""
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    app.add_exception_handler(StatusError, _status_error)

    def reviewer_of(request: Request) -> str:
        """Who is acting: the signed-in name when logins are on (a header is then ignored), else X-Reviewer."""
        if require_login:
            return session_name(request) or "local"
        return (request.headers.get("x-reviewer") or "local").strip()[:60] or "local"

    def guard(request: Request) -> str:
        """The reviewer name; enforces the approver token on state-changing calls when one is configured."""
        reviewer = reviewer_of(request)
        name, email = accounts.identity(request.cookies.get(cookie)) if require_login else ("", "")
        console.actor = f"{name} <{email}>" if email else reviewer  # the audit names the person by their email
        if token:
            given = request.headers.get("x-approver-token", "")
            if not hmac.compare_digest(given, token):
                raise HTTPException(401, "an approver token is required (X-Approver-Token)")
        return reviewer

    @app.get("/login", include_in_schema=False)
    @app.get("/register", include_in_schema=False)
    def auth_page() -> FileResponse:
        return FileResponse(STATIC / "auth.html", headers={"Cache-Control": "no-store"})

    def _signed(response: JSONResponse, token: str, request: Request) -> JSONResponse:
        response.set_cookie(cookie, token, max_age=SESSION_SECONDS, httponly=True, samesite="lax",
                            secure=request.url.scheme == "https")
        return response

    @app.post("/api/auth/register")
    def auth_register(body: AuthBody, request: Request) -> JSONResponse:
        token = accounts.register(body.name, body.email, body.password)
        return _signed(JSONResponse({"name": " ".join(body.name.split())}), token, request)

    @app.post("/api/auth/login")
    def auth_login(body: AuthBody, request: Request) -> JSONResponse:
        token, name = accounts.login(body.login or body.email or body.name, body.password)
        return _signed(JSONResponse({"name": name}), token, request)

    def _deliver_reset(name: str, email: str, code: str) -> None:
        """Email the one-time code when WORKER_SMTP_HOST is set; otherwise the server log carries it for the operator."""
        host = environ.get("WORKER_SMTP_HOST", "")
        if not host:
            log.warning("password reset code for %s <%s>: %s (valid 15 minutes; set WORKER_SMTP_HOST to email it)",
                        name, email, code)
            return
        msg = EmailMessage()
        msg["Subject"], msg["To"] = "Your Onboarding Worker reset code", email
        msg["From"] = environ.get("WORKER_SMTP_FROM", "onboarding-worker@localhost")
        msg.set_content(f"Hello {name},\n\nYour one-time code is {code}. It works for 15 minutes.\n"
                        "If you did not ask for it, ignore this message.\n")
        try:
            with smtplib.SMTP(host, int(environ.get("WORKER_SMTP_PORT", "587")), timeout=15) as smtp:
                smtp.starttls()
                if environ.get("WORKER_SMTP_USER"):
                    smtp.login(environ["WORKER_SMTP_USER"], environ.get("WORKER_SMTP_PASSWORD", ""))
                smtp.send_message(msg)
        except (OSError, smtplib.SMTPException):
            log.exception("could not email the reset code to %s", email)

    @app.post("/api/auth/forgot")
    def auth_forgot(body: AuthBody) -> dict[str, Any]:
        email = (body.email or body.login).strip().lower()
        found = accounts.forgot(email)
        if found:
            _deliver_reset(found[0], email, found[1])
        return {"ok": True, "message": "If that email is registered, a one-time code has been sent. It works for 15 minutes."}

    @app.post("/api/auth/reset")
    def auth_reset(body: AuthBody) -> dict[str, Any]:
        accounts.reset(body.email or body.login, body.code, body.password)
        return {"ok": True}

    @app.post("/api/auth/logout")
    def auth_logout(request: Request) -> JSONResponse:
        accounts.logout(request.cookies.get(cookie))
        response = JSONResponse({"ok": True})
        response.delete_cookie(cookie)
        return response

    @app.get("/api/auth/me")
    def auth_me(request: Request) -> dict[str, Any]:
        return {"login_required": require_login, "name": session_name(request)}

    @app.get("/")
    def index() -> RedirectResponse:
        return RedirectResponse("/console")

    @app.get("/inbox")
    def inbox_page() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/livez")
    def livez() -> dict[str, bool]:
        """The process is serving. No dependency is checked here, so a slow database never gets it killed."""
        return {"ok": True}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        checks = readiness(manager, Path(state_dir), environ)
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse({"ok": ok, "checks": checks}, status_code=200 if ok else 503)

    @app.get("/metrics")
    def metrics() -> Response:
        c = manager.counts()
        for status in ("working", "needs_approval", "idle", "error", "interrupted"):
            METRICS.set("runs", c["runs"].get(status, 0), status=status)
        METRICS.set("gates_waiting", c["gates_waiting"])
        METRICS.set("approvals_accepted_not_executed", manager.pending_approvals())
        METRICS.set("draining", 1 if manager.draining else 0)
        return Response(METRICS.render(), media_type="text/plain; version=0.0.4")

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        from worker.console_pages import model_roles
        from worker.smoke import status as smoke_status

        env = load_env(environ)
        provider = (env.get("WORKER_MODEL_PROVIDER") or "openai").lower()
        return {
            "ok": True,
            "version": __version__,
            "provider": provider,
            "model": env.get("WORKER_MODEL", ""),
            "fallbacks": env.get("WORKER_MODEL_FALLBACKS", ""),
            "roles": model_roles(env),
            "key_configured": provider not in SECRET_KEYS or bool(env.get(SECRET_KEYS[provider], "")),
            "token_required": bool(token),
            "templates": {
                n: {"fields": list(s.fields), "required": list(s.required)} for n, s in TEMPLATES.items()
            },
            "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
            "model_smoke": smoke_status(Path(state_dir)),  # warn only (worker.smoke); live models not verified
        }

    @app.post("/api/settings")
    def settings(body: Settings, request: Request) -> dict[str, Any]:
        guard(request)
        updates: dict[str, str] = {}
        for key, value in (
            ("WORKER_MODEL_PROVIDER", body.provider),
            ("WORKER_MODEL", body.model),
            ("WORKER_MODEL_FALLBACKS", body.fallbacks),
        ):
            if value is not None:
                if not _SAFE_VALUE.match(value):
                    raise HTTPException(400, f"{key} has characters that are not allowed")
                updates[key] = value
        if body.provider is not None and body.provider.lower() not in PROVIDERS:
            raise HTTPException(400, f"provider must be one of: {', '.join(PROVIDERS)}")
        if body.api_key:
            if not _SAFE_VALUE.match(body.api_key):
                raise HTTPException(400, "the API key has characters that are not allowed")
            provider = (
                body.provider or load_env(environ).get("WORKER_MODEL_PROVIDER") or "openai"
            ).lower()
            if provider not in SECRET_KEYS:
                raise HTTPException(400, f"the {provider} provider needs no API key")
            updates[SECRET_KEYS[provider]] = body.api_key
        if updates:
            _write_env(_env_file(environ), updates)
        return health()

    @app.get("/api/models")
    def list_models(refresh: bool = False) -> dict[str, Any]:
        try:
            return {"models": model_catalog.free_models(fetch_models, refresh=refresh)}
        except Exception as exc:
            raise HTTPException(502, f"could not reach OpenRouter: {exc}") from exc

    @app.post("/api/models/probe")
    def probe(body: Probe, request: Request) -> dict[str, Any]:
        guard(request)
        if not _SAFE_VALUE.match(body.model):
            raise HTTPException(400, "bad model id")
        return model_catalog.probe(body.model, load_env(environ))

    @app.get("/api/inbox")
    def inbox() -> dict[str, Any]:
        from worker.inbox import list_inbox

        return {"files": list_inbox([manager.inbox])}

    @app.put("/api/inbox/{name}")
    async def upload(name: str, request: Request) -> dict[str, Any]:
        guard(request)
        data = await _body(request, service=True)
        return manager.save_upload(name, data)

    @app.delete("/api/inbox/{name}")
    def remove(name: str, request: Request) -> dict[str, bool]:
        guard(request)
        manager.delete_upload(name)
        return {"ok": True}

    @app.get("/api/runs")
    def runs() -> dict[str, Any]:
        return {"runs": manager.list_runs()}

    @app.post("/api/runs")
    def new_run(body: NewRun, request: Request) -> dict[str, Any]:
        guard(request)
        return manager.create_run(body.file, body.sponsor, body.gp_id, body.request)

    @app.get("/api/runs/{run_id}")
    def run_state(run_id: str) -> dict[str, Any]:
        return manager.get(run_id)

    @app.post("/api/runs/{run_id}/messages")
    def message(run_id: str, body: Message, request: Request) -> dict[str, Any]:
        guard(request)
        return manager.send(run_id, body.text)

    @app.post("/api/runs/{run_id}/decision")
    def decision(run_id: str, body: Decision, request: Request) -> dict[str, Any]:
        reviewer = guard(request)
        return manager.decide(
            run_id, body.proposal_hash, body.verdict, body.feedback, reviewer, body.acknowledged
        )

    @app.get("/api/runs/{run_id}/audit")
    def audit(run_id: str, after: int = 0) -> dict[str, Any]:
        return manager.audit(run_id, after)

    @app.get("/api/downloads")
    def downloads() -> dict[str, Any]:
        return {"files": manager.downloads()}

    @app.get("/api/runs/{run_id}/files/{name}")
    def download(run_id: str, name: str) -> FileResponse:
        return FileResponse(manager.output_path(run_id, name), filename=name)

    @app.post("/api/runs/{run_id}/feedback")
    def feedback(run_id: str, body: FeedbackBody, request: Request) -> dict[str, Any]:
        guard(request)
        return manager.feedback(run_id, body.model_dump(exclude_none=True))

    console = Console(Path(state_dir) / "console_inbox", Path(state_dir) / "console.json")
    threading.Thread(target=_warm_chat, daemon=True).start()  # the first chat message pays no start-up cost
    console.standards_root = Path(state_dir)  # where the standards pages look for waiting uploads
    console.learning_key = load_env(environ).get("WORKER_LEARNING_KEY") or None  # cross-sponsor hints need it

    @app.get("/console")
    def console_page() -> FileResponse:
        return FileResponse(STATIC / "console.html", headers={"Cache-Control": "no-store"})

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/console.js")
    def console_js() -> FileResponse:
        return FileResponse(STATIC / "console.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @app.get("/api/console/state")
    def console_state(request: Request) -> dict[str, Any]:
        from worker.console_screens import state_view

        st = state_view(console, health())
        st["login"] = require_login
        if require_login:
            st["me"] = session_name(request)  # the signed-in person, whatever the machine's account is
        return st

    @app.put("/api/console/onboard/file/{name}")
    async def console_onboard_file(name: str, request: Request, mode: str = "new", sponsor: str = "") -> dict[str, Any]:
        """Read an uploaded sponsor or entity list; nothing is saved until the person confirms (act onb_save)."""
        guard(request)
        safe = _safe_name(name, "list.xlsx")
        data = await _body(request)
        path = console.inbox / safe
        path.write_bytes(data)
        return console.onboard_read(mode, sponsor, path)

    @app.get("/api/console/onboard/sample")
    def console_onboard_sample(mode: str = "new", sponsor: str = "") -> dict[str, Any]:
        return console.onboard_sample(mode, sponsor)

    @app.get("/api/console/standards/{kind}")
    def console_standard(kind: str, q: str = "", group: str = "All") -> dict[str, Any]:
        from worker import console_pages as P

        if kind == "tmpls":
            return P.templates_view(console.standards_root)
        if kind == "coa":
            return P.coa_view(q, group, console.standards_root)
        if kind == "ttm":
            return P.ttm_view(q, console.standards_root)
        raise HTTPException(404, "unknown standard")

    @app.put("/api/console/standards/upload/{kind}/{filename}")
    async def console_standard_upload(kind: str, filename: str, request: Request, name: str = "") -> dict[str, Any]:
        """File an uploaded replacement standard as an improvement card. Nothing live changes until approval."""
        from worker import console_standards as CS

        who = guard(request)
        data = await _body(request)
        safe = _safe_name(filename, "upload")
        return CS.propose_upload(manager._improvement_parts()[1], kind, safe, data, who, name)

    @app.get("/api/console/standards-file/{name}")
    def console_standard_file(name: str) -> FileResponse:
        from worker import console_pages as P

        path = P.standard_files().get(name)
        if path is None:
            raise HTTPException(404, "no such standard file")
        return FileResponse(path, filename=path.name)

    @app.get("/api/console/jobs/{job}/download")
    def console_download(job: str) -> FileResponse:
        path = console.download_path(job)
        return FileResponse(path, filename=path.name)

    @app.post("/api/console/act")
    def console_act(body: ConsoleAct, request: Request) -> dict[str, Any]:
        who = guard(request)
        if body.action.split("|", 1)[0] in APPROVING_ACTS:  # an approval or a sign-off names a person
            try:
                who = approver_name(who, demo=not token)
            except UnnamedApprover as exc:
                raise HTTPException(403, str(exc)) from exc
        console.act(body.action, body.text, who, body.context)
        from worker.console_screens import state_view

        return state_view(console, health())

    @app.put("/api/console/jobs/ask/attach")
    @app.post("/api/console/jobs/ask/attach")
    async def console_attach(request: Request, name: str = "", sponsor: str = "", type: str = "") -> dict[str, Any]:
        """Start a job from the person's own workbook: the raw bytes are the body, `name` the file name. Same name,
        size and type limits as the inbox. The reply is the console state plus `attach` (the file's own headers)."""
        guard(request)
        data = await _body(request)
        got = console.ask_attach_bytes(name or request.headers.get("x-file-name", ""), data,
                                       sponsor=sponsor or None, type_=type or None)
        console.save()
        from worker.console_screens import state_view

        return {**state_view(console, health()), "attach": got}

    def _issue_job(job: str) -> dict[str, Any]:
        from worker.console_working import find_job

        return find_job(console, job)

    @app.get("/issues.js")
    def issues_js() -> FileResponse:
        return FileResponse(STATIC / "issues.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @app.get("/api/console/jobs/{job}/issues")
    def issues_groups(job: str, page: int = 0, per: int = 50, status: str = "", q: str = "") -> dict[str, Any]:
        """One page of the issue groups with exact counts, the verdict and the next group. No rows."""
        from worker import console_issues as I

        return I.model(console, _issue_job(job), page=page, per=per, status=status, q=q)

    @app.get("/api/console/jobs/{job}/issues/{gid}/rows")
    def issues_rows(job: str, gid: str, page: int = 0, per: int = 50, q: str = "", after: int = 0,
                    desc: bool = False) -> dict[str, Any]:
        """One page (default 50, at most 200) of a group's rows in source-row order, with the exact total."""
        from worker import console_issues as I

        return I.rows_page(console, _issue_job(job), gid, page=page, per=per, q=q, after=after, desc=desc)

    @app.get("/api/console/jobs/{job}/issues/{gid}/detail")
    def issues_detail(job: str, gid: str) -> dict[str, Any]:
        from worker import console_issues as I

        return I.detail(console, _issue_job(job), gid)

    @app.post("/api/console/jobs/{job}/issues/preview")
    def issues_preview(job: str, body: IssuePicks) -> dict[str, Any]:
        """What deciding these groups would change, in exact counts and a sample. Changes nothing."""
        from worker import console_issues as I

        return I.preview(console, _issue_job(job), body.picks)

    @app.post("/api/console/jobs/{job}/issues/apply")
    def issues_apply(job: str, body: IssuePicks, request: Request) -> dict[str, Any]:
        """Decide groups. The reviewer (X-Reviewer) must be a person; the decision is audited before it takes effect."""
        from worker import console_issues as I

        got = I.apply(console, _issue_job(job), body.picks, guard(request), rule=body.rule, note=body.note)
        console.save()
        return got

    @app.post("/api/console/jobs/{job}/issues/undo")
    def issues_undo(job: str, body: IssueUndo, request: Request) -> dict[str, Any]:
        from worker import console_issues as I

        got = I.undo(console, _issue_job(job), guard(request), decision=body.decision, batch=body.batch,
                     reason=body.reason)
        console.save()
        return got

    @app.get("/recon.js")
    def recon_js() -> FileResponse:
        return FileResponse(STATIC / "recon.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @app.get("/api/console/jobs/{job}/recon")
    def recon_groups(job: str, page: int = 0, per: int = 50, status: str = "", ft: str = "") -> dict[str, Any]:
        """One page of the reconciliation table (a row per check and cause, exact counts) and the decisions made."""
        from worker import console_recon as RC

        return RC.model(console, _issue_job(job), page=page, per=per, status=status, ft=ft)

    @app.get("/api/console/jobs/{job}/recon/{gid}/rows")
    def recon_rows(job: str, gid: str, page: int = 0, per: int = 50, q: str = "", state: str = "") -> dict[str, Any]:
        """One page (default 50, at most 200) of a group's affected records, with the exact total."""
        from worker import console_recon as RC

        return RC.rows_page(console, _issue_job(job), gid, page=page, per=per, q=q, state=state)

    @app.post("/api/console/jobs/{job}/recon/preview")
    def recon_preview(job: str, body: IssuePicks) -> dict[str, Any]:
        from worker import console_recon as RC

        return RC.preview(console, _issue_job(job), body.picks, body.note)

    @app.post("/api/console/jobs/{job}/recon/apply")
    def recon_apply(job: str, body: IssuePicks, request: Request) -> dict[str, Any]:
        """Record a disposition of a break. The reviewer (X-Reviewer) must be a person; a note is required."""
        from worker import console_recon as RC

        got = RC.apply(console, _issue_job(job), body.picks, guard(request), note=body.note)
        console.save()
        return got

    @app.post("/api/console/jobs/{job}/recon/undo")
    def recon_undo(job: str, body: IssueUndo, request: Request) -> dict[str, Any]:
        from worker import console_recon as RC

        got = RC.undo(console, _issue_job(job), guard(request), decision=body.decision, batch=body.batch,
                      reason=body.reason)
        console.save()
        return got

    @app.get("/api/console/jobs/{job}/source")
    def issues_source(job: str, row: int = 1, count: int = 12) -> dict[str, Any]:
        """A window of the job's own source sheet, read from the Parquet cache."""
        from worker import console_issues as I

        return I.source_window(console, _issue_job(job), row, count)

    @app.post("/api/console/jobs/{job}/answer")
    def console_answer(job: str, body: ConsoleAnswer, request: Request) -> dict[str, Any]:
        """Answer one open question of a job. The answerer is the reviewer (X-Reviewer, self-declared and
        demo-grade); an agent, a role or a reserved name such as `local` is refused with 409."""
        who = guard(request)
        answered = console.answer_question(job, body.model_dump(), who)
        from worker.console_screens import state_view

        return {**state_view(console, health()), "answered": answered}

    @app.get("/idgen.js")
    def idgen_js() -> FileResponse:
        return FileResponse(STATIC / "idgen.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @app.get("/api/console/idgen")
    def idgen_get(record_type: str, sponsor: str = "", job: str = "") -> dict[str, Any]:
        from worker import console_idgen as G

        return G.view(console, sponsor or None, record_type, job or None)

    @app.post("/api/console/idgen/preview")
    def idgen_preview(body: IdgenBody) -> dict[str, Any]:
        from worker import console_idgen as G

        return G.preview(console, body.model_dump())

    @app.post("/api/console/idgen")
    def idgen_set(body: IdgenBody, request: Request) -> dict[str, Any]:
        """Decide the sponsor's ID logic for a record type. The decider is the reviewer (X-Reviewer); an agent, a
        role or a reserved name is refused with 409 and nothing changes."""
        from worker import console_idgen as G

        return G.decide(console, body.model_dump(), guard(request))

    @app.get("/records.js")
    def records_js() -> FileResponse:
        return FileResponse(STATIC / "records.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @app.get("/api/console/records/{page}")
    def records_get(page: str, sponsor: str = "") -> dict[str, Any]:
        from worker import console_records as R

        views = {"existing": R.existing_view, "fund": R.fund_view, "settings": R.settings_view}
        if page not in views:
            raise HTTPException(404, f"no records page {page!r}")
        return views[page](console, sponsor or None)

    @app.post("/api/console/records/act")
    def records_post(body: RecordsAct, request: Request) -> dict[str, Any]:
        from worker import console_records as R

        guard(request)
        return R.records_act(console, body.model_dump())

    @app.put("/api/console/records/file/{name}")
    async def records_file(name: str, request: Request, sponsor: str = "", type: str = "") -> dict[str, Any]:
        from worker import console_records as R

        guard(request)
        safe = _safe_name(name, "export.xlsx")
        data = await _body(request)
        path = console.inbox / safe
        path.write_bytes(data)
        try:
            return R.load_export(console, sponsor or console.S["sp"], type, path)
        except StatusError:
            raise
        except Exception as exc:  # an unreadable workbook
            raise HTTPException(409, f"the export could not be read: {exc}") from exc

    @app.get("/harness.js")
    def harness_js() -> FileResponse:
        return FileResponse(STATIC / "harness.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @app.get("/working.js")
    def working_js() -> FileResponse:
        return FileResponse(STATIC / "working.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @app.get("/api/console/jobs/{job}/working/{tab}")
    def console_working(job: str, tab: str) -> dict[str, Any]:
        from worker.console_working import find_job, working_view

        return working_view(console, find_job(console, job), tab)

    @app.post("/api/console/jobs/{job}/ask")
    def console_ask(job: str, body: AskJob, request: Request) -> dict[str, Any]:
        """A read-only question about one job (tier 3). Audited before it runs; nothing it returns changes the job."""
        from worker.audit import AuditError
        from worker.console_ask import ask_act
        from worker.console_working import WorkingError, find_job

        who = reviewer_of(request)
        try:
            return ask_act(console, find_job(console, job), body.question, body.sql, who)
        except WorkingError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        except AuditError as exc:
            raise HTTPException(503, f"the record could not be written, so nothing ran: {exc}") from exc

    @app.get("/reshape.js")
    def reshape_js() -> FileResponse:
        return FileResponse(STATIC / "reshape.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @app.get("/api/console/jobs/{job}/reshape")
    def console_reshape(job: str) -> dict[str, Any]:
        from worker.console_reshape import reshape_view
        from worker.console_working import find_job

        return reshape_view(console, find_job(console, job))

    @app.post("/api/console/jobs/{job}/reshape")
    def console_reshape_act(job: str, body: ReshapeAct, request: Request) -> dict[str, Any]:
        """The card's buttons: open, propose (the Layout Drafter), reuse, approve. A refusal says why (409); an
        approval is the reviewer's, bound to the hashes they saw; an agent name is refused."""
        from worker.audit import AuditError
        from worker.console_reshape import reshape_act
        from worker.console_working import find_job
        from worker.engine import Refused

        who = guard(request)
        try:
            return reshape_act(console, find_job(console, job), body.model_dump(), who)
        except Refused as exc:
            raise HTTPException(409, exc.sentence) from exc
        except AuditError as exc:
            raise HTTPException(503, f"the record could not be written, so nothing ran: {exc}") from exc

    @app.get("/api/console/harness/{page}")
    def harness_get(page: str, request: Request, name: str = "", sponsor: str = "") -> dict[str, Any]:
        from worker.console_harness import harness_page

        who = reviewer_of(request)
        out = harness_page(manager, page, name=name, sponsor=sponsor, admin=who in admins)
        if page == "health":
            from worker import console_chat

            out["chat_traces"] = console_chat.traces(console)
        return out

    @app.post("/api/console/harness/act")
    def harness_post(body: HarnessAct, request: Request) -> dict[str, Any]:
        from worker.console_harness import harness_act

        who = guard(request)
        return harness_act(manager, body.model_dump(), who, who in admins)

    @app.get("/learning.js")
    def learning_js() -> FileResponse:
        return FileResponse(STATIC / "learning.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @app.get("/api/console/learning/{part}")
    def learning_get(part: str, job: str = "") -> dict[str, Any]:
        from worker.console_learning import learning_view

        return learning_view(console, manager.state_dir, part, job)

    @app.put("/api/console/learning/report/{job}/{check}")
    async def learning_report_put(job: str, check: str, request: Request, name: str = "report.xlsx") -> dict[str, Any]:
        """The client's report for one reconciliation check (documented layout, xlsx or csv)."""
        from worker.console_learning import learning_report

        who = guard(request)
        data = await _body(request)
        return learning_report(console, job, check, name, data, who, manager.state_dir)

    @app.get("/api/console/learning/layout/{check}")
    def learning_layout_get(check: str) -> Response:
        from worker.console_learning import learning_layout

        fname, blob = learning_layout(check)
        return Response(blob, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    @app.post("/api/console/learning/act")
    def learning_post(body: LearningAct, request: Request) -> dict[str, Any]:
        from worker.console_learning import learning_act

        who = guard(request)
        return learning_act(console, manager.state_dir, body.model_dump(), who)

    @app.get("/api/improvements")
    def improvements() -> dict[str, Any]:
        return manager.improvements()

    @app.post("/api/improvements")
    def propose_improvement(body: ImprovementBody, request: Request) -> dict[str, Any]:
        return manager.propose_improvement(body.key, body.account, body.scope, guard(request))

    @app.post("/api/improvements/{card_id}/decision")
    def decide_improvement(card_id: str, body: ImprovementDecision, request: Request) -> dict[str, Any]:
        who = guard(request)
        return manager.decide_improvement(card_id, body.verdict, body.reason, who, who in admins,
                                          seen_sha256=body.proposal_sha256)

    @app.post("/api/improvements/rollback")
    def rollback_improvement(body: ImprovementDecision, request: Request) -> dict[str, Any]:
        return manager.rollback_improvement(guard(request), body.reason)

    @app.get("/api/health/metrics")
    def health_metrics(sponsor: str | None = None, skill: str | None = None) -> dict[str, Any]:
        return manager.health(sponsor, skill)

    @app.get("/api/sponsors/{sponsor}/feedback")
    def sponsor_feedback(sponsor: str, skill: str | None = None) -> dict[str, Any]:
        return manager.sponsor_feedback(sponsor, skill)

    @app.get("/api/sponsors/{sponsor}/memory")
    def memory(sponsor: str) -> dict[str, Any]:
        return manager.memory(sponsor)

    return app


def serve(host: str = "127.0.0.1", port: int = 8765, state_dir: str = ".worker_state") -> None:
    import uvicorn

    # log_config=None: uvicorn's own records go through the root handler (JSON); requests are logged by the
    # middleware with their request id, so the access log is off.
    env = dict(os.environ)
    if (env.get("WORKER_MODEL_PROVIDER") or "").lower() != "demo" and not env.get("WORKER_DEMO"):
        env.setdefault("WORKER_REQUIRE_LOGIN", "1")  # a real run asks who you are; the offline demo does not
    uvicorn.run(create_app(state_dir, environ=env), host=host, port=port, log_level="info", log_config=None, access_log=False)
