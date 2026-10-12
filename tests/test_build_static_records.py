"""M12: the static-record rules read from `skills/build-static-records` and the deal-required table.

Old equals new: every value the Python constants held is what the files hold. The deal-required ranges are one-way:
a change may add accounts, and a change that covers fewer accounts is refused.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import expected_rules as X
from worker import engine_console as EC
from worker import rulefiles
from worker.audit import AuditLog
from worker.console import Console, ConsoleError
from worker.decide import TableError
from worker.skills import load_skills

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "skills" / "build-static-records"


def test_sem_equals_synonyms_file() -> None:
    got = rulefiles.load_file(SKILL / "synonyms.yaml", repo_root=REPO)
    assert got.kind == "synonyms" and got.data["scope"] == "target"
    flat = {k: list(v) for block in got.data["types"].values() for k, v in block.items()}
    assert flat == X.SEM  # same fields, same words, same order
    assert EC.synonyms() == X.SEM
    assert EC.detect_header("Investor_ID", "", ["LP ID", "Name"]) == "LP ID"
    assert EC.detect_header("Country", "", ["Domicile"]) == "Domicile"
    assert EC.detect_header("Country", "", ["Nothing"]) == ""


def test_legal_forms_equal_python() -> None:
    assert EC.legal_forms() == frozenset(X.LEGAL_FORMS)
    assert EC.Words().name_key("Acme Holdings, Inc.") == EC.Words().name_key("acme holdings") == "acme holdings"


def test_deal_required_equals_python() -> None:
    accounts = [str(n) for n in range(10000)]
    accounts += ["1500-01", "  1650 Prepaid", "9999.5", "15", "", "abc", "x1500", "15001", "86400", "80500-1"]
    table = EC.deal_required_table()
    assert [r["id"] for r in table.rows][-1] == "not-required"
    for a in accounts:
        assert EC.deal_required(a) == X.deal_required(a), a
    # the whole range of four-digit numbers, straight from the table
    assert sorted(EC._accounts_required(table)) == [n for n in range(10000) if X.deal_required(str(n))]


def _sponsor_tables(tmp_path: Path, rows: str) -> Path:
    root = tmp_path / "sponsor-a"
    place = root / "skills" / "map-accounts" / "tables"
    place.mkdir(parents=True)
    (place / "deal-required.yaml").write_text(
        "schema_version: 1\nkind: table\nid: deal-required\nrows:\n" + rows, encoding="utf-8")
    return root


def test_overlay_narrowing_deal_required_refused(tmp_path: Path) -> None:
    base = EC._accounts_required(EC.deal_required_table())

    # adding a range is allowed and covers more accounts
    added = _sponsor_tables(tmp_path / "add", '  - {id: r7000-7099, when: {at_least: ">=7000", at_most: "<=7099"}, '
                                              "then: {required: true}}\n")
    wider = EC._accounts_required(EC.deal_required_table(added))
    assert wider == base | set(range(7000, 7100))

    # narrowing a range by replacing its row is refused, naming the accounts
    narrowed = _sponsor_tables(tmp_path / "narrow", '  - {id: r1500-1599, when: {at_least: ">=1550", '
                                                    'at_most: "<=1599"}, then: {required: true}}\n')
    with pytest.raises(EC.OneWayRefused, match="1500"):
        EC.deal_required_table(narrowed)

    # taking an account out with a row that says not required is refused by the table loader
    taken = _sponsor_tables(tmp_path / "take", '  - {id: skip-1520, when: {at_least: "==1520", at_most: "==1520"}, '
                                               "then: {required: false}}\n")
    with pytest.raises((EC.OneWayRefused, TableError), match="unseen|control|not required|1520"):
        EC.deal_required_table(taken)

    # a promoted row (overlay_rows) is held to the same rule
    with pytest.raises(EC.OneWayRefused):
        EC.deal_required_table(overlay_rows=[{"id": "r9000-9999", "when": {"at_least": ">=9500", "at_most": "<=9999"},
                                             "then": {"required": True}}])
    # and the controls floor, when it is supplied, may not be undercut either
    with pytest.raises(EC.OneWayRefused, match="nnnn-nnnn"):
        EC.deal_required_table(floor=["15-16"])


def test_console_job_start_refused_on_invalid_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c = Console(tmp_path / "inbox")
    c.act("prov|paid")
    c.act("start|Sponsor A|Investors")
    c.act("ask_set|cov|all")
    c.act("ask_sample")
    jobs_before = len(c.S["jobs"])

    root = tmp_path / "skills"
    shutil.copytree(REPO / "skills", root)
    table = root / "map-accounts" / "tables" / "deal-required.yaml"
    table.write_text(table.read_text(encoding="utf-8").replace("kind: table\n", "kind: table\nbogus: 1\n", 1),
                     encoding="utf-8")
    monkeypatch.setenv("WORKER_SKILLS_DIR", str(root))
    with pytest.raises(rulefiles.SchemaError):
        load_skills()

    with pytest.raises(ConsoleError, match="did not start"):
        c.act("ask_go")
    assert len(c.S["jobs"]) == jobs_before and c.job() is None  # no job was made
    assert c.S["ask"] is not None  # the person's ask is kept
    log = AuditLog(c.out_dir / "audit.jsonl").read()
    assert [x["action"] for x in log] == ["skill.load_refused"]
    assert log[0]["outputs"] == {"started": False}
    assert any("deal-required.yaml" in line for line in log[0]["inputs"]["problems"])

    monkeypatch.delenv("WORKER_SKILLS_DIR")  # with the files valid again the same ask starts
    c.act("ask_go")
    assert c.job() is not None
