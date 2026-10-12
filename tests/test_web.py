"""Web console: upload, run, hash-bound approval, downloads, audit, settings, token, restart."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from worker.demo import DEMO_FILE, SAMPLES
from worker.web import create_app

MODELS_PAYLOAD = {
    "data": [
        {
            "id": "vendor/good:free",
            "context_length": 100000,
            "pricing": {"prompt": "0", "completion": "0"},
            "supported_parameters": ["tools"],
            "architecture": {"input_modalities": ["text"]},
        },
        {
            "id": "vendor/notools:free",
            "context_length": 8000,
            "pricing": {"prompt": "0", "completion": "0"},
            "supported_parameters": [],
        },
        {
            "id": "vendor/paid",
            "context_length": 8000,
            "pricing": {"prompt": "0.1", "completion": "0.2"},
            "supported_parameters": ["tools"],
        },
    ]
}


def make_client(tmp_path: Path, **env: str) -> TestClient:
    environ = {
        "WORKER_MODEL_PROVIDER": "demo",
        "WORKER_ENV_FILE": str(tmp_path / "test.env"),
        **env,
    }
    app = create_app(tmp_path / "state", environ=environ, fetch_models=lambda: MODELS_PAYLOAD)
    return TestClient(app)


def upload_sample(client: TestClient, name: str = DEMO_FILE) -> None:
    r = client.put(f"/api/inbox/{name}", content=(SAMPLES / name).read_bytes())
    assert r.status_code == 200, r.text


def wait_for(client: TestClient, run_id: str, *statuses: str, timeout: float = 60) -> dict[str, Any]:
    end = time.time() + timeout
    while time.time() < end:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in statuses:
            return run
        time.sleep(0.1)
    raise AssertionError(f"run stayed {run['status']!r}, wanted {statuses}: {run.get('error')}")


def decide_ids(client: TestClient) -> None:
    """The sponsor's ID decision, by a named person through the console route: the one store the agent reads."""
    r = client.post("/api/console/idgen", headers={"X-Reviewer": "reviewer-1"}, json={
        "sponsor": "Sponsor A", "record_type": "Investors", "reason": "the sponsor's standard numbering",
        "scheme": {"mode": "sequential", "prefix": "GINV-", "width": 4}})
    assert r.status_code == 200, r.text


def start(client: TestClient, decided: bool = True) -> str:
    upload_sample(client)
    r = client.post("/api/runs", json={"file": DEMO_FILE, "sponsor": "sponsor-a", "gp_id": "GP-001"})
    assert r.status_code == 200, r.text
    if decided:
        decide_ids(client)
    return r.json()["id"]


def test_approval_is_refused_until_the_id_decision_is_made_and_then_goes_through(tmp_path: Path) -> None:
    c = make_client(tmp_path)
    run_id = start(c, decided=False)
    gate = wait_for(c, run_id, "needs_approval")["gates"][0]
    body = {"proposal_hash": gate["hash"], "verdict": "approve"}
    refused = c.post(f"/api/runs/{run_id}/decision", json=body, headers={"X-Reviewer": "reviewer-1"})
    assert refused.status_code == 409 and "ID decision for sponsor-a Investors has not been made" in refused.text
    assert c.get(f"/api/runs/{run_id}").json()["status"] == "needs_approval"  # still waiting, nothing written
    decide_ids(c)
    ok = c.post(f"/api/runs/{run_id}/decision", json=body, headers={"X-Reviewer": "reviewer-1"})
    assert ok.status_code == 200, ok.text


def test_health_and_index(tmp_path: Path) -> None:
    c = make_client(tmp_path)
    h = c.get("/api/health").json()
    assert h["ok"] and h["provider"] == "demo" and h["key_configured"] is True
    assert set(h["templates"]) >= {"Investors", "Commitments", "GL"}
    page = c.get("/inbox")
    assert page.status_code == 200 and "Onboarding Worker" in page.text


def test_upload_rules(tmp_path: Path) -> None:
    c = make_client(tmp_path)
    assert c.put("/api/inbox/evil.exe", content=b"x").status_code == 400
    assert c.put("/api/inbox/.hidden.xlsx", content=b"x").status_code == 400
    assert c.put("/api/inbox/empty.xlsx", content=b"").status_code == 400
    upload_sample(c)
    names = [f["name"] for f in c.get("/api/inbox").json()["files"]]
    assert names == [DEMO_FILE]
    assert c.delete(f"/api/inbox/{DEMO_FILE}").status_code == 200
    assert c.get("/api/inbox").json()["files"] == []


def test_run_to_approval_to_download(tmp_path: Path) -> None:
    c = make_client(tmp_path)
    run_id = start(c)
    run = wait_for(c, run_id, "needs_approval")
    gate = run["gates"][0]
    assert gate["name"] == "write_upload"
    assert gate["stage"]["rows"] == 24 and gate["stage"]["join"]["matched"] > 0
    assert [o for o in run["outputs"] if o["kind"] == "upload"] == []  # nothing is written before a human decides
    assert "Investors" in run["coverage"]
    review = gate["review"]
    assert review["can_approve"] and review["err_count"] == 0 and review["rows_out"] == 24
    assert review["reconciliation"]["output_rows"] == 24

    stale = c.post(f"/api/runs/{run_id}/decision", json={"proposal_hash": "0" * 64, "verdict": "approve"})
    assert stale.status_code == 409
    assert c.get(f"/api/runs/{run_id}").json()["status"] == "needs_approval"

    ok = c.post(
        f"/api/runs/{run_id}/decision",
        json={"proposal_hash": gate["hash"], "verdict": "approve"},
        headers={"X-Reviewer": "reviewer-1"},
    )
    assert ok.status_code == 200, ok.text
    done = wait_for(c, run_id, "idle")
    assert [o["name"] for o in done["outputs"] if o["kind"] == "upload"] == ["Investors_Upload.xlsx"]
    listed = c.get("/api/downloads").json()["files"]
    up = [f for f in listed if f["kind"] == "upload"]
    assert [f["name"] for f in up] == ["Investors_Upload.xlsx"]
    assert up[0]["run_id"] == run_id and up[0]["url"] == f"/api/runs/{run_id}/files/Investors_Upload.xlsx"
    dl = c.get(f"/api/runs/{run_id}/files/Investors_Upload.xlsx")
    assert dl.status_code == 200 and dl.content[:2] == b"PK"

    audit = c.get(f"/api/runs/{run_id}/audit").json()
    assert audit["verified"] is True
    approve = next(e for e in audit["entries"] if e["action"] == "gate.approve")
    assert approve["inputs"]["proposal_hash"] == gate["hash"]
    assert approve["inputs"]["reviewer"] == "reviewer-1"
    assert any(e["action"] == "write_upload" for e in audit["entries"])


def test_reject_writes_nothing(tmp_path: Path) -> None:
    c = make_client(tmp_path)
    run_id = start(c)
    gate = wait_for(c, run_id, "needs_approval")["gates"][0]
    r = c.post(
        f"/api/runs/{run_id}/decision",
        json={"proposal_hash": gate["hash"], "verdict": "reject", "feedback": "Country looks wrong"},
    )
    assert r.status_code == 200
    done = wait_for(c, run_id, "idle")
    assert [o for o in done["outputs"] if o["kind"] == "upload"] == []
    audit = c.get(f"/api/runs/{run_id}/audit").json()
    rejected = [e for e in audit["entries"] if e["action"] == "gate.reject"]
    assert rejected and "Country looks wrong" in rejected[0]["why"]


def test_cannot_message_while_waiting_and_decision_needs_a_gate(tmp_path: Path) -> None:
    c = make_client(tmp_path)
    run_id = start(c)
    wait_for(c, run_id, "needs_approval")
    r = c.post(f"/api/runs/{run_id}/messages", json={"text": "hello"})
    assert r.status_code == 409
    assert c.post("/api/runs", json={"file": "nope.xlsx"}).status_code == 404
    assert c.post("/api/runs", json={"file": DEMO_FILE, "sponsor": "../x"}).status_code == 400
    assert c.get("/api/runs/doesnotexist").status_code == 404


def test_output_download_is_confined(tmp_path: Path) -> None:
    c = make_client(tmp_path)
    run_id = start(c)
    wait_for(c, run_id, "needs_approval")
    assert c.get(f"/api/runs/{run_id}/files/run.json").status_code == 404
    assert c.get(f"/api/runs/{run_id}/files/..%2Frun.json").status_code in (404, 400)


def test_approver_token_guards_changes_not_reads(tmp_path: Path) -> None:
    c = make_client(tmp_path, WORKER_APPROVER_TOKEN="s3cret")
    assert c.get("/api/health").json()["token_required"] is True
    assert c.put(f"/api/inbox/{DEMO_FILE}", content=b"data").status_code == 401
    ok = c.put(f"/api/inbox/{DEMO_FILE}", content=b"data", headers={"X-Approver-Token": "s3cret"})
    assert ok.status_code == 200
    assert c.post("/api/runs", json={"file": DEMO_FILE}).status_code == 401
    assert c.get("/api/runs").status_code == 200


def test_settings_write_env_without_echoing_the_key(tmp_path: Path) -> None:
    c = make_client(tmp_path)
    r = c.post(
        "/api/settings",
        json={"provider": "openrouter", "model": "vendor/good:free", "api_key": "sk-or-secret-123"},
    )
    assert r.status_code == 200
    assert "sk-or-secret-123" not in r.text
    env = (tmp_path / "test.env").read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY=sk-or-secret-123" in env and "WORKER_MODEL=vendor/good:free" in env
    assert c.post("/api/settings", json={"model": "bad model; rm"}).status_code == 400
    assert c.post("/api/settings", json={"provider": "x"}).status_code == 400


def test_models_lists_only_free_tool_capable_first(tmp_path: Path) -> None:
    from worker import models as catalog

    catalog._cache.update(at=0.0, data=[])
    c = make_client(tmp_path)
    models = c.get("/api/models").json()["models"]
    ids = [m["id"] for m in models]
    assert ids == ["vendor/good:free", "vendor/notools:free"]  # paid one is dropped, tools first
    assert models[0]["tools"] is True and models[1]["tools"] is False


def test_restart_keeps_a_paused_gate_and_approval_still_works_exactly_once(tmp_path: Path) -> None:
    c = make_client(tmp_path)
    run_id = start(c)
    gate = wait_for(c, run_id, "needs_approval")["gates"][0]
    c2 = make_client(tmp_path)  # same state dir: a server restart
    again = c2.get(f"/api/runs/{run_id}").json()
    assert again["status"] == "needs_approval" and again["gates"][0]["hash"] == gate["hash"]
    ok = c2.post(f"/api/runs/{run_id}/decision", json={"proposal_hash": gate["hash"], "verdict": "approve"})
    assert ok.status_code == 200, ok.text
    done = wait_for(c2, run_id, "idle")
    assert [o["name"] for o in done["outputs"] if o["kind"] == "upload"] == ["Investors_Upload.xlsx"]
    twice = c2.post(f"/api/runs/{run_id}/decision", json={"proposal_hash": gate["hash"], "verdict": "approve"})
    assert twice.status_code == 409


def test_demo_model_declines_other_files(tmp_path: Path) -> None:
    c = make_client(tmp_path)
    name = "Fund_Roster.xlsx"
    upload_sample(c, name)
    run_id = c.post("/api/runs", json={"file": name, "sponsor": "sponsor-a", "gp_id": "GP-001"}).json()["id"]
    run = wait_for(c, run_id, "idle")
    assert "only knows the bundled sample" in run["messages"][-1]["text"]


@pytest.mark.parametrize("bad", ["", "x" * 200 + ".xlsx", "a:b.xlsx"])
def test_bad_names_refused(tmp_path: Path, bad: str) -> None:
    c = make_client(tmp_path)
    r = c.put(f"/api/inbox/{bad}", content=b"x") if bad else c.put("/api/inbox/", content=b"x")
    assert r.status_code in (400, 404, 405)


def test_feedback_is_one_call_from_any_job_and_readable_per_sponsor(tmp_path: Path) -> None:
    c = make_client(tmp_path)
    run_id = start(c)
    wait_for(c, run_id, "needs_approval")
    r = c.post(
        f"/api/runs/{run_id}/feedback",
        json={"kind": "row", "action": "flag", "reason": "Country looks wrong", "check": "required_blank"},
    )
    assert r.status_code == 200 and r.json() == {"recorded": True, "target": "table_row", "scope": "job"}
    bad = c.post(f"/api/runs/{run_id}/feedback", json={"kind": "row", "action": "adore"})
    assert bad.status_code == 400
    listed = c.get("/api/sponsors/sponsor-a/feedback").json()
    assert listed["count"] == 1 and listed["items"][0]["reason"] == "Country looks wrong"
    audit = c.get(f"/api/runs/{run_id}/audit").json()
    assert any(e["action"] == "feedback" for e in audit["entries"]) and audit["verified"] is True


def test_health_view_reports_business_metrics_per_sponsor(tmp_path: Path) -> None:
    c = make_client(tmp_path)
    run_id = start(c)
    run = wait_for(c, run_id, "needs_approval")
    ok = c.post(f"/api/runs/{run_id}/decision", json={"proposal_hash": run["gates"][0]["hash"], "verdict": "approve"})
    assert ok.status_code == 200
    wait_for(c, run_id, "idle")
    health = c.get("/api/health/metrics", params={"sponsor": "sponsor-a"}).json()
    assert health["definition"] == "1" and health["jobs"] >= 1
    assert health["totals"]["rows"] == 24 and health["totals"]["success_rate"] == 1.0
    assert next(iter(health["groups"])).startswith("sponsor-a/Investors/")
    assert health["last_promoted_change"] is None


def test_a_console_edit_becomes_an_improvement_card_and_needs_an_admin_for_the_organisation(tmp_path: Path) -> None:
    c = make_client(tmp_path, WORKER_ADMINS="admin-1")
    key = "| 6150 valuation adjustment | valuation"
    card = c.post("/api/improvements", json={"key": key, "account": "42020", "scope": "organisation"}).json()
    # no recorded cases yet, so there is no gain to show: the gates say so instead of waving it through
    assert card["status"] == "blocked" and "no gain on any case" in card["replay"]["reasons"]
    assert c.post("/api/improvements", json={"key": key, "account": "00000"}).status_code == 400
    assert c.post("/api/improvements", json={"key": "nonsense", "account": "42020"}).status_code == 400
    listed = c.get("/api/improvements").json()
    assert listed["golden_version"] == 0 and len(listed["cards"]) == 1
    denied = c.post(f"/api/improvements/{card['id']}/decision", json={"verdict": "approve", "reason": "ok"})
    assert denied.status_code in (403, 409)  # blocked card, and not an admin
    rej = c.post(f"/api/improvements/{card['id']}/decision", json={"verdict": "reject", "reason": "no cases yet"})
    assert rej.json()["status"] == "rejected"
    assert c.post("/api/improvements/rollback", json={"reason": "x"}).status_code == 409  # nothing to roll back


def test_the_root_lands_on_the_console(tmp_path: Path) -> None:
    r = make_client(tmp_path).get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/console"
