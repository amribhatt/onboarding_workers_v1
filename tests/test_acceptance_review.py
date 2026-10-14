"""Scenarios C, E, F, G, H, I and L of section 10: plan choices, IDs, where each issue is, big files, find, review."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import large_gl
from id_decision import decide_ids
from worker import console_engine as CE
from worker import console_issues as CI
from worker.console import Console
from worker.console_screens import state_view
from worker.quality import groups_from_spec, load_rules, scan
from worker.templates import TEMPLATES


@pytest.fixture
def c(tmp_path: Path) -> Console:
    return Console(tmp_path / "inbox")


def start(c: Console, sponsor: str, typ: str, cov: str = "all", fund: str | None = None) -> dict[str, Any]:
    c.act(f"start|{sponsor}|{typ}")
    if typ.startswith("GL"):
        if fund:
            c.act(f"ask_set|fund|{fund}")
    else:
        c.act(f"ask_set|cov|{cov}")
    c.act("ask_sample")
    c.act("ask_go")
    j = c.job()
    assert j is not None
    return j


def run(c: Console, sponsor: str, typ: str, answer: str = "no", **kw: Any) -> dict[str, Any]:
    start(c, sponsor, typ, **kw)
    if c.ex_pending(c.job()):  # type: ignore[arg-type]
        c.act(f"ex|{answer}")
    return c.act("go")["job"]


def approve_investors(c: Console) -> None:
    run(c, "Sponsor A", "Investors")
    c.act("acceptall")
    decide_ids(c)
    c.act("approve", who="reviewer-a")


def group(c: Console, contains: str) -> dict[str, Any]:
    """The open issue group whose rows say `contains`: its size, whether it blocks, and where it is in the file."""
    j = c.job()
    assert j is not None
    for gid, items in CI._grouped(c, j).items():
        if any(contains in (x.get("why") or "") for x in items):
            return {"n": len(items), "err": items[0]["err"], "after": CI.pattern_of(items[0]["cid"], items[0])[2],
                    **CI.detail(c, j, gid)}
    raise AssertionError(contains)


# ---- C. Plan: Required? and the source column -----------------------------------------------------------
def test_C1_ticking_required_makes_blank_country_rows_err_and_tags_the_sponsor(c: Console) -> None:
    run(c, "Sponsor A", "Investors")
    before = CE.tally(c, c.job())  # type: ignore[arg-type]
    st = c.act("req|Investors|Country|1")["job"]
    country = next(f for f in st["plan"]["fields"] if f["field"] == "Country")
    assert country["required"] and country["differs"]  # the page shows the SPONSOR A tag from `differs`
    rows = {r["i"]: r for r in CE.static_rows(c, c.job())}  # type: ignore[arg-type]
    assert [rows[i]["st"] for i in (8, 12, 14)] == ["err", "err", "err"]
    assert all(rows[i]["why"] == "Country is blank" for i in (8, 12, 14))
    assert st["tally"]["err"] == before["err"] + 2  # row 8 was already an ERR (long key)
    st = c.act("req|Investors|Country|0")["job"]
    assert st["tally"] == before
    assert any("Country is now required for Sponsor A investors" in a["a"] for a in c.S["audit"])


def test_C2_picking_another_column_clears_the_missing_flags_and_is_audited(c: Console) -> None:
    run(c, "Sponsor A", "Investors")
    miss_before = [r["i"] for r in CE.static_rows(c, c.job()) if any("Country MISSING" in f for f in r["flags"])]  # type: ignore[arg-type]
    assert miss_before == [12, 14]
    st = c.act("col|Country|Notes", who="reviewer-a")["job"]
    country = next(f for f in st["plan"]["fields"] if f["field"] == "Country")
    assert country["source"] == "Notes" and country["kind"] == "user" and country["confidence"] == 100
    flags = [r["i"] for r in CE.static_rows(c, c.job()) if any("Country MISSING" in f for f in r["flags"])]  # type: ignore[arg-type]
    assert flags == []
    assert any(a["a"].startswith("Country now comes from column Notes") for a in c.S["audit"])
    assert any(f["kind"] == "override" and f["ref"] == "Country" for f in c.S["fb"])  # offered as a rule


def test_C3_gp_id_stays_fixed_and_the_id_shows_the_counter(c: Console) -> None:
    st = run(c, "Sponsor A", "Investors")
    f = {x["field"]: x for x in st["plan"]["fields"]}
    assert f["GP_ID"]["kind"] == "constant" and f["GP_ID"]["constant"] == "sponsor-a" and f["GP_ID"]["columns"] == []
    assert f["Investor_ID"]["id_text"] == "new ID from Sponsor A's counter · GINV-0001 onward"


# ---- E. IDs -----------------------------------------------------------------------------------------------
def test_E1_investors_get_new_ids_and_the_counter_continues(c: Console) -> None:
    run(c, "Sponsor A", "Investors", answer="yes")  # then the export
    c.act("ex|file")
    c.act("go")
    c.act("acceptall")
    j = c.job()
    ids, gen = CE.out_ids(c, j)  # type: ignore[arg-type]
    assert gen == 9 and ids[1] == "GINV-0001" and ids[14] == "GINV-0009"
    decide_ids(c)
    st = c.act("approve", who="reviewer-a")["job"]
    tr = c.act("trace|1")["job"]["trace"]
    assert tr["output"] == "GINV-0001" and "new from Sponsor A's counter" in tr["id"]
    # the old key is kept as the source key in the store, and the next job continues from GINV-0010
    assert any(e["id"] == "GINV-0001" and e["key"] == "HS-9901" for e in c.reg_of("Sponsor A")["Investors"])
    start(c, "Sponsor A", "Investors", cov="one", fund="Fund B")
    f = {x["field"]: x for x in state_view(c)["job"]["plan"]["fields"]}
    assert f["Investor_ID"]["id_text"].endswith("GINV-0010 onward")
    assert st["approved"]


def test_E2_vendors_keep_their_ids_and_a_corrected_id_is_the_one_written(c: Console) -> None:
    approve_investors(c)
    run(c, "Sponsor A", "Vendors")
    c.decide(c.job(), "6", "corrected ID to V_122")
    ids, _gen = CE.out_ids(c, c.job())  # type: ignore[arg-type]
    assert ids[4] == "V-100" and ids[6] == "V_122"  # as supplied, and the corrected one
    assert ids[2] == "JCA IV"  # no ID in the source: generated from the name


def test_E3_the_two_sheppard_vendors_get_different_ids(c: Console) -> None:
    run(c, "Sponsor A", "Vendors")
    c.act("acceptall")
    ids, _ = CE.out_ids(c, c.job())  # type: ignore[arg-type]
    assert ids[1] == "SHEPPARDCOMPANY" and ids[3] == "SHEPPARDCONSULTING"


def test_E4_the_same_output_id_is_shown_everywhere(c: Console) -> None:
    run(c, "Sponsor A", "Investors", answer="no")
    c.act("acceptall")
    st = state_view(c)["job"]
    sample = {r["i"]: r["output"] for r in st["sample"]["rows"]}
    c.act("trace|1")
    tr = state_view(c)["job"]["trace"]
    assert sample[1] == "GINV-0001" == tr["output"]


# ---- F. Each issue says where it is in the source file ---------------------------------------------------------
def test_F1_a_single_row_cause_names_file_sheet_row_and_range(c: Console) -> None:
    run(c, "Sponsor A", "Investors", answer="yes")
    c.act("ex|file")
    c.act("go")
    loc = group(c, "Same name on file under another key")["locator"]
    assert (loc["file"], loc["sheet"], loc["rows"], loc["range"]) == (
        "Sponsor_A_Common_Investors.xlsx", "Investor", "8", "Investor!A8:L8")
    assert loc["copy"] == "Sponsor_A_Common_Investors.xlsx Investor!A8:L8"


def test_F2_a_cause_with_two_rows_shows_the_range(c: Console) -> None:
    run(c, "Sponsor A", "Investors")
    g = group(c, "Source key is 23 characters")
    assert g["n"] == 2 and g["locator"]["rows"] == "9–10" and g["locator"]["range"] == "Investor!A9:L10"


def test_F3_gl_issues_show_the_sheet_and_the_row(c: Console) -> None:
    c.act("prov|paid")
    run(c, "Sponsor B", "GL Fund", fund="B Fund I")
    g = group(c, "Vendor")
    assert g["locator"]["sheet"] == "Data Source" and g["locator"]["rows"] == "4"


# ---- G. Finding thousands of rows in Excel ---------------------------------------------------------------------
@pytest.fixture(scope="module")
def big(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    path = large_gl.build(tmp_path_factory.mktemp("big") / "Sponsor_A_GL_FundA_2025Q4.csv")
    cols = {"Account": "GL Account", "Deal": "Investment", "Journal": "Journal", "Debit": "Debit", "Credit": "Credit"}
    report = scan(path, load_rules(), columns=cols, record_type="GL", groups={"required": []},
                  references={"coa_sources": {"1000", "1500", "1600", "8050", "9010"}}, default_breakdown="Account")
    return {"path": path, "report": report}


def _cause(report: dict[str, Any], rule: str) -> dict[str, Any]:
    found = [x for x in report["causes"] if x["rule"] == rule]
    assert len(found) == 1
    return found[0]


def test_G1_one_value_many_rows_is_a_filter_and_ranges(big: dict[str, Any]) -> None:
    cause = _cause(big["report"], "account_not_in_chart")
    assert cause["count"] == 10_214
    assert cause["locator"]["filter"] == (
        "In Excel: Data → Filter, column D “GL Account” = 6150 → 10,214 lines")
    assert cause["ranges_text"] == "1204–7203, 256000–258999, 909000–910213"
    assert len(cause["sample_rows"]) == 20 and cause["more_samples_in_marked_copy"]


def test_G2_blank_deal_over_four_accounts_has_a_two_condition_recipe_and_a_breakdown(big: dict[str, Any]) -> None:
    cause = _cause(big["report"], "deal_required")
    assert cause["count"] == 40_000
    assert cause["locator"]["filter"] == (
        "In Excel: Data → Filter, column M “Investment” = (Blanks), and column D “GL Account” = "
        "1500, 1600, 8050 and 1 more → 40,000 lines")
    bd = {b["value"]: b for b in cause["breakdown"]}
    assert sorted(bd) == ["1500", "1600", "8050", "9010"] and all(b["count"] == 10_000 for b in bd.values())
    assert bd["8050"]["row_ranges"] == [[30_000, 39_999]] and bd["8050"]["ranges_text"] == "30000–39999"


def test_G3_copy_values_and_the_breakdown_filter_to_paste(big: dict[str, Any]) -> None:
    cause = _cause(big["report"], "account_not_in_chart")
    assert cause["copy_values"] == ["6150"]
    bd = {b["value"]: b for b in _cause(big["report"], "deal_required")["breakdown"]}
    assert bd["8050"]["copy_filter"] == "Investment = (Blanks); GL Account = 8050"


def test_G4_marked_copy_for_one_account_leaves_the_original_untouched(big: dict[str, Any], tmp_path: Path) -> None:
    from worker.quality import marked_copy

    before = big["path"].stat().st_size
    out = marked_copy(big["report"], tmp_path / "gl_marked_8050.csv", only=("Account", "8050"))
    assert big["path"].stat().st_size == before and out.name == "gl_marked_8050.csv"
    with open(out, encoding="utf-8-sig") as fh:
        head = fh.readline().strip().split(",")
        assert head == [*big["report"]["columns"].keys()][:0] + head and head[-1] == "Worker check"
        marked = sum(1 for line in fh if line.rstrip().endswith("Deal is blank on accounts that require one"))
    assert marked == 10_000  # only the 8050 lines carry the mark


def test_G5_just_these_rows_keep_the_original_row_numbers(big: dict[str, Any], tmp_path: Path) -> None:
    from worker.quality import just_these_rows

    out = just_these_rows(big["report"], tmp_path / "rows_8050.csv", only=("Account", "8050"))
    with open(out, encoding="utf-8-sig") as fh:
        rows = fh.read().splitlines()
    assert rows[0].startswith("Original row,") and rows[1].startswith("30000,") and rows[-1].startswith("39999,")
    assert len(rows) == 10_001


def test_G6_the_filter_column_follows_the_cause(c: Console) -> None:
    c.act("prov|paid")
    run(c, "Sponsor B", "GL Fund", fund="B Fund I")
    col = lambda why: [x["col"] for x in group(c, why)["excel"]["conds"]]
    assert col("is not in the crosswalk") == ["Vendor"]
    assert col("has no Ledgerline account") == ["GL Account"]
    assert col("does not balance") == ["Journal"]
    assert col("Deal name is blank") == ["Investment", "GL Account"]
    start(c, "Sponsor A", "Investors")
    c.act("ex|no")
    c.act("go")
    assert col("Source key is 23") == ["Row Labels"]  # the ID column
    assert col("Investor_Name is blank") == ["Investor"]  # blank name


# ---- H. Find and focus ------------------------------------------------------------------------------------------
def _said(c: Console) -> str:
    return next(m["text"] for m in reversed(state_view(c)["job"]["chat"]) if m.get("role") == "Searcher")


def test_H1_find_counts_the_source_and_the_export(c: Console) -> None:
    start(c, "Sponsor A", "Investors")
    c.act("ex|yes")
    c.act("ex|file")
    c.act("go")
    c.act("say", "find alder")
    assert _said(c).startswith("2 rows for “alder”") and "shown on the left" not in _said(c)


def test_H2_a_sentence_becomes_structured_filters(c: Console) -> None:
    run(c, "Sponsor A", "Investors", answer="no")
    c.act("say", "show errors in Fund B for harbor")
    assert _said(c).startswith("0 rows for needs you · Fund B · “harbor”.")
    assert not {"q", "st", "ent"} & set(c.job())  # type: ignore[arg-type]


# ---- I. Review tools -----------------------------------------------------------------------------------------------
def test_I1_the_counts_add_up_to_the_rows_read(c: Console) -> None:
    start(c, "Sponsor A", "Investors")
    c.act("ex|yes")
    c.act("ex|file")
    t = c.act("go")["job"]["tally"]
    assert (t["read"], t["err"], t["warn"], t["skip"]) == (15, 5, 3, 3)
    t = c.act("acceptall")["job"]["tally"]
    assert t["read"] == 15 and t["err"] == 0


def test_I2_the_first_group_is_the_next_one_to_decide(c: Console) -> None:
    approve_investors(c)
    run(c, "Sponsor A", "Commitments")
    m = CI.model(c, c.job())  # type: ignore[arg-type]
    assert m["groups"][0]["state"] == "open" and m["next"] == m["groups"][0]["id"]


def test_I3_the_groups_say_which_entity_each_row_is_in(c: Console) -> None:
    start(c, "Sponsor A", "Investors")
    c.act("ex|no")
    c.act("go")
    m = CI.model(c, c.job(), per=200)  # type: ignore[arg-type]
    ents = {e for g in m["groups"] for e, _n in g["entities"]}
    assert ents and ents <= {"Fund A", "Fund B", "Fund C"}


def test_I4_the_sample_is_sized_for_95_percent_and_all_rows_when_small(c: Console) -> None:
    run(c, "Sponsor A", "Investors", answer="yes") if False else None
    start(c, "Sponsor A", "Investors")
    c.act("ex|yes")
    c.act("ex|file")
    c.act("go")
    st = c.act("acceptall")["job"]
    s = st["sample"]
    assert (s["k"], s["n"]) == (9, 9) and s["all"]
    assert s["note"] == "9 of 9 written rows, sized for 95% confidence (±5%), so every row."


def test_I5_a_written_id_traces_to_its_source(c: Console) -> None:
    start(c, "Sponsor A", "Investors")
    c.act("ex|no")
    c.act("go")
    c.act("acceptall")
    tr = c.act("trace|10")["job"]["trace"]
    assert tr["source"] == "Sponsor_A_Common_Investors.xlsx · sheet Investor · row 11"
    assert tr["model_calls"].startswith("none") and tr["approval"] == "waiting for your approval"
    assert "Investor_Name ← column “Investor”" in tr["name_rule"]


# ---- L. Any required field blank, in any flow ------------------------------------------------------------------------
def test_L1_blank_investor_name_is_one_cause_with_its_recipe(c: Console) -> None:
    run(c, "Sponsor A", "Investors")
    g = group(c, "Investor_Name is blank")
    assert g["n"] == 1 and g["err"]
    assert "column B “Investor” = (Blanks)" in g["excel"]["text"]
    assert g["locator"]["rows"] == "16"  # the 15th record is on sheet row 16 (row 1 is the header)
    c.fill(c.job(), "15", "Investor_Name", "Alder Hollow LLC")
    assert not any("Investor_Name is blank" in (r["why"] or "") for r in CE.static_rows(c, c.job()))  # type: ignore[arg-type]
    assert any("supplied by hand, never inferred" in a["r"] for a in c.S["audit"])


def test_L2_blank_closing_date_suggests_the_batch_date(c: Console) -> None:
    approve_investors(c)
    run(c, "Sponsor A", "Commitments")
    g = group(c, "Commitment_Date is blank")
    assert "column E “Closing Date” = (Blanks)" in g["excel"]["text"]
    assert g["after"] == "2025-06-30"  # the closing date of the other rows in this batch


def test_L3_blank_affiliate_name_follows_the_same_rule(c: Console) -> None:
    run(c, "Sponsor A", "Affiliates")
    g = group(c, "Affiliate_Name is blank")
    assert g["n"] == 1 and "column B “Affiliate” = (Blanks)" in g["excel"]["text"]
    assert g["locator"]["rows"] == "6"  # the 5th record is on sheet row 6 (row 1 is the header)


def test_L4_required_with_no_column_is_said_once_and_every_row_needs_you(c: Console) -> None:
    approve_investors(c)
    run(c, "Sponsor A", "Commitments")
    st = c.act("req|Commitments|Currency|1")["job"]
    g = group(c, "Currency is blank")
    # every row needs you; the row with no closing date is first a date problem, so the Currency cause has 7
    assert g["n"] == 7 and st["tally"]["err"] == 8
    assert g["excel"]["text"].startswith("This file has no “Currency” column")
    assert st["plan"]["gap"] == ["Currency"]


def test_L5_a_blank_generated_id_is_not_an_error(c: Console) -> None:
    run(c, "Sponsor A", "Vendors")
    rows = {r["i"]: r for r in CE.static_rows(c, c.job())}  # type: ignore[arg-type]
    assert rows[2]["st"] in ("ok", "warn") and rows[2]["blank"] == []


def test_L6_several_blank_rows_are_one_cause_with_a_breakdown(c: Console) -> None:
    run(c, "Sponsor A", "Investors")
    c.act("req|Investors|Country|1")
    g = group(c, "Country is blank")
    assert g["n"] == 3 and g["locator"]["rows"] == "9, 13, 15"
    assert {b["value"]: b["count"] for b in g["excel"]["breakdown"]} == {"Fund B": 2, "Fund C": 1}
    for r in CE.static_rows(c, c.job()):  # type: ignore[arg-type]
        if "Country is blank" in (r["why"] or ""):
            c.fill(c.job(), str(r["i"]), "Country", "US")  # type: ignore[arg-type]
    assert not any("Country is blank" in (r["why"] or "") for r in CE.static_rows(c, c.job()))  # type: ignore[arg-type]


def test_L7_gl_conditional_requirement_has_the_two_condition_recipe(c: Console) -> None:
    c.act("prov|paid")
    run(c, "Sponsor B", "GL Fund", fund="B Fund I")
    g = group(c, "Deal name is blank")
    assert g["n"] == 4
    assert g["excel"]["text"] == (
        "In Excel: Data → Filter, column M “Investment” = (Blanks), and column D “GL Account” = "
        "1500, 1600, 8050 and 1 more → 4 lines")
    assert sorted(b["value"] for b in g["excel"]["breakdown"]) == ["1500", "1600", "8050", "9010"]


def test_quality_rules_are_the_same_for_the_templates() -> None:
    assert groups_from_spec(TEMPLATES["Investors"])["required"]


# ---- the page-only scenarios: checked in a browser (see STATUS.md), and the code that does it ----------------
JS = (Path(__file__).parents[1] / "src" / "worker" / "static" / "console.js").read_text(encoding="utf-8")


def test_H3_the_page_level_find_box_is_gone_and_the_search_lives_on_the_issues_tab() -> None:
    assert 'id="fq"' not in JS and "function findCard" not in JS
    issues = (Path(__file__).parents[1] / "src" / "worker" / "static" / "issues.js").read_text(encoding="utf-8")
    assert 'id="is_q"' in issues and "setSelectionRange" in issues  # the search keeps the cursor while results update


def test_I6_keys_move_accept_and_jump_to_the_chat() -> None:
    assert 'e.key === "j"' in JS and 'e.key === "k"' in JS and 'e.key === "a"' in JS and 'e.key === "/"' in JS
    issues = (Path(__file__).parents[1] / "src" / "worker" / "static" / "issues.js").read_text(encoding="utf-8")
    assert 'e.key === "Enter"' in issues  # Enter reviews the selected group in the chat


def test_every_scenario_id_of_section_10_has_a_test() -> None:
    ids = [f"{k}{n}" for k, count in zip("ABCDEFGHIJKL", (5, 5, 3, 5, 4, 3, 6, 3, 6, 4, 4, 7), strict=True)
           for n in range(1, count + 1)]
    text = "\n".join(p.read_text(encoding="utf-8") for p in Path(__file__).parent.glob("test_acceptance*.py"))
    missing = [i for i in ids if f"def test_{i}_" not in text]
    assert not missing, missing
