"""M24b: the console screens for 'the user decides an unknown column or format'.

No JS unit runner exists, so the renderer is run under node against a fake contract fixture."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "worker" / "static"
FIXTURE = ROOT / "tests" / "fixtures" / "questions_contract.json"
JS = [STATIC / n for n in ("console.js", "harness.js", "learning.js", "records.js", "working.js")]
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

DRIVER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const a = src.indexOf("/* QUESTIONS:BEGIN"), b = src.indexOf("/* QUESTIONS:END */");
if (a < 0 || b < 0) throw new Error("question module markers missing");
const mod = { exports: {} };
new Function("module", "window", src.slice(a, b))(mod, undefined);
const Q = mod.exports;
const job = JSON.parse(fs.readFileSync(process.argv[2], "utf8")).job;
const out = { html: Q.html(job, { sel: 0, sponsor: job.sponsor }) };
out.recommended = Object.fromEntries(job.questions.map((q) => [q.id, (Q.recommended(q) || {}).id || null]));
out.renderable = Object.fromEntries(job.questions.map((q) => [q.id, Q.renderable(q)]));
out.order = Q.order(job.questions).map((q) => q.id);
Q.pick("q-fmt-1", "day_first");
Q.setScope("q-fmt-1", "sponsor");
Q.setReason("q-fmt-1", "their export is UK");
out.build = Q.build(job, "q-fmt-1");
Q.setScope("q-fmt-1", "organisation");
out.build_org = Q.build(job, "q-fmt-1");
out.build_unknown = Q.build(job, "q-odd-1", "x");
Q.saved(job, "q-fmt-1", out.build.savedText);
const done = Object.assign({}, job, { questions: job.questions.filter((q) => q.id !== "q-fmt-1") });
out.after = Q.html(done, { sel: 0 });
console.log(JSON.stringify(out));
"""


def _run() -> dict[str, Any]:
    assert NODE
    p = subprocess.run([NODE, "-e", DRIVER, str(STATIC / "console.js"), str(FIXTURE)],
                       check=False, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


@needs_node
@pytest.mark.parametrize("path", JS, ids=lambda p: p.name)
def test_js_files_parse(path: Path) -> None:
    p = subprocess.run([NODE or "node", "--check", str(path)], check=False, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stderr


def test_required_hooks_and_mockup_classes() -> None:
    js = (STATIC / "console.js").read_text(encoding="utf-8")
    for hook in ("data-att=", 'data-drop="attach"', 'id="attfile"', "/api/console/jobs/ask/attach?",
                 "data-q-opt", "data-q-confirm", "data-q-undo", "data-q-reason", "data-q-scope", "data-q-chat",
                 "undo_answer|", "answer|${job.id}|", "QUESTIONS.html(j"):
        assert hook in js, hook
    # the mockup's classes, verbatim: cards, tiles, notes, pills, buttons and the upload zone
    html = (STATIC / "console.html").read_text(encoding="utf-8")
    for cls in (".card{", ".tile{", ".tile.sel{", ".note{", ".n-ok{", ".uplo{", ".pill{", ".btn{", ".lnk{", ".tiles{"):
        assert cls in html, cls
    # no new style was invented for the question cards
    assert not re.search(r"\.q[-\w]*\s*\{", html)
    # the j / k / a / slash keys keep working
    assert 'e.key === "/"' in js and 'e.key === "j"' in js and 'e.key === "a"' in js
    # phone width: the option tiles wrap on a 390px screen
    assert "minmax(min(100%,230px),1fr)" in js


@needs_node
def test_cards_render_from_the_contract() -> None:
    out = _run()
    h = out["html"]
    assert "Questions" in h and "Columns I do not recognise" in h and "Date formats" in h
    assert "118 of 120 rows conform" in h and "100 of 120 rows conform" in h
    assert h.count("data-q-confirm=") == 3          # the unknown-kind question has no Confirm
    assert "This job only" in h and "Every job for Sponsor X" in h
    assert 'placeholder="Reason (optional)"' in h
    assert "show 2 failing examples" in h and "show 5 failing examples" in h  # capped at 5
    assert "row 12: 3,1" in h and "row 13: 8,8" not in h
    assert 'data-q="q-field-1"' in h and "qsel" in h  # the first card is the selected one
    assert out["order"] == ["q-field-1", "q-fmt-1", "q-num-1", "q-odd-1"]


@needs_node
def test_recommended_only_when_the_contract_marks_it_and_never_for_day_month() -> None:
    out = _run()
    assert out["recommended"]["q-field-1"] == "commit_date"
    assert out["recommended"]["q-num-1"] == "comma_decimal"
    assert out["recommended"]["q-fmt-1"] is None
    assert out["recommended"]["q-odd-1"] is None
    # even when a (wrong) contract marks a day/month option, the card never shows the mark
    fx = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for o in fx["job"]["questions"][1]["options"]:
        o["recommended"] = True
    tmp = FIXTURE.parent / "_questions_marked.tmp.json"
    tmp.write_text(json.dumps(fx), encoding="utf-8")
    try:
        p = subprocess.run([NODE or "node", "-e", DRIVER, str(STATIC / "console.js"), str(tmp)],
                           check=False, capture_output=True, text=True, encoding="utf-8", timeout=60)
    finally:
        tmp.unlink()
    assert p.returncode == 0, p.stderr
    marked = json.loads(p.stdout)
    assert marked["recommended"]["q-fmt-1"] is None
    card = marked["html"].split('data-q="q-fmt-1"')[1].split('data-q="q-num-1"')[0]
    assert "Recommended" not in card and "a accepts" not in card


@needs_node
def test_no_organisation_scope_control_is_rendered() -> None:
    out = _run()
    assert "organisation" not in out["html"].lower()
    assert "organization" not in out["html"].lower()
    assert out["html"].count('data-q-scope="q-fmt-1"') == 2   # job and sponsor only
    # a scope the screen does not offer falls back to a offered one, never to organisation
    assert out["build"]["scope"] == "sponsor"
    assert out["build_org"]["scope"] == "job"


@needs_node
def test_confirm_sends_the_contract_action_and_the_card_shows_what_was_saved() -> None:
    out = _run()
    b = out["build"]
    assert b["action"] == "answer|job-7|q-fmt-1|day_first|sponsor"
    assert b["reason"] == "their export is UK"
    assert b["savedText"].startswith("Saved for Sponsor X: Commit Date, Read day-first")
    assert out["build_unknown"] is None
    after = out["after"]
    assert "Saved for Sponsor X" in after and 'data-q-undo="q-fmt-1"' in after and ">Undo<" in after


@needs_node
def test_a_question_the_screen_cannot_draw_is_shown_as_text_never_hidden() -> None:
    out = _run()
    assert out["renderable"]["q-odd-1"] is False
    card = out["html"].split('data-q="q-odd-1"')[1]
    assert "Are these amounts in thousands?" in card
    assert "Answer in the chat" in card and "data-q-chat" in card
