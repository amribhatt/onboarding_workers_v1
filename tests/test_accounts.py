from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import openpyxl
import pytest
import yaml
from langchain_core.messages import SystemMessage
from langgraph.checkpoint.memory import InMemorySaver

from scripted_model import ScriptedModel, approve, call, say
from worker.accounts import AccountStore, Coa, TypeMap, key_of, map_keys
from worker.agent import build_agent, decide, pending_gate, start
from worker.audit import AuditLog
from worker.context import RunContext
from worker.decide import Table, TableError
from worker.review import review
from worker.skills import catalog, load_skills, skills_dir
from worker.staging import Stage
from worker.templates import standards_dir

SKILL = load_skills()["map-accounts"]
CASES = json.loads((SKILL.path / "cases" / "cascade.json").read_text(encoding="utf-8"))


def _engine() -> tuple[Coa, TypeMap, Table]:
    base = standards_dir()
    return Coa.load(base / "Golden_COA.csv"), TypeMap.load(base / "Transaction_Type_Mapping.csv"), SKILL.table(
        "coa-cascade"
    )


def _run(cases: list[dict[str, Any]], store: dict[str, Any], table: Table | None = None) -> dict[str, Any]:
    coa, types, tbl = _engine()
    keys = {
        key_of(c["account_type"], c["account"], c["transaction_type"]): {
            "account_type": c["account_type"], "account": c["account"],
            "transaction_type": c["transaction_type"], "rows": 3,
        }
        for c in cases
    }
    results = map_keys(keys, coa, types, store, {}, table or tbl)
    return {r.key: r for r in results}


@pytest.mark.parametrize("case", CASES["cases"], ids=[c["name"] for c in CASES["cases"]])
def test_each_tier_fires_on_its_fixture(case: dict[str, Any]) -> None:
    got = _run([case], CASES["sponsor_store"])[key_of(case["account_type"], case["account"], case["transaction_type"])]
    for field, value in case["expect"].items():
        assert getattr(got, field) == value, (field, got.as_dict())
    if case["expect"]["action"] == "needs_you":
        assert 0 < len(got.candidates) <= 5  # the top candidates go to the person


def test_no_account_is_produced_that_is_not_in_the_chart() -> None:
    coa, _, _ = _engine()
    got = _run(CASES["cases"], {"bank | 1000 | capital call": {"account": "99999", "source": "bad"}})
    for r in got.values():
        assert r.account == "" or r.account in coa
    assert got[key_of("Bank", "1000", "Capital call")].tier != "L0 Historic"  # a stored non-chart account is ignored


def test_disallowed_combination_is_an_err_finding_and_nothing_is_applied(tmp_path: Path) -> None:
    got = _run([CASES["cases"][-1]], CASES["sponsor_store"])
    only = next(iter(got.values()))
    assert only.action == "blocked" and "not allowed for transaction type" in only.flags[0]


def test_bank_accounts_share_one_decision_on_their_first_four_digits() -> None:
    assert key_of("Bank", "1000-01", "X") == key_of("Bank", "1000-02", "X")
    assert key_of("Expense", "1000-01", "X") != key_of("Expense", "1000-02", "X")


def test_sponsor_override_beats_the_organisation_row(tmp_path: Path) -> None:
    sponsor_dir = tmp_path / "sponsor-a"
    place = sponsor_dir / "skills" / "map-accounts" / "tables"
    place.mkdir(parents=True)
    (place / "coa-cascade.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "coa-cascade",
                "inputs": {"historic": "bool", "pinned": "bool", "tt_known": "bool", "score": "number"},
                "rows": [{"id": "L3", "when": {"tt_known": True, "score": ">=0.9"},
                          "then": {"tier": "L3 Hybrid", "action": "suggest"}}],
            }
        ),
        encoding="utf-8",
    )
    case = {"account_type": "Capital", "account": "3000 Contributions", "transaction_type": "Capital call"}
    org = next(iter(_run([case], {}).values()))
    own = next(iter(_run([case], {}, SKILL.table("coa-cascade", sponsor_dir)).values()))
    assert org.row == "L3" and own.row == "L5"  # the stricter sponsor row replaced the organisation's L3 in place


def test_decision_table_first_match_typed_inputs_and_the_row_that_fired() -> None:
    table = Table.from_dict(
        {
            "id": "t", "inputs": {"n": "number", "flag": "bool"},
            "rows": [
                {"id": "a", "when": {"flag": True}, "then": {"out": "A"}},
                {"id": "b", "when": {"n": ">=5"}, "then": {"out": "B"}},
                {"id": "c", "then": {"out": "C"}},
            ],
        }
    )
    assert table.decide({"n": 9, "flag": True}).row == "a"
    d = table.decide({"n": 9, "flag": False})
    assert (d.row, d.output["out"]) == ("b", "B") and "row b" in d.why
    assert table.decide({"n": 1, "flag": False}).row == "c"
    with pytest.raises(TableError):
        table.decide({"n": "9", "flag": False})
    with pytest.raises(TableError, match="was not given"):
        table.decide({"n": 1})


def test_only_the_frontmatter_is_in_the_system_prompt_and_scenarios_load_on_demand(tmp_path: Path) -> None:
    text = catalog(list(load_skills().values()))
    assert "map-accounts (v1.0)" in text
    assert "Never pick an account that is not in the candidates" not in text  # the body is not loaded
    assert "bank-accounts.md" not in text and "first 4 digits" not in text
    assert skills_dir().is_dir()


def _ctx(tmp_path: Path) -> RunContext:
    out = tmp_path / "run" / "out"
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    book = openpyxl.Workbook()
    ws = book.active
    ws.append(["Account", "Type", "Amount"])
    for acct, typ in [("1000", "Capital call"), ("3000 Contributions", "Capital call"), ("7777 Odd thing", "Quarterly review"),
                      ("1000", "Capital call")]:
        ws.append([acct, typ, "10"])
    book.save(inbox / "gl.xlsx")
    return RunContext(
        sponsor="sponsor-a", gp_id="GP-001", inbox_roots=[inbox], out_dir=out, audit=AuditLog(out / "audit.jsonl"),
        stage=Stage(out), accounts_root=tmp_path / "state" / "accounts",
    )


def _lift() -> dict[str, Any]:
    return call(
        "lift", path="gl.xlsx", sheet="Sheet", template="GL",
        columns={"Account_Name": "A", "Transaction_Type": "B", "Total_Amount": "C"},
        constants={"GP_ID": "GP-001"}, header_row=1, first_row=2, why="copy",
    )


def test_use_skill_loads_the_body_then_a_scenario_and_records_the_version(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    script = [
        say("load", call("use_skill", skill="map-accounts", why="GL job")),
        say("case", call("use_skill", skill="map-accounts", scenario="bank-accounts.md", why="sub-accounts seen")),
        say("done"),
    ]
    model = ScriptedModel(script=script)
    start(build_agent(ctx, model, InMemorySaver()), ctx, "map the accounts", "t1")
    system = next(m for m in model.seen[0] if isinstance(m, SystemMessage))
    assert "map-accounts (v1.0)" in str(system.content) and "first 4 digits" not in str(system.content)
    loaded = [e for e in ctx.audit.read() if e["action"] == "skill.loaded"]
    assert [(e["inputs"]["version"], e["inputs"]["scenario"]) for e in loaded] == [("1.0", None), ("1.0", "bank-accounts.md")]
    tool_texts = [str(m.content) for m in model.seen[-1] if m.type == "tool"]
    assert any("first 4 digits" in t for t in tool_texts)


def test_a_confirmed_pick_becomes_l0_next_run_and_unmapped_lines_block_approval(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    # a prior approval for the bank account makes line 1 and 4 L0
    AccountStore(ctx.accounts_root, "sponsor-a").add(  # type: ignore[arg-type]
        key_of("", "1000", "Capital call"), "11010", source="earlier job", reason="approved earlier"
    )
    script = [
        say("lift", _lift()),
        say("map", call("map_accounts", stage_id="s1", why="map the GL accounts")),
        say("write", call("write_upload", stage_id="s2", template="GL", why="deliver")),
        say("confirm", call("confirm_account_mappings", stage_id="s2",
                            picks={key_of("", "3000 Contributions", "Capital call"): "31010",
                                   key_of("", "7777 Odd thing", "Quarterly review"): "99999"},
                            why="human chose")),
        say("confirm again", call("confirm_account_mappings", stage_id="s2",
                                  picks={key_of("", "3000 Contributions", "Capital call"): "31010",
                                         key_of("", "7777 Odd thing", "Quarterly review"): "53010"},
                                  why="human chose")),
        say("write again", call("write_upload", stage_id="s3", template="GL", why="deliver")),
        say("done"),
    ]
    agent = build_agent(ctx, ScriptedModel(script=script), InMemorySaver())
    result = start(agent, ctx, "map", "t1")
    assert [g["name"] for g in pending_gate(result)] == ["write_upload"]
    rows = ctx.stage.rows("s2")
    assert [r["Account_Number"] for r in rows] == ["11010", "", "", "11010"]
    r = review(ctx, "s2")
    assert [f["code"] for f in r["findings"] if f["severity"] == "ERR"].count("account_unmapped") == 1
    result = decide(agent, ctx, "t1", {"type": "reject", "message": "not yet: I will tell you the accounts"})
    assert [g["name"] for g in pending_gate(result)] == ["confirm_account_mappings"]  # the first confirm is gated
    result = decide(agent, ctx, "t1", {"type": "approve"}, "approved the picks", {"reviewer": "reviewer-a"})
    # the first confirm had an account that is not in the chart: refused, so the agent confirms again (gated)
    assert [g["name"] for g in pending_gate(result)] == ["confirm_account_mappings"]
    refused = [e for e in ctx.audit.read() if e["action"] == "confirm_account_mappings.error"]
    assert refused and "not in the Golden chart" in refused[0]["outputs"]["error"]
    result = decide(agent, ctx, "t1", {"type": "approve"}, "approved the picks", {"reviewer": "reviewer-a"})
    assert [g["name"] for g in pending_gate(result)] == ["write_upload"]
    assert [r["Account_Number"] for r in ctx.stage.rows("s3")] == ["11010", "31010", "53010", "11010"]
    approve(agent, ctx, "t1")
    assert (tmp_path / "run" / "out" / "GL_Upload.xlsx").exists()
    stored = AccountStore(ctx.accounts_root, "sponsor-a").entries()  # type: ignore[arg-type]
    assert stored[key_of("", "3000 Contributions", "Capital call")]["account"] == "31010"
    # the next run finds those picks at L0
    coa, types, table = _engine()
    again = map_keys(
        {key_of("", "3000 Contributions", "Capital call"): {"account_type": "", "account": "3000 Contributions",
                                                              "transaction_type": "Capital call", "rows": 1}},
        coa, types, stored, {}, table,
    )
    assert again[0].row == "L0" and again[0].account == "31010"
    stamp = next(e for e in ctx.audit.read() if e["action"] == "job.versions")
    assert stamp["outputs"]["skills"] == {"map-accounts": "1.0"}


def test_a_model_only_ranks_the_candidates_code_produced() -> None:
    coa, types, table = _engine()
    key = key_of("", "7777 Odd thing", "Quarterly review")
    info = {key: {"account_type": "", "account": "7777 Odd thing", "transaction_type": "Quarterly review", "rows": 1}}
    first = map_keys(info, coa, types, {}, {}, table)[0]
    best = first.candidates[0]["account"]
    ranked = map_keys(info, coa, types, {}, {}, table, ranker=lambda pending: {key: best})[0]
    assert ranked.account == best and ranked.action == "suggest" and "ranked by the model" in ranked.evidence
    invented = map_keys(info, coa, types, {}, {}, table, ranker=lambda pending: {key: "12345"})[0]
    assert invented.account == "" and invented.action == "needs_you"


def test_a_stored_mapping_needs_a_reason_and_golden_changes_only_through_cards(tmp_path: Path) -> None:
    store = AccountStore(tmp_path, "sponsor-a")
    store.add("k", "11010", source="job", reason="approved")
    assert not hasattr(AccountStore, "promote")  # the unaudited admin shortcut is gone (ticket 07 s3)
    with pytest.raises(ValueError, match="reason is required"):
        store.add("k2", "11010", source="job", reason="  ")


# ---- the script's logic lives in the engine (M9) --------------------------------------------------------
def test_map_keys_engine_equals_script() -> None:
    """`skills/map-accounts/scripts/map_keys.py` is gone. Its output on the cases' keys was recorded from the script
    before it was deleted (`tests/fixtures/map_keys_script_output.json`); the engine operation must give the same
    JSON, key for key, with the same row counts."""
    fixtures = Path(__file__).parent / "fixtures"
    coa, types, tbl = _engine()
    keys: dict[str, dict[str, Any]] = {}
    with open(fixtures / "map_keys_keys.csv", newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            k = key_of(r["account_type"], r["account"], r["transaction_type"])
            keys.setdefault(k, {**r, "rows": 0})["rows"] += 1
    got = map_keys(keys, coa, types, {}, {}, tbl)
    recorded = json.loads((fixtures / "map_keys_script_output.json").read_text(encoding="utf-8"))
    assert [r.as_dict() for r in got] == recorded
    assert [r["rows"] for r in recorded] == [3, 2, 1, 1, 1, 1]  # repeated keys are counted, not repeated
    assert not (SKILL.path / "scripts").exists()
