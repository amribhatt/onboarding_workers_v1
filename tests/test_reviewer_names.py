"""Demo-grade identity, made coherent (gaps step 4).

Identity on the web is self-declared (`X-Reviewer`). The rules, in one place (`contracts.approver_name`):

* a name that is an agent, a role, a model or reserved never approves;
* `local` is the unauthenticated demo reviewer: allowed only while no approver token is configured (one person at
  the keyboard), and it is recorded as `local reviewer`, never as the bare reserved word;
* with an approver token configured, an approval or a rejection names a person: a blank name and `local` are refused;
* the console's approval is recorded under the reviewer's name, not under a stock name.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import test_static_clean as F5
from test_web import make_client, start, wait_for
from worker.console import Console, ConsoleError
from worker.contracts import UnnamedApprover, approver_name


@pytest.mark.parametrize(("raw", "demo", "want"), [
    ("reviewer-x", True, "reviewer-x"),
    ("reviewer-x", False, "reviewer-x"),
    ("  Reviewer X   B  ", False, "Reviewer X B"),
    ("local", True, "local reviewer"),
    ("LOCAL", True, "local reviewer"),
    ("", True, "local reviewer"),
    ("   ", True, "local reviewer"),
])
def test_a_name_a_demo_reviewer_or_a_blank_in_demo_mode_is_recorded_as_a_person(raw: str, demo: bool, want: str) -> None:
    assert approver_name(raw, demo=demo) == want


@pytest.mark.parametrize(("raw", "demo"), [
    ("", False), ("   ", False), ("local", False), ("Local", False),
    ("agent", True), ("improver", True), ("system", False), ("console", True), ("Assistant", True),
    ("gpt-5.6-terra", True), ("policy:nightly", True), ("a/b", True),
])
def test_an_unnamed_or_non_person_approver_is_refused(raw: str, demo: bool) -> None:
    with pytest.raises(UnnamedApprover):
        approver_name(raw, demo=demo)


def _run_at_the_gate(c: Any) -> tuple[str, str]:
    run_id = start(c)
    run = wait_for(c, run_id, "needs_approval")
    return run_id, run["gates"][0]["hash"]


def test_with_a_token_the_decision_needs_a_named_person(tmp_path: Path) -> None:
    c = make_client(tmp_path, WORKER_APPROVER_TOKEN="s3cret")
    c.headers.update({"X-Approver-Token": "s3cret"})
    run_id, gate = _run_at_the_gate(c)
    url = f"/api/runs/{run_id}/decision"
    body = {"proposal_hash": gate, "verdict": "approve"}
    assert c.post(url, json=body).status_code == 403  # no name
    assert c.post(url, json=body, headers={"X-Reviewer": "local"}).status_code == 403  # the demo name
    assert c.post(url, json=body, headers={"X-Reviewer": "agent"}).status_code == 403
    assert c.get(f"/api/runs/{run_id}").json()["status"] == "needs_approval"  # nothing was released
    ok = c.post(url, json=body, headers={"X-Reviewer": "reviewer-x"})
    assert ok.status_code == 200, ok.text
    wait_for(c, run_id, "idle")
    approved = [e for e in c.get(f"/api/runs/{run_id}/audit").json()["entries"] if e["action"] == "gate.approve"]
    assert approved and approved[-1]["inputs"]["reviewer"] == "reviewer-x"


def test_in_demo_mode_the_unsigned_reviewer_is_recorded_as_local_reviewer(tmp_path: Path) -> None:
    c = make_client(tmp_path)
    run_id, gate = _run_at_the_gate(c)
    ok = c.post(f"/api/runs/{run_id}/decision", json={"proposal_hash": gate, "verdict": "approve"})
    assert ok.status_code == 200, ok.text
    wait_for(c, run_id, "idle")
    entries = c.get(f"/api/runs/{run_id}/audit").json()["entries"]
    approved = [e for e in entries if e["action"] == "gate.approve"]
    assert approved[-1]["inputs"]["reviewer"] == "local reviewer"
    written = [e for e in entries if e["action"] == "write_upload"]
    assert written[-1]["outputs"]["approved_by"] == "local reviewer"


def test_the_console_approval_is_recorded_under_the_reviewers_name(tmp_path: Path) -> None:
    c = Console(tmp_path / "inbox")
    c.act("prov|paid")
    j = F5.console_job(c, "Investors")
    c.act("acceptall")
    c.act("approve", who="reviewer-x")
    assert j["approved"] and j["approver"] == "reviewer-x"


@pytest.mark.parametrize("who", ["agent", "", "local", "improver"])
def test_the_console_approval_by_a_non_person_or_no_one_is_refused_by_the_library(tmp_path: Path, who: str) -> None:
    c = Console(tmp_path / "inbox")
    c.act("prov|paid")
    j = F5.console_job(c, "Investors")
    c.act("acceptall")
    with pytest.raises(ConsoleError):
        c.act("approve", who=who)
    assert not j["approved"] and "approver" not in j  # there is no default person


def test_the_web_refuses_an_unnamed_console_approval_when_a_token_is_set(tmp_path: Path) -> None:
    c = make_client(tmp_path, WORKER_APPROVER_TOKEN="s3cret")
    c.headers.update({"X-Approver-Token": "s3cret"})
    assert c.post("/api/console/act", json={"action": "approve|", "text": ""}).status_code == 403
    assert c.post("/api/console/act", json={"action": "so|signstatic|Fund A", "text": ""}).status_code == 403
    named = c.post("/api/console/act", json={"action": "approve|", "text": ""}, headers={"X-Reviewer": "reviewer-x"})
    assert named.status_code == 409  # a named person reaches the console, which says there is no job open
