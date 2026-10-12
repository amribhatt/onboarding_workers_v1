import openpyxl
import pytest

from worker.templates import TEMPLATES, Status, build_coverage, templates_dir

NAMES = ["Investors", "Commitments", "GL", "Affiliates", "Vendors", "Investments"]


@pytest.mark.parametrize("name", NAMES)
def test_fields_equal_file_header(name):
    wb = openpyxl.load_workbook(templates_dir() / f"{name}_Upload_Template.xlsx", read_only=True)
    header = next(wb.worksheets[0].iter_rows(min_row=1, max_row=1, values_only=True))
    assert TEMPLATES[name].fields == tuple(h for h in header if h)
    assert set(TEMPLATES[name].required) <= set(TEMPLATES[name].fields)


def test_required_defaults_to_first_two():
    assert TEMPLATES["Vendors"].required == TEMPLATES["Vendors"].fields[:2]


def _report():
    staged = {
        "GP_ID": ["GP-001", "GP-001", "GP-001"],
        "Investor_ID": ["A", "B", "C"],
        "Investor_Name": ["x", "", None],
        "Investor_Type": ["LP", "LP", "LP"],
    }
    return build_coverage(
        "Investors",
        staged,
        sources={
            "GP_ID": "const",
            "Investor_ID": "col A",
            "Investor_Name": "col B",
            "Investor_Type": "col C",
        },
        assumed={"Country": "US"},
        value_checks={"Investor_Type": "unknown codes"},
    )


def test_statuses():
    f = _report().fields
    assert f["GP_ID"].status is Status.MATCHED
    assert f["Investor_Type"].status is Status.CHECK_VALUES
    assert f["Country"].status is Status.ASSUMED
    assert f["Investor_Status"].status is Status.MISSING


def test_filled_total():
    c = _report().fields["Investor_Name"]
    assert (c.filled, c.total) == (1, 3)


def test_markdown_missing_first_and_required_gap():
    staged = {"GP_ID": ["GP-001"]}
    r = build_coverage("Investors", staged, {"GP_ID": "const"}, {}, {})
    assert r.missing_required() == ["Investor_ID", "Investor_Name"]
    md = r.to_markdown()
    assert md.index("INVESTOR_ID") < md.index("GP_ID: MATCHED")
    assert "can build: no (missing required: Investor_ID, Investor_Name)" in md
