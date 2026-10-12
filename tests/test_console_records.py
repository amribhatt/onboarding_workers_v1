"""UI parity: existing records, the entity popover and sponsor-specific settings, from real stored state."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import expected_rules as X
from worker import console_records as R
from worker import sample as S
from worker.console import Console
from worker.web import create_app


@pytest.fixture
def c(tmp_path: Path) -> Console:
    return Console(tmp_path / "inbox")


def types(v: dict) -> dict:
    return {t["type"]: t for t in v["types"]}


def test_empty_sponsor_says_so(c: Console) -> None:
    v = R.existing_view(c, "Sponsor A")
    assert v["total"] == 0
    assert [t["type"] for t in v["types"]] == list(X.REGT)
    inv = types(v)["Investors"]
    assert inv["count"] == 0 and inv["tail"] == [] and inv["ext"]["ans"] == ""
    assert "not yet asked" in inv["text"]


def test_records_come_from_the_one_store(c: Console) -> None:
    v = R.existing_view(c, "Sponsor B")
    inv = types(v)["Investors"]
    assert inv["count"] == len(c.reg_of("Sponsor B")["Investors"]) == 5
    assert len(inv["tail"]) == R.TAIL
    assert inv["tail"][-1]["id"] == c.reg_of("Sponsor B")["Investors"][-1]["id"]
    assert inv["ext"]["ans"] == "none"
    c.store_add("Sponsor B", "Vendors", [{"key": "V-777", "name": "Acme"}], "quick entry", "t")
    assert types(R.existing_view(c, "Sponsor B"))["Vendors"]["count"] == 2


def test_sample_export_and_none_answer(c: Console) -> None:
    v = R.records_act(c, {"op": "sample", "sponsor": "Sponsor A", "type": "Vendors"})
    ven = types(v)["Vendors"]
    assert ven["count"] >= 1 and ven["ext"]["ans"] == "file"
    assert c.S["sp"] == "Sponsor A"
    v = R.records_act(c, {"op": "none", "sponsor": "Sponsor A", "type": "Investors"})
    assert types(v)["Investors"]["ext"]["ans"] == "none"
    assert any("outside the app" in a["a"] for a in c.S["audit"])


def test_unknown_sponsor_and_type_refused(c: Console) -> None:
    with pytest.raises(R.RecordsError):
        R.existing_view(c, "Nobody")
    with pytest.raises(R.RecordsError):
        R.records_act(c, {"op": "sample", "sponsor": "Sponsor A", "type": "Commitments"})


def test_fund_view_lists_entities(c: Console) -> None:
    v = R.fund_view(c, "Sponsor B")
    names = [e["name"] for e in v["entities"]]
    assert names == c.ents_of("Sponsor B") and v["total"] == len(names)
    e0 = v["entities"][0]
    assert e0["id"] == c.entity_id(e0["name"]) and e0["static_done"] == c.static_done(e0["name"], "Sponsor B")
    assert v["current"] in names


def test_settings_golive_is_validated_and_audited(c: Console) -> None:
    before = len(c.S["audit"])
    with pytest.raises(R.RecordsError):
        R.records_act(c, {"op": "golive", "sponsor": "Sponsor A", "value": ""})
    with pytest.raises(R.RecordsError):
        R.records_act(c, {"op": "golive", "sponsor": "Sponsor A", "value": "next week"})
    assert len(c.S["audit"]) == before
    v = R.records_act(c, {"op": "golive", "sponsor": "Sponsor A", "value": "2026-12-01"})
    assert v["live"] == "2026-12-01"
    assert next(s for s in c.S["sponsors"] if s["n"] == "Sponsor A")["live"] == "2026-12-01"
    assert c.S["audit"][-1]["r"].startswith("analyst edit")


def test_settings_suffix_counters_and_required(c: Console) -> None:
    assert R.settings_view(c, "Sponsor A")["suffix"] is False
    assert R.records_act(c, {"op": "suffix", "sponsor": "Sponsor A", "value": "on"})["suffix"] is True
    assert c.S["lsuffix"]["Sponsor A"] is True
    c.S["req"]["Sponsor A|Investors|Email"] = True
    v = R.settings_view(c, "Sponsor A")
    assert v["required"] == [{"type": "Investors", "field": "Email", "required": True}]
    assert R.records_act(c, {"op": "reset_req", "sponsor": "Sponsor A", "type": "Investors", "value": "Email"})["required"] == []


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(str(tmp_path / "st")))


def test_endpoints(client: TestClient) -> None:
    r = client.get("/api/console/records/existing?sponsor=Sponsor B")
    assert r.status_code == 200 and r.json()["total"] >= 5
    assert client.get("/api/console/records/fund").json()["total"] >= 1
    assert client.get("/api/console/records/settings?sponsor=Sponsor A").json()["id"]
    assert client.get("/api/console/records/nope").status_code == 404
    assert client.get("/api/console/records/existing?sponsor=Nobody").status_code == 404
    r = client.post("/api/console/records/act", json={"op": "golive", "sponsor": "Sponsor A", "value": "bad"})
    assert r.status_code == 409
    r = client.post("/api/console/records/act", json={"op": "none", "sponsor": "Sponsor A", "type": "Vendors"})
    assert r.json()["types"][2]["ext"]["ans"] == "none"
    assert "RECORDS" in client.get("/records.js").text


def test_upload_export_over_http(client: TestClient, tmp_path: Path) -> None:
    p = S.write_export(tmp_path / "ex.xlsx", "Vendors")
    r = client.put("/api/console/records/file/ex.xlsx?sponsor=Sponsor A&type=Vendors", content=p.read_bytes())
    assert r.status_code == 200
    ven = next(t for t in r.json()["types"] if t["type"] == "Vendors")
    assert ven["count"] >= 1 and ven["ext"]["file"] == "ex.xlsx"
    assert client.put("/api/console/records/file/ex.xlsx?sponsor=Sponsor A&type=Nope", content=b"x").status_code == 409
