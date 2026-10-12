"""The console page code, run under Node with a stub DOM (no browser): the job screen's tabs, the chat as a launcher on
the job only, the issues table and the worker's review message, drawn from real server state. Skipped without node."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from worker import console_issues as CI
from worker.console import Console
from worker.console_screens import state_view

STATIC = Path(__file__).resolve().parents[1] / "src" / "worker" / "static"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

DRIVER = r"""
const fs = require("fs"), vm = require("vm");
const input = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
const els = {};
const mk = (id) => els[id] || (els[id] = { id, innerHTML: "", className: "", dataset: {}, style: {}, value: "", hidden: false,
  textContent: "", scrollTop: 0, offsetTop: 0, scrollHeight: 0, querySelectorAll: () => [], focus() {}, setSelectionRange() {},
  scrollIntoView() {}, click() {}, remove() {} });
global.document = { getElementById: mk, addEventListener() {}, querySelectorAll: () => [], querySelector: () => null,
  activeElement: null, body: { appendChild() {}, textContent: "" }, createElement: () => mk("tmp") };
global.window = global;
global.innerWidth = 1440;
global.matchMedia = () => ({ matches: false });
global.addEventListener = () => {};
global.location = { hash: input.hash || "" };
global.localStorage = { getItem: () => null, setItem() {} };
global.sessionStorage = global.localStorage;
global.navigator = {};
global.fetch = async (u) => ({ ok: true, json: async () => (/\/issues\?/.test(u) ? input.data : /\/rows/.test(u) ? input.rows : input.state) });
const src = fs.readFileSync(process.argv[2], "utf8") + "\n;globalThis.__t = { set(s) { S = s; }, render, setChat(c) { collapsed = c; } };";
vm.runInThisContext(src);
vm.runInThisContext(fs.readFileSync(process.argv[3], "utf8"));
__t.set(input.state);
if (input.data) { ISSUES.state.job = input.state.job.id; ISSUES.state.data = input.data; ISSUES.state.key = "x"; }
if (input.review) ISSUES.state.review = input.review;
__t.setChat(!!input.closed);
__t.render();
console.log(JSON.stringify({ page: mk("page").innerHTML, app: mk("app").className, tbar: mk("tbar").innerHTML }));
"""


@pytest.fixture
def c(tmp_path: Path) -> Console:
    return Console(tmp_path / "inbox")


def job5(c: Console) -> dict[str, Any]:
    for a in ("start|Sponsor A|Investors", "ask_set|cov|all", "ask_sample", "ask_go", "ex|yes", "ex|file", "go"):
        c.act(a)
    j = c.job()
    assert j is not None
    return j


def run(tmp_path: Path, **payload: Any) -> dict[str, Any]:
    f = tmp_path / "in.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    p = subprocess.run([NODE or "node", "-e", DRIVER, str(f), str(STATIC / "console.js"), str(STATIC / "issues.js")],
                       check=False, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_the_job_screen_is_tabs_with_the_chat_as_a_closed_launcher(c: Console, tmp_path: Path) -> None:
    j = job5(c)
    out = run(tmp_path, state=state_view(c), data=CI.model(c, j), hash="#tab=issues", closed=True)
    html = out["page"]
    for tab in ("Plan", "Issues", "Reconcile", "Output", "Activity"):
        assert f">{tab}" in html
    assert 'class="tabl on"' in html and 'href="#tab=issues"' in html
    assert "cop-fab" in html and 'class="cop' not in html  # the chat is a launcher, closed
    assert "Accept all" not in html and "Show working" not in html and 'id="fq"' not in html
    assert "Review" in html and "HS-REF-0000000000000042" in html and "On hold" in html
    assert "Ask client" not in html and "Waiting on client" not in html


def test_the_tab_comes_from_the_address(c: Console, tmp_path: Path) -> None:
    j = job5(c)
    st = state_view(c)
    plan = run(tmp_path, state=st, data=CI.model(c, j), hash="#tab=plan")["page"]
    assert 'class="tabl on" href="#tab=plan"' in plan and ">Issues" in plan
    out = run(tmp_path, state=st, data=CI.model(c, j), hash="#tab=activity")["page"]
    assert "Decisions on this job" in out and "No decisions yet" in out


def test_the_review_message_has_records_and_suggestions_not_decision_buttons(c: Console, tmp_path: Path) -> None:
    j = job5(c)
    m = CI.model(c, j)
    g = next(x for x in m["groups"] if x["cause"] == "idlong")
    st = state_view(c)
    st["me"] = ""  # no signed-in account: the page asks for the name itself
    review = {"gid": g["id"], "group": g, "page": 0, "per": 50, "why": False, "where": "", "err": "",
              "rows": CI.rows_page(c, j, g["id"]),
              "since": sum(1 for x in st["job"]["chat"] if x["who"] == "you")}  # the sentences already sent when it opened
    out = run(tmp_path, state=st, data=m, rows=review["rows"], review=review, closed=False)
    html = out["page"]
    assert "rows 1-2 of 2" in html and "0000000000000042" in html
    assert html.count('class="sug"') == len(g["options"]) and "suggested" in html
    assert "Update to the shortened IDs" in html and "Park this until the client answers" in html
    assert "Apply" not in html.split("Pick one")[0]  # no Apply before the person has sent a sentence
    assert "Becomes" in html and "Reason" in html and "Back to chat" in html  # the pane: records, then the action bar


def test_no_chat_markup_on_other_screens(c: Console, tmp_path: Path) -> None:
    job5(c)
    st = state_view(c)
    st["job"] = None
    st["ask"] = None
    st["view"] = "dash"
    out = run(tmp_path, state=st, closed=False)
    assert "cop" not in out["page"] and "Worker chat" not in out["tbar"] and "Chat with the worker" not in out["page"]


def test_a_pending_question_opens_the_chat_and_is_answerable_there(c: Console, tmp_path: Path) -> None:
    for a in ("start|Sponsor A|Investors", "ask_set|cov|all", "ask_sample", "ask_go"):
        c.act(a)
    st = state_view(c)
    assert st["job"] and st["job"]["ex_pending"]
    out = run(tmp_path, state=st, closed=True)["page"]  # closed asked for, the pending question opens it once
    assert 'class="cop' in out and "Were any Sponsor A investors loaded into Ledgerline outside this app" in out
    assert 'data-act="ex|' in out  # the answer controls are in the chat


def test_a_job_always_has_its_chat_whatever_the_shell_view(c: Console, tmp_path: Path) -> None:
    job5(c)
    for view in ("job", "existing", "pending", "sponsor"):
        st = state_view(c)
        st["view"] = view
        out = run(tmp_path, state=st, closed=True)["page"]
        assert "cop-fab" in out, view
    for a in ("start|Sponsor A|Commitments",):
        c.act(a)
    st = state_view(c)
    st["view"] = "existing"
    assert "cop-fab" in run(tmp_path, state=st, closed=True)["page"] or 'class="cop' in run(tmp_path, state=st)["page"]


def test_an_approved_job_does_not_say_it_waits_for_approval(c: Console, tmp_path: Path) -> None:
    j = job5(c)
    m = CI.model(c, j)
    m["verdict"].update(blocked=0)
    m["chips"].update(need=0, hold=0)
    m["groups"] = []
    st = state_view(c)
    for approved, want in ((False, "for your approval"), (True, "approved. The upload file is on the Output tab.")):
        st["job"]["approved"] = approved
        html = run(tmp_path, state=st, data=m, hash="#tab=issues", closed=True)["page"]
        assert want in html
        assert approved is False or "for your approval" not in html


def _review(c: Console, j: dict[str, Any], g: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
    return {"gid": g["id"], "group": g, "page": 0, "per": 50, "why": False, "where": "", "err": "", "alt": False,
            "rows": CI.rows_page(c, j, g["id"]),
            "since": sum(1 for x in st["job"]["chat"] if x["who"] == "you")}


def test_the_review_pane_is_records_first_one_line_a_row_and_does_not_scroll(c: Console, tmp_path: Path) -> None:
    """The pane is the group, not the conversation: no scrolling message list, one fitted table in the order Row,
    Record, Was, Becomes, a pager with the exact total, and Why / Open the source as plain links."""
    j = job5(c)
    m = CI.model(c, j)
    g = next(x for x in m["groups"] if x["cause"] == "idlong")
    st = state_view(c)
    rv = _review(c, j, g, st)
    html = run(tmp_path, state=st, data=m, rows=rv["rows"], review=rv, closed=False)["page"]
    assert 'class="rv"' in html and 'id="rvrecs"' in html and 'id="msgs"' not in html
    assert html.index(">Row<") < html.index(">Record<") < html.index(">Was<") < html.index(">Becomes<")
    assert "rows 1-2 of 2" in html and "Copy this page" in html
    assert 'class="lnk" data-is="why"' in html and 'class="lnk" data-is="src|' in html and 'data-is="rvx"' in html
    assert 'title="HS-REF-0000000000000042"' in html  # the whole value is the tooltip when a cell is shortened


def test_the_preview_and_its_one_apply_sit_in_the_action_bar_of_the_pane(c: Console, tmp_path: Path) -> None:
    j = job5(c)
    m = CI.model(c, j)
    g = next(x for x in m["groups"] if x["cause"] == "idlong")
    rv = _review(c, j, g, state_view(c))
    c.act("say", "Update to the shortened IDs", "Priya Shah", {"job": j["id"], "group": g["id"]})
    html = run(tmp_path, state=state_view(c), data=CI.model(c, j), rows=rv["rows"], review=rv, closed=False)["page"]
    bar = html.split('class="rv-bar"')[1]
    assert 'class="rv-msg"' in bar and bar.count(">Apply<") == 1 and "2 rows" in bar
    assert 'class="sug"' not in bar and "Other choices" in bar  # the choices come back on request


def test_the_output_tab_asks_for_the_name_when_none_is_known(c: Console, tmp_path: Path) -> None:
    """Approval needs a named person and the Output tab has no other place to give one: the card carries the box
    (the stub storage holds no name), and it is the one element with the id the page already listens to."""
    job5(c)
    st = state_view(c)
    st["me"] = ""  # no signed-in account is known
    st["job"]["tally"]["err"] = 0
    st["job"]["can_approve"] = True
    html = run(tmp_path, state=st, hash="#tab=output", closed=True)["page"]
    assert html.count('id="reviewer"') == 1 and "Approving as" in html and 'aria-label="Your name"' in html
    assert html.index('id="apnote"') < html.index('id="reviewer"') < html.index('data-act="approve"')


IDGEN_DRIVER = r"""
const fs = require("fs"), vm = require("vm");
const input = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
const el = { innerHTML: "", isConnected: true, querySelectorAll: function () { return []; }, querySelector: function () { return null; } };
global.window = global;
global.document = { querySelector: function () { return null; }, addEventListener() {} };
global.fetch = async function () { return { ok: true, json: async function () { return input.view; } }; };
global.reviewer = function () { return input.person; };
vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8"));
IDGEN.render(el, { job: { id: input.job, type: "Investors" }, sponsor: "Sponsor A" }).then(function () { console.log(JSON.stringify({ html: el.innerHTML })); });
"""


def _idgen_html(c: Console, tmp_path: Path, person: str) -> str:
    from worker import console_idgen as CG

    j = job5(c)
    f = tmp_path / "idg.json"
    view = CG.view(c, "Sponsor A", "Investors", j["id"])
    payload = json.dumps({"view": view, "job": j["id"], "person": person})
    f.write_text(payload, encoding="utf-8")
    args = [NODE or "node", "--eval", IDGEN_DRIVER, str(f), str(STATIC / "idgen.js")]
    p = subprocess.run(args, check=False, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stderr
    return str(json.loads(p.stdout)["html"])


def test_the_id_card_asks_for_the_name_when_the_tab_has_no_other_box(c: Console, tmp_path: Path) -> None:
    """On a job that has run, the Plan tab has no name box: the ID card said to type the name in a box that was not
    there. The card carries its own box while no name is known, and drops it once there is one."""
    anon = _idgen_html(c, tmp_path, "")
    named = _idgen_html(c, tmp_path, "a named person")
    assert "data-idg-who" in anon and 'aria-label="Your name"' in anon and "data-idg-save" in anon
    assert "data-idg-who" not in named and "data-idg-save" in named


COPY_DRIVER = r"""
const fs = require("fs"), vm = require("vm");
process.on("unhandledRejection", function () {});
const els = {};
const mk = function (id) {
  return els[id] || (els[id] = { id: id, innerHTML: "", className: "", dataset: {}, style: {}, value: "", hidden: false,
    textContent: "", querySelectorAll: function () { return []; }, focus() {}, setSelectionRange() {}, scrollIntoView() {},
    click() {}, remove() {} });
};
const listeners = [];
global.document = { getElementById: mk, addEventListener: function (n, f) { if (n === "click") listeners.push(f); },
  querySelectorAll: function () { return []; }, querySelector: function () { return null; }, activeElement: null,
  body: { appendChild() {}, textContent: "" }, createElement: function () { return mk("tmp"); } };
global.window = global;
global.innerWidth = 1440;
global.matchMedia = function () { return { matches: false }; };
global.addEventListener = function () {};
global.location = { hash: "" };
global.localStorage = { getItem: function () { return null; }, setItem() {} };
global.sessionStorage = global.localStorage;
const how = process.argv[2];
const write = function () { return how === "ok" ? Promise.resolve() : Promise.reject(new Error("denied")); };
Object.defineProperty(global, "navigator", { value: how === "none" ? {} : { clipboard: { writeText: write } }, configurable: true });
global.fetch = async function () { return { ok: true, json: async function () { return {}; } }; };
vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));
const target = { dataset: { copy: "row 1" } };
const ev = { target: { closest: function (sel) { return sel.indexOf("data-copy") >= 0 ? target : null; } } };
listeners.forEach(function (f) { f(ev); });
setTimeout(function () { console.log(JSON.stringify({ toast: mk("toast").innerHTML })); process.exit(0); }, 30);
"""


def _copy_toast(how: str) -> str:
    args = [NODE or "node", "--eval", COPY_DRIVER, str(STATIC / "console.js"), how]
    p = subprocess.run(args, check=False, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stderr
    return str(json.loads(p.stdout)["toast"])


def test_copy_says_copied_only_when_the_browser_copied() -> None:
    """Copy this page said "Copied" whatever the clipboard did, and a refusal surfaced as an unhandled rejection."""
    assert "Copied" in _copy_toast("ok")
    for how in ("denied", "none"):
        said = _copy_toast(how)
        assert "Could not copy" in said and "Copied<" not in said, how


def test_a_screen_reader_hears_the_stage_states_and_which_group_a_review_button_opens(c: Console, tmp_path: Path) -> None:
    """The tick and the colour were the only signs of a finished stage, and every row's button was just "Review"."""
    j = job5(c)
    out = run(tmp_path, state=state_view(c), data=CI.model(c, j), hash="#tab=issues", closed=True)["page"]
    assert ', done</span>' in out and 'aria-current="step"' in out
    assert 'aria-label="Review: IDs longer than Ledgerline allows"' in out
    assert out.count('aria-label="Review: ') == out.count('>Review</button>')


def test_the_console_sends_the_approver_token_on_api_calls_only_when_the_tab_has_one(tmp_path: Path) -> None:
    src = (STATIC / "console.js").read_text(encoding="utf-8")
    start = src.index("const approverToken")
    end = src.index("async function api(")
    script = tmp_path / "t.js"
    script.write_text(
        "const seen = []; let tok = '';\n"
        "global.window = global; global.sessionStorage = { getItem: () => tok };\n"
        "global.fetch = async (p, o) => { seen.push([p, (o && o.headers) || {}]); return {}; };\n"
        + src[start:end] +
        "(async () => {\n"
        "  await window.fetch('/api/console/state');\n"
        "  tok = 'tkn'; await window.fetch('/api/console/act', { headers: { A: '1' } });\n"
        "  await window.fetch('https://elsewhere.example/x'); await window.fetch('/static/x.js');\n"
        "  console.log(JSON.stringify(seen));\n"
        "})();\n", encoding="utf-8")
    out = subprocess.run([str(NODE), str(script)], capture_output=True, text=True, timeout=60, check=True).stdout
    seen = json.loads(out)
    assert seen[0] == ["/api/console/state", {}]
    assert seen[1] == ["/api/console/act", {"A": "1", "X-Approver-Token": "tkn"}]
    assert seen[2][1] == {} and seen[3][1] == {}
