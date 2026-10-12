"""M11b: the cases of build-static-records (`runner: build-static-records.headers`) run against the engine, and they
are the sample source files' own columns."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import expected_rules as X
from worker import console_demo as D
from worker import rulefiles
from worker import sample as S
from worker.rule_specs import CASE_RUNNERS

REPO = Path(__file__).resolve().parents[1]
CASES = REPO / "skills" / "build-static-records" / "cases" / "headers.json"


def _doc() -> dict[str, Any]:
    return json.loads(CASES.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_the_runner_is_registered_and_the_file_loads() -> None:
    assert "build-static-records.headers" in CASE_RUNNERS
    got = rulefiles.load_file(CASES, repo_root=REPO)
    assert got.kind == "cases" and got.data["runner"] == "build-static-records.headers"
    pin = rulefiles.load_skill(REPO / "skills" / "build-static-records", repo_root=REPO)
    assert any(f.kind == "cases" for f in pin.files.values())


def test_the_cases_are_the_sample_files_columns() -> None:
    """One case per template field of the five types that the sample source file has a column for; the expected
    column is the one the sample plan (`CFG`) maps, and the headers are the sample file's own."""
    want = {}
    for typ in X.STATIC:
        for name, src, _req, _conf in D.plan(typ):
            if src in S.SRCCOLS[typ]:
                want[(typ, name)] = src
    got = {(c["record_type"], c["field"]): c["expect"]["column"] for c in _doc()["cases"]}
    assert got == want
    for c in _doc()["cases"]:
        assert c["headers"] == S.SRCCOLS[c["record_type"]]


@pytest.mark.parametrize(("patch", "code"), [
    ({"headers": "Investor"}, "wrong_type"),
    ({"headers": []}, "wrong_type"),
    ({"field": ""}, "wrong_type"),
    ({"record_type": "not a type"}, "wrong_type"),
    ({"expect": {"column": 5}}, "wrong_type"),
    ({"expect": {"colour": "x"}}, "unknown_field"),
    ({"unknown": 1}, "unknown_field"),
])
def test_a_case_file_with_a_bad_case_is_refused(patch: dict[str, Any], code: str) -> None:
    doc = _doc()
    doc["cases"] = [{**doc["cases"][0], **patch}]
    with pytest.raises(rulefiles.SchemaError) as err:
        rulefiles.load_text("skills/build-static-records/cases/headers.json", json.dumps(doc))
    assert code in {p.code for p in err.value.problems}
