"""Migration M13: every golden-lock fixture replayed through `evaluate` must equal the lock.

The lock (`tests/fixtures/golden`, taken at M2) holds, per fixture, the result of the console path and of the agent
path. `evaluate` composes the agent path's functions (`staging`, `quality`, `checks`, `review`, `accounts`,
`staging.write_rows`), so it is compared with the lock's agent half: the rows (header, count and digest), the
findings, the ERR findings as (code, severity, row), the MISSING fields, the tally, the reconciliation, the IDs
issued, the tie-out and the upload file's content digest. The golden files are not changed.

Not compared, and why: the audit decision sequence (the engine records its own decisions: `eval.started`,
`stage.done`, `write_upload`; the lock's sequence is the agent tools' and the console's) and the digest of the whole
result that includes it. The lock's console half is compared where it agrees with the agent half (clean GL files
and the five static types, apart from E1: Commitments Fund_ID and Fund_Name) and, since M17, whole: a JobSpec
derived from the console's job (`console_engine.to_spec`) goes through `evaluate`, and what comes out is the console
half of the lock (rows of the file it writes, findings per row, ERR findings, MISSING, tie-out, tally, IDs issued,
the upload's content digest, and for GL the net of every journal) for all fourteen console fixtures. That covers E1
(Fund_ID and Fund_Name through the console's entity table), E2 (the tie-out in the write record), E3 (clash,
duplicate, generated-ID collision, bad ID characters, GL-only investment), E4 (a blank required field is an ERR in
the console's namespace), E5 (ID length on the record's own ID), E6 (the vendor, affiliate, investment and account
crosswalks) and the person's exclusions, fills and ID corrections.

Answer keys are read only by `worker.score`, only through `score()`, as the lock does.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

import expected_rules as X
import test_adapter_parity as AP
import test_gl_agent as GL
import test_golden_lock as G
import test_parity as P
import test_static_clean as F5
from id_decision import decide_ids
from port_fakes import production_ports
from tieout import tie_out
from worker import engine as E
from worker import replay as R
from worker import sample as S
from worker import workbook as wb
from worker.accounts import key_of
from worker.audit import AuditLog
from worker.console import Console
from worker.hashing import sha256_file
from worker.idgen import IdScheme
from worker.money import total
from worker.score import score
from worker.templates import TEMPLATES

FIX = G.FIX
ISSUED = {"Investors": "Investor_ID", "Commitments": "Commitment_ID"}
SPONSOR = "sponsor-a"


# ---- the gate's side: an approval is built here, never by the engine ---------------------------------------
def approval_for(ev: E.Evaluation, by: str = "reviewer-x", acknowledged: bool = True) -> E.Approval:
    warns = tuple(f.id for f in ev.findings if f.severity == "WARN") if acknowledged else ()
    return E.Approval("appr-1", by, ev.manifest.eval_key, ev.findings_digest, warns, "2026-10-11T09:00:00Z")


def locked(name: str) -> dict[str, Any]:
    return json.loads((G.GOLDEN / f"{name}.json").read_text(encoding="utf-8"))


def file_spec(path: Path, *, sheet: str | None = None, header: int | None = 1) -> E.SourceRef:
    return E.SourceRef("file", path.name, sheet, header, sha256_file(path))


def spec_of(record_type: str, ref: E.SourceRef, steps: list[dict[str, Any]], *, choices: tuple[E.Choice, ...] = (),
            context: dict[str, str] | None = None, job: str = "job-1") -> E.JobSpec:
    return E.JobSpec(job, SPONSOR, record_type, None, (ref,), E.PinSet(), E.Recipe.of(*steps), choices,
                     context or {"gp_id": SPONSOR})


# ---- the lock's agent half, rebuilt from an Evaluation --------------------------------------------------------
def engine_facts(ev: E.Evaluation, out: Path | None, template: str) -> dict[str, Any]:
    """Same shapes as `test_golden_lock.agent_facts`, from what `evaluate` returned and wrote."""
    if out is not None:
        if template == "GL":
            book = GL.read_file(out)
            header = [str(h) for h in book[0]]
            rows = [dict(zip(header, r, strict=True)) for r in book[1:]]
        else:
            header, rows = F5.read_file(out)
        tie = tie_out(rows, template, len(rows))
    else:
        rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in ev.staged(limit=10**9)]
        header = list(rows[0]) if rows else []
        tie = {}
    staged = [r for r in ev.staged(limit=10**9)]
    detail = [dict(f.detail) for f in ev.findings]
    findings = sorted(({k: v for k, v in f.items() if k not in ("offer", "engine")} for f in detail),
                      key=lambda f: (f["id"], f["severity"]))
    errs = [f for f in ev.findings if f.severity == "ERR"]
    warns = [f for f in ev.findings if f.severity == "WARN"]
    by_journal: dict[str, list[Any]] = {}
    for r in staged:
        by_journal.setdefault(r.get("Journal_ID", ""), []).append(r.get("Total_Amount"))
    facts: dict[str, Any] = {
        "rows": G._rows_facts(header, rows),
        "findings": findings,
        "err_findings": sorted([c, s, n] for c, s, n in P.agent_err({"findings": detail})),
        "missing": [{"field": m.field, "reason": "MISSING"}
                    for m in sorted(ev.missing, key=lambda m: m.field) if m.reason != "blank_in_source"],
        "tie_out": tie,
        "tally": {"err": len(errs), "warn": len(warns), "rows_read": ev.tally["read"],
                  "rows_out": ev.tie_out.rows_out if ev.tie_out else 0,
                  "can_approve": not errs and not warns},
        "reconciliation": dict(ev.reconciliation or {}),
        "ids_issued": [str(r[ISSUED[template]]) for r in rows] if template in ISSUED and out is not None else [],
        "upload": G._upload(out),
    }
    if template == "GL":
        facts["journal_net"] = {k: float(total(v)) for k, v in by_journal.items()}
    return facts


def assert_equals_lock(name: str, mine: dict[str, Any], keys: tuple[str, ...] | None = None) -> None:
    want = locked(name)["agent"]
    got = G._canon(mine)
    for k in keys or tuple(mine):
        assert k in want, f"{name}: the lock has no {k}"
        if got[k] != want[k]:
            raise AssertionError(f"{name}/{k}: {G._first_difference(want[k], got[k])}")


COMMON = ("rows", "findings", "err_findings", "missing", "tie_out", "tally", "reconciliation", "ids_issued", "upload")


# ---- static types ---------------------------------------------------------------------------------------------
def _static_steps(typ: str, *, clean: bool) -> list[dict[str, Any]]:
    lift = {"step": "lift", "source": 0, "columns": F5.LIFTS[typ], "header_row": 1, "first_row": 2,
            "constants": {"GP_ID": SPONSOR}}
    steps = [lift]
    if clean and typ == "Investors":
        steps.append({"step": "assign_ids", "id_field": "Investor_ID", "source_key_field": "Source_Investor_Key"})
    if clean and typ == "Commitments":
        steps += [{"step": "resolve_references", "field": "Investor_ID", "record_type": "Investors"},
                  {"step": "assign_ids", "id_field": "Commitment_ID"}]
    return steps


def _set_scheme(state: Path, typ: str) -> None:
    """What the human's `set_id_scheme` decision did before the agent assigned IDs: the console's prefix and width."""
    IdScheme(state / "ids", SPONSOR, typ, TEMPLATES[typ].id_generation).change(
        {"mode": "sequential", "prefix": X.pre(typ), "width": X.pad(typ), "start": 1, "pattern": ""},
        None, by="reviewer-a", reason="the sponsor's scheme, as the console issues IDs", record=lambda *_a, **_k: None)


@pytest.mark.parametrize("typ", G.STATIC)
def test_engine_equals_lock_static_clean(typ: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for t, keep in F5.CLEAN_ROWS.items():
        monkeypatch.setitem(S.CFG[t], "rows", [r for r in S.CFG[t]["rows"] if r.i in keep])
    c = Console(tmp_path / "inbox")
    c.act("prov|paid")
    if typ == "Commitments":
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
    c.act("approve", who="reviewer-a")
    src = Path(j["up"]["path"])
    if typ in ISSUED:
        _set_scheme(state, typ)
    out = tmp_path / "run" / "out"
    ports = production_ports(SPONSOR, inbox_roots=[c.inbox], out_dir=out, state=state, audit=AuditLog(out / "audit.jsonl"))
    spec = spec_of(typ, file_spec(src, sheet=j["up"]["sheet"]), _static_steps(typ, clean=True))

    first = E.evaluate(spec, ports)
    assert first.outcome == "complete" and not [f for f in first.findings if f.severity == "ERR"]
    assert not list(out.glob("*_Upload*.xlsx"))  # nothing is written before a person approves
    ev = E.evaluate(spec, ports, approval=approval_for(first), stop_after="write")
    assert ev.produced is not None and ev.reached == "write"
    written = out / f"{typ}_Upload.xlsx"
    assert ev.produced.path == written and ev.produced.sha256 == sha256_file(written)
    facts = engine_facts(first, written, typ)  # the findings as the person saw them, before approving
    assert_equals_lock(f"static_clean_{typ.lower()}", facts, COMMON)
    # where the console agrees with the agent (everything but E1), the engine equals the console's rows too
    con = locked(f"static_clean_{typ.lower()}")["console"]["rows"]
    if typ != "Commitments":
        assert G._canon(facts["rows"]) == con
    assert ev.tie_out is not None and ev.tie_out.rows_out == len(F5.CLEAN_ROWS[typ]) and ev.tie_out.ties
    assert AuditLog(out / "audit.jsonl").verify()[0]


@pytest.mark.parametrize("typ", G.STATIC)
def test_engine_equals_lock_static_dirty(typ: str, tmp_path: Path) -> None:
    c = Console(tmp_path / "inbox")
    c.act("prov|paid")
    j = F5.console_job(c, typ)
    src = Path(j["up"]["path"])
    out = tmp_path / "run" / "out"
    ports = production_ports(SPONSOR, inbox_roots=[c.inbox], out_dir=out, state=tmp_path / "state",
                             audit=AuditLog(out / "audit.jsonl"))
    spec = spec_of(typ, file_spec(src, sheet=j["up"]["sheet"]), _static_steps(typ, clean=False))
    ev = E.evaluate(spec, ports)
    refused = bool(locked(f"static_dirty_{typ.lower()}")["agent"]["refused"])
    appr = approval_for(ev)
    if refused:
        assert ev.outcome == "needs_human"
        with pytest.raises(E.Refused) as err:
            E.evaluate(spec, ports, approval=appr, stop_after="write")
        assert err.value.code == "open_err"
        assert not list(out.glob("*_Upload*.xlsx"))
        facts = engine_facts(ev, None, typ)
    else:  # E7: the WARN is acknowledged, then the file is written as it is
        written = E.evaluate(spec, ports, approval=appr, stop_after="write")
        assert written.produced is not None
        facts = engine_facts(ev, out / f"{typ}_Upload.xlsx", typ)
    facts["refused"] = refused
    assert_equals_lock(f"static_dirty_{typ.lower()}", facts, (*COMMON, "refused"))


# ---- GL Fund and GL LP ----------------------------------------------------------------------------------------
def _gl_engine(tmp: Path, c: Console, src: Path, typ: str, picks: dict[str, str]) -> tuple[E.JobSpec, Any, Path]:
    inbox = tmp / "agent_inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / src.name).write_bytes(src.read_bytes())
    out = tmp / "run" / "out"
    lp = typ == "GL LP"
    cols = GL.LP_COLUMNS if lp else GL.COLUMNS
    steps = [{"step": "lift_gl_lines", "source": 0, "fund_id": c.entity_id("Fund A"), "fund_name": "Fund A",
              "offsets": lp},
             {"step": "map_accounts"},
             {"step": "check_data_quality", "source": 0, "record_type": "GL", "columns": cols}]
    choices = tuple(E.Choice("map_account", f"key:{k}", a, "job", "reviewer-x", "these are the accounts")
                    for k, a in picks.items())
    ports = production_ports(SPONSOR, inbox_roots=[inbox], out_dir=out, state=c.root,
                             audit=AuditLog(out / "audit.jsonl"))
    spec = spec_of("GL", file_spec(inbox / src.name, header=None), steps, choices=choices)
    return spec, ports, out


def _gl_lines(tmp: Path, typ: str, clean: bool) -> tuple[Console, dict[str, Any], Path, dict[str, str]]:
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
    return c, j, src, picks


@pytest.mark.slow
@pytest.mark.parametrize("typ", ["GL Fund", "GL LP"])
def test_engine_equals_lock_gl_clean(typ: str, tmp_path: Path) -> None:
    c, _j, src, picks = _gl_lines(tmp_path, typ, True)
    spec, ports, out = _gl_engine(tmp_path, c, src, typ, picks)
    first = E.evaluate(spec, ports)
    assert first.outcome == "complete", [f.message for f in first.findings]
    ev = E.evaluate(spec, ports, approval=approval_for(first), stop_after="write")
    written = out / "GL_Upload.xlsx"
    assert ev.produced is not None and ev.produced.path == written
    facts = engine_facts(first, written, "GL")
    assert_equals_lock(f"gl_clean_{typ.split()[1].lower()}", facts, (*COMMON, "journal_net"))
    # the console and the engine write the same rows (the lock's console half agrees with its agent half here)
    assert G._canon(facts["rows"]) == locked(f"gl_clean_{typ.split()[1].lower()}")["console"]["rows"]
    tie = tie_out(_gl_book_rows(written), typ, ev.produced.rows)
    assert ev.tie_out is not None and float(ev.tie_out.debits or 0) == tie["debits"]
    assert float(ev.tie_out.credits or 0) == tie["credits"] and float(ev.tie_out.net or 0) == tie["net"]
    assert ev.tie_out.journals == tie["journals"]


def _gl_book_rows(path: Path) -> list[dict[str, Any]]:
    book = GL.read_file(path)
    header = [str(h) for h in book[0]]
    return [dict(zip(header, r, strict=True)) for r in book[1:]]


@pytest.mark.slow
@pytest.mark.parametrize("typ", ["GL Fund", "GL LP"])
def test_engine_equals_lock_gl_dirty(typ: str, tmp_path: Path) -> None:
    c, _j, src, picks = _gl_lines(tmp_path, typ, False)
    spec, ports, out = _gl_engine(tmp_path, c, src, typ, picks)
    ev = E.evaluate(spec, ports)
    assert ev.outcome == "needs_human"
    with pytest.raises(E.Refused) as err:
        E.evaluate(spec, ports, approval=approval_for(ev), stop_after="write")
    assert err.value.code == "open_err" and not list(out.glob("GL_Upload*"))
    facts = engine_facts(ev, None, "GL")
    facts["refused"] = True
    assert_equals_lock(f"gl_dirty_{typ.split()[1].lower()}", facts, (*COMMON, "journal_net", "refused"))
    assert {k for k, v in facts["journal_net"].items() if v} == ({"J-1003", "J-1005"} if typ == "GL Fund" else set())


# ---- score.py over a real engine output -----------------------------------------------------------------------
@pytest.mark.parametrize("name", ["set7", "set8"])
def test_engine_equals_lock_score_results(name: str, tmp_path: Path) -> None:
    src_dir = FIX / name
    sheet = wb.list_sheets(src_dir / "Fund_Roster.xlsx")["sheets"][0]["name"]
    out = tmp_path / "out"
    ports = production_ports(SPONSOR, inbox_roots=[src_dir], out_dir=out, state=tmp_path / "state",
                             audit=AuditLog(out / "audit.jsonl"))
    steps = [{"step": "lift", "source": 0, "columns": {"GP_ID": "A", "Fund_ID": "B"}, "header_row": 1,
              "first_row": 2}]
    spec = spec_of("Commitments", file_spec(src_dir / "Fund_Roster.xlsx", sheet=sheet), steps,
                   context={"gp_id": "GP-001"})
    first = E.evaluate(spec, ports)
    ev = E.evaluate(spec, ports, approval=approval_for(first), stop_after="write")
    assert ev.produced is not None
    res = score(out, src_dir / "answer_key")  # the only reader of the key
    want = locked(f"score_{name}")
    assert G._canon(G._upload(out / "Commitments_Upload.xlsx")) == want["upload"]
    assert G._canon(res) == want["score"]


# ---- the source files: how the engine opens each one ----------------------------------------------------------
@pytest.mark.parametrize("src", G._sources(), ids=lambda p: f"{p.parent.name}-{p.stem}")
def test_engine_profile_equals_lock_source_file(src: Path, tmp_path: Path) -> None:
    ports = production_ports(SPONSOR, inbox_roots=[src.parent], out_dir=tmp_path / "out", state=tmp_path / "state",
                             audit=AuditLog(tmp_path / "out" / "audit.jsonl"))
    spec = E.JobSpec("job-p", SPONSOR, None, None, (file_spec(src, header=None),), E.PinSet(), None, (), {})
    ev = E.evaluate(spec, ports, stop_after="read")
    assert ev.reached == "read" and ev.outcome == "stopped" and not list((tmp_path / "out").glob("*_Upload*"))
    facts = ev.source_facts[0]
    mine = {"fixture": f"{src.parent.name}/{src.name}", "content_sha256": R.xlsx_content_digest(src),
            "kind": facts["kind"], "sheets": facts["sheets"]}
    name = f"source_{src.parent.name}_{src.stem}".lower()
    want = json.loads((G.GOLDEN / f"{name}.json").read_text(encoding="utf-8"))
    got = G._canon(mine)
    assert got == want, G._first_difference(want, got)
    if ev.profile is not None:
        assert ev.profile.file_fp == sha256_file(src)


# ---- the eleven reconciliations -------------------------------------------------------------------------------
_RECON_HEADER = ["GL Date", "Journal", "Line", "GL Account", "Account Name", "Transaction Type", "Debit", "Credit",
                 "Description", "Investor", "Investment", "PR_DATE", "NO_OF_SHARES", "AGNST_COMMIT"]
_YES = {True: "Yes", False: "No", None: ""}


class _EngineRecon:
    """`worker.recon` with `run_check` answered by `evaluate`: the upload side comes from a GL file read by the
    engine (the rows `recon_scenarios` hands over are written back as that file's lines), the client side from a
    report file. Everything else is the real module."""

    def __init__(self, tmp: Path) -> None:
        import worker.recon as real

        self._real = real
        self._tmp = tmp
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def _files(self, rows: list[Any], report: list[dict[str, Any]] | None) -> dict[str, list[list[Any]]]:
        body: list[list[Any]] = [list(_RECON_HEADER)]
        n = 0
        for r in rows:
            if r.offset:  # an offset is made by the engine from its investor line
                continue
            n += 1
            body.append([r.date.isoformat() if r.date else "", f"J{n}", 1, r.acct, "", r.tt,
                         r.amt if r.amt > 0 else "", -r.amt if r.amt < 0 else "", "x", r.inv, r.invst,
                         r.pr.isoformat() if r.pr else "", "" if r.shares is None else r.shares, _YES[r.agn]])
        files: dict[str, list[list[Any]]] = {"gl.csv": body}
        if report is not None:
            head = ["line", "investor", "date", "tt", "measure", "period", "amount"]
            files["report.csv"] = [head, *([r[h] if r[h] is not None else "" for h in head] for r in report)]
        return files

    def _evaluate(self, check: str, rows: list[Any], report: list[dict[str, Any]] | None, *,
                  incomplete: bool = False, other: dict[str, str] | None = None) -> E.Evaluation:
        from port_fakes import MemoryReader, MemoryWorld
        from worker.ports import ListAudit, Ports, ProductionSkills

        self.calls += 1
        files = self._files(rows, report)
        offsets = any(r.offset for r in rows)
        investors = [{"key": k, "name": v, "source_key": k} for k, v in
                     {"LP-A": "Investor Alpha", "LP-B": "Investor Beta"}.items()]
        # the lines carry Ledgerline account numbers already: `accounts: source` reads them as they are (the
        # console path of the lock does not apply the transaction-type rules to them either)
        refs = [E.SourceRef("file", "gl.csv", None, 1, "")]
        step: dict[str, Any] = {"step": "reconcile", "check": check, "source": 0, "incomplete": incomplete,
                                "accounts": "source"}
        if report is not None:
            refs.append(E.SourceRef("file", "report.csv", None, 1, ""))
            step["report"] = 1
        if other:
            step["other"] = other
        steps = [{"step": "lift_gl_lines", "source": 0, "fund_id": "F-1", "fund_name": "Fund A", "offsets": offsets},
                 step]
        world = MemoryWorld(SPONSOR, records={"Investors": investors})
        ports = Ports(world, MemoryReader(files), ListAudit(), self._store, self._tmp / "out",
                      ProductionSkills(SPONSOR))
        spec = E.JobSpec(f"recon-{self.calls}", SPONSOR, "GL", None, tuple(refs), E.PinSet(), E.Recipe.of(*steps),
                         (), {"gp_id": SPONSOR})
        return E.evaluate(spec, ports)

    _store: Any = None

    def run_check(self, spec: Any, rows: list[Any], ctx: Any, coa: Any, report: Any, resolve: Any, aliases: Any,
                  fund_rows: list[Any] | None = None, lp_rows: list[Any] | None = None,
                  incomplete: bool = False) -> dict[str, Any]:
        other: dict[str, str] = {}
        for name, held in (("fund", fund_rows), ("lp", lp_rows)):
            if held is not None:
                other[name] = self._evaluate("tb", held, None).manifest.eval_key
        ev = self._evaluate(spec["id"], rows, report, incomplete=incomplete, other=other or None)
        assert ev.reconcile is not None, [f.message for f in ev.findings]
        return {k: ev.reconcile[k] for k in ("state", "notes", "rows")}


def test_engine_equals_lock_recon_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import recon_scenarios as RS
    import test_console_recon as TR
    from worker.ports import MemoryStore

    shim = _EngineRecon(tmp_path)
    shim._store = MemoryStore()  # the engine's own store: the pair checks read earlier evaluations from it
    monkeypatch.setattr(RS, "R", shim)
    monkeypatch.setattr(TR, "R", shim)
    got = json.loads(json.dumps(RS.scenarios(), default=str))
    assert shim.calls >= len(got)  # every one of the 47 went through evaluate
    want = locked("recon_checks")
    per = {name: {"state": v["state"], "rows": len(v["rows"]), "notes": len(v["notes"]), "digest": R.digest(v)}
           for name, v in sorted(got.items())}
    facts = G._canon({"scenarios": per, "digest": R.digest(got)})
    assert facts["scenarios"] == want["scenarios"], next(
        (n for n in want["scenarios"] if facts["scenarios"].get(n) != want["scenarios"][n]), "?")
    assert facts == want


# ---- the lock is covered whole --------------------------------------------------------------------------------
def test_every_golden_fixture_is_replayed_by_a_test_here() -> None:
    names = {p.stem for p in G.GOLDEN.glob("*.json")}
    replayed = {f"static_clean_{t.lower()}" for t in G.STATIC} | {f"static_dirty_{t.lower()}" for t in G.STATIC}
    replayed |= {f"gl_{kind}_{t}" for kind in ("clean", "dirty") for t in ("fund", "lp")}
    replayed |= {f"score_{n}" for n in ("set7", "set8")}
    replayed |= {f"source_{p.parent.name}_{p.stem}".lower() for p in G._sources()}
    replayed.add("recon_checks")
    assert names == replayed


# ---- the console half of the lock, through a spec derived from the console's job (M17) ------------------------------
def assert_equals_console_half(name: str, mine: dict[str, Any]) -> None:
    want = locked(name)["console"]
    got = G._canon(mine)
    for k in mine:
        assert k in want, f"{name}: the lock's console half has no {k}"
        if got[k] != want[k]:
            raise AssertionError(f"{name}[console]/{k}: {G._first_difference(want[k], got[k])}")


CONSOLE_STATIC = [(t, k) for k in ("clean", "dirty") for t in G.STATIC]
CONSOLE_GL = [(t, k) for k in ("clean", "dirty") for t in ("GL Fund", "GL LP")]


@pytest.mark.parametrize(("typ", "kind"), CONSOLE_STATIC, ids=lambda v: str(v))
def test_engine_equals_console_half_static(typ: str, kind: str, tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    clean = kind == "clean"
    c, j = AP.static_fixture(tmp_path, typ, clean, monkeypatch)
    facts, first, out = AP.console_half(c, j, tmp_path, write=clean)
    assert_equals_console_half(f"static_{kind}_{typ.lower()}", facts)
    assert set(facts) == {"rows", "findings", "err_findings", "missing", "tie_out", "tally", "ids_issued", "upload"}
    assert all(f.rule.rule_id.startswith("console.") for f in first.findings)  # the console's own namespace
    if clean:
        assert first.outcome == "complete" and out is not None and first.tally["err"] == 0
    else:
        assert first.outcome == "needs_human" and out is None
        refused = tmp_path / "refused"
        with pytest.raises(E.Refused) as err:
            E.evaluate(AP.CE.to_spec(c, j), AP.fresh_ports(c, j, refused), approval=AP.approval_for(first),
                       stop_after="write")
        assert err.value.code == "open_err" and not list(refused.glob("*.xlsx"))
    # the rows the person sees are the rows the engine paged out of its spool
    assert [r.i for r in first.rows(limit=10**9)] == list(range(1, first.tally["read"] + 1))


@pytest.mark.slow
@pytest.mark.parametrize(("typ", "kind"), CONSOLE_GL, ids=lambda v: str(v))
def test_engine_equals_console_half_gl(typ: str, kind: str, tmp_path: Path) -> None:
    clean = kind == "clean"
    c, j = AP.gl_fixture(tmp_path, typ, clean)
    facts, first, out = AP.console_half(c, j, tmp_path, write=clean)
    assert_equals_console_half(f"gl_{kind}_{typ.split()[1].lower()}", facts)
    assert set(facts) == {"rows", "findings", "err_findings", "missing", "tie_out", "tally", "journal_net",
                          "ids_issued", "upload"}
    assert all(f.rule.rule_id.startswith("console.") for f in first.findings)
    if clean:
        assert first.outcome == "complete" and out is not None
        assert first.tally["out"] == len(AP.GL.read_file(out)) - 1  # the lines and their offsets, as counted
    else:
        assert first.outcome == "needs_human" and out is None
        assert {x["j"] for x in first.facts["journals"] if x["diff"]} == (
            {"J-1003", "J-1005"} if typ == "GL Fund" else set())


def test_every_console_fixture_of_the_lock_is_replayed() -> None:
    have = {f"static_{k}_{t.lower()}" for t, k in CONSOLE_STATIC}
    have |= {f"gl_{k}_{t.split()[1].lower()}" for t, k in CONSOLE_GL}
    with_console = {p.stem for p in G.GOLDEN.glob("*.json") if "console" in json.loads(p.read_text("utf-8"))}
    assert have == with_console and len(have) == 14
    assert set(AP.fixture_names()) == have
