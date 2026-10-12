"""Migration M0: the console path and the agent path produce the same outputs, findings and tie-outs.

The same fixtures (the sample world's files, never an answer key) go through
  * the console path: `worker.console` through `worker.console_engine` (the one engine), and
  * the agent path: `worker.tools` / `worker.staging` / `worker.quality` driven by `tests/scripted_model.py`,
for every record type (Investors, Commitments, Investments, Vendors, Affiliates, GL Fund, GL LP), each as a clean
case and as a dirty case that is refused. They are compared on:
  * the rows of the output file, field for field;
  * the ERR findings as (code, severity, source row);
  * the tie-out totals of the written file (`tests/tieout.py`, recomputed from the file) and, for GL, the net of every journal.

Where the paths differ today, the difference is a named entry in `EXCEPTIONS` with its reason, and the exact
(code, severity, row) findings it covers are listed per type in `DIRTY`. The assertions go both ways: a divergence
that is not listed fails, and a listed one that has gone away fails too, so the list can only shrink. Later
migration steps (M1+) remove entries as the paths converge on one engine.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import test_gl_agent as GL
import test_static_clean as F5
from id_decision import decide_ids
from scripted_model import ScriptedModel, approve, call, say
from tieout import tie_out
from worker import console_demo as D
from worker import console_engine as CE
from worker import sample as S
from worker.accounts import key_of
from worker.agent import GateRefused, build_agent, decide, pending_gate, start
from worker.audit import AuditLog
from worker.console import Console
from worker.context import RunContext
from worker.money import total
from worker.registry import Registry
from worker.review import review
from worker.staging import Stage

Finding = tuple[str, str, int]  # (code, severity, source sheet row)

STATIC = ["Investors", "Commitments", "Investments", "Vendors", "Affiliates"]

# ---- the documented exceptions -------------------------------------------------------------------------------
EXCEPTIONS: dict[str, str] = {
    "E1_commitments_fund_columns":
        "The console fills Commitments Fund_ID / Fund_Name from its entity table; the agent path has no entity "
        "source, so the fields come from the lifted column or stay blank (never invented). Known since F5.",
    "E2_agent_audit_has_no_tie_out":
        "The console records the tie-out facts in its `write_upload` audit record; the agent path's record carries "
        "the file and row count only. The test recomputes the tie-out from each written file with the one "
        "`tests/tieout.py` function and checks it equals the console's recorded one.",
    "E3_console_only_checks":
        "Clash and duplicate checks against the store and the file (key clash, name clash, Ltd/Limited, the same "
        "name apart from punctuation), generated-ID collisions, bad ID characters and the GL-only investment check "
        "exist only in the console family (`engine_console.eval_static`). The agent path has no stage check for them.",
    "E4_blank_required_severity":
        "The console makes a blank required field an ERR on the row; the agent path reports required_blank as a "
        "WARN that a human acknowledges with a reason.",
    "E5_id_length_rows":
        "the console family (`engine_console.eval_static`) checks the length of the record's own ID, and only on a row with no other ERR; the agent "
        "path checks every ID field of every row, so it also flags Position_ID (Investments).",
    "E6_gl_crosswalk_and_account_double_report":
        "The console checks vendor, affiliate and investment references against its crosswalk; the agent path has "
        "no such check for them (investors are resolved by lookup: unknown_investor, compared as the same finding). "
        "The agent path also reports an unmapped account twice (quality account_not_in_chart and the mapping's "
        "account_unmapped), the console once.",
    "E7_agent_writes_dirty_vendors":
        "The console refuses the sample Vendors file (a generated-ID collision and a bad ID character); on the "
        "agent path those two checks do not exist, so after the WARN is acknowledged the file is written as is.",
    "E8_warn_findings_not_compared":
        "WARN findings are worded and counted per row by the console and per cause by the agent path; the test "
        "compares ERR findings and the gate decision, not WARN wording.",
}

# dirty case, per type: the findings seen on one path only, the codes both find on the same rows, whether the
# agent gate refuses, and the exceptions that explain the difference.
DIRTY: dict[str, dict[str, Any]] = {
    "Investors": {"common": {"id_too_long"}, "console_only": {("required_blank", "ERR", 16)}, "agent_only": set(),
                  "agent_refuses": True, "why": ["E4_blank_required_severity"]},
    "Commitments": {"common": {"missing_dependency"}, "console_only": {("required_blank", "ERR", 8)},
                    "agent_only": set(), "agent_refuses": True, "why": ["E4_blank_required_severity"]},
    "Investments": {"common": {"id_too_long"}, "console_only": {("gl_only_investment", "ERR", 6)},
                    "agent_only": {("id_too_long", "ERR", 3), ("id_too_long", "ERR", 6)}, "agent_refuses": True,
                    "why": ["E3_console_only_checks", "E5_id_length_rows"]},
    "Vendors": {"common": set(), "console_only": {("bad_id_chars", "ERR", 7), ("generated_id_collision", "ERR", 2),
                                                  ("generated_id_collision", "ERR", 4)},
                "agent_only": set(), "agent_refuses": False,
                "why": ["E3_console_only_checks", "E7_agent_writes_dirty_vendors"]},
    "Affiliates": {"common": {"id_too_long"}, "console_only": {("required_blank", "ERR", 6),
                                                               ("similar_in_file", "ERR", 3)},
                   "agent_only": set(), "agent_refuses": True,
                   "why": ["E3_console_only_checks", "E4_blank_required_severity"]},
    "GL Fund": {"common": {"account_not_in_chart", "deal_required", "journal_unbalanced"},
                "console_only": {("reference_not_in_crosswalk", "ERR", 4)},
                "agent_only": {("account_unmapped", "ERR", 9), ("account_unmapped", "ERR", 19)},
                "agent_refuses": True, "why": ["E6_gl_crosswalk_and_account_double_report"]},
    "GL LP": {"common": {"unknown_investor"}, "console_only": set(), "agent_only": set(), "agent_refuses": True,
              "why": []},
}


# ---- normalising each path's findings ------------------------------------------------------------------------
def _row_of(locator: str) -> int:
    m = re.search(r"(\d+)$", locator)
    return int(m.group(1)) if m else 0


def _ranges(text: str) -> list[int]:
    out: list[int] = []
    for part in re.split(r",\s*", text):
        m = re.fullmatch(r"(\d+)\s*[–-]\s*(\d+)", part.strip())
        if m:
            out.extend(range(int(m.group(1)), int(m.group(2)) + 1))
        elif part.strip().isdigit():
            out.append(int(part))
    return out


def agent_err(rev: dict[str, Any]) -> set[Finding]:
    """ERR findings of the agent path's review as (rule or code, ERR, source row)."""
    out: set[Finding] = set()
    for f in rev["findings"]:
        if f["severity"] != "ERR":
            continue
        code = f["rule"] if f["code"] == "data_quality" else f["code"]
        rows = _ranges(f["ranges"]) if f["code"] == "data_quality" else [_row_of(str(r)) for r in f["rows"]]
        out.update((code, "ERR", n) for n in (rows or [0]))
    return out


_STATIC_FLAGS = {"depMissing": "missing_dependency", "keyClash": "key_clash", "nameClash": "name_clash",
                 "suffix": "suffix_clash", "similar": "similar_in_file", "badChars": "bad_id_chars",
                 "collideGen": "generated_id_collision", "glOnly": "gl_only_investment"}


def console_static_err(c: Console, j: dict[str, Any]) -> set[Finding]:
    out: set[Finding] = set()
    for r in CE.static_rows(c, j):
        if r["st"] != "err":
            continue
        codes = {code for flag, code in _STATIC_FLAGS.items() if flag in r["fl"]}
        codes.update("required_blank" for _ in r["blank"])
        if "characters, limit is" in r["why"]:
            codes.add("id_too_long")
        if r["why"].startswith("ID collides"):
            codes.add("id_collision_after_cut")
        out.update((code, "ERR", r["row"]) for code in codes)
    return out


_GL_MESSAGES = [(re.compile(r"^Investor .* is not in the crosswalk"), "unknown_investor"),
                (re.compile(r"is not in the crosswalk"), "reference_not_in_crosswalk"),
                (re.compile(r"has no Ledgerline account"), "account_not_in_chart"),
                (re.compile(r"Deal name is blank"), "deal_required"),
                (re.compile(r"Journal does not balance"), "journal_unbalanced"),
                (re.compile(r"Transaction type .* is not mapped"), "transaction_type_unknown")]


def console_gl_err(c: Console, j: dict[str, Any]) -> set[Finding]:
    out: set[Finding] = set()
    for idx, x in enumerate(CE.gl_eval(c, j)["lines"]):
        for e in x["errs"]:
            for pat, code in _GL_MESSAGES:
                if pat.search(e["m"]):
                    out.add((code, "ERR", idx + 2))
                    break
    return out


def console_journal_net(c: Console, j: dict[str, Any]) -> dict[str, float]:
    return {x["j"]: x["diff"] for x in CE.gl_eval(c, j)["js"]}


def agent_journal_net(ctx: RunContext, stage_id: str) -> dict[str, float]:
    """The net of every journal in the staged table: the signed Total_Amount summed in cents."""
    by: dict[str, list[Any]] = {}
    for r in ctx.stage.rows(stage_id):
        by.setdefault(r["Journal_ID"], []).append(r["Total_Amount"])
    return {k: float(total(v)) for k, v in by.items()}


def assert_divergence(typ: str, con: set[Finding], age: set[Finding]) -> None:
    want = DIRTY[typ]
    assert con - age == want["console_only"], (typ, "console only", sorted(con - age))
    assert age - con == want["agent_only"], (typ, "agent only", sorted(age - con))
    assert {code for code, _s, _n in con & age} == want["common"], (typ, sorted(con & age))
    assert all(why in EXCEPTIONS for why in want["why"])
    assert (con != age) == bool(want["why"]), typ  # a listed exception must still be needed, and vice versa


# ---- static types: clean ---------------------------------------------------------------------------------------
@pytest.fixture
def clean_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    for typ, keep in F5.CLEAN_ROWS.items():
        rows = [r for r in S.CFG[typ]["rows"] if r.i in keep]
        monkeypatch.setitem(S.CFG[typ], "rows", rows)


def agent_context(tmp: Path, inbox: Path, state: Path) -> RunContext:
    out = tmp / "run" / "out"
    return RunContext(sponsor="sponsor-a", gp_id="sponsor-a", inbox_roots=[inbox], out_dir=out,
                      audit=AuditLog(out / "audit.jsonl"), stage=Stage(out), registry=Registry(state, "sponsor-a"),
                      id_root=state / "ids")


@pytest.mark.parametrize("typ", STATIC)
def test_parity_static_clean(typ: str, clean_samples: None, tmp_path: Path) -> None:
    c = Console(tmp_path / "inbox")
    c.act("prov|paid")
    if typ == "Commitments":  # its investors are approved first, in the shared store
        F5.console_job(c, "Investors")
        c.act("acceptall")
        decide_ids(c)
        c.act("approve", who="reviewer-a")
    state = tmp_path / "agent_state"
    if (c.root / "registry").is_dir():
        shutil.copytree(c.root / "registry", state / "registry")
    j = F5.console_job(c, typ)
    c.act("acceptall")
    decide_ids(c)
    if (c.root / "ids").is_dir():  # the sponsor's ID decision and counter, as the console found them (one store)
        shutil.copytree(c.root / "ids", state / "ids", dirs_exist_ok=True)
    c.act("approve", who="reviewer-a")
    want_header, want = F5.read_file(Path(j["out"]["path"]))

    ctx = agent_context(tmp_path, c.inbox, state)
    script = F5.agent_script(typ, Path(j["up"]["path"]).name, j["up"]["sheet"])
    agent = build_agent(ctx, ScriptedModel(script=script), InMemorySaver())
    result = start(agent, ctx, f"build me the {typ} sheet", "t1")
    if typ in ("Investors", "Commitments"):  # the ID scheme is a human decision
        result = decide(agent, ctx, "t1", {"type": "approve"}, "the sponsor numbers these like the console", {"reviewer": "reviewer-a"})
    rev = review(ctx, pending_gate(result)[0]["args"]["stage_id"])
    approve(agent, ctx, "t1", "checked against the source")
    got_header, got = F5.read_file(ctx.out_dir / f"{typ}_Upload.xlsx")

    skip = {"Fund_ID", "Fund_Name"} if typ == "Commitments" else set()  # E1
    assert F5.CONSOLE_ONLY.get(typ, set()) == skip
    assert got_header == want_header
    assert [{k: v for k, v in r.items() if k not in skip} for r in got] == \
           [{k: v for k, v in r.items() if k not in skip} for r in want]
    # no ERR on either path: the findings are identical (both empty), and both gates let the file through
    assert console_static_err(c, j) == agent_err(rev) == set()
    assert CE.tally(c, j)["err"] == 0 and rev["err_count"] == 0
    # tie-out: recomputed from each file by the one function; the console recorded the same in its audit (E2)
    assert tie_out(got, typ, len(got)) == tie_out(want, typ, len(want))
    recorded = next(e for e in AuditLog(Path(j["out"]["path"]).parent / "audit.jsonl").read()
                    if e["action"] == "write_upload")["outputs"]["tie_out"]
    assert recorded == tie_out(want, typ, len(want))
    assert "tie_out" not in next(e for e in ctx.audit.read() if e["action"] == "write_upload")["outputs"]


# ---- static types: dirty ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("typ", STATIC)
def test_parity_static_dirty(typ: str, tmp_path: Path) -> None:
    """The sample file with its planted defects: both paths find the same ERRs where both check, and the rest is
    exactly the listed divergence."""
    c = Console(tmp_path / "inbox")
    c.act("prov|paid")
    j = F5.console_job(c, typ)
    out = tmp_path / "run" / "out"
    ctx = RunContext(sponsor="sponsor-a", gp_id="sponsor-a", inbox_roots=[c.inbox], out_dir=out,
                     audit=AuditLog(out / "audit.jsonl"), stage=Stage(out),
                     registry=Registry(tmp_path / "state", "sponsor-a"))
    lift = call("lift", path=Path(j["up"]["path"]).name, sheet=j["up"]["sheet"], template=typ,
                columns=F5.LIFTS[typ], header_row=1, first_row=2, constants={"GP_ID": "sponsor-a"},
                why="copy the columns")
    script = [say("lift", lift), say("write", call("write_upload", stage_id="s1", template=typ, why="deliver")),
              say("done")]
    agent = build_agent(ctx, ScriptedModel(script=script), InMemorySaver())
    start(agent, ctx, f"build me the {typ} sheet", "t1")

    assert_divergence(typ, console_static_err(c, j), agent_err(review(ctx, "s1")))
    assert CE.tally(c, j)["err"] > 0  # the console does not write this file as it stands
    if DIRTY[typ]["agent_refuses"]:
        with pytest.raises(GateRefused):
            approve(agent, ctx, "t1", "looks fine")
        assert not list(ctx.out_dir.glob("*_Upload*.xlsx"))
    else:  # E7
        approve(agent, ctx, "t1", "looks fine")
        assert (ctx.out_dir / f"{typ}_Upload.xlsx").is_file()


# ---- GL Fund and GL LP ---------------------------------------------------------------------------------------
def gl_agent(tmp: Path, c: Console, src: Path, typ: str, picks: dict[str, str]) -> tuple[Any, RunContext, Any]:
    """The agent path over the same GL source file, sharing the console's registry (investor lookup)."""
    ctx = GL.agent_ctx(tmp, state=c.root)
    (ctx.inbox_roots[0] / src.name).write_bytes(src.read_bytes())
    lp = typ == "GL LP"
    script = [
        say("lift", call("lift_gl_lines", path=src.name, fund_id=c.entity_id("Fund A"), fund_name="Fund A",
                         offsets=lp, why="stage the GL lines")),
        say("map", call("map_accounts", stage_id="s1", why="map the GL accounts")),
        say("confirm", call("confirm_account_mappings", stage_id="s2", picks=picks, why="the person confirmed")),
        say("check", call("check_data_quality", path=src.name, record_type="GL",
                          columns=GL.LP_COLUMNS if lp else GL.COLUMNS, why="find every problem")),
        say("write", call("write_upload", stage_id="s3", template="GL", why="deliver the file")),
        say("done"),
    ]
    agent = build_agent(ctx, ScriptedModel(script=script), InMemorySaver())
    result = start(agent, ctx, "build me the GL sheet", "t1")
    assert [g["name"] for g in pending_gate(result)] == ["confirm_account_mappings"]
    decide(agent, ctx, "t1", {"type": "approve"}, "these are the accounts", {"reviewer": "reviewer-a"})
    return agent, ctx, result


def golden_picks(typ: str) -> dict[str, str]:
    golden = {a: g.split(" ", 1)[0] for a, g, flag in D.ACCTS if flag}
    return {key_of("", ln.acct, ln.tt): golden[ln.acct] for ln in S.GLD[typ]["lines"] if ln.acct in golden}


@pytest.mark.parametrize("typ", ["GL Fund", "GL LP"])
def test_parity_gl_clean(typ: str, tmp_path: Path) -> None:
    lines: list[Any] = []

    def build(c: Console) -> Path:
        if typ == "GL Fund":
            lines.extend(GL.CLEAN_FUND)
            return GL.write_clean(tmp_path / "src" / "gl.xlsx", lines)
        two = [e["source_key"] or e["key"] for e in c.registry("Sponsor A").entries("Investors")[:2]]
        lines.extend([("J-4001", 1, "3000", "Capital call", 0, 300000, two[0]),
                      ("J-4001", 2, "3000", "Capital call", 0, 150000, two[1]),
                      ("J-4002", 1, "3100", "Distribution", 150000, 0, two[1])])
        return GL.write_clean(tmp_path / "src" / "gl.xlsx", lines)

    c, j, src = GL.console_file(tmp_path, typ, build)
    assert CE.tally(c, j)["err"] == 0, CE.tally(c, j)
    con_err = console_gl_err(c, j)
    c.act("acceptall")
    decide_ids(c)
    c.act("approve", who="reviewer-a")
    want = GL.read_file(Path(j["out"]["path"]))
    want_net = console_journal_net(c, j)

    picks = {key_of("", acct, tt): c.known_account("Sponsor A", acct, tt) for _j, _l, acct, tt, *_r in lines}
    agent, ctx, _result = gl_agent(tmp_path, c, src, typ, picks)
    rev = review(ctx, "s3")
    approve(agent, ctx, "t1", "checked against the source")
    got = GL.read_file(ctx.out_dir / "GL_Upload.xlsx")

    assert got == want  # same header, same rows, same offsets, same amounts
    assert con_err == agent_err(rev) == set()
    assert rev["err_count"] == 0
    assert agent_journal_net(ctx, "s3") == want_net
    assert set(want_net.values()) == {0.0}
    # tie-out of the written files: debits, credits, net and journals agree, and match what the console recorded
    header = list(got[0])

    def as_rows(table: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
        return [dict(zip(header, r, strict=True)) for r in table[1:]]

    tie = tie_out(as_rows(got), typ, len(got) - 1)
    assert tie == tie_out(as_rows(want), typ, len(want) - 1)
    recorded = next(e for e in AuditLog(Path(j["out"]["path"]).parent / "audit.jsonl").read()
                    if e["action"] == "write_upload")["outputs"]["tie_out"]
    assert recorded == tie and tie["net"] == 0 and tie["debits"] == tie["credits"] > 0


@pytest.mark.parametrize("typ", ["GL Fund", "GL LP"])
def test_parity_gl_dirty(typ: str, tmp_path: Path) -> None:
    """The sample GL file with its planted defects (unmapped account, blank deal, journals off by cents, an unknown
    reference): both paths refuse it, find the same ERRs where both check, and agree on the net of every journal."""
    c, j, src = GL.console_file(tmp_path, typ, lambda _c: S.write_gl(tmp_path / "src" / "gl.xlsx", typ))
    agent, ctx, _result = gl_agent(tmp_path, c, src, typ, golden_picks(typ))
    rev = review(ctx, "s3")

    assert_divergence(typ, console_gl_err(c, j), agent_err(rev))
    assert CE.tally(c, j)["err"] > 0 and rev["err_count"] > 0
    net = console_journal_net(c, j)
    assert agent_journal_net(ctx, "s3") == net  # the same figure for every journal, off ones included
    assert {k for k, v in net.items() if v} == ({"J-1003", "J-1005"} if typ == "GL Fund" else set())
    with pytest.raises(GateRefused):
        approve(agent, ctx, "t1", "looks fine")
    assert not list(ctx.out_dir.glob("GL_Upload*"))
