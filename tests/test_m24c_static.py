"""M24c: the question cards read the SERVER's contract (STATUS.md, Migration M24a), not only the M24b fixture.

`tests/fixtures/questions_contract_server.json` is the `job` view the console served for an unknown-format
Commitments workbook (question_id / option_id, the recommendation on the question, examples, person_choice).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "worker" / "static"
FIXTURE = ROOT / "tests" / "fixtures" / "questions_contract_server.json"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

DRIVER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const a = src.indexOf("/* QUESTIONS:BEGIN"), b = src.indexOf("/* QUESTIONS:END */");
const mod = { exports: {} };
new Function("module", "window", src.slice(a, b))(mod, undefined);
const Q = mod.exports;
const job = JSON.parse(fs.readFileSync(process.argv[2], "utf8")).job;
const byField = Object.fromEntries(job.questions.map((q) => [q.field, q]));
const out = { html: Q.html(job, { sel: 0, sponsor: job.sponsor, reviewer: "Priya Rao" }) };
out.rec = Object.fromEntries(job.questions.map((q) => [q.field, (Q.recommended(q) || {}).id || null]));
const amt = byField.Commitment_Amount, dat = byField.Commitment_Date;
Q.setScope(amt.question_id, "sponsor");
Q.pick(amt.question_id, amt.recommended);
out.build = Q.build(job, amt.question_id);
Q.pick(dat.question_id, dat.options[0].option_id);
out.build_day = Q.build(job, dat.question_id);
Q.pickColumn(dat.question_id, "Commit Date");
out.build_custom = Q.build(job, dat.question_id);
Q.pickColumn(dat.question_id, "Not A Column");
out.build_bad = Q.build(job, dat.question_id);
console.log(JSON.stringify(out));
"""


def run() -> dict[str, Any]:
    p = subprocess.run([NODE or "node", "-e", DRIVER, str(STATIC / "console.js"), str(FIXTURE)],
                       check=False, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


@needs_node
def test_server_questions_render_with_counts_and_no_day_month_recommendation() -> None:
    out = run()
    h = out["html"]
    assert "Questions" in h and "Columns I do not recognise" in h and "Formats I will not guess" in h
    assert "4 of 4 rows conform" in h and "2 of 4 rows conform" in h
    assert "0 rows filled" in h                                  # a question with nothing to count says so
    amount = next(q for q in json.loads(FIXTURE.read_text("utf-8"))["job"]["questions"]
                  if q["field"] == "Commitment_Amount")
    assert out["rec"]["Commitment_Amount"] == amount["recommended"]
    assert out["rec"]["Commitment_Date"] is None                 # day or month first is the person's choice
    card = h.split('data-q="' + next(q["question_id"] for q in json.loads(FIXTURE.read_text("utf-8"))["job"]
                                     ["questions"] if q["field"] == "Commitment_Date") + '"')[1]
    assert "Recommended" not in card and "a accepts" not in card
    assert "show 2 failing examples" in h and "Commitments!3: 24/12/2025" in h
    assert "organisation" not in h.lower() and "organization" not in h.lower()


@needs_node
def test_the_card_carries_the_reviewer_and_a_column_of_your_own() -> None:
    out = run()
    h = out["html"]
    assert 'id="reviewer"' in h and 'value="Priya Rao"' in h and "self-declared" in h
    assert h.count("data-q-col=") == 2                           # one per card, every column of the file
    for header in ("LP ID", "Commit Date", "Amount", "Remarks"):
        assert f">{header}</option>" in h


@needs_node
def test_build_answers_with_the_servers_ids_and_a_custom_column_needs_a_known_header() -> None:
    out = run()
    b = out["build"]
    assert b["scope"] == "sponsor" and b["option"].startswith("opt-") and "custom" not in b
    assert b["action"].startswith("answer|j1|q-") and b["action"].endswith("|sponsor")
    assert out["build_day"]["option"].startswith("opt-")
    assert out["build_custom"]["custom"] == {"action": "map", "header": "Commit Date"}
    assert out["build_custom"]["option"] == ""
    assert out["build_bad"] is None


def test_page_wiring_sends_the_reviewer_the_sponsor_and_the_type() -> None:
    js = (STATIC / "console.js").read_text(encoding="utf-8")
    assert '"X-Reviewer"' in js and "headers: who()" in js and "...who()" in js
    assert 'q.set("sponsor"' in js and 'q.set("type"' in js
    assert "undo_answer|${j.id}|${qid}" in js
    assert 'accept=".xlsx,.xlsm"' in js          # the server reads Excel workbooks only
    assert "Applied: " in js


@needs_node
def test_answers_in_force_come_from_the_server_so_undo_survives_a_reload() -> None:
    fx = json.loads(FIXTURE.read_text("utf-8"))
    qid = fx["job"]["questions"][0]["question_id"]
    fx["job"]["questions"] = fx["job"]["questions"][1:]
    fx["job"]["answered"] = [{"question_id": qid, "field": "Commitment_Amount", "scope": "sponsor", "by": "Priya Rao",
                              "reason": "", "text": "Saved for Sponsor A: Amount, taken from Amount"}]
    tmp = FIXTURE.parent / "_answered.tmp.json"
    tmp.write_text(json.dumps(fx), encoding="utf-8")
    driver = (r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const a = src.indexOf("/* QUESTIONS:BEGIN"), b = src.indexOf("/* QUESTIONS:END */");
const mod = { exports: {} };
new Function("module", "window", src.slice(a, b))(mod, undefined);
const job = JSON.parse(fs.readFileSync(process.argv[2], "utf8")).job;
console.log(JSON.stringify({ html: mod.exports.html(job, { sel: 0, sponsor: job.sponsor }) }));
""")
    try:
        p = subprocess.run([NODE or "node", "-e", driver, str(STATIC / "console.js"), str(tmp)],
                           check=False, capture_output=True, text=True, encoding="utf-8", timeout=60)
    finally:
        tmp.unlink()
    assert p.returncode == 0, p.stderr
    h = json.loads(p.stdout)["html"]
    assert f'data-q-undo="{qid}"' in h and "Saved for Sponsor A: Amount, taken from Amount" in h
