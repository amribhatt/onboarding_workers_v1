"""F1: approving a console job writes the real upload workbook through the writer the agent path uses."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import openpyxl
import pytest
from fastapi.testclient import TestClient

from id_decision import decide_ids
from worker import console_engine as CE
from worker import engine_console as EC
from worker import templates as T
from worker.audit import AuditLog
from worker.console import Console, ConsoleError
from worker.web import create_app

TYPES = [("Investors", None), ("Commitments", None), ("Investments", None), ("Vendors", None),
         ("Affiliates", None), ("GL Fund", "Fund A"), ("GL LP", "Fund A")]


@pytest.fixture
def c(tmp_path: Path) -> Console:
    con = Console(tmp_path / "inbox")
    con.act("prov|paid")
    return con


def start(c: Console, sponsor: str, typ: str, fund: str | None = None) -> dict[str, Any]:
    c.act(f"start|{sponsor}|{typ}")
    if typ.startswith("GL"):
        if fund:
            c.act(f"ask_set|fund|{fund}")
    else:
        c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    j = c.job()
    assert j is not None
    if c.ex_pending(j):
        c.act("ex|no")
    c.act("go")
    return j


def read_file(path: str | Path) -> tuple[tuple[Any, ...], list[tuple[Any, ...]]]:
    book = openpyxl.load_workbook(path, read_only=True)
    try:
        rows = list(book.worksheets[0].iter_rows(values_only=True))
    finally:
        book.close()
    width = len(rows[0])  # a read-only sheet drops trailing blank cells
    return rows[0], [(*r, *([None] * (width - len(r)))) for r in rows[1:]]


def approve_all_types(c: Console, upto: int = len(TYPES)) -> dict[str, dict[str, Any]]:
    done = {}
    for typ, fund in TYPES[:upto]:
        j = start(c, "Sponsor A", typ, fund)
        c.act("acceptall")
        want = CE.tally(c, j)
        assert want["err"] == 0
        decide_ids(c)
        c.act("approve", who="reviewer-a")
        assert j["approved"] and j["out"]["rows"] == want["out"]
        done[typ] = j
    return done


def test_F1_nothing_is_written_before_approval_and_only_approved_jobs_download(c: Console) -> None:
    j = start(c, "Sponsor A", "Investors")
    assert not list(c.job_dir(j).glob("*.xlsx"))  # no upload file (the job's audit log exists: every action is recorded)
    with pytest.raises(ConsoleError, match="only an approved job"):
        c.download_path(j["id"])
    c.act("acceptall")
    decide_ids(c)
    c.act("approve", who="reviewer-a")
    assert c.download_path(j["id"]).parent == c.job_dir(j).resolve()
    with pytest.raises(ConsoleError):
        c.download_path("../../etc")


def test_F1_each_of_the_seven_types_writes_the_template_header_and_exactly_the_written_rows(c: Console) -> None:
    done = approve_all_types(c)
    for typ, j in done.items():
        spec = T.spec_of(j["sp"], typ)
        header, rows = read_file(j["out"]["path"])
        assert list(header) == list(spec.fields), typ
        assert len(rows) == j["written"] == j["out"]["rows"], typ
        assert j["out"]["file"] == T.file_name(j)
        ok, n = AuditLog(Path(j["out"]["path"]).with_name("audit.jsonl")).verify()
        assert ok and n >= 1, typ  # the chain verifies
        last = AuditLog(Path(j["out"]["path"]).with_name("audit.jsonl")).read()[-1]
        assert last["action"] == "write_upload" and last["outputs"]["tie_out"]["ties"] is True


def test_F1_static_files_carry_the_stored_ids_and_trace_to_the_source_cells(c: Console) -> None:
    done = approve_all_types(c)
    inv = done["Investors"]
    header, rows = read_file(inv["out"]["path"])
    ix = {h: n for n, h in enumerate(header)}
    ids = [r[ix["Investor_ID"]] for r in rows]
    assert ids == [inv["ids"][k] for k in sorted(inv["ids"], key=int)]  # the IDs stored at approval, in order
    assert len(set(ids)) == len(ids)
    # row 1 of the source (sheet row 2): its name and key come straight from the cells
    _sheet, src_header, src_rows = EC.read_sheet(inv["up"]["path"])
    first = dict(zip(src_header, src_rows[0][1], strict=False))
    assert rows[0][ix["Investor_Name"]] == first["Investor"]
    assert rows[0][ix["Source_Investor_Key"]] == first["Row Labels"]
    left_out = [r["nm"] for r in CE.static_rows(c, inv) if r["st"] == "skip"]  # type: ignore[arg-type]
    names = {r[ix["Investor_Name"]] for r in rows}
    assert left_out and not set(left_out) & names
    # commitments resolve the investor through the shared store
    cm_header, cm_rows = read_file(done["Commitments"]["out"]["path"])
    cx = {h: n for n, h in enumerate(cm_header)}
    assert cm_rows[0][cx["Investor_ID"]] == ids[0] and cm_rows[0][cx["Commitment_ID"]] == "CMT-0001"
    assert cm_rows[0][cx["Commitment_Amount"]] == 2500000 and cm_rows[0][cx["Currency"]] is None  # never invented


def test_F1_gl_lp_file_has_the_lines_and_their_offsets_as_counted(c: Console) -> None:
    approve_all_types(c, upto=6)
    j = start(c, "Sponsor A", "GL LP", "Fund A")
    t = CE.tally(c, j)
    assert (t["read"], t["out"], t["err"]) == (5, 8, 1)  # five lines, three clean + five offsets, one needs you
    assert len(CE.gl_eval(c, j)["offsets"]) == 5
    c.act("acceptall")
    decide_ids(c)
    c.act("approve", who="reviewer-a")
    header, rows = read_file(j["out"]["path"])
    ix = {h: n for n, h in enumerate(header)}
    assert len(rows) == 10 and sum(1 for r in rows if str(r[ix["Event_ID"]]).endswith("-OFF")) == 5
    by_journal: dict[str, float] = {}
    for r in rows:
        by_journal[r[ix["Journal_ID"]]] = by_journal.get(r[ix["Journal_ID"]], 0) + r[ix["Total_Amount"]]
    tie = AuditLog(Path(j["out"]["path"]).with_name("audit.jsonl")).read()[-1]["outputs"]["tie_out"]
    assert tie["rows"] == 10 and tie["net"] == round(sum(by_journal.values()), 2)


def test_F1_an_approved_job_keeps_its_output_facts(c: Console) -> None:
    j = start(c, "Sponsor A", "Investors")
    c.act("acceptall")
    decide_ids(c)
    c.act("approve", who="reviewer-a")
    before = dict(j["out"])
    start(c, "Sponsor A", "Commitments")
    assert j["out"] == before and j["written"] == before["rows"]


def test_F1_when_the_audit_record_cannot_be_written_nothing_is_approved(c: Console) -> None:
    j = start(c, "Sponsor A", "Investors")
    c.act("acceptall")
    folder = c.job_dir(j)
    (folder / "audit.jsonl").unlink(missing_ok=True)
    (folder / "audit.jsonl").mkdir(parents=True)  # the log cannot be opened for appending
    with pytest.raises(ConsoleError, match="Nothing was written|could not be recorded"):  # the action itself, or the write
        decide_ids(c)
        c.act("approve", who="reviewer-a")
    assert not j["approved"] and "ids" not in j and not list(folder.glob("*.xlsx"))
    assert c.S["F"]["Fund A"]["static"] == {} and c.reg_of("Sponsor A")["Investors"] == []


def test_F1_the_download_route_serves_the_file_of_an_approved_job_only(tmp_path: Path) -> None:
    app = create_app(tmp_path / "state", environ={"WORKER_MODEL_PROVIDER": "demo",
                                                  "WORKER_ENV_FILE": str(tmp_path / "t.env")})
    http = TestClient(app)
    http.post("/api/console/act", json={"action": "start|Sponsor A|Investors"})
    for a in ("ask_set|cov|all", "ask_sample", "ask_go"):
        assert http.post("/api/console/act", json={"action": a}).status_code == 200
    st = http.post("/api/console/act", json={"action": "ex|no"}).json()
    jid = st["job"]["id"]
    http.post("/api/console/act", json={"action": "go"})
    assert http.get(f"/api/console/jobs/{jid}/download").status_code == 409
    http.post("/api/console/act", json={"action": "acceptall"})
    decide = http.post("/api/console/idgen", headers={"X-Reviewer": "reviewer-1"}, json={
        "sponsor": "Sponsor A", "record_type": "Investors", "reason": "the sponsor's standard numbering",
        "scheme": {"mode": "sequential", "prefix": "GINV-", "width": 4}, "job": jid})
    assert decide.status_code == 200, decide.text
    done = http.post("/api/console/act", json={"action": "approve"}).json()["job"]
    assert done["download"] == f"/api/console/jobs/{jid}/download" and done["file"]["rows"] == 14
    r = http.get(done["download"])
    assert r.status_code == 200 and "Investors_Upload_Template_Sponsor_A.xlsx" in r.headers["content-disposition"]
    assert r.content[:2] == b"PK"
    assert http.get("/api/console/jobs/nope/download").status_code == 409
