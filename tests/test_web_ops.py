"""Operations surface: liveness, readiness, Prometheus text, request ids, JSON logs, and what readiness says when
the database or the audit verification is not well."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from worker.db.core import Db, SqlAuditLog
from worker.db.verify import VERIFICATION_STREAM
from worker.obs import Metrics, bind, configure_logging
from worker.web import create_app


def _env(tmp_path: Path, **more: str) -> dict[str, str]:
    return {"WORKER_MODEL_PROVIDER": "demo", "WORKER_ENV_FILE": str(tmp_path / "none.env"), **more}


def test_liveness_readiness_metrics_and_a_request_id(tmp_path: Path) -> None:
    app = create_app(tmp_path / "state", environ=_env(tmp_path))
    with TestClient(app) as client:
        live = client.get("/livez", headers={"X-Request-ID": "req-123"})
        assert live.status_code == 200 and live.headers["x-request-id"] == "req-123"
        ready = client.get("/readyz")
        assert ready.status_code == 200 and ready.json()["checks"] == {
            "draining": "ok", "state_dir": "ok", "skills": "ok", "approvals": "ok"}
        text = client.get("/metrics").text
        assert 'http_requests_total{route="/livez",status="200"} 1' in text
        assert "# TYPE runs gauge" in text and "draining 0" in text
        app.state.manager._draining.set()
        down = client.get("/readyz")
        assert down.status_code == 503 and down.json()["checks"]["draining"].startswith("failed")
        assert client.get("/livez").status_code == 200  # liveness does not follow readiness


def test_readiness_with_a_database_reports_its_schema_and_a_failed_verification(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'ow.db').as_posix()}"
    db = Db(url)
    db.create_all()
    app = create_app(tmp_path / "state", environ=_env(tmp_path, DATABASE_URL=url))
    with TestClient(app) as client:
        ok = client.get("/readyz")
        assert ok.status_code == 200
        assert {k: ok.json()["checks"][k] for k in ("database", "schema", "audit_verification")} == {
            "database": "ok", "schema": "ok", "audit_verification": "ok"}
        SqlAuditLog(db, VERIFICATION_STREAM).record("audit.verification_failed", "a stream is broken")
        bad = client.get("/readyz")
        assert bad.status_code == 503 and "verification failed" in bad.json()["checks"]["audit_verification"]


def test_readiness_names_an_unreachable_database(tmp_path: Path) -> None:
    app = create_app(tmp_path / "state", environ=_env(
        tmp_path, DATABASE_URL=f"sqlite:///{(tmp_path / 'no_such_dir' / 'x.db').as_posix()}"))
    with TestClient(app) as client:
        r = client.get("/readyz")
        assert r.status_code == 503 and r.json()["checks"]["database"].startswith("unreachable")


def test_json_logs_carry_the_bound_context_and_never_a_secret(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging({"WORKER_LOG_FORMAT": "json"})
    with bind(request_id="r-1", run_id="run-9"):
        logging.getLogger("worker.test").info("approved", extra={"approver_token": "s3cret", "rows": 12})
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["msg"] == "approved" and line["request_id"] == "r-1" and line["run_id"] == "run-9"
    assert line["rows"] == 12 and line["approver_token"] == "[redacted]" and "s3cret" not in json.dumps(line)
    assert line["level"] == "info" and line["logger"] == "worker.test"


def test_metrics_text_format() -> None:
    m = Metrics()
    m.inc("things_total", kind="a")
    m.inc("things_total", 2, kind="a")
    m.set("level", 3.5)
    m.observe("job", 0.25, stage="read")
    text = m.render()
    assert 'things_total{kind="a"} 3' in text and "level 3.5" in text
    assert 'job_seconds_sum{stage="read"} 0.25' in text and 'job_seconds_count{stage="read"} 1' in text
    assert "# TYPE things_total counter" in text
