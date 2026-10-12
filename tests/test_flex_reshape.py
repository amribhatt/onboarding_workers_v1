"""Ticket 11, the user-facing half: the job screen's "Reshape this sheet" card. The model is scripted and the
container is the in-process stand-in of `flex_local`; everything between them is the real code path (profile, role,
propose, verify, approve, derived source, replay, reuse)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import openpyxl
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

import flex_local as L
import worker.console_reshape as CR
from port_fakes import FakeRunner
from scripted_model import ScriptedModel
from worker import console_engine as CE
from worker.console import Console
from worker.engine import Refused
from worker.roles import RoleRunner
from worker.web import create_app

IMAGE = "sha256:" + "6" * 64
ANALYST = "Priya Rao"


def answer(program: str, case: str = "nested_pivot") -> AIMessage:
    return AIMessage(json.dumps({"summary": "Unpivots the statuses.", "confidence": 0.8, "output": {
        "program": program, "case": case, "steps": ["Read each investor row.", "Set aside totals once proven."]}}))


class Drafter:
    """The scripted Layout Drafter every `propose` click talks to; `script` is consumed in order."""

    def __init__(self, *script: AIMessage) -> None:
        self.model = ScriptedModel(script=list(script))

    def __call__(self, c: Console, audit: Any) -> RoleRunner:
        return RoleRunner(audit=audit, models={"layout_drafter": self.model})


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    r = FakeRunner(L.local_layout, image=IMAGE)
    monkeypatch.setattr(CR, "runner_from_env", lambda blobs=None, env=None: r)
    CR._RUNNERS.clear()
    return r


@pytest.fixture
def c(tmp_path: Path) -> Console:
    con = Console(tmp_path / "inbox")
    con.act("prov|paid")
    return con


def register(rows: list[list[str]]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet"
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def transposed(*ids: str) -> bytes:
    n = len(ids)
    return register([["Field", *[f"#{i + 1}" for i in range(n)]], ["Investor ID", *ids],
                     ["Investor Name", *[f"Investor {i}" for i in ids]], ["Country", *["US"] * n]])


def job(c: Console, data: bytes, name: str, sponsor: str = "Sponsor A", typ: str = "Investors") -> dict[str, Any]:
    c.act(f"start|{sponsor}|{typ}")
    c.act("ask_set|cov|all")
    c.ask_attach_bytes(name, data)
    c.act("ask_go")
    j = c.job()
    assert j is not None
    if c.ex_pending(j):
        c.act("ex|no")
    return j


def pivot_job(c: Console) -> dict[str, Any]:
    return job(c, L.PIVOT.read_bytes(), "investors_pivot.xlsx")


def act(c: Console, j: dict[str, Any], who: str = ANALYST, **body: Any) -> dict[str, Any]:
    return CR.reshape_act(c, j, body, who)


def propose(c: Console, j: dict[str, Any], monkeypatch: pytest.MonkeyPatch, *script: AIMessage) -> dict[str, Any]:
    d = Drafter(*script)
    monkeypatch.setattr(CR, "role_runner", d)
    return act(c, j, action="propose")


def approve(c: Console, j: dict[str, Any], v: dict[str, Any], who: str = ANALYST, **over: Any) -> dict[str, Any]:
    p = v["proposal"]
    body = {"action": "approve", "proposal_id": p["id"], "bound": p["bound"], "scope": "job",
            "acknowledged": [w["code"] for w in p["warns"]], "reason": "checked the preview", **over}
    return act(c, j, who, **body)


# ---- when the card appears -----------------------------------------------------------------------------
def test_the_card_appears_for_a_sheet_the_reader_cannot_place(c: Console, sandbox: FakeRunner) -> None:
    v = CR.reshape_view(c, pivot_job(c))
    assert v["show"] and v["sandbox"] and v["proposal"] is None and v["reuse"] == []
    assert any("title" in r for r in v["reasons"]) and any("merged" in r for r in v["reasons"])
    assert v["file"] == "investors_pivot.xlsx"


def test_a_flat_sheet_has_no_card_but_can_be_opened_by_hand(c: Console, sandbox: FakeRunner) -> None:
    flat = register([["Investor ID", "Investor Name", "Country"], ["HS-1", "Alpha", "US"], ["HS-2", "Beta", "UK"]])
    j = job(c, flat, "flat.xlsx")
    v = CR.reshape_view(c, j)
    assert not v["show"] and v["can_open"] and v["reasons"] == []
    assert CR.reshape_act(c, j, {"action": "open"}, ANALYST)["show"]


def test_a_sample_file_gets_no_card(c: Console, sandbox: FakeRunner) -> None:
    c.act("start|Sponsor A|Investors")
    c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    j = c.job()
    assert j is not None and not CR.reshape_view(c, j)["can_open"]


# ---- propose, check, approve ---------------------------------------------------------------------------
def test_the_proposal_shows_steps_checks_exact_counts_and_a_before_and_after(
        c: Console, sandbox: FakeRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    j = pivot_job(c)
    p = propose(c, j, monkeypatch, answer(L.PIVOT_PROGRAM))["proposal"]
    assert p["steps"][0] == "Read each investor row." and p["blocks"] == []
    assert [w["code"] for w in p["warns"]] == ["subtotal_rounded"]  # the sheet prints 5714.85 for 5714.8516
    k = p["counts"]
    assert k["rows_out"] == 43 and k["used"] + k["set_aside"] == k["cells_in"] and k["subtotals_proven"] > 8
    assert k["set_aside_by_reason"]["subtotal"] > 8
    assert p["after"]["columns"][0] == {"name": "Investor type", "role": "dimension"}
    assert p["after"]["rows"][0][0] == {"text": "Endowment", "from": "Sheet!A5"}  # the host's copy, with its address
    fates = {c_["fate"] for r in p["before"] for c_ in r["cells"]}
    assert {"used", "title", "header"} <= fates and "open" not in fates  # every cell has a fate
    assert set(p["bound"]) == {"input_grid_sha256", "output_table_sha256", "verification_report_sha256"}
    assert "def build(book)" in p["program"]
    assert sandbox.calls == 2


def test_a_block_cannot_be_approved_by_anyone(c: Console, sandbox: FakeRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    j = pivot_job(c)
    v = propose(c, j, monkeypatch, answer(L.UNACCOUNTED), answer(L.UNACCOUNTED))
    assert v["proposal"]["blocks"][0]["code"] == "unaccounted_cells" and v["note"]
    with pytest.raises(Refused) as exc:
        approve(c, j, v, who="Controller Chen", acknowledged=["unaccounted_cells"])
    assert exc.value.code == "blocked"
    assert "derived" not in j["up"]


def test_every_warn_must_be_ticked_and_the_approval_is_bound_to_what_was_seen(
        c: Console, sandbox: FakeRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    j = pivot_job(c)
    v = propose(c, j, monkeypatch, answer(L.PIVOT_PROGRAM))
    with pytest.raises(Refused) as exc:
        approve(c, j, v, acknowledged=[])
    assert exc.value.code == "warn_unacknowledged"
    stale = {**v["proposal"]["bound"], "output_table_sha256": "0" * 64}
    with pytest.raises(Refused) as exc:
        approve(c, j, v, bound=stale)
    assert exc.value.code == "stale_approval"
    assert "derived" not in j["up"]


def test_an_agent_name_never_approves(c: Console, sandbox: FakeRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    j = pivot_job(c)
    v = propose(c, j, monkeypatch, answer(L.PIVOT_PROGRAM))
    for who in ("layout_drafter", "agent", "system", "local"):
        with pytest.raises((Refused, CR.ReshapeError)):
            approve(c, j, v, who=who)
    assert "derived" not in j["up"]


def test_approval_makes_the_table_the_jobs_source_and_it_replays_without_a_sandbox(
        c: Console, sandbox: FakeRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    j = pivot_job(c)
    v = approve(c, j, propose(c, j, monkeypatch, answer(L.PIVOT_PROGRAM)))
    assert v["applied"]["rows"] == 43 and v["applied"]["by"] == ANALYST and v["proposal"] is None
    ev = CE.evaluation(c, j)
    src = ev.manifest.sources[0]
    assert src.kind == "derived" and src.ref == j["up"]["derived"]["table_sha256"]
    rec = ev.manifest.as_record()
    assert rec["derived_sources"][0]["approval_id"] == v["applied"]["approval_id"]
    assert ev.tally["read"] == 43
    audit = [e["action"] for e in CR.flex_of(c, j).audit.read()]
    assert audit.index("flex.exec.started") < audit.index("flex.artifact.created") < audit.index(
        "flex.artifact.approved")


def test_a_refused_click_is_audited_and_nothing_changes(c: Console, sandbox: FakeRunner,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    j = pivot_job(c)
    v = propose(c, j, monkeypatch, answer(L.PIVOT_PROGRAM))
    before = len(CR.flex_of(c, j).audit.read())
    with pytest.raises(Refused):
        approve(c, j, v, acknowledged=[])
    assert [e["action"] for e in CR.flex_of(c, j).audit.read()][before:] == ["flex.refused"]


# ---- reuse ---------------------------------------------------------------------------------------------
def test_a_saved_program_is_offered_on_an_exact_match_and_one_click_applies_it(
        c: Console, sandbox: FakeRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    first = job(c, transposed("HS-1", "HS-2", "HS-3"), "register_q1.xlsx")
    v = propose(c, first, monkeypatch, answer(L.TRANSPOSED_PROGRAM, "transposed"))
    assert v["proposal"]["blocks"] == [] and v["proposal"]["warns"] == []
    approve(c, first, v, scope="sponsor")

    second = job(c, transposed("HS-7", "HS-8", "HS-9", "HS-10"), "register_q2.xlsx")  # same layout, other data
    offered = CR.reshape_view(c, second)
    assert [r["case"] for r in offered["reuse"]] == ["transposed"]
    calls = sandbox.calls
    done = act(c, second, action="reuse", artifact_id=offered["reuse"][0]["artifact_id"])
    assert done["applied"]["rows"] == 4 and done["applied"]["by"] == ANALYST  # a clean report: the click is the approval
    assert sandbox.calls == calls + 2  # the program ran again, under the whole suite


def test_a_saved_program_is_not_offered_to_another_sponsor_or_another_layout(
        c: Console, sandbox: FakeRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    first = job(c, transposed("HS-1", "HS-2", "HS-3"), "register_q1.xlsx")
    approve(c, first, propose(c, first, monkeypatch, answer(L.TRANSPOSED_PROGRAM, "transposed")), scope="sponsor")
    other_sponsor = job(c, transposed("HS-1", "HS-2", "HS-3"), "register_b.xlsx", sponsor="Sponsor B")
    assert CR.reshape_view(c, other_sponsor)["reuse"] == []
    other_layout = job(c, register([["Field", "#1", "#2", "#3"], ["Name", "a", "b", "c"]]), "other.xlsx")
    assert CR.reshape_view(c, other_layout)["reuse"] == []


def test_a_reuse_with_a_warn_opens_the_full_card_instead_of_approving(
        c: Console, sandbox: FakeRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    first = pivot_job(c)
    approve(c, first, propose(c, first, monkeypatch, answer(L.PIVOT_PROGRAM)), scope="sponsor")
    second = job(c, L.PIVOT.read_bytes(), "investors_pivot_v2.xlsx")
    aid = CR.reshape_view(c, second)["reuse"][0]["artifact_id"]
    v = act(c, second, action="reuse", artifact_id=aid)
    assert v["applied"] is None and v["proposal"]["reused"] == aid
    assert [w["code"] for w in v["proposal"]["warns"]] == ["subtotal_rounded"]


# ---- the sandbox is off --------------------------------------------------------------------------------
def test_without_a_sandbox_the_card_says_so_and_proposes_nothing(c: Console, monkeypatch: pytest.MonkeyPatch,
                                                                 sandbox: FakeRunner) -> None:
    monkeypatch.setattr(CR, "runner_from_env", lambda blobs=None, env=None: None)
    CR._RUNNERS.clear()
    j = pivot_job(c)
    assert CR.reshape_view(c, j)["sandbox"] is False
    d = Drafter(answer(L.PIVOT_PROGRAM))
    monkeypatch.setattr(CR, "role_runner", d)
    v = act(c, j, action="propose")
    assert v["proposal"] is None and "sandbox" in v["note"] and d.model.seen == []


# ---- over HTTP -----------------------------------------------------------------------------------------
def test_the_routes_and_the_script(tmp_path: Path, sandbox: FakeRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(tmp_path / "state", environ={"WORKER_MODEL_PROVIDER": "demo",
                                                  "WORKER_ENV_FILE": str(tmp_path / "t.env")})
    http = TestClient(app)
    http.post("/api/console/act", json={"action": "start|Sponsor A|Investors"})
    http.post("/api/console/act", json={"action": "ask_set|cov|all"})
    r = http.put("/api/console/jobs/ask/attach?name=investors_pivot.xlsx", content=L.PIVOT.read_bytes(),
                 headers={"X-Reviewer": ANALYST})
    assert r.status_code == 200, r.text
    http.post("/api/console/act", json={"action": "ask_go"})
    jid = http.get("/api/console/state").json()["job"]["id"]
    monkeypatch.setattr(CR, "role_runner", Drafter(answer(L.PIVOT_PROGRAM)))
    assert http.get(f"/api/console/jobs/{jid}/reshape").json()["show"] is True
    p = http.post(f"/api/console/jobs/{jid}/reshape", json={"action": "propose"}, headers={"X-Reviewer": ANALYST})
    assert p.status_code == 200 and p.json()["proposal"]["blocks"] == []
    prop = p.json()["proposal"]
    body = {"action": "approve", "proposal_id": prop["id"], "bound": prop["bound"], "acknowledged": ["subtotal_rounded"],
            "scope": "job", "reason": "looks right"}
    assert http.post(f"/api/console/jobs/{jid}/reshape", json={**body, "acknowledged": []},
                     headers={"X-Reviewer": ANALYST}).status_code == 409
    assert http.post(f"/api/console/jobs/{jid}/reshape", json=body, headers={"X-Reviewer": "agent"}).status_code == 409
    done = http.post(f"/api/console/jobs/{jid}/reshape", json=body, headers={"X-Reviewer": ANALYST})
    assert done.status_code == 200 and done.json()["applied"]["rows"] == 43
    assert http.get("/reshape.js").status_code == 200 and "window.RESHAPE" in http.get("/reshape.js").text
    assert 'data-reshape' in http.get("/console.js").text and "/reshape.js" in http.get("/console").text
    assert http.post("/api/console/jobs/zzz/reshape", json={"action": "open"}).status_code == 404


# ---- the script's markup, from the real payload, under node with a stub DOM (no browser) -----------------
NODE = r"""
const fs = require("fs");
const payload = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
global.document = { addEventListener() {}, querySelector() { return null; } };
global.window = global; global.CSS = { escape: (s) => s };
global.fetch = async () => ({ ok: true, json: async () => payload });
eval(fs.readFileSync(process.argv[2], "utf8"));
const host = { innerHTML: "", dataset: process.env.RS_ONLY ? { only: "1" } : {}, isConnected: true };
window.RESHAPE.render(host, { id: "j1", type: "Investors" }).then(() => { process.stdout.write(host.innerHTML); });
"""


def render_with_node(tmp_path: Path, payload: dict[str, Any]) -> str:
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    (tmp_path / "r.js").write_text(NODE, encoding="utf-8")
    (tmp_path / "p.json").write_text(json.dumps(payload), encoding="utf-8")
    js = Path(CR.__file__).parent / "static" / "reshape.js"
    out = subprocess.run([node, str(tmp_path / "r.js"), str(js), str(tmp_path / "p.json")], capture_output=True,
                         text=True, timeout=60, check=True, encoding="utf-8")
    return out.stdout


def test_the_card_markup_for_a_warn_a_block_and_an_unplaced_sheet(
        c: Console, sandbox: FakeRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    j = pivot_job(c)
    empty = render_with_node(tmp_path, CR.reshape_view(c, j))
    assert "I cannot place investors_pivot.xlsx as a table" in empty and "Propose a reshape program" in empty
    assert "Row 1 is not a row of column headers" in empty
    warn = render_with_node(tmp_path, propose(c, j, monkeypatch, answer(L.PIVOT_PROGRAM)))
    assert 'data-rs="tick|subtotal_rounded"' in warn and "Approve this reshape" in warn
    assert 'data-rs="approve" disabled' in warn  # the WARN is not ticked yet
    assert "Before: the sheet" in warn and "After: the table the job will read" in warn and "Endowment" in warn
    j2 = job(c, L.PIVOT.read_bytes(), "investors_pivot_b.xlsx", sponsor="Sponsor B")
    blocked = render_with_node(tmp_path, propose(c, j2, monkeypatch, answer(L.UNACCOUNTED), answer(L.UNACCOUNTED)))
    assert "no override: nobody can approve this" in blocked and 'data-rs="approve" disabled' in blocked
    assert "BLOCK</span>" in blocked and "not accounted for" in blocked  # the open cells are shown in the sheet


TICK = r"""
const fs = require("fs");
const payload = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const host = { innerHTML: "", dataset: process.env.RS_ONLY ? { only: "1" } : {}, isConnected: true };
const on = {};
global.document = { addEventListener(t, f) { on[t] = f; }, querySelector() { return host; } };
global.window = global; global.CSS = { escape: (s) => s };
global.fetch = async () => ({ ok: true, json: async () => payload });   // the real payload: it has no `id`
eval(fs.readFileSync(process.argv[2], "utf8"));
window.RESHAPE.render(host, { id: "j1", type: "Investors" }).then(() => {
  const before = host.innerHTML;
  const box = { dataset: { rs: "tick|subtotal_rounded" }, checked: true, closest: (s) => (s === "[data-rs]" ? box : host) };
  on.change({ target: box });
  process.stdout.write(JSON.stringify({ before, after: host.innerHTML }));
});
"""


def test_ticking_a_warn_in_the_card_sticks_and_enables_approve(
        c: Console, sandbox: FakeRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Found in a real browser: the server view has no `id`, so the card read its ticks under another key and the
    box un-ticked itself, leaving Approve disabled for ever."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    j = pivot_job(c)
    payload = propose(c, j, monkeypatch, answer(L.PIVOT_PROGRAM))
    assert "id" not in payload
    (tmp_path / "t.js").write_text(TICK, encoding="utf-8")
    (tmp_path / "p.json").write_text(json.dumps(payload), encoding="utf-8")
    js = Path(CR.__file__).parent / "static" / "reshape.js"
    out = subprocess.run([node, str(tmp_path / "t.js"), str(js), str(tmp_path / "p.json")], capture_output=True,
                         text=True, timeout=60, check=True, encoding="utf-8")
    html = json.loads(out.stdout)
    assert 'data-rs="approve" disabled' in html["before"] and 'data-rs="tick|subtotal_rounded" >' in html["before"]
    assert 'data-rs="tick|subtotal_rounded" checked' in html["after"]
    assert 'data-rs="approve" disabled' not in html["after"]
    assert "data-rs-who" not in html["before"]  # no name function on the page: nothing to ask for

    # with no name known, the card asks for it itself (the Who box of the questions card may not be on this tab)
    (tmp_path / "t.js").write_text(TICK.replace("const host =", 'global.reviewer = () => "";\nconst host ='), encoding="utf-8")
    out = subprocess.run([node, str(tmp_path / "t.js"), str(js), str(tmp_path / "p.json")], capture_output=True,
                         text=True, timeout=60, check=True, encoding="utf-8")
    assert 'data-rs-who="1"' in json.loads(out.stdout)["before"]


def test_the_saved_program_is_the_first_thing_on_the_tab_a_replay_job_lands_on(
        c: Console, sandbox: FakeRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A run job lands on Issues, where the raw sheet shows as blocked rows. The offer is shown there first, as a
    one-click card (the click stays the person's approval); with nothing to offer the host stays empty."""
    first = job(c, transposed("HS-1", "HS-2", "HS-3"), "register_q1.xlsx")
    v = propose(c, first, monkeypatch, answer(L.TRANSPOSED_PROGRAM, "transposed"))
    approve(c, first, v, scope="sponsor")
    second = job(c, transposed("HS-7", "HS-8"), "register_q2.xlsx")
    monkeypatch.setenv("RS_ONLY", "1")
    offer = render_with_node(tmp_path, CR.reshape_view(c, second))
    assert 'id="reshape-offer"' in offer and "Use this one" in offer and "Propose a reshape program" not in offer
    assert "I cannot place" not in offer
    none = render_with_node(tmp_path, {**CR.reshape_view(c, second), "reuse": []})
    assert none == ""
    applied = render_with_node(tmp_path, {**CR.reshape_view(c, second), "applied": {"by": "x", "rows": 1,
                                                                                     "artifact_id": "a" * 20}})
    assert applied == ""
    page = (Path(CR.__file__).parent / "static" / "console.js").read_text(encoding="utf-8")
    issues_tab = page.split('tab === "issues") panel =')[1].split("\n")[0]
    assert issues_tab.index("data-reshape") < issues_tab.index("issuesCard(j)")
