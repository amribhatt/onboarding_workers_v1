"""The Reconcile tab as a table of checks in the grammar of the Issues tab: a row per check and cause with exact counts,
the affected records paged on the server (default 50, at most 200, exact total), and a decision that needs a note, is
audited with the reviewer before it takes effect, and is undone by an audited revert. Fictitious data only."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import test_console_recon as TR
from worker import console_issues as CI
from worker import console_learning as L
from worker import console_recon as CR
from worker.console import Console, ConsoleError
from worker.console_screens import state_view
from worker.web import create_app

STATIC = Path(__file__).resolve().parents[1] / "src" / "worker" / "static"
REPORT = (b"Line,Trans Type,Amount\n11010 Operating bank account,,382500.40\n15010,,400003\n32010,,245000\n"
          b"31010,,-1000000\n15910,,0\n42010,,-125000\n51020,,12500\n")


@pytest.fixture
def solo(tmp_path: Path) -> Console:
    return Console(tmp_path / "inbox")


@pytest.fixture(scope="module")
def shared(tmp_path_factory: pytest.TempPathFactory) -> tuple[Console, dict[str, Any]]:
    """One GL job with a client report for the trial balance, built once; every test starts from a clean record."""
    tmp = tmp_path_factory.mktemp("recon")
    c = Console(tmp / "inbox")
    j = TR.custom_job(c, tmp)
    L.learning_report(c, j["id"], "tb", "client_tb.csv", REPORT, "reviewer-a")
    return c, j


@pytest.fixture
def c(shared: tuple[Console, dict[str, Any]]) -> Console:
    return shared[0]


@pytest.fixture
def job(shared: tuple[Console, dict[str, Any]]) -> dict[str, Any]:
    c, j = shared
    j.pop("recon_hist", None)
    j["approved"] = False
    j["chat"].clear()
    log = c.job_dir(j) / "audit.jsonl"
    if log.is_dir():
        log.rmdir()
    elif log.exists():
        log.unlink()
    return j


def group(m: dict[str, Any], gid: str) -> dict[str, Any]:
    return next(g for g in m["groups"] if g["id"] == gid)


def worker_msg(c: Console) -> dict[str, Any]:
    return next(m for m in reversed(state_view(c)["job"]["chat"]) if m.get("options"))


# ---- the table: exact counts, causes and options from the skill table ---------------------------------------------
def test_one_row_per_check_and_cause_with_exact_counts_and_status(c: Console, job: dict[str, Any]) -> None:
    m = CR.model(c, job)
    diff = group(m, "rc.tb.rcdiff")
    assert diff["n"] == 3 and diff["state"] == "open" and diff["label"] == "Doesn't tie" and diff["family"] == "no_tie"
    assert [o["id"] for o in diff["options"]] == ["fix", "out", "park"] and diff["recommended"] == ""
    assert diff["impact"] == "block" and diff["counts"] == {"open": 3, "fix": 0, "out": 0, "hold": 0}
    assert group(m, "rc.evl.rcgap")["n"] == 1  # a check that cannot run is one record: the check itself
    assert m["checks"]["records"] == sum(g["n"] for g in m["groups"]) == m["checks"]["open"]
    assert m["checks"]["clean"] + len({g["check"] for g in m["groups"]}) == m["checks"]["applies"]
    assert m["chips"]["need"] == m["total"] and m["units"] == "records"


def test_no_label_option_or_family_is_a_literal_in_code() -> None:
    src = Path(CR.__file__).read_text(encoding="utf-8")
    for word in ("Mark as fixed", "Leave these out", "Park this", "Doesn't tie", "Waiting for the client"):
        assert word not in src
    for cid in ("rcdiff", "rcone", "rcjudge", "rcreport", "rcgap", "rctie"):
        cz = CI.causes()[cid]
        assert cz.family in {"no_tie", "missing"} and 1 <= len(cz.options) <= 4


def test_records_are_paged_on_the_server_with_an_exact_total(c: Console, job: dict[str, Any]) -> None:
    g = "rc.tb.rcdiff"
    first = CR.rows_page(c, job, g, per=2)
    assert first["total"] == 3 and first["pages"] == 2 and first["start"] == 1 and first["end"] == 2
    assert abs(first["rows"][0]["diff"]) >= abs(first["rows"][1]["diff"])  # the biggest break first
    last = CR.rows_page(c, job, g, per=2, page=1)
    assert last["start"] == 3 and last["end"] == 3 and len(last["rows"]) == 1
    assert CR.rows_page(c, job, g, per=2, page=99)["page"] == 1  # a jump past the end lands on the last page
    assert CR.rows_page(c, job, g, per=10_000)["per"] == CI.PAGE_MAX
    assert CR.rows_page(c, job, g)["per"] == CI.DEFAULT_PAGE
    assert CR.rows_page(c, job, g, q="gain")["total"] == 1


def test_every_journal_is_a_record_not_just_the_twelve_the_card_lists(solo: Console, tmp_path: Path) -> None:
    c = solo
    j = TR.gl_fund(c, tmp_path)
    cols = len(TR.S.GLSRC)

    def row(n: int, jn: str, acct: str, dr: float, cr: float) -> list[Any]:
        r: list[Any] = [None] * (cols + 1)
        for name, v in (("GL Date", "2025-02-15"), ("Journal", jn), ("Line", n), ("GL Account", acct),
                        ("Transaction Type", "Expense"), ("Debit", dr), ("Credit", cr), ("Description", "x")):
            r[TR.S.GLSRC.index(name)] = v
        return r

    lines = []
    for k in range(60):  # sixty journals, each out of balance by 10
        lines += [row(1, f"J{k}", "6100", 100, 0), row(2, f"J{k}", "1000", 0, 90)]
    p = tmp_path / "many.xlsx"
    TR.write_gl(p, lines, ["No of Shares"])
    j["up"]["path"] = str(p)
    c.save()
    m = CR.model(c, j)
    bal = [g for g in m["groups"] if g["check"] == "bal"]
    assert bal and sum(g["n"] for g in bal) >= 60  # past the card's cap of 12
    rows = CR.rows_page(c, j, bal[0]["id"])
    assert rows["per"] == 50 and rows["total"] == bal[0]["n"] and len(rows["rows"]) == min(50, bal[0]["n"])
    assert CR.rows_page(c, j, bal[0]["id"], per=200)["pages"] == -(-bal[0]["n"] // 200)


# ---- deciding: preview, one Apply, note, audit before effect, undo --------------------------------------------------
def test_a_preview_records_nothing(c: Console, job: dict[str, Any]) -> None:
    pv = CR.preview(c, job, [{"id": "rc.tb.rcdiff", "option": "fix", "value": "the client rounded"}])
    assert pv["n"] == 3 and pv["groups"][0]["rows"] and pv["groups"][0]["note"] == "the client rounded"
    assert not job.get("recon_hist")
    log = c.job_dir(job) / "audit.jsonl"
    assert not log.exists() or "recon.decided" not in log.read_text(encoding="utf-8")


def test_a_note_is_required_for_every_option_and_nothing_happens_without_one(c: Console, job: dict[str, Any]) -> None:
    for opt in ("fix", "out", "park"):
        for bad in ("", "   ", "x" * (CI.NOTE_MAX + 1)):
            with pytest.raises(CI.IssueError):
                CR.apply(c, job, [{"id": "rc.tb.rcdiff", "option": opt, "value": bad}], "Priya Shah")
    assert not job.get("recon_hist")


def test_a_decision_is_audited_with_the_reviewer_then_undone_by_an_audited_revert(c: Console, job: dict[str, Any]) -> None:
    got = CR.apply(c, job, [{"id": "rc.tb.rcdiff", "option": "fix", "value": "agreed with the client"}], "Priya Shah")
    assert got["decisions"][0]["rows"] == 3
    log = [json.loads(x) for x in (c.job_dir(job) / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    rec = next(x for x in log if x["action"] == "recon.decided")
    assert rec["inputs"]["reviewer"] == "Priya Shah" and rec["inputs"]["note"] == "agreed with the client"
    assert "Priya Shah" in rec["rationale"]
    g = group(CR.model(c, job), "rc.tb.rcdiff")
    assert g["state"] == "fix" and g["by"] == "Priya Shah" and g["counts"]["fix"] == 3 and g["options"] == []
    assert CR.rows_page(c, job, "rc.tb.rcdiff", state="open")["total"] == 0
    assert CR.rows_page(c, job, "rc.tb.rcdiff", state="fix")["total"] == 3
    with pytest.raises(CI.IssueError):  # nothing open is left to decide
        CR.apply(c, job, [{"id": "rc.tb.rcdiff", "option": "out", "value": "again"}], "Priya Shah")
    CR.undo(c, job, "Priya Shah", decision=got["decisions"][0]["id"])
    assert group(CR.model(c, job), "rc.tb.rcdiff")["state"] == "open"
    assert any(x["action"] == "recon.reverted" for x in json.loads("[" + ",".join(
        (c.job_dir(job) / "audit.jsonl").read_text(encoding="utf-8").splitlines()) + "]"))


def test_a_hold_is_released_only_with_a_note_and_the_figures_never_change(c: Console, job: dict[str, Any]) -> None:
    before = L.recon_view(c, job, c.root)["checks"]
    h = CR.apply(c, job, [{"id": "rc.tb.rcdiff", "option": "park", "value": "waiting on the client"}], "Priya Shah")
    assert group(CR.model(c, job), "rc.tb.rcdiff")["state"] == "hold"
    with pytest.raises(CI.IssueError, match="note"):
        CR.undo(c, job, "Sam Lee", decision=h["decisions"][0]["id"], reason=" ")
    CR.undo(c, job, "Sam Lee", decision=h["decisions"][0]["id"], reason="client answered")
    assert group(CR.model(c, job), "rc.tb.rcdiff")["state"] == "open"
    assert L.recon_view(c, job, c.root)["checks"] == before  # a decision never moves a figure


def test_an_agent_a_blank_name_or_an_approved_job_cannot_decide(c: Console, job: dict[str, Any]) -> None:
    pick = [{"id": "rc.tb.rcdiff", "option": "out", "value": "not part of the tie"}]
    for who in ("", "local", "key_resolver", "agent"):
        with pytest.raises(CI.IssueError):
            CR.apply(c, job, pick, who)
    job["approved"] = True
    with pytest.raises(CI.IssueError, match="approved"):
        CR.apply(c, job, pick, "Priya Shah")
    assert not job.get("recon_hist")


def test_nothing_changes_when_the_record_cannot_be_written(c: Console, job: dict[str, Any]) -> None:
    folder = c.job_dir(job)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "audit.jsonl").mkdir()  # the log cannot be opened for appending
    with pytest.raises(CI.IssueError, match="Nothing was changed"):
        CR.apply(c, job, [{"id": "rc.tb.rcdiff", "option": "out", "value": "reason"}], "Priya Shah")
    assert not job.get("recon_hist")


def test_the_other_options_of_a_waiting_check_are_only_leave_out_and_park(c: Console, job: dict[str, Any]) -> None:
    with pytest.raises(CI.IssueError, match="not an option"):
        CR.apply(c, job, [{"id": "rc.bs.rcreport", "option": "fix", "value": "reason"}], "Priya Shah")


# ---- the chat: a sentence becomes a preview with one Apply ----------------------------------------------------------
def test_a_sentence_becomes_a_preview_then_one_apply_then_undo(c: Console, job: dict[str, Any]) -> None:
    ctx = {"job": job["id"], "group": "rc.tb.rcdiff"}
    c.act("say", "Mark as fixed, with a note: the client rounded", "Priya Shah", ctx)
    last = worker_msg(c)
    assert "3 records" in last["text"] and [o["label"] for o in last["options"]] == ["Apply"]
    assert not job.get("recon_hist")  # a sentence changes nothing
    c.act(last["options"][0]["action"], "", "Priya Shah")
    assert group(CR.model(c, job), "rc.tb.rcdiff")["state"] == "fix"
    done = worker_msg(c)
    assert [o["label"] for o in done["options"]] == ["Undo"]
    c.act(done["options"][0]["action"], "", "Priya Shah")
    assert group(CR.model(c, job), "rc.tb.rcdiff")["state"] == "open"


def test_a_sentence_without_a_note_asks_for_one_and_a_park_is_released_by_a_sentence(c: Console, job: dict[str, Any]) -> None:
    ctx = {"job": job["id"], "group": "rc.tb.rcdiff"}
    c.act("say", "Park this until the client answers", "Priya Shah", ctx)
    assert "needs a note" in next(m for m in reversed(state_view(c)["job"]["chat"]) if m["who"] == "w" and "note" in m["text"])["text"]
    assert not job.get("recon_hist")
    c.act("say", "Park this until the client answers: sent the question", "Priya Shah", ctx)
    c.act(worker_msg(c)["options"][0]["action"], "", "Priya Shah")
    assert group(CR.model(c, job), "rc.tb.rcdiff")["state"] == "hold"
    c.act("say", "Release:", "Priya Shah", ctx)
    assert group(CR.model(c, job), "rc.tb.rcdiff")["state"] == "hold"
    c.act("say", "Release: client answered", "Priya Shah", ctx)
    btn = worker_msg(c)["options"][0]
    assert btn["label"] == "Release"
    c.act(btn["action"], "", "Priya Shah")
    assert group(CR.model(c, job), "rc.tb.rcdiff")["state"] == "open"


def test_the_apply_button_refuses_a_name_that_is_no_person(c: Console, job: dict[str, Any]) -> None:
    c.act("say", "Leave these out of the check: not in scope", "Priya Shah", {"job": job["id"], "group": "rc.tb.rcdiff"})
    with pytest.raises(ConsoleError):
        c.act(worker_msg(c)["options"][0]["action"], "", "local")
    assert not job.get("recon_hist")


def test_an_issues_group_id_is_not_taken_for_a_reconciliation_group(c: Console, job: dict[str, Any]) -> None:
    assert CR.chat_reply(c, job, "idlong-abc123", "Leave these out") is False


# ---- the ties of investors and commitments: rows still needing a person point to the Issues tab ----------------------
def test_the_static_tie_lists_the_rows_that_need_a_person_and_decides_nothing_here(solo: Console) -> None:
    c = solo
    for a in ("start|Sponsor A|Investors", "ask_set|cov|all", "ask_sample", "ask_go", "ex|yes", "ex|file", "go"):
        c.act(a)
    j = c.job()
    assert j is not None
    m = CR.model(c, j)
    tie = group(m, "rc.tie.rctie")
    assert tie["n"] == CI.verdict(c, j)["blocked"] == 5 and tie["impact"] == "block"
    assert [o["id"] for o in tie["options"]] == ["go"]
    with pytest.raises(CI.IssueError, match="Issues tab"):
        CR.apply(c, j, [{"id": tie["id"], "option": "go", "value": "x"}], "Priya Shah")
    page = CR.rows_page(c, j, tie["id"], per=2)
    assert page["total"] == 5 and len(page["rows"]) == 2 and page["rows"][0]["note"]
    # deciding the rows where they are decided makes the group go away
    for g in CI.model(c, j)["groups"]:
        if g["state"] == "open":
            CI.apply(c, j, [{"id": g["id"], "option": "out"}], "Priya Shah")
    assert all(g["id"] != "rc.tie.rctie" for g in CR.model(c, j)["groups"])


def test_a_commitment_whose_investor_is_not_on_file_is_its_own_row(solo: Console) -> None:
    c = solo
    for a in ("start|Sponsor A|Commitments", "ask_set|cov|all", "ask_sample", "ask_go", "go"):
        c.act(a)
    j = c.job()
    assert j is not None
    inv = [x for items in CI._grouped(c, j).values() for x in items if x["cid"] == "depmissing"]
    m = CR.model(c, j)
    got = [g for g in m["groups"] if g["check"] == "inv"]
    assert len(got) == (1 if inv else 0)
    if inv:
        assert got[0]["n"] == len(inv) and got[0]["cause"] == "rctie"


# ---- the routes -----------------------------------------------------------------------------------------------------
def test_the_routes_page_decide_and_undo_with_the_reviewer_header(tmp_path: Path) -> None:
    app = create_app(tmp_path / "state", environ={"WORKER_MODEL_PROVIDER": "demo",
                                                  "WORKER_ENV_FILE": str(tmp_path / "t.env")})
    http = TestClient(app)
    for a in ("prov|paid", "start|Sponsor B|GL Fund", "ask_set|fund|B Fund I", "ask_sample", "ask_go", "go"):
        http.post("/api/console/act", json={"action": a})
    jid = http.get("/api/console/state").json()["job"]["id"]
    m = http.get(f"/api/console/jobs/{jid}/recon?per=2").json()
    assert m["total"] >= 1 and len(m["groups"]) == min(2, m["total"]) and m["checks"]["applies"] >= 1
    assert http.get(f"/api/console/jobs/{jid}/recon?per=9999").json()["per"] == 200
    every = http.get(f"/api/console/jobs/{jid}/recon?per=200").json()["groups"]
    g0 = next(g for g in every if g["options"] and g["options"][0]["id"] != "go")  # the tie group points to Issues
    gid, opt = g0["id"], g0["options"][-1]["id"]
    rows = http.get(f"/api/console/jobs/{jid}/recon/{gid}/rows?per=1").json()
    assert rows["total"] == g0["n"] and rows["per"] == 1
    body = {"picks": [{"id": gid, "option": opt}], "note": "client has not sent it"}
    assert http.post(f"/api/console/jobs/{jid}/recon/preview", json=body).json()["n"] == g0["n"]
    assert http.post(f"/api/console/jobs/{jid}/recon/apply", json=body).status_code == 409  # "local" is no person
    ok = http.post(f"/api/console/jobs/{jid}/recon/apply", json=body, headers={"X-Reviewer": "Priya Shah"})
    assert ok.status_code == 200, ok.text
    did = ok.json()["decisions"][0]["id"]
    hdr = {"X-Reviewer": "Priya Shah"}
    if opt == "park":  # a hold's release needs a note of its own
        assert http.post(f"/api/console/jobs/{jid}/recon/undo", json={"decision": did}, headers=hdr).status_code == 409
    undone = http.post(f"/api/console/jobs/{jid}/recon/undo", json={"decision": did, "reason": "client answered"},
                       headers=hdr)
    assert undone.status_code == 200 and undone.json()["reverted"] == [did]
    assert http.get("/recon.js").status_code == 200
    assert 'src="/recon.js"' in (STATIC / "console.html").read_text(encoding="utf-8")


def test_the_page_code_has_no_decision_logic_and_the_secondary_actions_are_links() -> None:
    js = (STATIC / "recon.js").read_text(encoding="utf-8")
    assert "/recon" in js and "Math.abs" not in js and "reduce(" not in js  # no sum or difference is computed here
    assert 'class="lnk" data-rc="why"' in js and 'class="lnk" data-rc="next"' in js


# ---- the page, under Node with a stub DOM (no browser) ------------------------------------------------------------
NODE = shutil.which("node")
DRIVER = r"""
const fs = require("fs"), vm = require("vm");
const input = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
const els = {};
const mk = (id) => els[id] || (els[id] = { id, innerHTML: "", className: "", dataset: {}, style: {}, value: "", hidden: false,
  textContent: "", scrollTop: 0, offsetTop: 0, scrollHeight: 0, querySelectorAll: () => [], focus() {},
  setSelectionRange() {}, scrollIntoView() {}, click() {}, remove() {} });
const listeners = {};
global.document = { getElementById: mk, addEventListener(t, f) { (listeners[t] = listeners[t] || []).push(f); },
  querySelectorAll: () => [], querySelector: () => null, activeElement: null,
  body: { appendChild() {}, textContent: "" }, createElement: () => mk("tmp") };
global.window = global;
global.innerWidth = 1440;
global.matchMedia = () => ({ matches: false });
global.addEventListener = () => {};
global.location = { hash: input.hash || "" };
global.localStorage = { getItem: () => null, setItem() {} };
global.sessionStorage = global.localStorage;
global.navigator = {};
const calls = [];
global.fetch = async (u, o) => { calls.push(String(u)); return { ok: true, json: async () => (/\/recon\/[^/]+\/rows/.test(u) ? input.rows : /\/recon\?/.test(u) ? input.data : /\/issues\?/.test(u) ? input.issues : input.state) }; };
const src = fs.readFileSync(process.argv[2], "utf8") + "\n;globalThis.__t = { set(s) { S = s; }, render, setChat(c) { collapsed = c; } };";
vm.runInThisContext(src);
vm.runInThisContext(fs.readFileSync(process.argv[3], "utf8"));
vm.runInThisContext(fs.readFileSync(process.argv[4], "utf8"));
(async () => {
  __t.set(input.state);
  if (input.data) { RECON.state.job = input.state.job.id; RECON.state.data = input.data; RECON.state.key = "x"; }
  if (input.review) RECON.state.review = input.review;
  __t.setChat(!!input.closed);
  __t.render();
  const first = mk("page").innerHTML;
  let after = "";
  if (input.click) {
    const el = { dataset: { rc: input.click }, closest(sel) { return sel === "[data-rc]" ? this : null; } };
    for (const f of listeners.click || []) f({ target: el });
    await new Promise((r) => setTimeout(r, 20));
    after = mk("page").innerHTML;
  }
  console.log(JSON.stringify({ page: first, after, calls }));
})();
"""


def run_node(tmp_path: Path, **payload: Any) -> dict[str, Any]:
    f = tmp_path / "in.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    p = subprocess.run([NODE or "node", "-e", DRIVER, str(f), str(STATIC / "console.js"), str(STATIC / "issues.js"),
                        str(STATIC / "recon.js")], check=False, capture_output=True, text=True, encoding="utf-8",
                       timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_reconcile_tab_draws_the_table_and_the_review_message(c: Console, job: dict[str, Any], tmp_path: Path) -> None:
    m = CR.model(c, job)
    g = group(m, "rc.tb.rcdiff")
    out = run_node(tmp_path, state=state_view(c), issues=CI.model(c, job), data=m, hash="#tab=recon", closed=True)["page"]
    assert "Doesn&#39;t tie" in out or "Doesn't tie" in out or "tie" in out
    assert out.count('data-rc="rv|') == len(m["groups"]) and "Needs you" in out and "On hold" in out
    assert "Open" in out and "Dual-Entity Level" in out and "learning" not in out.split("Reconcile")[0]
    rows = CR.rows_page(c, job, g["id"])
    review = {"gid": g["id"], "group": g, "page": 0, "per": 50, "why": False, "err": "", "rows": rows}
    opened = run_node(tmp_path, state=state_view(c), issues=CI.model(c, job), data=m, rows=rows, review=review, hash="#tab=recon",
                      closed=False)["page"]
    assert f"records 1-{rows['end']} of {rows['total']}" in opened and "42010 Gain realised on disposals" in opened
    assert opened.count('class="sug"') == 3 and "Mark as fixed, with a note" in opened
    assert "Apply" not in opened.split("Pick one")[0]  # no Apply before a sentence has been sent
    waiting = group(m, "rc.bs.rcreport")
    rev2 = {"gid": waiting["id"], "group": waiting, "page": 0, "per": 50, "why": False, "err": "", "rows": CR.rows_page(c, job, waiting["id"])}
    assert "Download blank layout" in run_node(tmp_path, state=state_view(c), issues=CI.model(c, job), data=m, review=rev2, hash="#tab=recon",
                                                closed=False)["page"]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_tab_is_not_fetched_off_the_reconcile_tab_and_review_asks_the_server_for_records(
        c: Console, job: dict[str, Any], tmp_path: Path) -> None:
    m = CR.model(c, job)
    off = run_node(tmp_path, state=state_view(c), issues=CI.model(c, job), hash="#tab=issues", closed=True)
    assert not any("/recon" in u for u in off["calls"])
    on = run_node(tmp_path, state=state_view(c), issues=CI.model(c, job), data=m, rows=CR.rows_page(c, job, "rc.tb.rcdiff"), hash="#tab=recon",
                  closed=False, click="rv|rc.tb.rcdiff")
    assert any(re.search(r"/recon/rc.tb.rcdiff/rows\?page=0&per=\d+", u) for u in on["calls"])
    assert "42010 Gain realised on disposals" in on["after"]
