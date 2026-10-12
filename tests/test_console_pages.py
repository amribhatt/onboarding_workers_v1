"""UI parity 1: view models of the shell (nav, dashboard, work lists, standards, settings) and their endpoints."""

from __future__ import annotations

import io
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import openpyxl
import pytest
from fastapi.testclient import TestClient

from worker import console_pages as P
from worker import templates as T
from worker.console import Console
from worker.web import create_app


@pytest.fixture
def c(tmp_path: Path) -> Console:
    return Console(tmp_path / "inbox")


def pend(c: Console, sp: str, fund: str, days_old: int = 0, rows: int = 5) -> dict[str, Any]:
    j = {"id": f"p{len(c.S['jobs'])}", "sp": sp, "type": "Investors", "fund": fund, "rows": rows, "status": "pending",
         "d": (date.today() - timedelta(days=days_old)).isoformat()}
    c.S["jobs"].append(j)
    return j


# ---- nav -------------------------------------------------------------------------------------------------------
def test_nav_counts_come_from_jobs_and_chain(c: Console) -> None:
    n = P.nav_view(c)
    assert (n["pending"], n["rejected"], n["aging"]) == (0, 0, 0)
    assert [h["view"] for h in n["harness"]] == ["impr", "health", "skills"]
    pend(c, "Sponsor A", "Fund A")
    pend(c, "Sponsor A", "Fund A", days_old=P.AGING_DAYS + 3)
    c.S["F"]["B Fund I"]["client"] = -1
    n = P.nav_view(c)
    assert (n["pending"], n["rejected"], n["aging"]) == (2, 1, 1)
    assert n["sponsors"] == [s["n"] for s in c.S["sponsors"]]


def test_nav_active_sponsor_only_on_sponsor_pages(c: Console) -> None:
    c.act("switch|Sponsor B")
    assert P.nav_view(c)["active_sponsor"] == "Sponsor B"
    c.act("view|dash")
    assert P.nav_view(c)["active_sponsor"] == ""


# ---- dashboard -------------------------------------------------------------------------------------------------
def test_dashboard_totals_and_percentages_are_counted(c: Console) -> None:
    d = P.dashboard_view(c)
    sp, ent, jobs = d["tiles"]
    assert sp["total"] == len(c.S["sponsors"])
    assert sum(r["n"] for r in sp["rows"]) == sp["total"]
    assert sum(r["n"] for r in ent["rows"]) == ent["total"] == sum(len(c.ents_of(s["n"])) for s in c.S["sponsors"])
    assert [r["label"] for r in jobs["rows"]] == ["Pending approval", "Approved", "Needs rework"]
    assert jobs["rows"][1]["n"] == 5  # the seeded approved jobs of Sponsor B
    assert d["cycle"] == {"median": None, "completed": 0}  # nobody completed: no invented median


def test_dashboard_empty_state_has_no_division_by_zero(tmp_path: Path) -> None:
    con = Console(tmp_path / "i")
    con.S.update(sponsors=[], ent={}, jobs=[])
    d = P.dashboard_view(con)
    assert d["sponsors"] == [] and all(t["total"] == 0 for t in d["tiles"])
    assert all(r["pc"] == "0%" for t in d["tiles"] for r in t["rows"])


def test_sponsor_status_and_progress(c: Console) -> None:
    assert P.sponsor_progress(c, "Sponsor A")[0] in ("Active", "Due soon", "Aging")
    for e in c.ents_of("Sponsor B"):
        f = c.S["F"][e]
        f.update(migrated=1, client=1, bundle=1, signed={"static": 1, "gl": 1})
    assert P.sponsor_progress(c, "Sponsor B") == ("Completed", 100)
    d = P.dashboard_view(c)
    assert d["tiles"][0]["rows"][3]["n"] == 1  # Completed
    assert d["cycle"]["completed"] == 1


def test_sponsor_without_entities_has_zero_progress(c: Console) -> None:
    c.S["sponsors"].append({"id": "GP-009", "n": "Sponsor Z", "live": "2099-01-01"})
    c.S["ent"]["Sponsor Z"] = []
    row = next(s for s in P.dashboard_view(c)["sponsors"] if s["name"] == "Sponsor Z")
    assert row["entities"] == 0 and row["pct"] == 0 and row["status"] == "Active"


def test_past_go_live_is_aging(c: Console) -> None:
    c.S["sponsors"][0]["live"] = (date.today() - timedelta(days=1)).isoformat()
    name = c.S["sponsors"][0]["n"]
    assert P.sponsor_progress(c, name)[0] == "Aging"


# ---- lists -----------------------------------------------------------------------------------------------------
def test_list_pending_rejected_aging(c: Console) -> None:
    pend(c, "Sponsor A", "Fund A", rows=7)
    pend(c, "Sponsor B", "B Fund I", days_old=P.AGING_DAYS + 1, rows=9)
    c.S["F"]["Fund A"]["client"] = -1
    pending = P.list_view(c, "pending")
    assert pending["title"] == "Pending approval" and len(pending["rows"]) == 2
    assert {r["rows"] for r in pending["rows"]} == {7, 9}
    aging = P.list_view(c, "aging")
    assert [r["sponsor"] for r in aging["rows"]] == ["Sponsor B"]
    rej = P.list_view(c, "rejected")
    assert [(r["sponsor"], r["fund"], r["status"]) for r in rej["rows"]] == [("Sponsor A", "Fund A", "Rework")]


def test_lists_are_empty_when_nothing_matches(c: Console) -> None:
    for kind in ("pending", "rejected", "aging"):
        assert P.list_view(c, kind)["rows"] == []


# ---- standards --------------------------------------------------------------------------------------------------
def test_templates_view_lists_real_files() -> None:
    v = P.templates_view()
    assert v["count"] == len(T.TEMPLATES)
    by = {r["file"]: r for r in v["rows"]}
    assert by["Investors_Upload_Template.xlsx"]["columns"] == len(T.TEMPLATES["Investors"].fields)
    assert all(r["status"] == "Configured" for r in v["rows"])


def test_coa_view_filters_by_group_and_search() -> None:
    full = P.coa_view()
    assert full["total"] == full["shown"] > 20 and full["groups"][0] == "All"
    g = full["groups"][1]
    only = P.coa_view(group=g)
    assert 0 < only["shown"] < full["total"] and {r["grouping"] for r in only["rows"]} == {g}
    one = P.coa_view(q=full["rows"][0]["account"])
    assert one["shown"] >= 1 and one["rows"][0]["account"] == full["rows"][0]["account"]
    assert P.coa_view(q="zzzz-no-such")["rows"] == []


def test_ttm_view_filters() -> None:
    full = P.ttm_view()
    assert full["total"] == full["shown"] > 10
    hit = P.ttm_view(q=full["rows"][0]["code"].lower())
    assert hit["rows"][0]["code"] == full["rows"][0]["code"]
    assert P.ttm_view(q="zzzz")["rows"] == []


def test_missing_standard_file_is_an_honest_empty_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(P.T, "standards_dir", lambda: tmp_path)
    v = P.coa_view()
    assert v["missing"] is True and v["rows"] == [] and v["total"] == 0
    assert P.ttm_view()["missing"] is True


def test_standard_files_are_only_real_files() -> None:
    files = P.standard_files()
    assert "Investors_Upload_Template.xlsx" in files and "Golden_COA.csv" in files
    assert all(p.is_file() for p in files.values())


# ---- settings + the combined view ---------------------------------------------------------------------------
def test_settings_view_reports_health_and_absence() -> None:
    v = P.settings_view({"provider": "demo", "model": "m", "fallbacks": "f", "key_configured": True})
    assert (v["provider"], v["model"], v["fallbacks"], v["key_configured"], v["available"]) == ("demo", "m", "f", True, True)
    assert P.settings_view(None)["available"] is False


def test_pages_view_builds_only_the_current_page(c: Console) -> None:
    assert P.pages_view(c)["dashboard"] is not None and P.pages_view(c)["list"] is None
    c.act("view|pending")
    p = P.pages_view(c)
    assert p["list"]["title"] == "Pending approval" and p["dashboard"] is None
    for v in ("tmpls", "coa", "ttm"):
        c.act(f"view|{v}")
        assert P.pages_view(c)["standards"]["kind"] == v
    c.act("view|settings")
    assert P.pages_view(c, {"provider": "demo"})["settings"]["provider"] == "demo"


# ---- endpoints ------------------------------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(str(tmp_path / "st")))


def xlsx_bytes(rows: list[list[Any]]) -> bytes:
    book = openpyxl.Workbook()
    for r in rows:
        book.active.append(r)
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


def test_state_carries_pages(client: TestClient) -> None:
    s = client.get("/api/console/state").json()
    assert s["pages"]["nav"]["pending"] == 0 and s["pages"]["dashboard"]["tiles"][0]["title"] == "Sponsors"
    a = client.post("/api/console/act", json={"action": "view|settings"}).json()
    assert a["pages"]["settings"]["available"] is True  # the act response carries the same pages
    s = client.get("/api/console/state").json()
    assert s["pages"]["settings"]["available"] is True and "provider" in s["pages"]["settings"]


def test_upload_sponsor_list_then_save(client: TestClient) -> None:
    body = xlsx_bytes([["Sponsor ID", "Sponsor name", "Go-live date"], ["GP-101", "Sponsor Q", "2027-02-01"],
                       ["GP-102", "Sponsor R", None], ["GP-001", "Sponsor A", "2027-01-01"]])
    r = client.put("/api/console/onboard/file/list.xlsx?mode=new", content=body)
    assert r.status_code == 200
    j = r.json()
    assert j["read"] == 3 and [x["n"] for x in j["rows"]] == ["Sponsor Q", "Sponsor R"]
    assert j["skipped"][0]["row"]["n"] == "Sponsor A"  # clash reported, never added
    rows = [{**x, "live": x["live"] or "2027-04-01"} for x in j["rows"]]
    a = client.post("/api/console/act", json={"action": "onb_save|new||from list.xlsx", "text": json.dumps(rows)})
    assert a.status_code == 200
    assert {s["name"] for s in a.json()["sponsors"]} >= {"Sponsor Q", "Sponsor R"}


def test_upload_rejects_a_list_without_id_and_name(client: TestClient) -> None:
    r = client.put("/api/console/onboard/file/bad.xlsx?mode=new", content=xlsx_bytes([["Colour"], ["red"]]))
    assert r.status_code == 409 and "sponsor ID and name" in r.json()["detail"]


def test_upload_name_is_sanitised(client: TestClient, tmp_path: Path) -> None:
    body = xlsx_bytes([["Sponsor ID", "Sponsor name", "Go-live date"], ["GP-201", "Sponsor S", "2027-02-01"]])
    r = client.put("/api/console/onboard/file/..%2F..%2Fevil.xlsx?mode=new", content=body)
    assert r.status_code in (200, 404)
    assert not (tmp_path / "evil.xlsx").exists()
    assert not (tmp_path / "st" / "evil.xlsx").exists()


def test_sample_endpoint_marks_missing_go_live(client: TestClient) -> None:
    j = client.get("/api/console/onboard/sample?mode=new").json()
    assert len(j["rows"]) == 3 and [r["live"] for r in j["rows"]].count("") == 1


def test_save_blocks_missing_go_live(client: TestClient) -> None:
    rows = [{"id": "GP-300", "n": "Sponsor T", "live": ""}]
    r = client.post("/api/console/act", json={"action": "onb_save|new|", "text": json.dumps(rows)})
    assert r.status_code == 409 and "go-live" in r.json()["detail"]


def test_standards_endpoints(client: TestClient) -> None:
    assert client.get("/api/console/standards/tmpls").json()["count"] == len(T.TEMPLATES)
    coa = client.get("/api/console/standards/coa", params={"q": "bank", "group": "All"}).json()
    assert 0 < coa["shown"] < coa["total"]
    assert client.get("/api/console/standards/ttm").json()["total"] > 10
    assert client.get("/api/console/standards/nope").status_code == 404


def test_download_blank_template_and_refuse_others(client: TestClient) -> None:
    r = client.get("/api/console/standards-file/Investors_Upload_Template.xlsx")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert client.get("/api/console/standards-file/Golden_COA.csv").status_code == 200
    assert client.get("/api/console/standards-file/pyproject.toml").status_code == 404
