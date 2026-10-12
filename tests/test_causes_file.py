"""Migration gaps, step 2: the console's causes and its starting rules are files, not code.

* `skills/check-data-quality/tables/causes.yaml` holds each console code's family, own severity and sentence (it was
  `engine_console.CAUSES`);
* a console job's starting rules read `nameMax` from the `name_too_long` rule of `checks.yaml` and the tolerances from
  the `balance` control of `controls.yaml` (they were `engine_console.DEFAULT_RULES`).

Changing the file changes what the engine says: that is the proof it is read from there.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from worker import engine_console as EC
from worker import skills as K

REPO = Path(__file__).resolve().parents[1]

# the table as it stood in code before the move (data in a test is fine; the point is that code holds none)
WAS = {
    "key_clash": ("existing", "ERR"), "name_clash": ("existing", "ERR"), "suffix_clash": ("existing", "ERR"),
    "similar_in_file": ("uniqueness", "ERR"), "required_blank": ("completeness", "ERR"),
    "missing_dependency": ("existing", "ERR"), "bad_id_chars": ("validity", "ERR"),
    "generated_id_collision": ("uniqueness", "ERR"), "gl_only_investment": ("consistency", "ERR"),
    "id_too_long": ("validity", "ERR"), "id_collision_after_cut": ("uniqueness", "ERR"),
    "unknown_investor": ("consistency", "ERR"), "reference_not_in_crosswalk": ("consistency", "ERR"),
    "account_not_in_chart": ("consistency", "ERR"), "transaction_type_unknown": ("validity", "ERR"),
    "deal_required": ("completeness", "ERR"), "journal_unbalanced": ("balance", "ERR"),
    "name_too_long": ("validity", "WARN"), "id_cut": ("validity", "WARN"), "id_prefix_dropped": ("validity", "WARN"),
    "optional_missing": ("completeness", "WARN"), "fuzzy_match": ("existing", "WARN"),
    "description_blank": ("completeness", "WARN"), "journal_within_tolerance": ("balance", "WARN"),
    "transform_failed": ("validity", "ERR"),
}


def test_every_cause_is_in_the_skill_file_with_its_family_and_severity() -> None:
    assert {c: (EC.cause(c)["family"], EC.cause(c)["severity"]) for c in WAS} == WAS
    assert all(EC.cause(c)["sentence"] for c in WAS)


def test_a_code_the_file_does_not_name_is_an_error_not_a_quiet_default() -> None:
    with pytest.raises(KeyError, match="not_a_cause_at_all"):
        EC.cause("not_a_cause_at_all")


def test_the_causes_come_from_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tree = tmp_path / "skills"
    shutil.copytree(REPO / "skills", tree)
    path = tree / "check-data-quality" / "tables" / "causes.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace(
        "sentence: \"A required field is blank\"", "sentence: \"A field the target needs is empty\""), encoding="utf-8")
    monkeypatch.setenv("WORKER_SKILLS_DIR", str(tree))
    assert EC.cause("required_blank")["sentence"] == "A field the target needs is empty"


def test_the_starting_rules_come_from_the_rule_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rules = EC.default_rules()
    quality = K.find_skill("check-data-quality")
    assert quality is not None
    name_rule = next(r for r in quality.checks()["rules"] if r["id"] == "name_too_long")
    assert rules["nameMax"] == name_rule["max"]
    tol = quality.controls()["balance"]["journal_unbalanced"]["tolerance"]
    assert rules["tolPass"] == rules["tolWarn"] == float(tol)
    tree = tmp_path / "skills"
    shutil.copytree(REPO / "skills", tree)
    checks = tree / "check-data-quality" / "checks.yaml"
    text = checks.read_text(encoding="utf-8")
    assert "    max: 100\n" in text
    checks.write_text(text.replace("    max: 100\n", "    max: 80\n"), encoding="utf-8")
    monkeypatch.setenv("WORKER_SKILLS_DIR", str(tree))
    assert EC.default_rules()["nameMax"] == 80


def test_a_caller_cannot_change_the_defaults_by_editing_what_it_got() -> None:
    mine = EC.default_rules()
    mine["nameMax"] = 5
    assert EC.default_rules()["nameMax"] != 5
