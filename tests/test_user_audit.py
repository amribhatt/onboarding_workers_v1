"""Everything a person does is on the record: every action except moving around the screens is audited BEFORE it takes
effect (who, what, with which values), a refusal is audited too, a change to a field records its value before and after,
and if the record cannot be written the action does not happen. Requests that bypass `Console.act` are audited by a
middleware, inside the login gate. Fictitious data only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import openpyxl
import pytest
from fastapi.testclient import TestClient

from worker.console import Console, ConsoleError
from worker.web import create_app

WHO = "Priya Shah"
ACTOR = "Priya Shah <priya@example.com>"


@pytest.fixture
def c(tmp_path: Path) -> Console:
    return Console(tmp_path / "inbox")


def job5(c: Console) -> dict[str, Any]:
    c.act("start|Sponsor A|Investors")
    c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    c.act("ex|no")
    c.act("go")
    j = c.job()
    assert j is not None
    return j


def entries(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []


def jlog(c: Console, j: dict[str, Any]) -> list[dict[str, Any]]:
    return entries(c.job_dir(j) / "audit.jsonl")


def acts(log: list[dict[str, Any]], kind: str = "user.action") -> list[str]:
    return [e["inputs"]["action"] for e in log if e["action"] == kind]


def test_every_action_but_navigation_is_audited_with_who_and_what(c: Console) -> None:
    j = job5(c)
    c.actor = ACTOR
    before = len(acts(jlog(c, j)))
    for action in ("acceptall", "req|Investors|Country|1", "col|Country|Country", "prov|paid"):
        c.act(action, "", WHO)
    got = [e for e in jlog(c, j) if e["action"] == "user.action"][before:]
    assert [e["inputs"]["action"] for e in got] == ["acceptall", "req", "col", "prov"]
    assert all(e["inputs"]["actor"] == ACTOR and e["rationale"].startswith(ACTOR) for e in got)
    assert got[2]["inputs"]["args"] == ["Country", "Country"]
    n = len(jlog(c, j))
    for nav in ("tab|plan", "fund|Fund B", "toast_close"):  # moving around changes no data: not on the record
        c.act(nav, "", WHO)
    assert len(jlog(c, j)) == n


def test_a_field_change_records_the_value_before_and_after(c: Console) -> None:
    j = job5(c)
    c.act("col|Country|Type", "", WHO)
    c.act("col|Country|Country", "", WHO)
    ch = [e["inputs"] for e in jlog(c, j) if e["action"] == "user.field_changed" and e["inputs"]["action"] == "col"]
    assert [(x["before"], x["after"]) for x in ch][-2:] == [("", "Type"), ("Type", "Country")]
    c.act("req|Investors|Country|1", "", WHO)
    rq = [e["inputs"] for e in jlog(c, j) if e["action"] == "user.field_changed" and e["inputs"]["action"] == "req"][-1]
    assert rq["before"] is False and rq["after"] is True


def test_a_refused_action_is_on_the_record_too(c: Console) -> None:
    c.act("start|Sponsor A|Investors")
    c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    j = c.job()
    assert j is not None
    with pytest.raises(ConsoleError):
        c.act("approve", "", WHO)  # nothing is converted yet
    refused = [e for e in jlog(c, j) if e["action"] == "user.action_refused"]
    assert refused and refused[-1]["inputs"]["action"] == "approve" and refused[-1]["inputs"]["reason"]


def test_if_the_action_cannot_be_recorded_it_does_not_happen(c: Console) -> None:
    j = job5(c)
    audit = c.job_dir(j) / "audit.jsonl"
    audit.unlink()
    audit.mkdir()  # the log cannot be opened for appending
    with pytest.raises(ConsoleError, match="could not be recorded"):
        c.act("col|Country|Type", "", WHO)
    assert "Country" not in j["map"]


def test_actions_before_a_job_exists_go_to_the_persons_own_log(c: Console) -> None:
    c.act("start|Sponsor A|Investors", "", WHO)
    c.act("ask_set|cov|all", "", WHO)
    c.act("ask_sample", "", WHO)
    log = entries(Path(c.out_dir) / "user_actions.audit.jsonl")
    assert acts(log)[:3] == ["start", "ask_set", "ask_sample"]
    up = next(e for e in log if e["action"] == "source.attached")
    assert up["inputs"]["no"] == 1 and up["inputs"]["kind"] == "sample"


def test_a_typed_sentence_is_recorded_by_its_length_not_its_text(c: Console) -> None:
    j = job5(c)
    c.act("say", "a private remark about the client", WHO)
    e = [x for x in jlog(c, j) if x["action"] == "user.action" and x["inputs"]["action"] == "say"][-1]["inputs"]
    assert e["text"] == "33 characters" and "private" not in json.dumps(e)


# ---- requests that do not go through Console.act ----------------------------------------------------------------------
def _client(tmp_path: Path) -> TestClient:
    env = {"WORKER_MODEL_PROVIDER": "demo", "WORKER_ENV_FILE": str(tmp_path / "e.env"), "WORKER_REQUIRE_LOGIN": "1"}
    return TestClient(create_app(tmp_path / "state", environ=env), follow_redirects=False)


def test_a_mutating_request_is_audited_before_it_runs_and_names_the_person(tmp_path: Path) -> None:
    cl = _client(tmp_path)
    cl.post("/api/auth/register", json={"name": "Priya Rao", "email": "priya@example.com", "password": "correct horse 1"})
    for a, t in (("start|Sponsor A", ""), ("say", "build the investor upload for all entities from the sample"), ("say", "go"),
                 ("ex|no", ""), ("go", "")):
        assert cl.post("/api/console/act", json={"action": a, "text": t}).status_code == 200
    job = cl.get("/api/console/state").json()["job"]["id"]
    src = next((tmp_path / "state").glob("console_inbox/*Investors*.xlsx"))
    wb = openpyxl.load_workbook(src)
    wb.worksheets[0].insert_rows(2)
    out = tmp_path / "v2.xlsx"
    wb.save(out)
    log = next((tmp_path / "state").glob("console_out/*/*/audit.jsonl"))
    r = cl.put(f"/api/console/jobs/{job}/source?name=v2.xlsx", content=out.read_bytes())
    assert r.status_code == 200
    got = entries(log)
    req = [e for e in got if e["action"] == "user.request" and e["inputs"]["method"] == "PUT"]
    assert req and req[-1]["inputs"]["actor"] == "Priya Rao <priya@example.com>" and req[-1]["inputs"]["path"].endswith("/source")
    assert [e["action"] for e in got].index(req[-1]["action"]) < [e["action"] for e in got].index("source.replaced")  # before
    n = len(got)
    cl.post(f"/api/console/jobs/{job}/issues/preview", json={"picks": []})  # a preview changes nothing: not an action
    assert len(entries(log)) == n


def test_an_unsigned_request_is_refused_and_leaves_no_entry(tmp_path: Path) -> None:
    cl = _client(tmp_path)
    r = cl.put("/api/console/jobs/j1/source?name=x.xlsx", content=b"x")
    assert r.status_code == 401
    assert not list((tmp_path / "state").glob("console_out/**/audit.jsonl"))
