"""On a phone the navigation drawer must show its items, and close when one is chosen.

An older narrow-screen rule hid `.side .nav`; the later drawer rule moved `.side` off screen but never restored the
items, so the opened drawer showed only the group headings. Choosing an item left the drawer open over the page
(both found in a real browser at 390 px)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from worker.console import Console
from worker.console_screens import state_view

STATIC = Path(__file__).resolve().parents[1] / "src" / "worker" / "static"
HTML = (STATIC / "console.html").read_text(encoding="utf-8")


def test_drawer_rule_restores_the_nav_items() -> None:
    hide = HTML.index(".side .nav,.side .seg{display:none}")
    drawer = re.search(r"\.app\.no \.side\{transform:none\}", HTML)
    assert drawer, "drawer rule missing"
    block = HTML[drawer.start():HTML.index("}}", drawer.start()) + 2]
    assert drawer.start() > hide and ".side .nav{display:flex}" in block


def test_narrow_layout_keeps_the_top_bar_stuck_and_cards_inside_the_screen() -> None:
    """Found in a real browser: at 820 px and below `.main{overflow:auto}` with an auto-height app made the top bar
    scroll away with the page, and a card's grid column grew to its chips' full width (a sideways scroll at 312 px)."""
    drawer = re.search(r"\.app\.no \.side\{transform:none\}", HTML)
    assert drawer
    block = HTML[drawer.start():HTML.index("}}", drawer.start()) + 2]
    assert ".main{overflow:visible}" in block
    assert ".card>*{min-width:0}" in HTML


def test_the_page_says_it_is_loading_until_the_state_arrives() -> None:
    """Against PostgreSQL the first state took 11 s and the page was blank all that time."""
    assert 'id="page"><div class="faint" role="status"' in HTML and "Loading the console" in HTML


DRIVER = r"""
const fs = require("fs"), vm = require("vm");
const input = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
const els = {}, on = {};
const mk = (id) => els[id] || (els[id] = { id, innerHTML: "", className: "", dataset: {}, style: {}, value: "", hidden: false,
  textContent: "", scrollTop: 0, offsetTop: 0, scrollHeight: 0, querySelectorAll: () => [], focus() {},
  setSelectionRange() {}, scrollIntoView() {}, click() {}, remove() {} });
global.document = { getElementById: mk, addEventListener(t, f) { (on[t] = on[t] || []).push(f); }, querySelectorAll: () => [],
  querySelector: () => null, activeElement: null, body: { appendChild() {}, textContent: "" }, createElement: () => mk("tmp") };
global.window = global; global.innerWidth = 390;
global.matchMedia = () => ({ matches: true });
global.addEventListener = () => {};
global.location = { hash: "" };
global.localStorage = { getItem: () => null, setItem() {} };
global.sessionStorage = global.localStorage;
global.navigator = {};
global.fetch = async () => ({ ok: true, json: async () => input.state });
vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8") + "\n;globalThis.__t = { set(s) { S = s; }, render };");
__t.set(input.state); __t.render();
const click = (dataset, inside) => on.click.forEach((f) => f({ target: { closest: (s) => ({
  dataset, textContent: "x", closest: (q) => (q === "#side" && inside ? {} : null), setAttribute() {} }) } }));
click({ lay: "navo" }, false);
const opened = mk("app").className;
click({ act: "view|dash" }, true);
console.log(JSON.stringify({ opened, closed: mk("app").className }));
"""


def test_choosing_a_nav_item_closes_the_phone_drawer(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    f = tmp_path / "in.json"
    f.write_text(json.dumps({"state": state_view(Console(tmp_path / "inbox"))}), encoding="utf-8")
    p = subprocess.run([node, "-e", DRIVER, str(f), str(STATIC / "console.js")], check=False, capture_output=True,
                       text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stderr
    got = json.loads(p.stdout)
    assert got["opened"].endswith(" no") and not got["closed"].endswith(" no")
