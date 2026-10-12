"""The strict rule-file loader (migration step M3): one contract test per error code, the three layers of a control,
the controls hash, and every current skill file.

The fixtures under tests/fixtures/rulefiles/good form a small valid repo; a test changes one thing in a copy and
expects exactly the code it is about. Fictitious data only.
"""

from __future__ import annotations

import json
import re
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from worker import rulefiles as rf
from worker.rule_checks import CONTROL_BOUNDS
from worker.rule_recipe import regex_problem
from worker.rule_schema import CODES

FIX = Path(__file__).parent / "fixtures" / "rulefiles"
REPO = Path(__file__).resolve().parents[1]
LINE = re.compile(r"^(?P<path>[^:]+):(?P<pointer>[^:]*): (?P<code>[a-z_]+): (?P<sentence>.+)$")
CTX = rf.Context(
    record_types=frozenset({"Commitments", "Investors", "GL"}),
    target_fields={"Commitments": frozenset({"GP_ID", "Fund_ID", "Investor_ID", "Commitment_Amount", "Commitment_ID",
                                             "Commitment_Date"})},
    target_keys={"Commitments": frozenset({"Commitment_ID"})},
)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    dest = tmp_path / "repo"
    shutil.copytree(FIX / "good", dest)
    return dest


def edit(tree: Path, rel: str, old: str, new: str) -> None:
    p = tree / rel
    text = p.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {rel}"
    p.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")


def skill(tree: Path, name: str, **kw: Any) -> rf.Pin:
    return rf.load_skill(tree / "skills" / name, repo_root=tree, **kw)


def refused(fn: Any, *a: Any, **kw: Any) -> rf.SchemaError:
    with pytest.raises(rf.SchemaError) as e:
        fn(*a, **kw)
    return e.value


def has(err: rf.SchemaError, code: str, path: str = "", pointer: str | None = None) -> rf.Problem:
    hits = [p for p in err.problems if p.code == code and path in p.path
            and (pointer is None or p.pointer == pointer)]
    assert hits, f"no {code} at {path}:{pointer}; got:\n" + "\n".join(err.lines)
    return hits[0]


def only(err: rf.SchemaError, code: str) -> None:
    assert set(err.codes) == {code}, "\n".join(err.lines)


# ---- the good tree ----------------------------------------------------------------------------------------
def test_kinds_and_codes_are_closed() -> None:
    assert rf.KINDS == ("target", "table", "checks", "recon-specs", "synonyms", "cases", "controls", "recipe")
    assert len(CODES) == 13 and len(set(CODES)) == 13
    with pytest.raises(ValueError):
        rf.Problem("a.yaml", "x", "made_up_code", "no")


def test_good_tree_loads_every_skill_and_target_and_recipe(tree: Path) -> None:
    pins = rf.load_skills(tree / "skills", repo_root=tree)
    assert set(pins) == {"check-data-quality", "map-accounts", "reconcile-gl", "build-static-records"}
    assert dict(pins["reconcile-gl"].schema_versions) == {"cases": 1, "checks": 1, "controls": 1, "recon-specs": 1,
                                                          "synonyms": 1, "table": 1}
    target = rf.load_file(tree / "templates" / "Commitments.target.yaml", repo_root=tree, context=CTX)
    assert target.kind == "target" and target.controls["id_formats"]["max_length"] == 20
    recipe = rf.load_file(tree / "templates/sponsors/sponsor-a/recipes/Commitments.yaml", repo_root=tree,
                          context=CTX)
    assert recipe.kind == "recipe" and set(recipe.data["fields"]) >= {"Commitment_Date", "Fund_ID"}


def test_good_overlays_load(tree: Path) -> None:
    base = tree / "templates/sponsors/sponsor-a/skills"
    pin = skill(tree, "map-accounts", overlay_dir=base / "map-accounts")
    assert any(f.overlay for f in pin.files.values())
    pin = skill(tree, "check-data-quality", overlay_dir=base / "check-data-quality")
    assert sum(f.overlay for f in pin.files.values()) == 1
    org = rf.load_file(tree / "templates/Commitments.target.yaml", repo_root=tree, context=CTX)
    mine = rf.load_file(tree / "templates/sponsors/sponsor-a/Commitments.target.yaml", repo_root=tree, context=CTX,
                        base=org)
    assert mine.overlay


def test_error_lines_have_the_documented_format(tree: Path) -> None:
    edit(tree, "skills/check-data-quality/checks.yaml", "    severity: ERR\n    message: \"Deal",
         "    severity: WARN\n    message: \"Deal")
    err = refused(skill, tree, "check-data-quality")
    assert err.lines == (
        ("skills/check-data-quality/checks.yaml:rules[1].severity: looser_than_control: deal_required is WARN, "
        "below its floor ERR in controls.yaml; only a controller can lower it"),)
    for line in err.lines:
        assert LINE.match(line)


def test_a_refused_pin_lists_every_problem_and_loads_nothing(tree: Path) -> None:
    edit(tree, "skills/check-data-quality/controls.yaml", "id_too_long: 20", "id_too_long: 30")
    edit(tree, "skills/check-data-quality/checks.yaml", "family: validity\n    op: max_length\n    fields: \"@names\"",
         "family: validity\n    op: max_length\n    fields: \"@names\"\n    colour: red")
    edit(tree, "skills/map-accounts/tables/coa-cascade.yaml", "score: \">=0.5\"}, then: {tier: L3",
         "score: \"~0.5\"}, then: {tier: L3")
    edit(tree, "skills/reconcile-gl/specs.yaml", "fsg: pnl", "fsg: pnll")
    err = refused(rf.load_skills, tree / "skills", repo_root=tree)
    assert len(err.problems) >= 4
    paths = {p.path for p in err.problems}
    assert len(paths) == 4  # one skill's problem does not hide another's
    assert all(LINE.match(line) for line in err.lines)


# ---- one contract test per error code ---------------------------------------------------------------------
def test_code_unknown_kind(tree: Path) -> None:
    edit(tree, "skills/map-accounts/tables/coa-cascade.yaml", "kind: table", "kind: tabel")
    err = refused(skill, tree, "map-accounts")
    p = has(err, "unknown_kind", "coa-cascade.yaml", "kind")
    assert "tabel" in p.message and "recon-specs" in p.message


def test_code_unsupported_schema_version(tree: Path) -> None:
    edit(tree, "skills/map-accounts/tables/coa-cascade.yaml", "schema_version: 1", "schema_version: 7")
    err = refused(skill, tree, "map-accounts")
    has(err, "unsupported_schema_version", "coa-cascade.yaml", "schema_version")


def test_code_missing_field(tree: Path) -> None:
    edit(tree, "skills/map-accounts/tables/coa-cascade.yaml", "hit: first\n", "")
    err = refused(skill, tree, "map-accounts")
    has(err, "missing_field", "coa-cascade.yaml", "hit")
    only(err, "missing_field")


def test_missing_header_is_missing_field_not_a_guess(tree: Path) -> None:
    edit(tree, "skills/map-accounts/checks.yaml", "schema_version: 1\nkind: checks\n", "")
    err = refused(skill, tree, "map-accounts")
    has(err, "missing_field", "checks.yaml", "schema_version")
    has(err, "missing_field", "checks.yaml", "kind")


def test_code_unknown_field(tree: Path) -> None:
    edit(tree, "skills/check-data-quality/checks.yaml", "    fix: \"Type {field} for these rows\"\n",
         "    fix: \"Type {field} for these rows\"\n    threshold: 3\n")
    err = refused(skill, tree, "check-data-quality")
    has(err, "unknown_field", "checks.yaml", "rules[0].threshold")  # today quality.load_rules passes this on


def test_code_wrong_type(tree: Path) -> None:
    edit(tree, "skills/check-data-quality/checks.yaml", "    severity: WARN\n", "    severity: LOW\n")
    err = refused(skill, tree, "check-data-quality")
    has(err, "wrong_type", "checks.yaml", "rules[3].severity")


@pytest.mark.parametrize("old,new,pointer", [
    ("score: \">=0.5\"}, then: {tier: L3", "score: \"~0.5\"}, then: {tier: L3", "rows[2].when.score"),
    ("historic: true}", "historic: {between: [1, 2]}}", "rows[0].when.historic"),
    ("historic: true}", "historic: \">=1\"}", "rows[0].when.historic"),
])
def test_code_unknown_operator_in_a_condition(tree: Path, old: str, new: str, pointer: str) -> None:
    edit(tree, "skills/map-accounts/tables/coa-cascade.yaml", old, new)
    err = refused(skill, tree, "map-accounts")
    has(err, "unknown_operator", "coa-cascade.yaml", pointer)


def test_code_unknown_operator_in_a_rule_and_a_field_group(tree: Path) -> None:
    edit(tree, "skills/check-data-quality/checks.yaml", "op: blank\n    fields: \"@required\"",
         "op: median\n    fields: \"@required\"")
    edit(tree, "skills/check-data-quality/checks.yaml", "fields: \"@ids\"", "fields: \"@idz\"")
    err = refused(skill, tree, "check-data-quality")
    has(err, "unknown_operator", "checks.yaml", "rules[0].op")
    has(err, "unknown_operator", "checks.yaml", "rules[2].fields")


def test_code_unknown_reference_series(tree: Path) -> None:
    edit(tree, "skills/reconcile-gl/specs.yaml", "fsl_prefix: cash", "fsl_prefix: cots")
    err = refused(skill, tree, "reconcile-gl")
    p = has(err, "unknown_reference", "specs.yaml", "checks[1].measures.cash.where[0].fsl_prefix")
    assert "cots" in p.message and "account-series" in p.message


@pytest.mark.parametrize("old,new,where", [
    ("tt_any: purchase", "tt_any: buyback", "words"),
    ("line: net-income", "line: net-profit", "cash-flow-sections"),
    ("order: [bal, net]", "order: [bal, nett]", "order"),
    ("[ni, 1], [buy, 1]", "[ni, 1], [bye, 1]", "of"),
])
def test_code_unknown_reference_other_names(tree: Path, old: str, new: str, where: str) -> None:
    edit(tree, "skills/reconcile-gl/specs.yaml", old, new)
    err = refused(skill, tree, "reconcile-gl")
    only(err, "unknown_reference")


def test_code_unknown_reference_check_needs_a_style_and_an_applicability_row(tree: Path) -> None:
    edit(tree, "skills/reconcile-gl/controls.yaml", "check_style: {tb: tiered, cf: flat}", "check_style: {tb: tiered}")
    edit(tree, "skills/reconcile-gl/tables/fund-type-applicability.yaml", "  - {id: cf, when: {check: cf}, then: "
         "{DE: true, LP: false}}\n", "")
    err = refused(skill, tree, "reconcile-gl")
    msgs = " ".join(p.message for p in err.problems)
    assert "check_style" in msgs and "fund-type-applicability" in msgs
    only(err, "unknown_reference")


def test_code_duplicate_id_row(tree: Path) -> None:
    edit(tree, "skills/map-accounts/tables/coa-cascade.yaml", "{id: L1,", "{id: L0,")
    err = refused(skill, tree, "map-accounts")
    has(err, "duplicate_id", "coa-cascade.yaml", "rows[1].id")


def test_code_duplicate_id_yaml_key_is_refused_not_last_wins() -> None:
    err = refused(rf.load_file, FIX / "bad/duplicate_key/skills/map-accounts/tables/coa-cascade.yaml",
                  repo_root=FIX / "bad/duplicate_key")
    p = has(err, "duplicate_id", "coa-cascade.yaml", "hit")
    assert "twice" in p.message


def test_code_duplicate_id_json_key_and_case_name(tree: Path) -> None:
    edit(tree, "skills/map-accounts/cases/cascade.json", '"description": "One case per tier.",',
         '"description": "One case per tier.", "description": "again",')
    edit(tree, "skills/map-accounts/cases/cascade.json", '"L5 nothing matches"', '"L0 historic wins over everything"')
    err = refused(skill, tree, "map-accounts")
    has(err, "duplicate_id", "cascade.json", "description")
    has(err, "duplicate_id", "cascade.json", "cases[1].name")


def test_code_duplicate_id_table_across_skills(tree: Path) -> None:
    src = tree / "skills/map-accounts/tables/coa-cascade.yaml"
    dst = tree / "skills/build-static-records/tables"
    dst.mkdir()
    shutil.copy(src, dst / "coa-cascade.yaml")
    err = refused(rf.load_skills, tree / "skills", repo_root=tree)
    has(err, "duplicate_id", "map-accounts/tables/coa-cascade.yaml", "id")


def test_code_no_catch_all_last_row_has_a_when(tree: Path) -> None:
    edit(tree, "skills/map-accounts/tables/coa-cascade.yaml", "  - {id: L5, then:",
         "  - {id: L5, when: {historic: false}, then:")
    err = refused(skill, tree, "map-accounts")
    has(err, "no_catch_all", "coa-cascade.yaml", "rows[4]")


def test_code_no_catch_all_row_without_when_in_the_middle(tree: Path) -> None:
    edit(tree, "skills/map-accounts/tables/coa-cascade.yaml", "  - {id: L1, when: {pinned: true}, then:",
         "  - {id: L1, then:")
    err = refused(skill, tree, "map-accounts")
    p = has(err, "no_catch_all", "coa-cascade.yaml", "rows[1]")
    assert "never run" in p.message


def test_code_out_of_bounds_tolerance_over_the_code_limit(tree: Path) -> None:
    edit(tree, "skills/reconcile-gl/controls.yaml", "money-tiered: {pass_up_to: 1, warn_up_to: 5}",
         "money-tiered: {pass_up_to: 1, warn_up_to: 30}")
    err = refused(skill, tree, "reconcile-gl")
    p = has(err, "out_of_bounds", "controls.yaml", "tolerances.money-tiered.warn_up_to")
    assert "25" in p.message


@pytest.mark.parametrize("old,new,pointer", [
    ("balance: {target: 0}", "balance: {target: 1}", "balance.target"),
    ("needs_judgement: REVIEW", "needs_judgement: PASS", "levels.needs_judgement"),
    ("cf: [gl_date]", "cf: [pr_date]", "required_fields.cf"),
    ("int: {pass_up_to: 0, warn_up_to: 0}", "int: {pass_up_to: 1, warn_up_to: 1}", "tolerances.int.pass_up_to"),
])
def test_code_out_of_bounds_recon_controls(tree: Path, old: str, new: str, pointer: str) -> None:
    edit(tree, "skills/reconcile-gl/controls.yaml", old, new)
    err = refused(skill, tree, "reconcile-gl")
    has(err, "out_of_bounds", "controls.yaml", pointer)


def test_code_out_of_bounds_ceiling_and_target_and_recipe(tree: Path) -> None:
    edit(tree, "skills/check-data-quality/controls.yaml", "id_too_long: 20", "id_too_long: 30")
    err = refused(skill, tree, "check-data-quality")
    has(err, "out_of_bounds", "controls.yaml", "max_ceiling.id_too_long")
    edit(tree, "templates/Commitments.target.yaml", "max_length: 20", "max_length: 35")
    err = refused(rf.load_file, tree / "templates/Commitments.target.yaml", repo_root=tree, context=CTX)
    has(err, "out_of_bounds", "Commitments.target.yaml", "controls.id_formats.max_length")


def test_code_looser_than_control_severity_below_floor(tree: Path) -> None:
    edit(tree, "skills/check-data-quality/checks.yaml", "    severity: ERR\n    message: \"Deal",
         "    severity: WARN\n    message: \"Deal")
    err = refused(skill, tree, "check-data-quality")
    has(err, "looser_than_control", "checks.yaml", "rules[1].severity")


def test_stricter_than_the_control_is_allowed(tree: Path) -> None:
    edit(tree, "skills/check-data-quality/checks.yaml", "    max: 100\n    severity: WARN",
         "    max: 60\n    severity: ERR")
    skill(tree, "check-data-quality")


@pytest.mark.parametrize("old,new,pointer", [
    ("max: 20", "max: 25", "rules[2].max"),
    ('ranges: ["1500-1599", "1600-1699", "9000-9999"]', 'ranges: ["1500-1599", "9000-9999"]',
     "rules[1].when.ranges"),
    ("    tolerance: 0\n", "    tolerance: 0.01\n", "rules[4].tolerance"),
])
def test_code_looser_than_control_other_fields(tree: Path, old: str, new: str, pointer: str) -> None:
    edit(tree, "skills/check-data-quality/checks.yaml", old, new)
    err = refused(skill, tree, "check-data-quality")
    code = "out_of_bounds" if pointer.endswith("tolerance") else "looser_than_control"
    has(err, code, "checks.yaml", pointer)


def test_code_looser_than_control_recipe_truncating_an_id(tree: Path) -> None:
    edit(tree, "templates/sponsors/sponsor-a/recipes/Commitments.yaml", "      - {op: case, to: upper}\n",
         "      - {op: case, to: upper}\n      - {op: truncate, max: 10}\n")
    err = refused(rf.load_file, tree / "templates/sponsors/sponsor-a/recipes/Commitments.yaml", repo_root=tree,
                  context=CTX)
    has(err, "looser_than_control", "Commitments.yaml", "fields.Investor_ID.ops[2]")


def test_code_overlay_forbidden_control_row(tree: Path) -> None:
    p = tree / "templates/sponsors/sponsor-a/skills/map-accounts/tables/coa-cascade.yaml"
    p.write_text(p.read_text(encoding="utf-8")
                 + "  - {id: L0, when: {historic: true}, then: {tier: L0 Historic, action: suggest}}\n", encoding="utf-8")
    err = refused(skill, tree, "map-accounts", overlay_dir=p.parent.parent)
    has(err, "overlay_forbidden", "sponsor-a", "rows[2]")


def test_code_overlay_forbidden_row_that_passes_unseen_and_inputs(tree: Path) -> None:
    p = tree / "templates/sponsors/sponsor-a/skills/map-accounts/tables/coa-cascade.yaml"
    p.write_text(p.read_text(encoding="utf-8").replace(
        "rows:\n", "inputs: {historic: bool}\nrows:\n  - {id: S0, when: {score: \">=0.1\"}, then: {action: apply}}\n"),
        encoding="utf-8")
    err = refused(skill, tree, "map-accounts", overlay_dir=p.parent.parent)
    has(err, "overlay_forbidden", "sponsor-a", "inputs")
    p2 = has(err, "overlay_forbidden", "sponsor-a", "rows[0]")
    assert "passes unseen" in p2.message


def test_code_overlay_forbidden_controls_file_and_locked_fields(tree: Path) -> None:
    base = tree / "templates/sponsors/sponsor-a/skills/check-data-quality"
    shutil.copy(tree / "skills/check-data-quality/controls.yaml", base / "controls.yaml")
    edit(tree, "templates/sponsors/sponsor-a/skills/check-data-quality/checks.yaml", 'fields: "@names"',
         'fields: "@ids"')
    err = refused(skill, tree, "check-data-quality", overlay_dir=base)
    has(err, "overlay_forbidden", "controls.yaml", "")
    has(err, "overlay_forbidden", "checks.yaml", "rules[0].fields")


def test_overlay_cannot_lower_severity_or_drop_a_target_requirement(tree: Path) -> None:
    base = tree / "templates/sponsors/sponsor-a/skills/check-data-quality"
    edit(tree, "templates/sponsors/sponsor-a/skills/check-data-quality/checks.yaml", "    max: 80\n    severity: ERR",
         "    max: 80\n    severity: WARN")
    edit(tree, "skills/check-data-quality/checks.yaml", "    max: 100\n    severity: WARN",
         "    max: 100\n    severity: ERR")
    err = refused(skill, tree, "check-data-quality", overlay_dir=base)
    has(err, "looser_than_control", "sponsor-a", "rules[0].severity")
    edit(tree, "templates/sponsors/sponsor-a/Commitments.target.yaml", "id_formats: {max_length: 15,",
         "id_formats: {max_length: 20,")
    edit(tree, "templates/sponsors/sponsor-a/Commitments.target.yaml", "required: [GP_ID, Fund_ID, Investor_ID, "
         "Commitment_Amount, Commitment_Date]", "required: [GP_ID, Fund_ID, Investor_ID]")
    edit(tree, "templates/sponsors/sponsor-a/Commitments.target.yaml", "key: [Commitment_ID]", "key: [Other_ID]")
    org = rf.load_file(tree / "templates/Commitments.target.yaml", repo_root=tree)
    err = refused(rf.load_file, tree / "templates/sponsors/sponsor-a/Commitments.target.yaml", repo_root=tree,
                  base=org)
    has(err, "looser_than_control", "Commitments.target.yaml", "controls.required")
    has(err, "overlay_forbidden", "Commitments.target.yaml", "controls.key")


def test_code_layout_stray_file_and_misfiled_names(tree: Path) -> None:
    (tree / "skills/map-accounts/notes.txt").write_text("stray\n", encoding="utf-8")
    (tree / "skills/map-accounts/tables/sub").mkdir()
    (tree / "skills/map-accounts/tables/sub/x.yaml").write_text("a: 1\n", encoding="utf-8")
    err = refused(skill, tree, "map-accounts")
    has(err, "layout", "notes.txt", "")
    has(err, "layout", "tables/sub/x.yaml", "")
    only(err, "layout")


def test_code_layout_id_must_match_its_file_and_sponsor_its_folder(tree: Path) -> None:
    edit(tree, "skills/map-accounts/tables/coa-cascade.yaml", "id: coa-cascade", "id: coa-cascade-2")
    err = refused(skill, tree, "map-accounts")
    has(err, "layout", "coa-cascade.yaml", "id")
    edit(tree, "templates/sponsors/sponsor-a/recipes/Commitments.yaml", "sponsor: sponsor-a", "sponsor: sponsor-b")
    err = refused(rf.load_file, tree / "templates/sponsors/sponsor-a/recipes/Commitments.yaml", repo_root=tree)
    has(err, "layout", "Commitments.yaml", "sponsor")


def test_no_code_file_is_allowed_in_a_skill_folder(tree: Path) -> None:
    scripts = tree / "skills/map-accounts/scripts"
    scripts.mkdir()
    (scripts / "map_keys.py").write_text("x = 1", encoding="utf-8")
    has(refused(skill, tree, "map-accounts"), "layout", "map_keys.py")


# ---- YAML strictness --------------------------------------------------------------------------------------
def test_yaml_anchors_and_aliases_are_refused() -> None:
    err = refused(rf.load_file, FIX / "bad/anchor/skills/map-accounts/tables/coa-cascade.yaml",
                  repo_root=FIX / "bad/anchor")
    p = has(err, "wrong_type", "coa-cascade.yaml", "inputs")
    assert "anchor" in p.message


def test_yaml_syntax_error_and_tags_are_refused(tree: Path) -> None:
    edit(tree, "skills/map-accounts/checks.yaml", "kind: checks", "kind: checks\nbad: [unclosed")
    has(refused(skill, tree, "map-accounts"), "wrong_type", "checks.yaml", "")
    edit(tree, "skills/map-accounts/checks.yaml", "bad: [unclosed", "bad: !!python/object:os.system x")
    has(refused(skill, tree, "map-accounts"), "wrong_type", "checks.yaml")


# ---- table, checks, spec, synonym, case schemas -----------------------------------------------------------
def test_table_inputs_outputs_and_values_are_typed(tree: Path) -> None:
    f = "skills/map-accounts/tables/coa-cascade.yaml"
    edit(tree, f, "action: apply, confidence: 0.97", "action: maybe, confidence: high")
    err = refused(skill, tree, "map-accounts")
    has(err, "wrong_type", "coa-cascade.yaml", "rows[0].then.action")
    has(err, "wrong_type", "coa-cascade.yaml", "rows[0].then.confidence")


def test_table_unknown_input_and_output_names(tree: Path) -> None:
    f = "skills/map-accounts/tables/coa-cascade.yaml"
    edit(tree, f, "when: {historic: true}", "when: {historical: true}")
    edit(tree, f, "then: {tier: L5 No match, action: needs_you}", "then: {tier: L5 No match, verdict: needs_you}")
    err = refused(skill, tree, "map-accounts")
    has(err, "unknown_reference", "coa-cascade.yaml", "rows[0].when.historical")
    has(err, "unknown_reference", "coa-cascade.yaml", "rows[4].then.verdict")


def test_table_enum_needs_values_and_csv_is_a_type(tree: Path) -> None:
    edit(tree, "skills/map-accounts/tables/coa-cascade.yaml", ", values: [apply, suggest, needs_you], unseen: [apply]",
         "")
    err = refused(skill, tree, "map-accounts")
    has(err, "missing_field", "coa-cascade.yaml", "outputs.action.values")


def test_checks_guard_needs_enforced_by_and_placeholders_are_closed(tree: Path) -> None:
    edit(tree, "skills/map-accounts/checks.yaml", " enforced_by: worker.checks,\n     message: \"the account number",
         "\n     message: \"the account number")
    edit(tree, "skills/check-data-quality/checks.yaml", '"{field} is blank"', '"{fieldd} is blank"')
    with pytest.raises(rf.SchemaError) as e:
        rf.load_skills(tree / "skills", repo_root=tree)
    err = e.value
    has(err, "missing_field", "map-accounts/checks.yaml", "rules[0].enforced_by")
    has(err, "unknown_reference", "check-data-quality/checks.yaml", "rules[0].message")


def test_specs_selector_and_enum_and_derived_rules(tree: Path) -> None:
    f = "skills/reconcile-gl/specs.yaml"
    edit(tree, f, "[{fsg: pnl}]", "[{fsgg: pnl}]")
    edit(tree, f, "unmatched: zero\n  rule: 'PASS up to {pass}'", "unmatched: guess\n  rule: 'PASS up to {pss}'")
    edit(tree, f, "op: {label: Net cash from operating, section: operating}",
         "op: {label: Net cash from operating, section: operating, of: [[ni, 1]]}")
    err = refused(skill, tree, "reconcile-gl")
    has(err, "unknown_operator", "specs.yaml", "checks[1].measures.ni.where[0].fsgg")
    has(err, "wrong_type", "specs.yaml", "checks[1].unmatched")
    has(err, "unknown_reference", "specs.yaml", "checks[1].rule")
    has(err, "wrong_type", "specs.yaml", "checks[1].derived.op")


def test_specs_may_not_be_overlaid(tree: Path) -> None:
    base = tree / "templates/sponsors/sponsor-a/skills/reconcile-gl"
    base.mkdir(parents=True)
    shutil.copy(tree / "skills/reconcile-gl/specs.yaml", base / "specs.yaml")
    has(refused(skill, tree, "reconcile-gl", overlay_dir=base), "overlay_forbidden", "specs.yaml")


def test_synonyms_duplicate_header_and_unknown_report_column(tree: Path) -> None:
    edit(tree, "skills/reconcile-gl/synonyms.yaml", '["investor", "investor name", "lp"]',
         '["investor", "Investor", "lp"]')
    edit(tree, "skills/reconcile-gl/synonyms.yaml", "  amount:", "  colour:")
    err = refused(skill, tree, "reconcile-gl")
    has(err, "duplicate_id", "synonyms.yaml", "concepts.investor[1]")
    has(err, "unknown_reference", "synonyms.yaml", "concepts.colour")


def test_synonyms_same_header_under_two_fields_is_allowed(tree: Path) -> None:
    pin = skill(tree, "build-static-records")
    c = next(iter(pin.files.values())).data["concepts"]
    assert "Row Labels" in c["Investor_ID"] and "Row Labels" in c["Source_Investor_Key"]


def test_cases_runner_and_fixture_references(tree: Path) -> None:
    f = "skills/reconcile-gl/cases/known-gl.json"
    edit(tree, f, '"ledger": "fund"', '"ledger": "nofund"')
    has(refused(skill, tree, "reconcile-gl"), "unknown_reference", "known-gl.json", "cases[0].ledger")
    edit(tree, f, '"ledger": "nofund"', '"ledger": "fund"')
    edit(tree, f, '"check": "tb"', '"check": "zz"')
    has(refused(skill, tree, "reconcile-gl"), "unknown_reference", "known-gl.json", "cases[0].check")
    edit(tree, f, '"runner": "reconcile-gl.check"', '"runner": "reconcile-gl.other"')
    has(refused(skill, tree, "reconcile-gl"), "unknown_operator", "known-gl.json", "runner")


# ---- target and recipe ------------------------------------------------------------------------------------
@pytest.mark.parametrize("old,new,code,pointer", [
    ("generation: {mode: source}", "generation: {mode: custom}", "missing_field", "controls.id_formats.generation.pattern"),
    ("generation: {mode: source}", "generation: {mode: mystery}", "wrong_type", "controls.id_formats.generation.mode"),
    ("name_match: exclude", "name_match: skip", "wrong_type", "controls.dedup.name_match"),
    ("record_type: Investors", "record_type: Mystery", "unknown_reference", "controls.depends[0].record_type"),
    ("[GP_ID, Fund_ID, Investor_ID, Commitment_Amount]", "[GP_ID, Fund_ID, Investor_ID, Nope]",
     "unknown_reference", "controls.required[3]"),
    ("output: xlsx", "output: pdf", "wrong_type", "output"),
])
def test_target_fields(tree: Path, old: str, new: str, code: str, pointer: str) -> None:
    edit(tree, "templates/Commitments.target.yaml", old, new)
    err = refused(rf.load_file, tree / "templates/Commitments.target.yaml", repo_root=tree, context=CTX)
    has(err, code, "Commitments.target.yaml", pointer)


@pytest.mark.parametrize("old,new,code,pointer", [
    ("      - {op: case, to: upper}\n", "      - {op: shout}\n", "unknown_operator", "fields.Investor_ID.ops[1].op"),
    (('    from: "Total Commitment"\n    ops:\n      - {op: number_parse, decimal: ".", thousands: [","], '
     'negative: [minus, brackets], currency: ["$"]}'),
     '    from: "Total Commitment"\n    ops:\n      - {op: negate}', "wrong_type", "fields.Commitment_Amount.ops[0]"),
    ('patterns: ["DD/MM/YYYY", "MM/DD/YY"], yy_pivot: 50', 'patterns: ["DD/MM/YYYY", "MM/DD/YY"]',
     "missing_field", "fields.Commitment_Date.ops[0].yy_pivot"),
    ('patterns: ["DD/MM/YYYY", "MM/DD/YY"], yy_pivot: 50', 'patterns: ["DD/MM"]',
     "wrong_type", "fields.Commitment_Date.ops[0].patterns[0]"),
    ("{missing: {reason: \"the source has no fund column\"}}", "{from: \"Fund\", supply: {value: \"F-1\"}}",
     "wrong_type", "fields.Fund_ID"),
    ("GP_ID: {supply: {value: \"GP-001\"}}", "GP_ID: {supply: {value: \"GP-001\"}, ops: [{op: trim}]}",
     "unknown_field", "fields.GP_ID.ops"),
    ("      - {op: trim, inner: collapse}\n", "      - {op: regex_extract, pattern: \"(a|b)\"}\n",
     "unknown_operator", "fields.Investor_ID.ops[0].pattern"),
    ("      - {op: crosswalk, records: Investors, match_on: key, take: id, on_miss: missing}\n",
     "      - {op: join, parts: [{setup: office_id}]}\n", "unknown_reference", "fields.Investor_ID.ops[2].parts[0].setup"),
    ("on_miss: missing}", "on_miss: keep}", "wrong_type", "fields.Investor_ID.ops[2].on_miss"),
    ("Commitment_Amount:\n    from", "Commitment_Nonsense:\n    from", "unknown_reference",
     "fields.Commitment_Nonsense"),
    ("approved_at: \"2026-10-11T00:00:00Z\"", "approved_at: \"yesterday\"", "wrong_type", "approved_at"),
])
def test_recipe_fields(tree: Path, old: str, new: str, code: str, pointer: str) -> None:
    rel = "templates/sponsors/sponsor-a/recipes/Commitments.yaml"
    edit(tree, rel, old, new)
    err = refused(rf.load_file, tree / rel, repo_root=tree, context=CTX)
    has(err, code, "Commitments.yaml", pointer)


def test_recipe_bounds_are_out_of_bounds(tree: Path) -> None:
    rel = "templates/sponsors/sponsor-a/recipes/Commitments.yaml"
    many = "\n".join("      - {op: trim}" for _ in range(9))
    edit(tree, rel, "      - {op: trim, inner: collapse}\n", many + "\n")
    err = refused(rf.load_file, tree / rel, repo_root=tree, context=CTX)
    has(err, "out_of_bounds", "Commitments.yaml", "fields.Investor_ID.ops")


def test_recipe_supply_must_be_canonical_for_the_field(tree: Path) -> None:
    rel = "templates/sponsors/sponsor-a/recipes/Commitments.yaml"
    edit(tree, rel, "  GP_ID: {supply: {value: \"GP-001\"}}\n",
         "  GP_ID: {supply: {value: \"GP-001\"}}\n  Commitment_Date2: {missing: {reason: x}}\n")
    # a *_Date field supplied as a string a person typed in another format is refused, never reinterpreted
    text = (tree / rel).read_text(encoding="utf-8").replace("Commitment_Date2: {missing: {reason: x}}",
                                                            'Closing_Date: {supply: {value: "31/03/2025"}}')
    (tree / rel).write_text(text, encoding="utf-8")
    err = refused(rf.load_file, tree / rel, repo_root=tree, context=CTX)
    has(err, "wrong_type", "Commitments.yaml", "fields.Closing_Date.supply.value")


def test_recipe_ops_flow_types_between_ops() -> None:
    def load(ops: str, field: str = "Commitment_Date") -> Any:
        text = ("schema_version: 1\nkind: recipe\nid: Commitments\nsponsor: sponsor-a\nfields:\n"
                f"  {field}:\n    from: Src\n    ops: {ops}\n"
                "approved_by: x\napproved_at: '2026-10-11T00:00:00Z'\nreason: y\n")
        return rf.load_text("templates/sponsors/sponsor-a/recipes/Commitments.yaml", text)

    load("[{op: date_parse, patterns: [YYYY-MM-DD]}]")
    load("[{op: trim}, {op: number_parse, decimal: '.'}, {op: negate}]", "Commitment_Amount")
    has(refused(load, "[{op: trim}]"), "wrong_type", pointer="fields.Commitment_Date.ops")  # text into a date
    has(refused(load, "[{op: date_parse, patterns: [YYYY-MM-DD]}, {op: number_parse, decimal: '.'}]"), "wrong_type",
        pointer="fields.Commitment_Date.ops[1]")
    has(refused(load, "[{op: number_parse, decimal: '.'}, {op: when, test: {left: value, cmp: gt, right: '0'}, "
                      "then: {text: x}, else: keep}]", "Commitment_Amount"), "wrong_type")
    has(refused(load, "[{op: when, test: {left: value, cmp: gt, right: '0'}, then: keep, else: blank}]",
                "Commitment_Amount"), "wrong_type")  # ordered compare needs a number first


@pytest.mark.parametrize("pattern,ok", [
    ("^(\\d{4})", True), ("([A-Z]+)-\\d+", True), ("(a|b)", False), ("(?:a)(b)", False), ("(\\d)+", False),
    ("a(b)(c)", False), ("(.)", False), ("(a)\\1", False), ("(a+?)", False), ("(\\d{51})", False), ("abc", False),
    ("(a)" + "b?" * 7, False),
])
def test_regex_safe_subset(pattern: str, ok: bool) -> None:
    assert (regex_problem(pattern) is None) is ok


# ---- three layers: bounds, controls, learnable ------------------------------------------------------------
def test_code_bounds_equal_the_ones_recon_enforces() -> None:
    from worker import recon

    mine, theirs = CONTROL_BOUNDS["reconcile-gl"], recon.CONTROL_LIMITS
    assert mine["tolerances"] == theirs["tolerances"]
    assert mine["levels"] == theirs["levels"]
    assert mine["required_floor"] == theirs["required_floor"]
    assert set(mine["fields"]) == set(theirs["fields"])
    assert mine["balance_target"] == theirs["balance_target"]


def test_control_numbers_are_decimals_not_floats(tree: Path) -> None:
    pin = skill(tree, "reconcile-gl")
    ctl = pin.files["skills/reconcile-gl/controls.yaml"].controls
    assert ctl["tolerances"]["pct"]["pass_up_to"] == Decimal("0.01")
    assert all(isinstance(v["pass_up_to"], Decimal) for v in ctl["tolerances"].values())


def test_controls_hash_ignores_comments_and_learnable_text_but_not_control_values(tree: Path) -> None:
    before = skill(tree, "check-data-quality")
    edit(tree, "skills/check-data-quality/controls.yaml", "# Floors", "# Edited floors")
    edit(tree, "skills/check-data-quality/checks.yaml", '"{field} is blank"', '"{field} is empty"')
    same = skill(tree, "check-data-quality")
    assert same.controls_hash == before.controls_hash
    edit(tree, "skills/check-data-quality/checks.yaml", "    severity: WARN\n", "    severity: ERR\n")
    assert skill(tree, "check-data-quality").controls_hash != before.controls_hash  # a rule's severity is a control
    edit(tree, "skills/check-data-quality/controls.yaml", "name_too_long: WARN", "name_too_long: ERR")
    assert skill(tree, "check-data-quality").controls_hash != same.controls_hash


def test_controls_hash_is_over_values_not_spelling(tree: Path) -> None:
    a = skill(tree, "reconcile-gl").controls_hash
    edit(tree, "skills/reconcile-gl/controls.yaml", "pct: {pass_up_to: 0.01, warn_up_to: 0.01}",
         "pct: {pass_up_to: 0.010, warn_up_to: 0.0100}")
    assert skill(tree, "reconcile-gl").controls_hash == a
    edit(tree, "skills/reconcile-gl/controls.yaml", "money-flat: {pass_up_to: 1, warn_up_to: 1}",
         "money-flat: {pass_up_to: 2, warn_up_to: 2}")
    assert skill(tree, "reconcile-gl").controls_hash != a


def test_canonical_json_is_sorted_compact_and_decimal_as_text() -> None:
    assert rf.canonical_json({"b": Decimal("1.50"), "a": [Decimal(0), Decimal("-0")]}) == '{"a":["0","0"],"b":"1.5"}'


def test_control_rows_of_a_table_are_in_the_hash(tree: Path) -> None:
    a = skill(tree, "map-accounts").controls_hash
    edit(tree, "skills/map-accounts/tables/coa-cascade.yaml", "confidence: 0.97", "confidence: 0.96")
    assert skill(tree, "map-accounts").controls_hash != a  # L0 passes unseen, so it is a control row
    b = skill(tree, "map-accounts").controls_hash
    edit(tree, "skills/map-accounts/tables/coa-cascade.yaml", '">=0.5"}, then: {tier: L3', '">=0.6"}, then: {tier: L3')
    assert skill(tree, "map-accounts").controls_hash == b  # L3 is learnable


# ---- the files as they are today --------------------------------------------------------------------------
def _current_files() -> list[Path]:
    return sorted(p for p in (REPO / "skills").rglob("*") if p.suffix in (".yaml", ".json") and p.is_file())


def test_a_skill_file_without_its_header_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    shutil.copytree(REPO / "skills" / "map-accounts", root / "skills" / "map-accounts")
    p = root / "skills" / "map-accounts" / "tables" / "coa-cascade.yaml"
    text = p.read_text(encoding="utf-8")
    p.write_text(text.replace("schema_version: 1\nkind: table\n", "", 1), encoding="utf-8")
    err = refused(rf.load_skill, root / "skills" / "map-accounts", repo_root=root)
    has(err, "missing_field", "skills/map-accounts/tables/coa-cascade.yaml", "schema_version")


def test_every_skill_file_loads_strictly() -> None:
    """Every file under skills/** carries its header and loads."""
    files = _current_files()
    assert len(files) >= 14
    for p in files:
        got = rf.load_file(p, repo_root=REPO)
        assert got.schema_version == 1, p
        assert got.kind == rf.expected_kind(p.relative_to(REPO).as_posix()), p
        head = (json.loads(p.read_text(encoding="utf-8")) if p.suffix == ".json"
                else yaml.safe_load(p.read_text(encoding="utf-8")))
        assert head["schema_version"] == 1 and head["kind"] == got.kind and "version" not in head, p
    pins = rf.load_skills(REPO / "skills", repo_root=REPO)
    assert {"map-accounts", "reconcile-gl", "check-data-quality"} <= set(pins)
    for p in files:
        if p.suffix == ".yaml":
            assert not re.search(r"(?m)^[^#\n]*[:-]\s+[&*]\w", p.read_text(encoding="utf-8")), p  # no anchors


def test_every_current_skill_loads_as_a_pin_and_ids_are_unique_across_skills() -> None:
    pins = rf.load_skills(REPO / "skills", repo_root=REPO)
    assert {"map-accounts", "reconcile-gl", "check-data-quality"} <= set(pins)
    ids = [f.id for p in pins.values() for f in p.files.values() if f.kind == "table"]
    assert len(ids) == len(set(ids)) >= 7
    assert all(len(p.controls_hash) == 64 for p in pins.values())


def test_real_checks_and_recon_controls_agree_with_the_layers() -> None:
    """The values the repo runs on today satisfy every layer: nothing here is looser than its control."""
    pins = rf.load_skills(REPO / "skills", repo_root=REPO)
    ctl = pins["reconcile-gl"].files["skills/reconcile-gl/controls.yaml"].controls
    assert ctl["balance"]["target"] == Decimal(0)
    assert ctl["tolerances"]["money-tiered"]["warn_up_to"] == Decimal(5)
