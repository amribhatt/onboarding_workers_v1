from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from worker.staging import Stage
from worker.templates import TEMPLATES, effective_settings, templates_for


def _xlsx(path: Path, header: list[str], rows: list[list[str]] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    book = openpyxl.Workbook()
    ws = book.active
    ws.append(header)
    for r in rows or []:
        ws.append(r)
    book.save(path)
    return path


@pytest.fixture
def overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "templates"
    own = list(TEMPLATES["Investors"].fields) + ["Tax_Residency"]
    _xlsx(base / "sponsors" / "sponsor-a" / "Investors_Upload_Template.xlsx", own)
    (base / "sponsors" / "sponsor-b").mkdir(parents=True)
    _xlsx(base / "sponsors" / "sponsor-b" / "Vendors_Upload_Template.xlsx", ["Vendor_ID", "Vendor_Name", "Terms"])
    monkeypatch.setenv("WORKER_TEMPLATES_DIR", str(base))
    return base


def test_overlay_replaces_only_the_named_record_type(overlay: Path) -> None:
    specs = templates_for("sponsor-a")
    assert "Tax_Residency" in specs["Investors"].fields
    for name in TEMPLATES:
        if name != "Investors":
            assert specs[name] == TEMPLATES[name]


def test_sponsor_never_sees_another_sponsors_overlay(overlay: Path) -> None:
    assert templates_for("sponsor-b")["Investors"] == TEMPLATES["Investors"]
    assert "Terms" not in templates_for("sponsor-a")["Vendors"].fields
    assert templates_for("sponsor-b")["Vendors"].fields == ("Vendor_ID", "Vendor_Name", "Terms")


def test_malformed_sponsor_is_refused(overlay: Path) -> None:
    with pytest.raises(ValueError, match="unknown sponsor"):
        templates_for("../sponsor-a")


def test_sponsor_without_overlay_gets_defaults(overlay: Path) -> None:
    assert templates_for("sponsor-z") == dict(TEMPLATES)


def test_job_beats_sponsor_beats_organisation_with_labels(overlay: Path, tmp_path: Path) -> None:
    org = effective_settings("sponsor-z", "Investors")
    assert {v["source"] for v in org.values()} == {"organisation"}

    spon = effective_settings("sponsor-a", "Investors")
    assert spon["fields"]["source"] == "sponsor override"
    assert "Tax_Residency" in spon["fields"]["value"]
    assert spon["output"]["source"] == "organisation"

    job_file = _xlsx(tmp_path / "job.xlsx", ["Investor_ID", "Investor_Name", "Region"])
    job = effective_settings("sponsor-a", "Investors", job_template=job_file)
    assert job["fields"]["source"] == "job override"
    assert job["fields"]["value"] == ("Investor_ID", "Investor_Name", "Region")
    assert job["required"]["value"] == ("Investor_ID", "Investor_Name")
    # the sponsor's other settings are untouched by the job file
    assert job["output"]["source"] == "organisation"
    # finished jobs do not change: the sponsor view is unchanged by a job template
    assert effective_settings("sponsor-a", "Investors")["fields"]["source"] == "sponsor override"


def test_unknown_record_type_is_refused(overlay: Path) -> None:
    with pytest.raises(ValueError, match="unknown record type"):
        effective_settings("sponsor-a", "Nonsense")


# ---- the spine pieces that ticket 13 builds on -------------------------------------------------
def _lifted(tmp_path: Path, rows: list[list[str]]) -> tuple[Stage, str]:
    src = _xlsx(tmp_path / "src.xlsx", ["id", "name"], rows)
    st = Stage(tmp_path / "out")
    sid = st.lift(src, "Sheet", {"Investor_ID": "A", "Investor_Name": "B"}, 1, 2, template="Investors")
    return st, sid


def test_distinct_collapses_repeats_and_reports_conflicts(tmp_path: Path) -> None:
    st, sid = _lifted(tmp_path, [["I1", "Alpha"], ["I2", "Beta"], ["I1", "Alpha"], ["I2", "Gamma"], ["", "Nokey"]])
    out = st.distinct(sid, ["Investor_ID"])
    meta = st.summary(out)["distinct"]
    assert (meta["source_rows"], meta["kept"], meta["collapsed"], meta["blank_key"]) == (5, 2, 2, 1)
    assert meta["conflicts"] == 1
    assert [r["Investor_Name"] for r in st.rows(out)] == ["Alpha", "Beta"]


def test_run_start_audit_records_which_template_won(overlay: Path, tmp_path: Path) -> None:
    import json

    from worker.agent import start
    from worker.audit import AuditLog
    from worker.context import RunContext

    class _Agent:
        def invoke(self, *_a, **_k):
            return {}

    out = tmp_path / "run"
    out.mkdir()
    ctx = RunContext("sponsor-a", "GP-001", [], out, AuditLog(out / "audit.jsonl"), Stage(out, templates_for("sponsor-a")))
    start(_Agent(), ctx, "go", "t1")
    first = json.loads((out / "audit.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert first["outputs"]["template_source"]["Investors"] == "sponsor override"
    assert first["outputs"]["template_source"]["Vendors"] == "organisation"
