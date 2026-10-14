"""The eleven reconciliations as the reconcile-gl skill: rules loaded from the skill's data, version identity, the
controls and their limits, sponsor override, parity with the pre-move engine, and the gated table edit."""

from __future__ import annotations

import json
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

import expected_rules as X
from recon_scenarios import scenarios
from worker import console_harness as H
from worker import recon as R
from worker.audit import AuditLog
from worker.improver import WriteRefused, fast_loop, guard
from worker.skills import load_skills
from worker.web import create_app

FIX = Path(__file__).parent / "fixtures"
SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "reconcile-gl"


def _copy_skill(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    shutil.copytree(SKILL_DIR, root / "reconcile-gl")
    return root


# ---- the skill is a skill: data and prose only ----------------------------------------------------------------
def test_reconcile_gl_is_an_installed_skill_with_tables_controls_and_cases() -> None:
    s = load_skills()["reconcile-gl"]
    assert s.version == "1.0" and s.stamp == "reconcile-gl@1.0"
    tables = {p.stem for p in (SKILL_DIR / "tables").glob("*.yaml")}
    assert tables == {"account-series", "cash-flow-sections", "cross-check", "fund-type-applicability",
                      "presentation-sign", "trans-type-words"}
    assert (SKILL_DIR / "controls.yaml").is_file() and (SKILL_DIR / "specs.yaml").is_file()
    assert any((SKILL_DIR / "cases").glob("*.json"))


def test_no_executable_code_sits_in_the_skill_folder() -> None:
    allowed = {".md", ".yaml", ".json"}
    assert {p.suffix for p in SKILL_DIR.rglob("*") if p.is_file()} <= allowed
    assert not (SKILL_DIR / "scripts").exists()


# ---- rules come from the skill, and say which version produced them --------------------------------------------
def test_specs_text_is_unchanged_by_the_move() -> None:
    before = json.loads((FIX / "recon_specs_text.json").read_text(encoding="utf-8"))
    for s in R.SPECS:
        want = before[s["id"]]
        for k in ("n", "sel", "rule", "steps", "layout_text", "tol", "scope", "per"):
            if k in want:
                assert s[k] == want[k], (s["id"], k)


def test_every_check_has_the_identity_of_the_rules_that_built_it() -> None:
    rules = R.default_rules()
    assert rules.stamp == "reconcile-gl@1.0"
    assert len(rules.digest) == 12 and rules.ident == f"reconcile-gl@1.0#{rules.digest}"
    assert all(R.rules_of(s) is rules for s in R.SPECS)


def test_identity_is_a_content_hash_that_follows_the_files(tmp_path: Path) -> None:
    root = _copy_skill(tmp_path)
    a = R.load_rules(skills_root=root).digest
    assert a == R.load_rules(skills_root=root).digest == R.default_rules().digest
    t = root / "reconcile-gl" / "tables" / "trans-type-words.yaml"
    t.write_text(t.read_text(encoding="utf-8").replace("exit", "exit, sell-down"), encoding="utf-8")
    assert R.load_rules(skills_root=root).digest != a
    # a sponsor override changes the identity too, and the base folder does not
    sp = tmp_path / "sp"
    tdir = sp / "skills" / "reconcile-gl" / "tables"   # the per-skill overlay layout (M8)
    tdir.mkdir(parents=True)
    (tdir / "trans-type-words.yaml").write_text(yaml.safe_dump(
        {"id": "trans-type-words", "inputs": {"words": "string"},
         "rows": [{"id": "proceeds", "when": {"words": "proceeds"}, "then": {"words_list": "sold, exit"}}]}),
        encoding="utf-8")
    other = R.load_rules(sp, skills_root=root)
    assert other.digest != R.load_rules(skills_root=root).digest
    assert other.words("proceeds") == ("sold", "exit")


def test_a_sponsor_row_changes_the_figures_it_governs(tmp_path: Path) -> None:
    from test_console_recon import FUND, rows_from

    rows = rows_from(FUND)
    sp = tmp_path / "sp"
    tdir = sp / "skills" / "reconcile-gl" / "tables"   # the per-skill overlay layout (M8)
    tdir.mkdir(parents=True)
    # for this sponsor a "Disposal" is not a return of capital any more: it lands in other investment cost
    (tdir / "trans-type-words.yaml").write_text(yaml.safe_dump(
        {"id": "trans-type-words", "inputs": {"words": "string"},
         "rows": [{"id": "proceeds", "when": {"words": "proceeds"}, "then": {"words_list": "return of capital"}}]}),
        encoding="utf-8")
    base, mine = R.default_rules(), R.load_rules(sp)

    def sections(rules: R.Rules) -> dict[str, float]:
        spec = {s["id"]: s for s in rules.specs}["cf"]
        res = R.run_check(spec, rows, R.make_ctx(rows, {}), {}, None, lambda *_: "", {})
        return {r["label"]: r["a"] for r in res["rows"]}

    assert sections(base)["Net cash from investing"] == sections(mine)["Net cash from investing"]  # sum is same
    spec = {s["id"]: s for s in mine.specs}["cf"]
    assert spec["measures"]["roc"]["where"][0]["tt_any"] == ("return of capital",)
    assert R.rules_of(spec) is mine


def test_cash_flow_sections_table_moves_a_line_between_sections(tmp_path: Path) -> None:
    root = _copy_skill(tmp_path)
    t = root / "reconcile-gl" / "tables" / "cash-flow-sections.yaml"
    t.write_text(t.read_text(encoding="utf-8").replace(
        "{id: borrowings, when: {line: borrowings}, then: {section: financing}}",
        "{id: borrowings, when: {line: borrowings}, then: {section: operating}}"), encoding="utf-8")
    spec = {s["id"]: s for s in R.load_rules(skills_root=root).specs}["cf"]
    assert ("bor", 1) in spec["derived"]["op"]["of"] and ("bor", 1) not in spec["derived"]["fin"]["of"]
    base = R.SPEC["cf"]
    assert ("bor", 1) in base["derived"]["fin"]["of"]


def test_fund_type_matrix_comes_from_its_table() -> None:
    assert R.SPEC["evl"]["scope"] == ["DE"] and R.SPEC["pcap"]["scope"] == ["DLP", "LP"]


# ---- controls: bounded, never improvable -----------------------------------------------------------------------
def _controls(tmp_path: Path, edit: Any) -> Path:
    root = _copy_skill(tmp_path)
    p = root / "reconcile-gl" / "controls.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    edit(data)
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return root


@pytest.mark.parametrize("edit, why", [
    (lambda d: d["tolerances"]["money-flat"].update(pass_up_to=50, warn_up_to=50), "outside the allowed limits"),
    (lambda d: d["tolerances"]["money-tiered"].update(warn_up_to=0.5), "outside the allowed limits"),
    (lambda d: d["tolerances"]["int"].update(pass_up_to=1, warn_up_to=1), "outside the allowed limits"),
    (lambda d: d["levels"].update(over_warn_limit="WARN"), "floor"),
    (lambda d: d["levels"].update(needs_judgement="PASS"), "floor"),
    (lambda d: d["balance"].update(target=10), "balance target"),
    (lambda d: d["required_fields"].update(irr=["investor"]), "cannot drop"),
])
def test_controls_outside_the_limits_are_refused_at_load(tmp_path: Path, edit: Any, why: str) -> None:
    root = _controls(tmp_path, edit)
    with pytest.raises(R.RulesError, match=why):
        R.load_rules(skills_root=root)


def test_controls_inside_the_limits_take_effect(tmp_path: Path) -> None:
    root = _controls(tmp_path, lambda d: d["tolerances"]["money-flat"].update(pass_up_to=2, warn_up_to=2))
    rules = R.load_rules(skills_root=root)
    assert R.classify(100.0, 98.5, "money", "flat", rules) == (1.5, "PASS")
    assert R.classify(100.0, 98.5, "money", "flat")[1] == "ERROR"  # the default rules are unchanged
    assert "over 2 is an ERROR" in {s["id"]: s for s in rules.specs}["is"]["rule"]


def test_the_improver_cannot_touch_the_controls() -> None:
    for kind in ("table_row", "threshold", "know_how"):
        with pytest.raises(WriteRefused):
            guard(kind, "skills/reconcile-gl/controls.yaml")


def test_fast_loop_never_promotes_a_controls_change(tmp_path: Path) -> None:
    from worker.improver import ImprovementStore

    store = ImprovementStore(tmp_path / "imp", AuditLog(tmp_path / "imp" / "audit.jsonl"))
    proposal = {"kind": "threshold", "target": "skills/reconcile-gl/controls.yaml", "scope": "sponsor",
                "change": {"row": {"id": "x", "then": {"a": 1}}}, "evidence": {}, "proposed_by": "improver",
                "summary": "loosen the tolerance"}
    cards = fast_loop(store, [proposal], [], None, None, auto_promote_sponsor=True, approver="ops")  # type: ignore[arg-type]
    assert cards == [] and store.cards() == []
    log = (tmp_path / "imp" / "audit.jsonl").read_text(encoding="utf-8")
    assert "improver.refused" in log


def test_only_a_named_other_controller_changes_the_controls(tmp_path: Path) -> None:
    root = _copy_skill(tmp_path)
    skill_dir = root / "reconcile-gl"
    audit = AuditLog(tmp_path / "audit.jsonl")
    new = yaml.safe_load((skill_dir / "controls.yaml").read_text(encoding="utf-8"))
    new["tolerances"]["money-tiered"]["warn_up_to"] = 10
    who = ("alex", "blake")  # the controllers list (WORKER_ADMINS)
    with pytest.raises(PermissionError):
        R.apply_controls(skill_dir, new, "improver", "", "wider band", audit, controllers=who)
    with pytest.raises(PermissionError):
        R.apply_controls(skill_dir, new, "alex", "Alex", "wider band", audit, controllers=who)
    bad = json.loads(json.dumps(new))
    bad["tolerances"]["money-tiered"]["warn_up_to"] = 500
    with pytest.raises(R.RulesError):
        R.apply_controls(skill_dir, bad, "alex", "blake", "too wide", audit, controllers=who)
    R.apply_controls(skill_dir, new, "alex", "blake", "the sponsor's auditors accept a 10 band", audit,
                     controllers=who)
    assert R.load_rules(skills_root=root).thresholds("tiered", "money")[1] == Decimal(10)
    assert "control.changed" in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert yaml.safe_load((SKILL_DIR / "controls.yaml").read_text(encoding="utf-8"))["tolerances"][
        "money-tiered"]["warn_up_to"] == 5  # the repository copy is untouched


# ---- exact arithmetic ------------------------------------------------------------------------------------------
def test_sums_are_exact_decimals_not_binary_floats() -> None:
    r = R.Row("11010", "Assets", "Cash and equivalents", 0.1, None, "", "x")
    rows = [R.Row("11010", "Assets", "Cash and equivalents", 0.1, None, "", "x") for _ in range(3)]
    rows.append(R.Row("11010", "Assets", "Cash and equivalents", -0.3, None, "", "x"))
    items = R.measure_items(R.SPEC["tb"], rows + [r], R.make_ctx(rows, {}))
    assert items[(("11010",), "bal")] == Decimal("0.1")
    assert all(isinstance(v, Decimal) for v in items.values())
    assert R.classify(0.1 + 0.2, 0.3, "money", "flat") == (0.0, "PASS")
    assert R.classify(1.005, 0.0, "money", "flat")[0] == 1.01


# ---- parity with the engine before the move --------------------------------------------------------------------
def test_the_eleven_checks_give_identical_rows_before_and_after_the_move() -> None:
    golden = json.loads((FIX / "recon_parity.json").read_text(encoding="utf-8"))
    got = json.loads(json.dumps(scenarios(), default=str))
    assert set(got) == set(golden)
    for name in golden:
        assert got[name] == golden[name], name


# ---- the harness: library, detail, controls read only, gated table edit ------------------------------------------
@pytest.fixture
def manager(tmp_path: Path) -> Any:
    app = create_app(tmp_path / "state", environ={"WORKER_MODEL_PROVIDER": "demo",
                                                  "WORKER_ENV_FILE": str(tmp_path / "t.env")})
    return app.state.manager


def test_library_lists_the_skill_and_detail_shows_tables_and_read_only_controls(manager: Any) -> None:
    lib = {s["name"]: s for s in H.skills_view(manager)["skills"]}
    assert lib["reconcile-gl"]["tables"] == sorted(
        ["account-series", "cash-flow-sections", "cross-check", "fund-type-applicability", "presentation-sign",
         "trans-type-words"])
    assert lib["reconcile-gl"]["cases"] >= 10 and lib["reconcile-gl"]["stamp"] == "reconcile-gl@1.0"
    d = H.skill_view(manager, "reconcile-gl")
    assert d["stamp"].startswith("reconcile-gl@1.0") and all(t["replayable"] for t in d["tables"])
    assert d["controls"]["editable"] is False and "controller" in d["controls"]["who"]
    assert d["controls"]["tolerances"]["money-tiered"] == {"pass_up_to": 1.0, "warn_up_to": 5.0}


def _cells(manager: Any, table: str, edit: Any = None) -> list[dict[str, Any]]:
    t = next(x for x in H.skill_view(manager, "reconcile-gl")["tables"] if x["id"] == table)
    cells = json.loads(json.dumps(t["rows"]))
    if edit:
        edit(cells)
    return cells


def test_an_unchanged_recon_table_passes_the_fixtures_but_is_not_a_change(manager: Any) -> None:
    r = H.harness_act(manager, {"action": "test", "skill": "reconcile-gl", "table": "trans-type-words",
                                "rows": _cells(manager, "trans-type-words")}, "alex", False)
    assert r["replayable"] and r["changed"] is False and r["passed"] is False
    assert r["fixtures"]["pass_before"] == r["fixtures"]["total"] >= 10


def test_an_edit_that_breaks_a_known_gl_case_is_caught(manager: Any) -> None:
    def distributions_to_operating(cells: list[dict[str, Any]]) -> None:
        next(r for r in cells if r["id"] == "distributions")["then"]["section"] = "operating"

    r = H.harness_act(manager, {"action": "test", "skill": "reconcile-gl", "table": "cash-flow-sections",
                                "rows": _cells(manager, "cash-flow-sections", distributions_to_operating)}, "alex", False)
    assert r["changed"] and r["passed"] is False and r["fixtures"]["broke"]


def test_a_recon_table_edit_is_gated_and_only_a_named_human_promotes_it(manager: Any, tmp_path: Path) -> None:
    # a recorded case says what the analyst expects: a transfer in on a cost account is operating for this fund
    cases = tmp_path / "state" / "recon_cases"
    cases.mkdir(parents=True)
    known = json.loads((SKILL_DIR / "cases" / "known-gl.json").read_text(encoding="utf-8"))
    extra = [{"j": "J9", "n": 1, "acct": "1500", "tt": "Transfer", "dr": 50_000, "cr": 0, "date": "2025-07-01",
              "inv": "", "invst": "", "pr": "", "shares": None, "agn": ""},
             {"j": "J9", "n": 2, "acct": "1000", "tt": "Transfer", "dr": 0, "cr": 50_000, "date": "2025-07-01",
              "inv": "", "invst": "", "pr": "", "shares": None, "agn": ""}]
    fixture = {"offsets": False, "lines": known["fixtures"]["fund"]["lines"] + extra}
    case = json.loads(json.dumps(next(c for c in known["cases"] if c["check"] == "cf")))
    case.update(name="a transfer on a cost account is operating here", ledger="fund-transfer")
    case["expect"] = {"state": "needs-report", "worst": "PASS", "figures": {
        "Net cash from operating": -62_500.0, "Net cash from investing": -355_000.0}}
    (cases / "analyst.json").write_text(json.dumps({"fixtures": {"fund-transfer": fixture}, "cases": [case]}),
                                        encoding="utf-8")

    def to_operating(cells: list[dict[str, Any]]) -> None:
        next(x for x in cells if x["id"] == "other-investment-cost")["then"]["section"] = "operating"

    tested = H.harness_act(manager, {"action": "test", "skill": "reconcile-gl", "table": "cash-flow-sections",
                                     "rows": _cells(manager, "cash-flow-sections", to_operating)}, "alex", False)
    assert tested["passed"] and tested["fixtures"]["fixed"] and not tested["fixtures"]["broke"]
    body = {"action": "propose", "skill": "reconcile-gl", "table": "cash-flow-sections", "scope": "sponsor",
            "sponsor": "sponsor-a", "rows": _cells(manager, "cash-flow-sections", to_operating)}
    filed = H.harness_act(manager, body, "alex", False)
    assert filed["status"] == ["ready"]
    cid = filed["cards"][0]
    with pytest.raises(H.HarnessError):  # the proposer does not approve their own change
        H.harness_act(manager, {"action": "promote", "card": cid}, "alex", False)
    done = H.harness_act(manager, {"action": "promote", "card": cid}, "blake", False)
    assert done["status"] == "promoted"
    # the promoted row is part of the rules the next run loads, and the identity moves with it
    after = R.rules_for(None, manager.state_dir)
    assert after.section("other-investment-cost") == "operating" and after.digest != R.default_rules().digest
    spec = {s["id"]: s for s in after.specs}["cf"]
    assert ("oth", 1) in spec["derived"]["op"]["of"]


def test_a_recorded_case_that_contradicts_a_known_gl_case_blocks_the_change(manager: Any, tmp_path: Path) -> None:
    cases = tmp_path / "state" / "recon_cases"
    cases.mkdir(parents=True)
    known = json.loads((SKILL_DIR / "cases" / "known-gl.json").read_text(encoding="utf-8"))
    case = json.loads(json.dumps(next(c for c in known["cases"] if c["check"] == "cf")))
    case.update(name="gains are operating")
    case["expect"] = {"state": "needs-report", "worst": "PASS", "figures": {"Net cash from operating": 32_500.0}}
    (cases / "analyst.json").write_text(json.dumps({"cases": [case]}), encoding="utf-8")

    def gains_to_operating(cells: list[dict[str, Any]]) -> None:
        next(x for x in cells if x["id"] == "realised-gains")["then"]["section"] = "operating"

    body = {"action": "propose", "skill": "reconcile-gl", "table": "cash-flow-sections", "scope": "sponsor",
            "sponsor": "sponsor-a", "rows": _cells(manager, "cash-flow-sections", gains_to_operating)}
    assert H.harness_act(manager, body, "alex", False)["status"] == ["blocked"]


# ---- one money implementation ---------------------------------------------------------------------------------
def test_recon_has_no_float_arithmetic_for_amounts_and_uses_money() -> None:
    import re

    src = (Path(R.__file__)).read_text(encoding="utf-8")
    code = "\n".join(line.split("#")[0] for line in src.splitlines())
    assert "from worker.money import" in code
    for banned in ("ROUND_HALF_UP", ".quantize(", "def _d(", "def D(", "def q2(", "def f2("):
        assert banned not in code, banned
    # float( appears only where a rate or a display value is made, never to add or compare amounts
    allowed = {"abs_diff=float(abs(diff))", '"pass_up_to": float(p)', '"balance_target": float(c["balance_target"])',
               "(parse_date(k[1]), float(v))", "(d, float(items.get("}
    for line in code.splitlines():
        if re.search(r"(?<![\w.])float\(", line):
            assert any(a in line for a in allowed), f"float arithmetic on an amount? {line.strip()}"
    assert "round(" not in code.replace("return round((lo + hi) / 2 * 100, 2)", "")


def test_report_upload_is_audited_with_the_content_hash_next_to_the_label(tmp_path: Path) -> None:
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Line", "Investor", "Date", "Amount"])
    ws.append(["Ending balance", "", "2025-12-31", "1,234.505"])
    buf = io.BytesIO()
    wb.save(buf)
    audit = AuditLog(tmp_path / "audit.jsonl")
    rules = R.default_rules()
    meta = R.save_report(tmp_path / "job", "tb", "r.xlsx", buf.getvalue(), "alex", audit, rules)
    entry = next(e for e in audit.read() if e["action"] == "recon.report_uploaded")
    blob = json.dumps(entry)
    assert rules.digest and rules.digest in blob and rules.stamp in blob
    assert meta["skill_hash"] == rules.digest
    assert R.load_report(tmp_path / "job", "tb")["skill_hash"] == rules.digest


# ---- report header words are a skill file (M10) ------------------------------------------------------------------
def test_recon_alias_equals_synonyms_file() -> None:
    data = yaml.safe_load((SKILL_DIR / "synonyms.yaml").read_text(encoding="utf-8"))
    assert data["kind"] == "synonyms" and data["schema_version"] == 1 and data["scope"] == "report"
    assert {k: tuple(v) for k, v in data["concepts"].items()} == X.RECON_ALIAS  # every key and value, in order
    assert R.default_rules().aliases == X.RECON_ALIAS


def test_sponsor_synonym_overlay_is_add_only(tmp_path: Path) -> None:
    root = _copy_skill(tmp_path)
    sponsor = tmp_path / "sponsor-a"
    (sponsor / "skills" / "reconcile-gl").mkdir(parents=True)
    ov = sponsor / "skills" / "reconcile-gl" / "synonyms.yaml"
    ov.write_text(yaml.safe_dump({"schema_version": 1, "kind": "synonyms", "scope": "report",
                                  "concepts": {"amount": ["Closing Position", "balance"], "date": ["As Of"]}}),
                  encoding="utf-8")
    mine = R.load_rules(sponsor, skills_root=root).aliases
    base = R.load_rules(skills_root=root).aliases
    assert mine["amount"][:2] == ("Closing Position", "balance")  # added words are tried first
    assert mine["date"][0] == "As Of"
    for key, words in base.items():  # nothing of the organisation's was removed
        assert {w.casefold() for w in words} <= {w.casefold() for w in mine[key]}, key
    assert mine["investor"] == base["investor"]
    # a header only the sponsor's word names is read through the rules that carry it
    rep = tmp_path / "r.csv"
    rep.write_text("Line,Investor,As Of,Closing Position\nNAV,LP-1,2024-03-31,10\n", encoding="utf-8")
    assert R.read_report(rep, mine)[0]["amount"] == Decimal(10)
    with pytest.raises(R.ReportError):
        R.read_report(rep, base)
    # an overlay of another scope, or naming a column the engine does not read, is refused
    ov.write_text(yaml.safe_dump({"schema_version": 1, "kind": "synonyms", "scope": "target",
                                  "record_type": "Investors", "concepts": {"amount": ["x"]}}), encoding="utf-8")
    with pytest.raises(R.RulesError):
        R.load_rules(sponsor, skills_root=root)
    ov.write_text(yaml.safe_dump({"schema_version": 1, "kind": "synonyms", "scope": "report",
                                  "concepts": {"colour": ["x"]}}), encoding="utf-8")
    with pytest.raises(R.RulesError):
        R.load_rules(sponsor, skills_root=root)
