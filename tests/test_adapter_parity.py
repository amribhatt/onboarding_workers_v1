"""Migration M17: the console adapter of the one engine (ticket 03 D9), console half.

The console decides no row any more. `console_engine.to_spec(c, j)` copies a console job into a `JobSpec`, the
engine evaluates it, and the console pages the result. This file proves the adapter:

* `console_to_spec` equals the recorded spec of every golden-lock fixture (`tests/fixtures/adapter_parity`); an
  intended change to the translation is re-recorded with `UPDATE_ADAPTER_PARITY=1` and read as a diff;
* the spec's `eval_key` changes with each decision the person makes and with nothing else;
* approval builds an `Approval` and calls `evaluate(stop_after="write")`: the engine writes the file, the job's own
  audit record keeps its shape, and the manifest is on the job and in the engine's `write_upload` record;
* no `console*.py` imports the sample world, apart from the demo seed;
* the console's views take their record-type facts from the target files.

The agent half (`agent_to_spec` equals `console_to_spec`, equal `eval_key`) is added by M18 after this merges.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from typing import Any

import pytest

import expected_rules as X
import test_gl_agent as GL
import test_golden_lock as G
import test_parity as P
import test_static_clean as F5
from id_decision import decide_ids
from tieout import tie_out
from worker import console_engine as CE
from worker import engine as E
from worker import engine_console as EC
from worker import record_facts as RF
from worker import sample as S
from worker import templates as T
from worker.audit import AuditLog
from worker.console import Console, ConsoleError
from worker.context import Budget
from worker.ports import FileReader, ListAudit, MemoryStore, Ports, ProductionSkills

SRC = Path(__file__).resolve().parents[1] / "src" / "worker"
RECORDED = Path(__file__).parent / "fixtures" / "adapter_parity"
STATIC = P.STATIC
GL_TYPES = ["GL Fund", "GL LP"]


# ---- the fixtures of the golden lock, the console side ---------------------------------------------------------
def static_fixture(tmp: Path, typ: str, clean: bool, monkeypatch: pytest.MonkeyPatch) -> tuple[Console, dict[str, Any]]:
    """The console job of `test_golden_lock._static_clean` / `_static_dirty`, without the agent."""
    if clean:
        for t, keep in F5.CLEAN_ROWS.items():
            monkeypatch.setitem(S.CFG[t], "rows", [r for r in S.CFG[t]["rows"] if r.i in keep])
    c = Console(tmp / "inbox")
    c.act("prov|paid")
    if clean and typ == "Commitments":
        F5.console_job(c, "Investors")
        c.act("acceptall")
        decide_ids(c)
        c.act("approve", who="reviewer-a")
    j = F5.console_job(c, typ)
    if clean:
        c.act("acceptall")
        decide_ids(c)
        c.act("approve", who="reviewer-a")
    return c, j


def gl_fixture(tmp: Path, typ: str, clean: bool) -> tuple[Console, dict[str, Any]]:
    """The console job of `test_golden_lock._gl_run`, without the agent."""
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

    c, j, _src = GL.console_file(tmp, typ, build)
    if clean:
        c.act("acceptall")
        decide_ids(c)
        c.act("approve", who="reviewer-a")
    return c, j


def fixture_names() -> list[str]:
    return ([f"static_{k}_{t.lower()}" for k in ("clean", "dirty") for t in STATIC]
            + [f"gl_{k}_{t.split()[1].lower()}" for k in ("clean", "dirty") for t in GL_TYPES])


def build_fixture(name: str, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Console, dict[str, Any], bool]:
    kind, clean_s, rest = name.split("_", 2)
    clean = clean_s == "clean"
    if kind == "static":
        typ = next(t for t in STATIC if t.lower() == rest)
        c, j = static_fixture(tmp, typ, clean, monkeypatch)
    else:
        c, j = gl_fixture(tmp, f"GL {rest.capitalize() if rest == 'fund' else rest.upper()}", clean)
    return c, j, clean


# ---- the console half of the lock, rebuilt from an evaluation -----------------------------------------------------
def approval_for(ev: E.Evaluation, by: str = "analyst") -> E.Approval:
    warns = tuple(f.id for f in ev.findings if f.severity == "WARN")
    return E.Approval("appr-test", by, ev.manifest.eval_key, ev.findings_digest, warns, "2026-10-11T09:00:00Z")


def fresh_ports(c: Console, j: dict[str, Any], out: Path, audit: Any | None = None) -> Ports:
    """Ports of the production kind for a console job, with nothing shared with the console's own evaluations."""
    path = Path(j["up"]["path"])
    return Ports(world=CE.ConsoleWorld(c, j["sp"]), reader=FileReader([path.parent]), audit=audit or ListAudit(),
                 store=MemoryStore(), out_dir=out, skills=ProductionSkills(T.sponsor_slug(j["sp"])))


def err_codes(detail: dict[str, Any]) -> list[str]:
    errs = detail["errs"]
    return list(detail["codes"][: len(errs)])


def console_half(c: Console, j: dict[str, Any], tmp: Path, *, write: bool) -> tuple[dict[str, Any], E.Evaluation,
                                                                                    Path | None]:
    """`evaluate` over the console-derived spec, shaped as the lock's console half: rows of the file it writes (when
    approved), findings, ERR findings, MISSING, tie-out, tally, IDs issued, the upload's content digest."""
    typ = j["type"]
    out_dir = tmp / "engine_out"
    ports = fresh_ports(c, j, out_dir)
    spec = CE.to_spec(c, j)
    first = E.evaluate(spec, ports)
    out: Path | None = None
    if write:
        done = E.evaluate(spec, ports, approval=approval_for(first), stop_after="write")
        assert done.produced is not None
        out = done.produced.path
    outcomes = list(first.rows(limit=10**9))
    if typ in ("GL Fund", "GL LP"):
        lines = [o.detail for o in outcomes if o.detail and not o.detail.get("off")]
        header: list[str] = []
        frows: list[dict[str, Any]] = []
        if out is not None:
            book = GL.read_file(out)
            header = [str(h) for h in book[0]]
            frows = [dict(zip(header, r, strict=True)) for r in book[1:]]
        err = sorted([code, "ERR", i + 2] for i, d in enumerate(lines) for code in err_codes(d))
        facts: dict[str, Any] = {
            "rows": G._rows_facts(header, frows),
            "findings": [{"row": i + 2, "message": e["m"]} for i, d in enumerate(lines) for e in d["errs"]],
            "err_findings": err, "missing": [],
            "tie_out": tie_out(frows, typ, len(frows)) if out is not None else {},
            "tally": dict(first.tally),
            "journal_net": {x["j"]: x["diff"] for x in first.facts["journals"]},
            "ids_issued": [], "upload": G._upload(out),
        }
        return facts, first, out
    rows = [dict(o.detail or {}) for o in outcomes]
    header, frows = F5.read_file(out) if out is not None else ([], [])
    approved = bool(j.get("approved"))
    ids = first.facts.get("ids") or {}
    facts = {
        "rows": G._rows_facts(header, frows),
        "findings": sorted(({"row": r["row"], "status": r["st"], "why": r["why"], "flags": list(r["fl"])}
                            for r in rows if r["st"] != "ok"), key=lambda f: (f["row"], f["status"], f["why"])),
        "err_findings": sorted([code, "ERR", r["row"]] for r in rows if r["st"] == "err" for code in r["codes"]),
        "missing": G._missing_from_flags([(r["row"], r["flags"]) for r in rows]),
        "tie_out": tie_out(frows, typ, len(frows)) if out is not None else {},
        "tally": dict(first.tally),
        "ids_issued": [str(v) for _k, v in sorted((int(k), v) for k, v in ids.items())] if approved else [],
        "upload": G._upload(out),
    }
    return facts, first, out


# ---- the recorded specs -------------------------------------------------------------------------------------------
def spec_record(spec: E.JobSpec) -> dict[str, Any]:
    """The spec as data, with the machine's paths made relative: what the recorded file holds."""
    return json.loads(E.canonical({
        "job_id": spec.job_id, "sponsor": spec.sponsor, "record_type": spec.record_type, "entity": spec.entity,
        "sources": [{"kind": s.kind, "ref": Path(s.ref).name, "sheet": s.sheet, "header_row": s.header_row,
                     "fingerprint": s.fingerprint} for s in spec.sources],
        "recipe": [{**b, **({"id_scheme": {**b["id_scheme"], "year": "the job's date"}} if b.get("id_scheme") else {})}
                   for b in (spec.recipe.bindings if spec.recipe else ())],  # the year is the day the test ran
        "choices": [{"kind": x.kind, "subject": x.subject, "value": x.value, "scope": x.scope, "by": x.by,
                     "reason": x.reason} for x in spec.choices],
        "context": dict(spec.context)}))


@pytest.mark.parametrize("name", fixture_names())
def test_console_to_spec_equals_the_recorded_spec(name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                  request: pytest.FixtureRequest) -> None:
    if name.startswith("gl_"):
        request.applymarker(pytest.mark.slow)
    c, j, _clean = build_fixture(name, tmp_path, monkeypatch)
    got = spec_record(CE.to_spec(c, j))
    path = RECORDED / f"console_{name}.json"
    if os.environ.get("UPDATE_ADAPTER_PARITY", "") not in ("", "0"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(got, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        return
    assert path.is_file(), f"no recorded spec for {name}: run with UPDATE_ADAPTER_PARITY=1 and read the diff"
    want = json.loads(path.read_text(encoding="utf-8"))
    assert got == want, G._first_difference(want, got)


# ---- the translation ----------------------------------------------------------------------------------------------
@pytest.fixture
def con(tmp_path: Path) -> Console:
    c = Console(tmp_path / "inbox")
    c.act("prov|paid")
    return c


def test_spec_eval_key_is_stable_and_follows_every_decision(con: Console) -> None:
    j = F5.console_job(con, "Investors")
    base = CE.to_spec(con, j).eval_key()
    assert CE.to_spec(con, j).eval_key() == base  # the same job, the same key
    keys = [base]
    for do in (lambda: con.decide(j, "6", "exclude as duplicate of INV-0007"), lambda: con.fill(j, "15", "Country", "US"),
               lambda: con.act("col|Country|Domicile", who="reviewer-a")):
        do()
        keys.append(CE.to_spec(con, j).eval_key())
    assert len(set(keys)) == len(keys)  # one new key per decision
    j["dec"].pop("6")
    undone = CE.to_spec(con, j).eval_key()
    assert undone not in keys  # undoing one decision of three is not the start again
    j["fill"].pop("15|Country")
    con.act("col|Country|", who="reviewer-a")  # the column choice is kept as a (blank) choice: the key still differs
    assert CE.to_spec(con, j).eval_key() not in (base,)


def test_spec_carries_the_choices_as_closed_kinds(con: Console) -> None:
    j = F5.console_job(con, "Commitments")
    con.decide(j, "6", "corrected ID to HS-9977")
    con.decide(j, "7", "exclude as incorrect (blank)")
    con.fill(j, "7", "Commitment_Date", "2025-06-30")
    con.set_date(j, "7")
    spec = CE.to_spec(con, j)
    kinds = {(x.kind, x.subject): x.value for x in spec.choices}
    assert kinds[("correct_id", "row:6")] == "HS-9977"
    assert kinds[("exclude", "row:7")].startswith("exclude")
    assert kinds[("supply", "row:7|Commitment_Date")] == "2025-06-30" and ("supply", "date:7") in kinds
    assert {x.kind for x in spec.choices} <= set(E.CHOICE_KINDS)
    assert all(x.by == "analyst" and x.scope == "job" and x.reason for x in spec.choices)
    assert spec.record_type == "Commitments" and spec.sponsor == "sponsor-a" and spec.job_id == j["id"]


def test_the_console_stores_the_eval_key_and_the_sheet(con: Console) -> None:
    j = F5.console_job(con, "Investors")
    assert j["eval_key"] and len(j["eval_key"]) == 64
    assert j["up"]["sheet"]
    before = j["eval_key"]
    con.decide(j, "6", "exclude as incorrect (x)")
    CE.tally(con, j)
    assert j["eval_key"] != before


def test_views_page_the_evaluation_rows(con: Console, monkeypatch: pytest.MonkeyPatch) -> None:
    j = F5.console_job(con, "Investors")
    pages: list[tuple[int, int]] = []
    real = E.Evaluation.rows

    def spy(self: E.Evaluation, status: str | None = None, offset: int = 0, limit: int = 200, **kw: Any) -> Any:
        pages.append((offset, limit))
        return real(self, status, offset, limit, **kw)

    monkeypatch.setattr(E.Evaluation, "rows", spy)
    con._engine_held = {}  # nothing kept: the rows are asked for afresh
    rows = CE.static_rows(con, j)
    assert rows and pages  # the screen's rows came from the spool through `Evaluation.rows`
    assert CE.static_rows(con, j) is rows  # and asked for once while nothing changes
    assert [r["i"] for r in rows] == list(range(1, len(rows) + 1))


# ---- approval: the engine writes the file ----------------------------------------------------------------------------
def test_approve_builds_an_approval_and_calls_evaluate_to_write(con: Console, monkeypatch: pytest.MonkeyPatch) -> None:
    j = F5.console_job(con, "Investors")
    con.act("acceptall")
    calls: list[dict[str, Any]] = []
    real = E.evaluate

    def spy(spec: E.JobSpec, ports: Ports, **kw: Any) -> E.Evaluation:
        calls.append({"stop_after": kw.get("stop_after", "check"), "approval": kw.get("approval")})
        return real(spec, ports, **kw)

    monkeypatch.setattr(E, "evaluate", spy)
    decide_ids(con)
    con.act("approve", who="reviewer-a")
    writes = [x for x in calls if x["stop_after"] == "write"]
    assert len(writes) == 1
    appr = writes[0]["approval"]
    assert isinstance(appr, E.Approval) and appr.by == "reviewer-a" and appr.eval_key
    assert j["approved"] and j["out"]["rows"] == j["written"]
    # the file is where the console says and named as it has always been
    assert Path(j["out"]["path"]).is_file() and j["out"]["file"] == T.file_name(j)
    assert not list(Path(j["out"]["path"]).parent.glob(".*part*"))  # the .part folder is gone


def test_approve_keeps_the_job_audit_record_and_puts_the_manifest_in_the_engine_record(con: Console) -> None:
    j = F5.console_job(con, "Investors")
    con.act("acceptall")
    decide_ids(con)
    con.act("approve", who="reviewer-a")
    log = AuditLog(Path(j["out"]["path"]).with_name("audit.jsonl")).read()
    uploads = [e for e in log if e["action"] == "write_upload"]
    assert len(uploads) == 1  # the one upload record; the log also holds the person's actions (user.action ...)
    rec = uploads[0]
    # its shape, and since M19 the manifest on every write (naming the replay record)
    assert set(rec["outputs"]) == {"file", "tie_out", "ids_issued", "decisions", "manifest"}
    assert rec["outputs"]["tie_out"]["ties"] and rec["outputs"]["manifest"]["replay_record"]["record_sha256"]
    assert rec["inputs"] == {"job": j["id"], "sponsor": "Sponsor A", "record_type": "Investors",
                             "template": "Investors"}
    m = j["manifest"]
    assert len(m["eval_key"]) == 64  # the evaluation the file was written from
    assert {"eval_key", "pins", "controls", "schema_versions", "engine", "models", "sources", "recipe_checksum",
            "choices_checksum", "spool_checksum"} <= set(m)
    assert m["engine"] == E.ENGINE_VERSION and m["pins"]["skills"] and m["controls"] and m["schema_versions"]
    engine_log = [e for e in AuditLog(con.out_dir / "engine.audit.jsonl").read() if e["action"] == "write_upload"]
    assert len(engine_log) == 1
    in_record = engine_log[0]["outputs"]["manifest"]
    # the record is written before the file is, so it names the stages sealed so far; the job's has the write too
    assert {k: v for k, v in in_record.items() if k != "stage_keys"} == {k: v for k, v in m.items() if k != "stage_keys"}
    assert set(in_record["stage_keys"]) <= set(m["stage_keys"]) and "write" in m["stage_keys"]
    assert engine_log[0]["inputs"]["approved_by"] == "reviewer-a" and engine_log[0]["inputs"]["job"] == j["id"]
    assert AuditLog(con.out_dir / "engine.audit.jsonl").verify()[0]


@pytest.mark.slow
def test_every_approved_job_has_its_manifest(con: Console) -> None:
    from test_console_upload import TYPES, start

    for typ, fund in TYPES:
        j = start(con, "Sponsor A", typ, fund)
        con.act("acceptall")
        decide_ids(con)
        con.act("approve", who="reviewer-a")
        assert j["approved"] and j["manifest"]["pins"]["skills"], typ
        assert j["manifest"]["recipe_checksum"] and j["manifest"]["eval_key"], typ


def test_nothing_is_written_when_the_job_audit_cannot_be_written(con: Console) -> None:
    j = F5.console_job(con, "Investors")
    con.act("acceptall")
    folder = con.job_dir(j)
    (folder / "audit.jsonl").unlink(missing_ok=True)
    (folder / "audit.jsonl").mkdir(parents=True)
    with pytest.raises(ConsoleError, match="didn't write the file|couldn't record"):
        decide_ids(con)
        con.act("approve", who="reviewer-a")
    assert not j["approved"] and "ids" not in j and "manifest" not in j
    assert not list(folder.glob("*.xlsx")) and not list(folder.glob(".*"))  # no file and no .part left


def test_a_stale_approval_is_refused_by_the_engine(con: Console) -> None:
    j = F5.console_job(con, "Investors")
    con.act("acceptall")
    held = CE._entry(con, j)
    ev = held.ev
    appr = E.Approval("a-1", "analyst", ev.manifest.eval_key, "not-the-findings-the-person-saw", (), "t")
    with pytest.raises(E.Refused) as err:
        E.evaluate(held.spec, CE._ports(con, j, held.store, CE.ConsoleAudit(CE._engine_log(con))), approval=appr,
                   stop_after="write")
    assert err.value.code == "stale_approval"
    assert not Path(con.job_dir(j)).exists() or not list(con.job_dir(j).glob("*.xlsx"))


def test_the_agent_cannot_approve_a_console_job(con: Console) -> None:
    j = F5.console_job(con, "Investors")
    held = CE._entry(con, j)
    appr = E.Approval("a-2", "writer", held.ev.manifest.eval_key, held.ev.findings_digest, (), "t")
    with pytest.raises(E.Refused) as err:
        E.evaluate(held.spec, CE._ports(con, j, held.store, CE.ConsoleAudit(CE._engine_log(con))), approval=appr,
                   stop_after="write")
    assert err.value.code == "agent_approver"


# ---- what the console reads --------------------------------------------------------------------------------------
def _imports_sample(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "worker.sample" or (mod == "worker" and any(a.name == "sample" for a in node.names)):
                return True
        elif isinstance(node, ast.Import) and any(a.name == "worker.sample" for a in node.names):
            return True
    return False


def test_no_console_module_imports_the_sample_world_except_the_demo_seed() -> None:
    files = sorted(SRC.glob("console*.py"))
    assert len(files) >= 8
    offenders = [p.name for p in files if p.name != "console_demo.py" and _imports_sample(p)]
    assert offenders == []
    assert _imports_sample(SRC / "console_demo.py")  # the one place that does
    # and the demo seed is imported only for what is demo: the seed, the sample files and the mockup's plan data
    users = [p.name for p in SRC.glob("console*.py") if "console_demo" in p.read_text(encoding="utf-8")]
    assert "console.py" in users


def test_record_type_facts_come_from_the_target_files() -> None:
    assert RF.types() == X.TYPES and RF.static_types() == tuple(X.STATIC)
    assert RF.gen_types() == tuple(X.GEN) and RF.id_when_blank() == X.IDGEN
    assert RF.unique_types() == tuple(X.UNIQ) and RF.stored_types() == tuple(X.REGT)
    for t in X.TYPES:
        assert RF.next_of(t) == X.NEXT.get(t) and RF.is_gl(t) == (t in ("GL Fund", "GL LP"))
    for t, (tpl, limit, pre, pad) in X.FORMATS.items():
        assert (RF.id_prefix(t), RF.id_pad(t), RF.id_limit(t)) == (pre, pad, limit)
        assert RF.template_stem(t) == tpl and RF.id_fmt(t, 7) == X.id_fmt(t, 7)
    assert RF.template_stem("GL Fund") == X.GL_TEMPLATE == RF.template_stem("GL LP")


def test_default_rules_are_the_ones_a_console_job_starts_with() -> None:
    assert EC.default_rules() == X.DEFAULT_RULES


# ---- the checks are in the engine and in their own namespace --------------------------------------------------------
def test_the_console_only_checks_are_findings_of_the_console_namespace(con: Console) -> None:
    """E3, E4, E5 and E6 of the parity exceptions: raised by `evaluate`, as `console.<code>`, never lowered."""
    seen: dict[str, str] = {}
    for typ in ("Investors", "Commitments", "Investments", "Vendors", "Affiliates"):
        con.act(f"start|Sponsor A|{typ}")
        con.act("ask_set|cov|all")
        con.act("ask_sample")
        con.act("ask_go")
        j = con.job()
        assert j is not None
        if con.ex_pending(j):
            con.act("ex|file")  # the Ledgerline export is read: what is already on file can clash
        j["rules"]["suffix"] = True  # the sponsor's rule: Ltd and Limited are one word
        con.act("go")
        for f in CE.evaluation(con, j).findings:
            assert f.rule.rule_id.startswith("console."), f.rule.rule_id
            seen[str(f.detail["code"])] = f.severity
    assert {"key_clash", "name_clash", "suffix_clash", "similar_in_file", "missing_dependency", "bad_id_chars",
            "generated_id_collision", "gl_only_investment", "required_blank", "id_too_long"} <= set(seen)
    assert seen["required_blank"] == "ERR"  # E4: the console namespace; the agent path's scale says WARN


def test_a_severity_is_raised_to_the_skills_floor_and_never_lowered() -> None:
    floors = {"severity_floor": {"required_blank": "ERR", "name_too_long": "WARN"}}
    assert EC.console_level("required_blank", floors) == "ERR"
    assert EC.console_level("name_too_long", floors) == "WARN"
    assert EC.console_level("name_too_long", {"severity_floor": {"name_too_long": "ERR"}}) == "ERR"  # raised
    assert EC.console_level("key_clash", floors) == "ERR"  # the console's own code, no skill rule
    assert EC.console_level("fuzzy_match", {}) == "WARN"
    # a check-data-quality rule's own level may raise a console cause, never lower it
    assert EC.console_level("id_too_long", {}, {"id_too_long": "WARN"}) == "ERR"


# ---- web jobs get the spend cap ---------------------------------------------------------------------------------
def test_a_web_job_gets_the_spend_cap_from_the_environment(tmp_path: Path) -> None:
    from worker.service import RunManager, ServiceError

    capped = RunManager(tmp_path / "a", lambda: None, budget_env={"WORKER_BUDGET_TOKENS": "1000",  # type: ignore[arg-type,return-value]
                                                                  "WORKER_BUDGET_CALLS": "7"})
    try:
        ctx = capped._context("sponsor-a", "GP-001", tmp_path / "out1")
        again = capped._context("sponsor-a", "GP-001", tmp_path / "out2")
        assert (ctx.budget.max_tokens, ctx.budget.max_calls) == (1000, 7)
        assert ctx.budget is not again.budget  # each job counts its own calls
    finally:
        capped.close()
    free = RunManager(tmp_path / "b", lambda: None, budget_env={})  # type: ignore[arg-type,return-value]
    try:
        got = free._context("sponsor-a", "GP-001", tmp_path / "out3").budget  # unset = the default cap
        assert (got.max_tokens, got.max_calls) == (Budget.DEFAULT_TOKENS, Budget.DEFAULT_CALLS)
    finally:
        free.close()
    bad = RunManager(tmp_path / "c", lambda: None, budget_env={"WORKER_BUDGET_CALLS": "many"})  # type: ignore[arg-type,return-value]
    try:
        with pytest.raises(ServiceError, match="WORKER_BUDGET_CALLS"):
            bad._context("sponsor-a", "GP-001", tmp_path / "out4")
    finally:
        bad.close()
