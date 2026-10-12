"""Migration M2: the golden lock.

For every fixture the engine is run as it behaves TODAY and the canonical result is compared with a stored golden:
the rows (count and digest), the findings, the MISSING fields, the tie-out, the tally, the IDs issued, the upload's
content digest, the audit decision sequence and the `score.py` results. Later migration steps must keep every golden
byte for byte, or record the intended change in `tests/fixtures/golden/CHANGES.md` and regenerate with
`--update-golden` (or `UPDATE_GOLDEN=1`):

    UPDATE_GOLDEN=1 uv run pytest tests/test_golden_lock.py -q
    PYTHONPATH=tests uv run pytest -p test_golden_lock --update-golden tests/test_golden_lock.py -q

The fixtures are the ones of `tests/test_parity.py` (the five static types and GL Fund / GL LP, each clean and
dirty, through the console path and the agent path), the sources under `tests/fixtures/{acceptance,messy,set7,
set8}`, and the eleven reconciliations over `tests/fixtures/recon_parity.json`. Answer keys are read only by
`worker.score` and only through its `score()` call; this module never opens one.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import test_gl_agent as GL
import test_parity as P
import test_static_clean as F5
from id_decision import decide_ids
from recon_scenarios import scenarios
from scripted_model import ScriptedModel, approve, call, say
from tieout import tie_out
from worker import console_engine as CE
from worker import replay as R
from worker import sample as S
from worker import workbook as wb
from worker.accounts import key_of
from worker.agent import GateRefused, build_agent, decide, pending_gate, start
from worker.audit import AuditLog
from worker.console import Console
from worker.context import RunContext
from worker.review import review
from worker.score import score
from worker.templates import build_coverage

FIX = Path(__file__).parent / "fixtures"
GOLDEN = FIX / "golden"
STATIC = P.STATIC
SLOW = pytest.mark.slow


def pytest_addoption(parser: Any) -> None:  # honoured when this module is loaded as a plugin (-p test_golden_lock)
    parser.addoption("--update-golden", action="store_true", default=False,
                     help="regenerate tests/fixtures/golden from the current behaviour")


def _updating(request: pytest.FixtureRequest) -> bool:
    try:
        flag = bool(request.config.getoption("--update-golden"))
    except ValueError:
        flag = False
    return flag or os.environ.get("UPDATE_GOLDEN", "") not in ("", "0")


# ---- canonical facts -------------------------------------------------------------------------------------------
def _scrub(obj: Any, *roots: Path) -> Any:
    """Make every path in a result relative to the temp folders of this run."""
    text = json.dumps(obj, default=str)
    for root in roots:
        for form in {str(root), str(root).replace("\\", "/"), str(root).replace("\\", "\\\\")}:
            text = text.replace(form, "<tmp>")
    return json.loads(text)


def _rows_facts(header: list[str], rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"header": header, "count": len(rows), "digest": R.digest(rows)}


def _decisions(entries: list[dict[str, Any]], *roots: Path) -> dict[str, Any]:
    # The trace (model.call spans, run.stopped) and the identity of a run (trace id, seconds, token counts) are
    # bookkeeping of one invocation, not decisions: the same volatile keys replay ignores (replay.VOLATILE).
    # `chars` is the length of the reply text, which carries the absolute output path: it moves with the temp folder.
    spans = {"model.call", "tool.span", "run.stopped", "model.truncated"}
    drop = {"trace_id", "seconds", "tokens_in", "tokens_out", "chars"}
    seq = [{k: {n: v for n, v in (e.get(k) or {}).items() if n not in drop} if k in ("inputs", "outputs") and e.get(k)
            else e.get(k) for k in ("action", "rationale", "inputs", "outputs")}
           for e in entries if e["action"] not in spans]
    seq = _scrub(seq, *roots)
    return {"count": len(seq), "actions": [d["action"] for d in seq], "digest": R.digest(seq)}


def _upload(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    f = R.output_facts(path)
    return {"name": f["name"], "format": f["format"], "content_sha256": f["content_sha256"]}


def _result(facts: dict[str, Any]) -> dict[str, Any]:
    """Add the digest that stands for the whole result (the replay record's `result_sha256`)."""
    shaped = {"output": ({"format": "xlsx", "content_sha256": facts["upload"]["content_sha256"]}
                         if facts["upload"] else {}),
              "findings": facts["findings"],
              "tie_out": facts["tie_out"], "missing": facts["missing"], "tally": facts["tally"],
              "ids_issued": facts["ids_issued"], "decisions": facts["decisions"]["digest"]}
    return {**facts, "result_sha256": R.result_digest(shaped)}


def _canon(facts: Any) -> Any:
    return json.loads(R.canonical(facts))


def _lock(request: pytest.FixtureRequest, name: str, facts: Any) -> None:
    got = _canon(facts)
    path = GOLDEN / f"{name}.json"
    if _updating(request):
        GOLDEN.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(got, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        return
    assert path.is_file(), f"no golden for {name}: run with --update-golden and record it in CHANGES.md"
    want = json.loads(path.read_text(encoding="utf-8"))
    if got != want:
        raise AssertionError(f"{name}: {_first_difference(want, got)}")


def _first_difference(want: Any, got: Any, where: str = "") -> str:
    if isinstance(want, dict) and isinstance(got, dict):
        for k in sorted(set(want) | set(got)):
            if want.get(k) != got.get(k):
                return _first_difference(want.get(k), got.get(k), f"{where}/{k}")
    if isinstance(want, list) and isinstance(got, list):
        for i, (a, b) in enumerate(zip(want, got, strict=False)):
            if a != b:
                return _first_difference(a, b, f"{where}[{i}]")
        return f"{where}: {len(want)} item(s) locked, {len(got)} now"
    return f"{where or '/'}: locked {str(want)[:120]!r}, now {str(got)[:120]!r}"


# ---- the console path ------------------------------------------------------------------------------------------
def _missing_from_flags(flag_rows: list[tuple[int, list[str]]]) -> list[dict[str, Any]]:
    by: dict[str, list[int]] = {}
    for row, flags in flag_rows:
        for f in flags:
            m = re.match(r"(.+?) MISSING", f)
            if m:
                by.setdefault(m.group(1), []).append(row)
    return [{"field": k, "reason": "MISSING", "rows": v} for k, v in sorted(by.items())]


def console_static_facts(c: Console, j: dict[str, Any], out: Path | None, *roots: Path) -> dict[str, Any]:
    typ = j["type"]
    ev = CE.static_rows(c, j)
    findings = sorted(
        ({"row": r["row"], "status": r["st"], "why": r["why"], "flags": [f for f in r["fl"]]}
         for r in ev if r["st"] != "ok"), key=lambda f: (f["row"], f["status"], f["why"]))
    header: list[str] = []
    rows: list[dict[str, Any]] = []
    if out is not None:
        header, rows = F5.read_file(out)
    tie = tie_out(rows, typ, len(rows)) if out is not None else {}
    audit = Path(j["out"]["path"]).parent / "audit.jsonl" if out is not None else None
    entries = AuditLog(audit).read() if audit is not None else []
    return {
        "rows": _rows_facts(header, rows),
        "findings": findings,
        "err_findings": sorted([c_, s, n] for c_, s, n in P.console_static_err(c, j)),
        "missing": _missing_from_flags([(r["row"], r["flags"]) for r in ev]),
        "tie_out": tie,
        "tally": CE.tally(c, j),
        "ids_issued": [str(v) for _k, v in sorted((int(k), v) for k, v in (j.get("ids") or {}).items())],
        "upload": _upload(out),
        "decisions": _decisions(entries, *roots),
    }


def console_gl_facts(c: Console, j: dict[str, Any], out: Path | None, *roots: Path) -> dict[str, Any]:
    ev = CE.gl_eval(c, j)
    findings = [{"row": i + 2, "message": e["m"]} for i, x in enumerate(ev["lines"]) for e in x["errs"]]
    header: list[str] = []
    rows: list[dict[str, Any]] = []
    tie: dict[str, Any] = {}
    if out is not None:
        book = GL.read_file(out)
        header = [str(h) for h in book[0]]
        rows = [dict(zip(header, r, strict=True)) for r in book[1:]]
        tie = tie_out(rows, j["type"], len(rows))
    entries = AuditLog(Path(j["out"]["path"]).parent / "audit.jsonl").read() if out is not None else []
    return {
        "rows": _rows_facts(header, rows),
        "findings": findings,
        "err_findings": sorted([c_, s, n] for c_, s, n in P.console_gl_err(c, j)),
        "missing": [],
        "tie_out": tie,
        "tally": CE.tally(c, j),
        "journal_net": P.console_journal_net(c, j),
        "ids_issued": [],
        "upload": _upload(out),
        "decisions": _decisions(entries, *roots),
    }


# ---- the agent path --------------------------------------------------------------------------------------------
def _missing_agent(ctx: RunContext, sid: str, assumed: dict[str, str]) -> list[dict[str, Any]]:
    meta = ctx.stage.meta(sid)
    rows = ctx.stage.rows(sid)
    staged = {f: [r.get(f, "") for r in rows] for f in (rows[0] if rows else {}) if not f.startswith("_")}
    cov = build_coverage(meta["template"], staged, dict(meta.get("sources", {})), assumed, {}, ctx.stage.templates)
    return [{"field": f, "reason": "MISSING"} for f in sorted(cov.missing())]


def agent_facts(ctx: RunContext, sid: str, out: Path | None, rev: dict[str, Any], assumed: dict[str, str],
                *roots: Path) -> dict[str, Any]:
    header: list[str] = []
    rows: list[dict[str, Any]] = []
    tie: dict[str, Any] = {}
    template = rev["template"]
    if out is not None:
        if template == "GL":
            book = GL.read_file(out)
            header = [str(h) for h in book[0]]
            rows = [dict(zip(header, r, strict=True)) for r in book[1:]]
        else:
            header, rows = F5.read_file(out)
        tie = tie_out(rows, template if template != "GL" else "GL", len(rows))
    else:
        rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in ctx.stage.rows(sid)]
        header = list(rows[0]) if rows else []
    issued = {"Investors": "Investor_ID", "Commitments": "Commitment_ID"}.get(template)
    findings = sorted(({k: v for k, v in f.items() if k != "offer"} for f in rev["findings"]),
                      key=lambda f: (f["id"], f["severity"]))
    return {
        "rows": _rows_facts(header, rows),
        "findings": findings,
        "err_findings": sorted([c_, s, n] for c_, s, n in P.agent_err(rev)),
        "missing": _missing_agent(ctx, sid, assumed),
        "tie_out": tie,
        "tally": {"err": rev["err_count"], "warn": rev["warn_count"], "rows_read": rev["rows_read"],
                  "rows_out": rev["rows_out"], "can_approve": rev["can_approve"]},
        "reconciliation": rev["reconciliation"],
        "ids_issued": [str(r[issued]) for r in rows] if issued and out is not None else [],
        "upload": _upload(out),
        "decisions": _decisions(ctx.audit.read(), *roots),
    }


# ---- static types ----------------------------------------------------------------------------------------------
def _static_clean(tmp: Path, typ: str) -> dict[str, Any]:
    import shutil

    c = Console(tmp / "inbox")
    c.act("prov|paid")
    if typ == "Commitments":
        F5.console_job(c, "Investors")
        c.act("acceptall")
        decide_ids(c)
        c.act("approve", who="reviewer-a")
    state = tmp / "agent_state"
    if (c.root / "registry").is_dir():
        shutil.copytree(c.root / "registry", state / "registry")
    j = F5.console_job(c, typ)
    if (c.root / "ids").is_dir():  # the sponsor's ID decision (a named person's, made in the console) is the agent's
        shutil.copytree(c.root / "ids", state / "ids", dirs_exist_ok=True)
    c.act("acceptall")
    decide_ids(c)
    c.act("approve", who="reviewer-a")
    roots = (tmp,)
    con = console_static_facts(c, j, Path(j["out"]["path"]), *roots)

    ctx = P.agent_context(tmp, c.inbox, state)
    script = F5.agent_script(typ, Path(j["up"]["path"]).name, j["up"]["sheet"])
    agent = build_agent(ctx, ScriptedModel(script=script), InMemorySaver())
    result = start(agent, ctx, f"build me the {typ} sheet", "t1")
    if typ in ("Investors", "Commitments"):
        result = decide(agent, ctx, "t1", {"type": "approve"}, "the sponsor numbers these like the console", {"reviewer": "reviewer-a"})
    gate = pending_gate(result)[0]
    sid = gate["args"]["stage_id"]
    rev = review(ctx, sid)
    approve(agent, ctx, "t1", "checked against the source")
    age = agent_facts(ctx, sid, ctx.out_dir / f"{typ}_Upload.xlsx", rev, {"GP_ID": "constant"}, *roots)
    return {"console": _result(con), "agent": _result(age)}


def _static_dirty(tmp: Path, typ: str) -> dict[str, Any]:
    c = Console(tmp / "inbox")
    c.act("prov|paid")
    j = F5.console_job(c, typ)
    con = console_static_facts(c, j, None, tmp)
    out = tmp / "run" / "out"
    from worker.registry import Registry
    from worker.staging import Stage

    ctx = RunContext(sponsor="sponsor-a", gp_id="sponsor-a", inbox_roots=[c.inbox], out_dir=out,
                     audit=AuditLog(out / "audit.jsonl"), stage=Stage(out),
                     registry=Registry(tmp / "state", "sponsor-a"))
    lift = call("lift", path=Path(j["up"]["path"]).name, sheet=j["up"]["sheet"], template=typ,
                columns=F5.LIFTS[typ], header_row=1, first_row=2, constants={"GP_ID": "sponsor-a"},
                why="copy the columns")
    script = [say("lift", lift), say("write", call("write_upload", stage_id="s1", template=typ, why="deliver")),
              say("done")]
    agent = build_agent(ctx, ScriptedModel(script=script), InMemorySaver())
    start(agent, ctx, f"build me the {typ} sheet", "t1")
    rev = review(ctx, "s1")
    refused = False
    try:
        approve(agent, ctx, "t1", "looks fine")
    except GateRefused:
        refused = True
    written = ctx.out_dir / f"{typ}_Upload.xlsx"
    age = agent_facts(ctx, "s1", written if written.is_file() else None, rev, {"GP_ID": "constant"}, tmp)
    age["refused"] = refused
    return {"console": _result(con), "agent": _result(age)}


@pytest.mark.parametrize("typ", STATIC)
def test_golden_static_clean(typ: str, request: pytest.FixtureRequest, tmp_path: Path,
                             monkeypatch: pytest.MonkeyPatch) -> None:
    for t, keep in F5.CLEAN_ROWS.items():
        monkeypatch.setitem(S.CFG[t], "rows", [r for r in S.CFG[t]["rows"] if r.i in keep])
    _lock(request, f"static_clean_{typ.lower()}", _static_clean(tmp_path, typ))


@pytest.mark.parametrize("typ", STATIC)
def test_golden_static_dirty(typ: str, request: pytest.FixtureRequest, tmp_path: Path) -> None:
    _lock(request, f"static_dirty_{typ.lower()}", _static_dirty(tmp_path, typ))


# ---- GL Fund and GL LP -----------------------------------------------------------------------------------------
def _gl_run(tmp: Path, typ: str, clean: bool) -> dict[str, Any]:
    lines: list[Any] = []

    def build(c: Console) -> Path:
        if not clean:
            return S.write_gl(tmp / "src" / "gl.xlsx", typ)
        if typ == "GL Fund":
            lines.extend(GL.CLEAN_FUND)
        else:
            two = [e["source_key"] or e["key"] for e in c.registry("Sponsor A").entries("Investors")[:2]]
            lines.extend([("J-4001", 1, "3000", "Capital call", 0, 300000, two[0]),
                          ("J-4001", 2, "3000", "Capital call", 0, 150000, two[1]),
                          ("J-4002", 1, "3100", "Distribution", 150000, 0, two[1])])
        return GL.write_clean(tmp / "src" / "gl.xlsx", lines)

    c, j, src = GL.console_file(tmp, typ, build)
    if clean:
        c.act("acceptall")
        decide_ids(c)
        c.act("approve", who="reviewer-a")
        picks = {key_of("", acct, tt): c.known_account("Sponsor A", acct, tt) for _j, _l, acct, tt, *_r in lines}
    else:
        picks = P.golden_picks(typ)
    out = Path(j["out"]["path"]) if clean else None
    con = console_gl_facts(c, j, out, tmp)
    agent, ctx, _r = P.gl_agent(tmp, c, src, typ, picks)
    rev = review(ctx, "s3")
    refused = False
    if clean:
        approve(agent, ctx, "t1", "checked against the source")
    else:
        try:
            approve(agent, ctx, "t1", "looks fine")
        except GateRefused:
            refused = True
    written = ctx.out_dir / "GL_Upload.xlsx"
    age = agent_facts(ctx, "s3", written if written.is_file() else None, rev, {"GP_ID": "constant"}, tmp)
    age["journal_net"] = P.agent_journal_net(ctx, "s3")
    age["refused"] = refused
    return {"console": _result(con), "agent": _result(age)}


@pytest.mark.parametrize("typ", [pytest.param("GL Fund", marks=SLOW), pytest.param("GL LP", marks=SLOW)])
def test_golden_gl_clean(typ: str, request: pytest.FixtureRequest, tmp_path: Path) -> None:
    _lock(request, f"gl_clean_{typ.split()[1].lower()}", _gl_run(tmp_path, typ, True))


@pytest.mark.parametrize("typ", [pytest.param("GL Fund", marks=SLOW), pytest.param("GL LP", marks=SLOW)])
def test_golden_gl_dirty(typ: str, request: pytest.FixtureRequest, tmp_path: Path) -> None:
    _lock(request, f"gl_dirty_{typ.split()[1].lower()}", _gl_run(tmp_path, typ, False))


# ---- the source files: how the engine opens each one -----------------------------------------------------------
def _sources() -> list[Path]:
    out: list[Path] = []
    for d in ("acceptance", "messy", "set7", "set8"):
        out += sorted(p for p in (FIX / d).glob("*.xlsx") if "answer_key" not in p.parts)
    return out


@pytest.mark.parametrize("src", _sources(), ids=lambda p: f"{p.parent.name}-{p.stem}")
def test_golden_source_file_as_opened(src: Path, request: pytest.FixtureRequest) -> None:
    """What `list_sheets` and `look` show of each fixture, and the typed-cell digest of the fixture itself."""
    info = wb.list_sheets(src)
    sheets: list[dict[str, Any]] = []
    for s in info.get("sheets", []):
        look = wb.look(src, s["name"], 1, 12, 1, 10)
        sheets.append({**s, "look_digest": R.digest(look)})
    facts = {"fixture": f"{src.parent.name}/{src.name}", "content_sha256": R.xlsx_content_digest(src),
             "kind": info.get("kind"), "sheets": sheets}
    _lock(request, f"source_{src.parent.name}_{src.stem}".lower(), facts)


# ---- score.py over a real engine output ------------------------------------------------------------------------
def _commitments_run(tmp: Path, src_dir: Path) -> Path:
    from worker.staging import Stage

    out = tmp / "out"
    ctx = RunContext(sponsor="sponsor-a", gp_id="GP-001", inbox_roots=[src_dir], out_dir=out,
                     audit=AuditLog(out / "audit.jsonl"), stage=Stage(out))
    sheet = wb.list_sheets(src_dir / "Fund_Roster.xlsx")["sheets"][0]["name"]
    lift = call("lift", path="Fund_Roster.xlsx", sheet=sheet, template="Commitments",
                columns={"GP_ID": "A", "Fund_ID": "B"}, header_row=1, first_row=2, why="copy fund ids")
    script = [say("lift", lift),
              say("write", call("write_upload", stage_id="s1", template="Commitments", why="deliver the file")),
              say("done")]
    agent = build_agent(ctx, ScriptedModel(script=script), InMemorySaver())
    start(agent, ctx, "build me the Commitments sheet", "t1")
    approve(agent, ctx, "t1", "looks right")
    return out


@pytest.mark.parametrize("name", ["set7", "set8"])
def test_golden_score_results(name: str, request: pytest.FixtureRequest, tmp_path: Path) -> None:
    out = _commitments_run(tmp_path, FIX / name)
    res = score(out, FIX / name / "answer_key")  # the only reader of the key
    facts = {"upload": _upload(out / "Commitments_Upload.xlsx"), "score": res,
             "decisions": _decisions(AuditLog(out / "audit.jsonl").read(), tmp_path)}
    _lock(request, f"score_{name}", facts)


# ---- the eleven reconciliations --------------------------------------------------------------------------------
def test_golden_recon_checks(request: pytest.FixtureRequest) -> None:
    got = json.loads(json.dumps(scenarios(), default=str))
    old = json.loads((FIX / "recon_parity.json").read_text(encoding="utf-8"))
    assert got == old  # the lock sits on the file written before the rules moved
    per = {name: {"state": v["state"], "rows": len(v["rows"]), "notes": len(v["notes"]),
                  "digest": R.digest(v)} for name, v in sorted(got.items())}
    _lock(request, "recon_checks", {"scenarios": per, "digest": R.digest(got)})


# ---- the lock itself -------------------------------------------------------------------------------------------
def test_changes_ledger_exists_and_every_golden_is_canonical() -> None:
    text = (GOLDEN / "CHANGES.md").read_text(encoding="utf-8")
    assert text.startswith("# Golden lock changes") and "| date |" in text
    for p in GOLDEN.glob("*.json"):
        doc = json.loads(p.read_text(encoding="utf-8"))
        assert doc == _canon(doc), p.name  # stored in canonical form: no volatile keys, numbers as text


# ---- a golden may change only with a CHANGES.md row ------------------------------------------------------------
def goldens_changed_without_a_row(diff: str) -> list[str]:
    """The golden files a unified diff (of `tests/fixtures/golden`) changes whose name no added CHANGES.md line
    mentions. Pure text in, names out: the guard below feeds it `git diff`, the tests feed it synthetic diffs."""
    changed: list[str] = []
    added_rows: list[str] = []
    current = ""
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current = line.split(" b/", 1)[-1]
            if current.endswith(".json") and current.rsplit("/", 1)[-1] != "":
                changed.append(current.rsplit("/", 1)[-1][: -len(".json")])
        elif line.startswith("+") and not line.startswith("+++") and current.endswith("CHANGES.md"):
            added_rows.append(line)
    rows = "\n".join(added_rows)
    return sorted({stem for stem in changed if stem not in rows})


def test_guard_flags_a_golden_changed_without_a_row() -> None:
    diff = ("diff --git a/tests/fixtures/golden/static_dirty_affiliates.json b/tests/fixtures/golden/static_dirty_affiliates.json\n"
            "+++ b/tests/fixtures/golden/static_dirty_affiliates.json\n+  \"x\": 1\n")
    assert goldens_changed_without_a_row(diff) == ["static_dirty_affiliates"]


def test_guard_accepts_a_golden_changed_with_a_row_naming_it() -> None:
    diff = ("diff --git a/tests/fixtures/golden/static_dirty_affiliates.json b/tests/fixtures/golden/static_dirty_affiliates.json\n"
            "+  \"x\": 1\n"
            "diff --git a/tests/fixtures/golden/CHANGES.md b/tests/fixtures/golden/CHANGES.md\n"
            "+++ b/tests/fixtures/golden/CHANGES.md\n"
            "+| 2026-10-11 | static_dirty_affiliates | finding | a | b | why | someone |\n")
    assert goldens_changed_without_a_row(diff) == []


def test_guard_ignores_a_row_added_to_a_different_file() -> None:
    diff = ("diff --git a/tests/fixtures/golden/gl_clean_fund.json b/tests/fixtures/golden/gl_clean_fund.json\n"
            "+  \"x\": 1\n"
            "diff --git a/README.md b/README.md\n+gl_clean_fund\n")
    assert goldens_changed_without_a_row(diff) == ["gl_clean_fund"]


def test_no_golden_changed_without_a_changes_row() -> None:
    """CI form: `GOLDEN_BASE_REF` is the branch point (for example `origin/main`); with none set, the committed
    HEAD is the base, so a developer's uncommitted regeneration without its row fails here before it is committed."""
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git is not available")
    base = os.environ.get("GOLDEN_BASE_REF", "HEAD")
    root = Path(__file__).resolve().parents[1]
    run = subprocess.run(["git", "diff", "--no-color", base, "--", "tests/fixtures/golden"], cwd=root,
                         capture_output=True, text=True, encoding="utf-8", check=False)
    if run.returncode != 0:
        pytest.skip(f"git diff against {base} failed: {run.stderr.strip()[:200]}")
    untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "tests/fixtures/golden"],
                               cwd=root, capture_output=True, text=True, encoding="utf-8", check=False).stdout
    diff = run.stdout + "".join(f"diff --git a/{n} b/{n}\n" for n in untracked.split() if n.endswith(".json"))
    missing = goldens_changed_without_a_row(diff)
    assert not missing, f"golden file(s) changed without a row in tests/fixtures/golden/CHANGES.md: {missing}"
