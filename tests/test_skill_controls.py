"""Migration M9: controls for map-accounts and check-data-quality, and no code under skills/.

The controls files hold today's values; a learnable value (a rule, a sponsor's levels, the console's Required?) may
only be at least as strict. Fictitious names only.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from worker import checks as K
from worker import console_demo as D
from worker import engine_console as EC
from worker import quality as Q
from worker import rulefiles as rf
from worker.console import Console, ConsoleError
from worker.skills import load_skills, skills_dir

REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / "skills"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A private copy of the repository's skills folder, laid out as `<root>/skills/<skill>`."""
    root = tmp_path / "repo"
    shutil.copytree(SKILLS, root / "skills")
    return root


def _load(tree: Path, name: str, **kw: Any) -> rf.Pin:
    return rf.load_skill(tree / "skills" / name, repo_root=tree, **kw)


def _refused(tree: Path, name: str, **kw: Any) -> rf.SchemaError:
    with pytest.raises(rf.SchemaError) as e:
        _load(tree, name, **kw)
    return e.value


def _rewrite(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")


# ---- the controls equal today's behaviour ------------------------------------------------------------------
def test_controls_files_equal_todays_values() -> None:
    skills = load_skills()
    quality = skills["check-data-quality"].controls()
    rules = {r["id"]: r for r in skills["check-data-quality"].checks()["rules"]}
    assert quality["severity_floor"] == {rid: r["severity"] for rid, r in rules.items()}  # no rule is above its floor
    assert quality["max_ceiling"] == {"id_too_long": 20, "name_too_long": 100}
    assert {k: v["tolerance"] for k, v in quality["balance"].items()} == {"journal_unbalanced": 0}
    mapping = skills["map-accounts"].controls()
    assert mapping["severity_floor"] == {"account_unmapped": "ERR", "account_not_allowed": "ERR",
                                         "account_not_in_chart": "ERR", "transaction_type_unknown": "WARN"}
    assert {rid: K.DEFAULT_LEVELS[rid] for rid in ("account_unmapped", "account_not_allowed",
                                                   "transaction_type_unknown")} == {
        k: v for k, v in mapping["severity_floor"].items() if k != "account_not_in_chart"}


def test_the_deal_required_floor_is_the_ranges_of_the_job_code() -> None:
    floor = load_skills()["check-data-quality"].controls()["ranges_floor"]["deal_required"]
    spans = [tuple(int(x) for x in r.split("-")) for r in floor]
    for n in range(10000):
        assert EC.deal_required(str(n)) == any(a <= n <= b for a, b in spans), n


def test_the_cascade_rows_that_apply_unseen_are_control_rows() -> None:
    pin = load_skills()["map-accounts"].pin
    table = next(f for f in pin.files.values() if f.kind == "table")
    assert [r["id"] for r in table.controls["control_rows"]] == ["L0", "L1"]  # controller only (ticket 01 D3)
    assert table.controls["unseen"] == {"action": ["apply"]}


def test_a_sponsor_table_cannot_change_a_cascade_control_row(tmp_path: Path) -> None:
    skill = load_skills()["map-accounts"]
    root = tmp_path / "sponsor-a"
    place = root / "skills" / "map-accounts" / "tables"
    place.mkdir(parents=True)
    row = {"id": "L1", "when": {"pinned": True, "tt_known": True}, "then": {"tier": "L1 Transaction type",
                                                                           "action": "apply", "confidence": 0.5}}
    (place / "coa-cascade.yaml").write_text(
        yaml.safe_dump({"schema_version": 1, "kind": "table", "id": "coa-cascade", "rows": [row]}), encoding="utf-8")
    with pytest.raises(Exception, match="control row"):
        skill.table("coa-cascade", root)  # the sponsor's root finds the per-skill place (M8 hand-over)


# ---- a learnable value may not be looser than its control ---------------------------------------------------
def test_overlay_cannot_lower_severity(tree: Path, tmp_path: Path) -> None:
    # the organisation's own learnable file, edited below its floor
    checks = tree / "skills/check-data-quality/checks.yaml"
    _rewrite(checks, "    fields: \"@ids\"\n    max: 20\n    severity: ERR", "    fields: \"@ids\"\n    max: 20\n    severity: WARN")
    err = _refused(tree, "check-data-quality")
    assert any(p.code == "looser_than_control" and "id_too_long" in p.message for p in err.problems), err.lines
    shutil.copy(SKILLS / "check-data-quality/checks.yaml", checks)
    _rewrite(checks, "    max: 20\n", "    max: 25\n")  # a longer ID than the ceiling
    err = _refused(tree, "check-data-quality")
    assert any(p.code == "looser_than_control" and p.pointer.endswith("max") for p in err.problems), err.lines
    shutil.copy(SKILLS / "check-data-quality/checks.yaml", checks)
    _rewrite(checks, '"1600-1699", ', "")  # a deal-required range dropped
    err = _refused(tree, "check-data-quality")
    assert any(p.code == "looser_than_control" and "deal_required" in p.message for p in err.problems), err.lines

    # a sponsor's overlay of the same file: raising is allowed, lowering is refused
    shutil.copy(SKILLS / "check-data-quality/checks.yaml", checks)
    org = yaml.safe_load(checks.read_text(encoding="utf-8"))
    rule = next(r for r in org["rules"] if r["id"] == "id_too_long")
    overlay = tree / "templates/sponsors/sponsor-a/skills/check-data-quality"
    overlay.mkdir(parents=True)
    (overlay / "checks.yaml").write_text(yaml.safe_dump(
        {"schema_version": 1, "kind": "checks", "rules": [{**rule, "severity": "WARN"}]}), encoding="utf-8")
    err = _refused(tree, "check-data-quality", overlay_dir=overlay)
    assert any(p.code == "looser_than_control" and p.pointer == "rules[0].severity" for p in err.problems), err.lines
    name_rule = next(r for r in org["rules"] if r["id"] == "name_too_long")
    (overlay / "checks.yaml").write_text(yaml.safe_dump(
        {"schema_version": 1, "kind": "checks", "rules": [{**name_rule, "severity": "ERR"}]}), encoding="utf-8")
    assert _load(tree, "check-data-quality", overlay_dir=overlay)  # a raise loads

    # and at run time a sponsor's levels.json (or a template's levels) lowers nothing below the floor
    rules = {r.id: r for r in Q.load_rules()}
    assert rules["id_too_long"].severity == "ERR" and rules["name_too_long"].severity == "WARN"
    assert K.finding("account_unmapped", "m", {"account_unmapped": "WARN"})["severity"] == "ERR"
    assert K.finding("account_not_allowed", "m", {"account_not_allowed": "WARN"})["severity"] == "ERR"
    assert K.finding("transaction_type_unknown", "m", {"transaction_type_unknown": "ERR"})["severity"] == "ERR"
    assert K.finding("transaction_type_unknown", "m", None)["severity"] == "WARN"
    assert K.finding("gp_id_differs", "m", {"gp_id_differs": "ERR"})["severity"] == "ERR"  # a raise is allowed


def test_a_sponsor_level_cannot_lower_a_data_quality_cause(tmp_path: Path) -> None:
    src = tmp_path / "ids.csv"
    src.write_text("Investor_ID,Investor_Name\n" + "X" * 25 + ",Alpha\n", encoding="utf-8")
    rules = Q.load_rules()
    kw: dict[str, Any] = {"columns": {"Investor_ID": "Investor_ID", "Investor_Name": "Investor_Name"},
                          "record_type": "Investors",
                          "groups": {"required": [], "ids": ["Investor_ID"], "names": ["Investor_Name"],
                                     "dates": [], "amounts": [], "key": []}}
    lowered = Q.scan(src, rules, levels={"id_too_long": "WARN"}, **kw)
    plain = Q.scan(src, rules, **kw)
    sev = {c["rule"]: c["severity"] for c in lowered["causes"]}
    assert sev["id_too_long"] == "ERR" == {c["rule"]: c["severity"] for c in plain["causes"]}["id_too_long"]


# ---- Required? may only raise strictness --------------------------------------------------------------------
@pytest.fixture
def console(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Console:
    monkeypatch.setenv("WORKER_ADMINS", "casey-controller,drew-controller")
    return Console(tmp_path / "inbox")


def _open_job(c: Console) -> None:
    c.act("start|Sponsor A|Investors")
    c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    assert c.job() is not None


def _default_required(record_type: str) -> str:
    return next(f[0] for f in D.plan(record_type) if f[2])


def _default_optional(record_type: str) -> str:
    return next(f[0] for f in D.plan(record_type) if not f[2])


def test_required_untick_needs_controller(console: Console) -> None:
    c = console
    _open_job(c)
    rtype = "Investors"
    field = _default_required(rtype)
    optional = _default_optional(rtype)
    row = next(f for f in D.plan(rtype) if f[0] == field)
    assert c.req_of(rtype, row) is True

    # an analyst can tick a field that is optional by default, and take their own tick back
    c.set_required(None, rtype, optional, True)
    assert c.world().required(rtype, optional, False) is True
    c.set_required(None, rtype, optional, False)
    assert c.world().required(rtype, optional, False) is False

    # unticking what the organisation requires is refused without a controller, and nothing changes
    for who in ("", "analyst-1", "console", "improver"):
        with pytest.raises(ConsoleError, match="controller"):
            c.set_required(None, rtype, field, False, controller=who)
        assert c.req_of(rtype, row) is True and f"Sponsor A|{rtype}|{field}" not in c.S["req"]
    with pytest.raises(ConsoleError, match="controller"):
        c.act(f"req|{rtype}|{field}|0")  # the button path carries no controller either

    # a named controller can: the choice is recorded with who and why
    c.set_required(None, rtype, field, False, controller="casey-controller")
    assert c.req_of(rtype, row) is False
    assert c.S["req_ctl"][f"Sponsor A|{rtype}|{field}"] == "casey-controller"
    assert any("casey-controller" in a["r"] and field in a["a"] for a in c.S["audit"])

    # an override that has no controller behind it (for instance an old state file) never loosens
    c.S["req_ctl"].clear()
    assert c.req_of(rtype, row) is True and c.world().required(rtype, field, True) is True

    # ticking it again by anyone is always fine
    c.set_required(None, rtype, field, True)
    assert c.req_of(rtype, row) is True


def test_required_untick_through_the_button_with_a_controller(console: Console) -> None:
    _open_job(console)
    field = _default_required("Investors")
    console.act(f"req|Investors|{field}|0|drew-controller")
    assert console.S["req"][f"Sponsor A|Investors|{field}"] is False


# ---- no code in a skill folder ------------------------------------------------------------------------------
def test_no_python_under_skills() -> None:
    assert skills_dir() == SKILLS
    stray = sorted(p.relative_to(SKILLS).as_posix() for p in SKILLS.rglob("*")
                   if p.is_file() and p.suffix in (".py", ".pyc", ".sh", ".ps1", ".bat"))
    assert stray == []
    assert not (SKILLS / "map-accounts" / "scripts").exists()


def test_a_python_file_in_a_skill_folder_is_refused_by_the_loader(tree: Path) -> None:
    scripts = tree / "skills/map-accounts/scripts"
    scripts.mkdir()
    (scripts / "map_keys.py").write_text("x = 1\n", encoding="utf-8")
    err = _refused(tree, "map-accounts")
    assert set(err.codes) == {"layout"}


def test_the_skill_names_the_engine_operation_and_it_exists() -> None:
    import worker.accounts as A

    rules = {r["id"]: r for r in load_skills()["map-accounts"].checks()["rules"]}
    module, _, name = rules["cascade_decides_every_key"]["enforced_by"].rpartition(".")
    assert module == "worker.accounts" and callable(getattr(A, name))
