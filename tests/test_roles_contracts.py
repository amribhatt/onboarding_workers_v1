"""Migration M14: the agent roles, evaluated per role with the scripted model (topology D7).

Hard zeros: an invented binding, a pick outside the candidates, a number that is not in the engine result. Each role
gets one repair attempt, then a Question built by code. Every hand-off is audited before it happens and every model
call is counted against the job's spend cap.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from openpyxl import Workbook

from scripted_model import ScriptedModel
from worker.audit import AuditError, AuditLog
from worker.context import Budget, BudgetExceeded, RunContext
from worker.contracts import (
    GATED_TOOLS,
    ContractError,
    JobUnit,
    ProfileColumn,
    SourceProfile,
)
from worker.roles import (
    READ_ONLY_TOOLS,
    ROLE_TOOLS,
    KeyItem,
    RoleError,
    RoleRunner,
    binding_options,
    draft,
    explain,
    make_brief,
    map_fields,
    numbers_in,
    profile_sheet,
    resolve_keys,
)
from worker.staging import Stage
from worker.templates import TemplateSpec
from worker.tools import build_tools
from worker.transforms import Row, rule_for

HANDOFFS = ("handoff.sent", "handoff.returned", "handoff.rejected")

INVESTORS = TemplateSpec(
    name="Investors",
    fields=("GP_ID", "Investor_ID", "Investor_Name", "Commitment_Date", "Commitment_Amount"),
    required=("GP_ID", "Investor_ID", "Investor_Name"),
)
VENDORS = TemplateSpec(
    name="Vendors", fields=("GP_ID", "Vendor_ID", "Vendor_Name"), required=("GP_ID", "Vendor_ID", "Vendor_Name")
)
TEMPLATES = {"Investors": INVESTORS, "Vendors": VENDORS}

PROFILE = SourceProfile(
    profile_id="p-sponsor-a-1",
    file_fp="fp-1",
    sheet="Investors",
    header_row=1,
    layout="flat",
    columns=(
        ProfileColumn(col_id="A", header="Inv ID", kind="id"),
        ProfileColumn(col_id="B", header="Inv Name", kind="text"),
        ProfileColumn(col_id="C", header="Trade Dt", kind="date"),
    ),
)
ROWS = [
    Row.of(f"Investors!{n}", {"Inv ID": f"INV-{n}", "Inv Name": f"Investor {name}", "Trade Dt": dt})
    for n, name, dt in ((2, "Alder", "13/04/2024"), (3, "Birch", "02/05/2024"), (4, "Cedar", "28/12/2023"))
]


def reply(obj: Any) -> AIMessage:
    return AIMessage(json.dumps(obj))


def actions(audit: AuditLog, only: tuple[str, ...] = HANDOFFS) -> list[str]:
    return [e["action"] for e in audit.read() if e["action"] in only]


def runner_for(tmp_path: Path, budget: Budget | None = None, **models: ScriptedModel) -> RoleRunner:
    return RoleRunner(audit=AuditLog(tmp_path / "audit.jsonl"), budget=budget, models=models, trace_id="t-1")


def binding(field: str, action: str, **kw: Any) -> dict[str, Any]:
    return {"field": field, "action": action, "reason": kw.pop("reason", "the header names it"),
            "confidence": kw.pop("confidence", 0.9), **kw}


# ---------------------------------------------------------------------------------------------------- Mapper


def test_mapper_invented_binding_is_a_hard_zero_then_one_repair_then_a_question(tmp_path: Path) -> None:
    invented = reply({"summary": "Bound the investor id.", "confidence": 0.5,
                      "output": {"bindings": [binding("Investor_ID", "map", col_id="Z")]}})
    model = ScriptedModel(script=[invented, invented, invented])
    runner = runner_for(tmp_path, mapper=model)
    out = map_fields(runner, unit_id="u-1", spec=INVESTORS, profile=PROFILE, rows=ROWS)

    assert len(model.seen) == 2  # the answer and ONE repair, never a third call
    assert "column Z is not in the sheet" in model.seen[1][-1].content  # the repair names the problem
    assert out.status == "needs_human" and out.result is not None and out.result.status == "needs_human"
    invented_kept = [b for b in out.decided.bindings if b.col_id == "Z"]
    assert invented_kept == []  # hard zero
    assert {q.subject[0].ref for q in out.questions} == {
        "Investor_ID", "Investor_Name", "Commitment_Date", "Commitment_Amount"
    }
    assert all(q.recommended is None for q in out.questions)
    assert [b.field for b in out.decided.bindings] == ["GP_ID"]  # only what code itself decided
    assert actions(runner.audit) == ["handoff.sent", "handoff.rejected", "handoff.sent", "handoff.rejected"]
    rejected = [e for e in runner.audit.read() if e["action"] == "handoff.rejected"]
    assert rejected[0]["inputs"]["schema_valid"] is True and rejected[0]["inputs"]["verify_result"] == "fail"


def test_mapper_target_first_keeps_clean_bindings_and_never_recommends_a_date_order(tmp_path: Path) -> None:
    plan = {"bindings": [
        binding("Investor_ID", "map", col_id="A"),
        binding("Investor_Name", "map", col_id="B"),
        binding("Commitment_Date", "transform", col_id="C",
                recipe=[{"op": "date_parse", "args": {"patterns": ["D/M/YYYY"]}}],
                reason="the dates read day first"),
        binding("Commitment_Amount", "missing", reason="no column holds an amount"),
    ]}
    alternatives = {"Commitment_Date": [binding(
        "Commitment_Date", "transform", col_id="C", recipe=[{"op": "date_parse", "args": {"patterns": ["M/D/YYYY"]}}],
        reason="the other reading")]}
    model = ScriptedModel(script=[reply({"summary": "Ids and names map as they are; the date order is yours.",
                                         "confidence": 0.8, "output": plan, "alternatives": alternatives})])
    runner = runner_for(tmp_path, mapper=model)
    out = map_fields(runner, unit_id="u-1", spec=INVESTORS, profile=PROFILE, rows=ROWS)

    assert len(model.seen) == 1
    assert actions(runner.audit) == ["handoff.sent", "handoff.returned"]
    bound = {b.field: b for b in out.decided.bindings}
    assert bound["Investor_ID"].col_id == "A" and bound["Investor_Name"].col_id == "B"
    assert bound["GP_ID"].action == "supply" and bound["GP_ID"].value_from == "setup"
    assert bound["Commitment_Amount"].action == "missing"
    assert "Commitment_Date" not in bound  # an order choice is never applied by code
    (q,) = out.questions
    assert q.kind == "unknown_format" and q.subject[0].ref == "Commitment_Date"
    assert q.recommended is None
    readings = [o for o in q.options if o.action == "transform"]
    assert len(readings) == 2 and all(o.preview.total == 0 for o in readings)
    assert readings[0].payload["evidence"] == {"conform": 3, "total": 3}  # day first reads every row
    assert q.options[-1].action == "declare_missing"
    assert out.result.status == "needs_human" and len(out.result.summary) <= 600


def test_mapper_makes_no_call_for_a_known_layout(tmp_path: Path) -> None:
    profile = SourceProfile(profile_id="p-2", file_fp="fp-2", sheet="Vendors", header_row=1, layout="flat",
                            columns=(ProfileColumn(col_id="A", header="Vendor_ID", kind="id"),
                                     ProfileColumn(col_id="B", header="Vendor Name", kind="text")))
    rows = [Row.of("Vendors!2", {"Vendor_ID": "V-1", "Vendor Name": "Larch Supplies"})]
    runner = runner_for(tmp_path)  # no model at all: a call would fail
    out = map_fields(runner, unit_id="u-2", spec=VENDORS, profile=profile, rows=rows)
    assert out.status == "code" and out.result is None and runner.calls == []
    assert [b.action for b in out.decided.bindings] == ["supply", "map", "map"]


def test_mapper_refuses_an_invented_constant_and_a_transform_outside_the_vocabulary(tmp_path: Path) -> None:
    bad = {"bindings": [
        binding("Investor_ID", "transform", col_id="A", recipe=[{"op": "default", "args": {"value": {"text": "X"}}}]),
        binding("Investor_Name", "transform", col_id="B", recipe=[{"op": "translate", "args": {}}]),
        binding("Commitment_Amount", "supply", value_from="setup"),
    ]}
    model = ScriptedModel(script=[reply({"summary": "Filled the gaps.", "confidence": 0.9, "output": bad})] * 2)
    runner = runner_for(tmp_path, mapper=model)
    out = map_fields(runner, unit_id="u-1", spec=INVESTORS, profile=PROFILE, rows=ROWS)
    problems = [e["outputs"]["problems"] for e in runner.audit.read() if e["action"] == "handoff.rejected"]
    text = " ".join(problems[0])
    assert "invented constant" in text and "outside the closed transform vocabulary" in text
    assert "Commitment_Amount does not come from setup" in text
    assert out.status == "needs_human" and [b.field for b in out.decided.bindings] == ["GP_ID"]


def test_binding_options_are_previewed_by_code_and_a_date_order_is_never_recommended() -> None:
    rule = rule_for("Commitment_Date")
    options = binding_options("Commitment_Date", rule, "Trade Dt", "C", ["13/04/2024", "28/12/2023"])
    assert [o.action for o in options] == ["transform", "transform", "declare_missing"]
    assert all(o.preview.total == 0 for o in options)  # day first reads every row, still not recommended
    assert options[0].payload["evidence"] == {"conform": 2, "total": 2}
    assert options[1].payload["evidence"] == {"conform": 0, "total": 2}
    from worker.contracts import choose_recommended

    assert choose_recommended(options) is None

    account = binding_options("Account_Name", rule_for("Account_Name"), "Account", "A",
                              ["1000 - Cash", "2000 - Payables"])
    assert [o.payload["recipe"] for o in account[:3]] == [
        [], [{"op": "split", "sep": " - ", "index": 1}], [{"op": "split", "sep": " - ", "index": 2}]
    ]
    assert account[0].preview.conform == 2 and choose_recommended(account) == account[0].option_id
    assert len(account) == 4  # at most 3 proposals plus MISSING


# ---------------------------------------------------------------------------------------------- Key Resolver


ITEMS = [
    KeyItem(key="Cash at bank", candidates=("1000", "1010"), text={"account_type": "asset"}),
    KeyItem(key="Trade payables", candidates=("2000",), text={"account_type": "liability"}),
]


def test_key_resolver_pick_outside_candidates_is_a_hard_zero(tmp_path: Path) -> None:
    def picks(a: str | None, b: str | None) -> AIMessage:
        return reply({"summary": "Picked.", "confidence": 0.7,
                      "output": {"picks": {"Cash at bank": a, "Trade payables": b}}})

    script = [
        picks("1000", "9999"),  # voter 1: outside the candidates
        picks("1000", "9999"),  # voter 1 repair: still outside, so voter 1 abstains
        picks("1000", "2000"),  # voter 2
        picks("1000", "2000"),  # voter 3
    ]
    model = ScriptedModel(script=script)
    runner = runner_for(tmp_path, key_resolver=model)
    out = resolve_keys(runner, unit_id="u-1", items=ITEMS)

    assert len(model.seen) == 4
    assert out.abstained == 1 and len(out.results) == 2
    assert out.decided == {"Cash at bank": "1000", "Trade payables": "2000"}
    assert "9999" not in out.decided.values()  # hard zero
    assert out.questions == ()
    assert actions(runner.audit).count("handoff.rejected") == 2
    assert actions(runner.audit).count("handoff.returned") == 2


def test_key_resolver_disagreement_becomes_a_question_and_lookup_needs_no_call(tmp_path: Path) -> None:
    def picks(a: str | None) -> AIMessage:
        return reply({"summary": "Picked.", "confidence": 0.6, "output": {"picks": {"Cash at bank": a}}})

    model = ScriptedModel(script=[picks("1000"), picks("1010"), picks(None)])
    runner = runner_for(tmp_path, key_resolver=model)
    out = resolve_keys(runner, unit_id="u-1", items=ITEMS, lookup={"Trade payables": "2000"})

    assert out.by_lookup == 1 and out.decided == {"Trade payables": "2000"}
    (q,) = out.questions
    assert q.kind == "disagreement" and q.subject[0].ref == "Cash at bank"
    assert [o.payload["candidate"] for o in q.options] == ["1000", "1010"] and q.recommended is None
    briefs = [e["inputs"]["brief_id"] for e in runner.audit.read() if e["action"] == "handoff.sent"]
    assert len(set(briefs)) == 3  # one brief per voter

    runner2 = runner_for(tmp_path / "again")
    out2 = resolve_keys(runner2, unit_id="u-1", items=ITEMS[1:], lookup={"Trade payables": "2000"})
    assert out2.decided == {"Trade payables": "2000"} and runner2.calls == []


# ------------------------------------------------------------------------------------------------- Explainer


ENGINE_ROWS = {"r1": {"account": "1000", "difference": "1200.00"}, "r2": {"account": "2000", "difference": "0"}}


def test_explainer_number_not_in_the_engine_result_is_a_hard_zero(tmp_path: Path) -> None:
    wrong = reply({"summary": "Check 11 break.", "confidence": 0.7,
                   "output": {"likely_cause": "A difference of 1,250.00 on account 1000.", "cites": ["r1"]}})
    right = reply({"summary": "Check 11 break.", "confidence": 0.7,
                   "output": {"likely_cause": "A difference of 1,200.00 on account 1000: a late accrual.",
                              "cites": ["r1"]}})
    model = ScriptedModel(script=[wrong, right])
    runner = runner_for(tmp_path, explainer=model)
    out = explain(runner, unit_id="u-r", cause_id="c-1", rows=ENGINE_ROWS, check_no=11)

    assert out.status == "ok" and out.decided.cites == ("r1",)
    assert "1,200.00" in out.decided.likely_cause
    assert actions(runner.audit) == ["handoff.sent", "handoff.rejected", "handoff.sent", "handoff.returned"]
    first = next(e for e in runner.audit.read() if e["action"] == "handoff.rejected")
    assert any("1250" in p for p in first["outputs"]["problems"])
    assert numbers_in("GP-001 on 2024-03-31 owes 1,250.00 and 11.43%") == [Decimal("1250.00"), Decimal("11.43")]


def test_explainer_invented_cite_twice_becomes_a_question(tmp_path: Path) -> None:
    bad = reply({"summary": "Break.", "confidence": 0.4, "output": {"likely_cause": "Timing.", "cites": ["r9"]}})
    model = ScriptedModel(script=[bad, bad, bad])
    runner = runner_for(tmp_path, explainer=model)
    out = explain(runner, unit_id="u-r", cause_id="c-2", rows=ENGINE_ROWS, check_no=11)
    assert len(model.seen) == 2 and out.status == "needs_human" and out.decided is None
    (q,) = out.questions
    assert q.subject[0].ref == "c-2" and q.recommended is None
    assert out.result.status == "needs_human" and out.result.output is None


# -------------------------------------------------------------------------------------------------- Profiler


def test_profiler_is_asked_only_when_code_cannot_route(tmp_path: Path) -> None:
    runner = runner_for(tmp_path)  # no model: code must decide
    out = profile_sheet(runner, unit_id="u-1", file_fp="fp-1", sheet="Investors",
                        rows={1: ["Investor_ID", "Investor_Name"]}, templates=TEMPLATES)
    assert out.status == "code" and out.decided.header_row == 1 and runner.calls == []
    assert out.decided.record_type_candidates[0].type == "Investors"

    answer = reply({"summary": "An investor list under a title row.", "confidence": 0.9, "output": {
        "header_row": 2, "layout": "flat",
        "columns": [{"col_id": "A", "header": "Inv ID", "kind": "id"}, {"col_id": "B", "header": "Inv Name",
                                                                         "kind": "text"}],
        "record_type_candidates": [{"type": "Investors", "confidence": 0.9, "evidence": "investor id and name"}],
    }})
    model = ScriptedModel(script=[answer])
    runner = runner_for(tmp_path / "m", profiler=model)
    out = profile_sheet(runner, unit_id="u-1", file_fp="fp-1", sheet="Investors",
                        rows={1: ["Sponsor A investor list"], 2: ["Inv ID", "Inv Name"]}, templates=TEMPLATES)
    assert out.status == "ok" and out.decided.header_row == 2 and len(model.seen) == 1
    assert out.result.output == out.decided and out.result.status == "ok"


def test_profiler_schema_failure_gets_one_repair_then_a_question(tmp_path: Path) -> None:
    model = ScriptedModel(script=[AIMessage("It looks like investors to me."), reply({"summary": "x"}),
                                  AIMessage("never sent")])
    runner = runner_for(tmp_path, profiler=model)
    out = profile_sheet(runner, unit_id="u-1", file_fp="fp-1", sheet="Investors",
                        rows={1: ["Inv ID", "Inv Name"]}, templates=TEMPLATES)
    assert len(model.seen) == 2
    rejected = [e["inputs"] for e in runner.audit.read() if e["action"] == "handoff.rejected"]
    assert [r["schema_valid"] for r in rejected] == [False, False]
    (q,) = out.questions
    assert q.kind == "low_confidence" and q.recommended is None
    assert {o.action for o in q.options} == {"pick", "skip_unit"}


def test_profiler_invented_column_or_type_is_refused(tmp_path: Path) -> None:
    answer = reply({"summary": "Payments.", "confidence": 0.95, "output": {
        "header_row": 1, "layout": "flat", "columns": [{"col_id": "C", "header": "Amount", "kind": "number"}],
        "record_type_candidates": [{"type": "Payments", "confidence": 0.95, "evidence": "amounts"}],
    }})
    model = ScriptedModel(script=[answer, answer])
    runner = runner_for(tmp_path, profiler=model)
    out = profile_sheet(runner, unit_id="u-1", file_fp="fp-1", sheet="S", rows={1: ["Inv ID", "Inv Name"]},
                        templates=TEMPLATES)
    problems = next(e for e in runner.audit.read() if e["action"] == "handoff.rejected")["outputs"]["problems"]
    assert any("invented column" in p for p in problems) and any("invented record type" in p for p in problems)
    assert out.status == "needs_human" and out.decided is None


# ------------------------------------------------------------------------------------ hand-offs and budgets


def test_handoff_audit_is_written_before_the_model_is_called(tmp_path: Path) -> None:
    good = reply({"summary": "Late accrual.", "confidence": 0.7,
                  "output": {"likely_cause": "A late accrual on account 1000.", "cites": ["r1"]}})
    model = ScriptedModel(script=[good])
    runner = runner_for(tmp_path, explainer=model)
    explain(runner, unit_id="u-r", cause_id="c-1", rows=ENGINE_ROWS)
    sent, returned = runner.audit.read()
    assert sent["action"] == "handoff.sent" and sent["inputs"]["output_checksum"] is None
    assert returned["action"] == "handoff.returned" and returned["inputs"]["output_checksum"]
    assert sent["inputs"]["brief_id"] == returned["inputs"]["brief_id"]
    assert sent["inputs"]["model_id"] == "scripted" and sent["inputs"]["role"] == "explainer"
    assert runner.audit.verify()[0]

    broken = AuditLog(tmp_path / "broken.jsonl")

    def no_audit(*args: object, **kwargs: object) -> dict:
        raise AuditError("disk full")

    broken.record = no_audit  # type: ignore[method-assign]
    silent = ScriptedModel(script=[good])
    with pytest.raises(AuditError):
        explain(RoleRunner(audit=broken, models={"explainer": silent}), unit_id="u-r", cause_id="c-1",
                rows=ENGINE_ROWS)
    assert silent.seen == []  # no audit, no hand-off


def test_budget_is_enforced_across_roles(tmp_path: Path) -> None:
    budget = Budget(max_calls=2)
    explainer = ScriptedModel(script=[
        reply({"summary": "Late accrual.", "confidence": 0.7,
               "output": {"likely_cause": "A late accrual.", "cites": ["r1"]}}),
        reply({"summary": "never sent", "confidence": 0.7, "output": {"likely_cause": "x", "cites": ["r1"]}}),
    ])
    drafter = ScriptedModel(script=[reply({"summary": "Add Inv ID as a synonym of Investor_ID.", "confidence": 0.6,
                                           "output": None})])
    runner = runner_for(tmp_path, budget, explainer=explainer, drafter=drafter)
    assert explain(runner, unit_id="u-r", cause_id="c-1", rows=ENGINE_ROWS).status == "ok"
    done = draft(runner, cluster_id="cl-1", target_file="skills/build-static-records/synonyms.yaml",
                 proposal_kind="synonym", examples=[{"header": "Inv ID", "field": "Investor_ID"}])
    assert done.status == "ok" and done.decided.startswith("Add Inv ID")
    with pytest.raises(BudgetExceeded):
        explain(runner, unit_id="u-r", cause_id="c-2", rows=ENGINE_ROWS)
    assert len(explainer.seen) == 1 and budget.calls == 2
    tail = [e["action"] for e in runner.audit.read()][-3:]
    assert tail == ["handoff.sent", "budget.exceeded", "handoff.rejected"]


def test_roles_get_read_only_tools_or_none(tmp_path: Path) -> None:
    assert all(tools <= READ_ONLY_TOOLS for tools in ROLE_TOOLS.values())
    assert not READ_ONLY_TOOLS & GATED_TOOLS
    with pytest.raises(ContractError):
        make_brief("mapper", "u-1", "bind", {}, tools=["write_upload"])
    runner = runner_for(tmp_path, mapper=ScriptedModel(script=[]))
    brief = make_brief("mapper", "u-1", "bind", {}, tools=["lift"])
    with pytest.raises(RoleError):
        runner.call(brief, {}, instructions="x", build=lambda o: None, verify=lambda r, raw: [], facts={})
    assert runner.audit.read() == []  # refused before anything was sent
    drafter = ScriptedModel(script=[reply({"summary": "Lower the tolerance in controls.yaml.", "confidence": 0.9,
                                           "output": None})] * 2)
    out = draft(runner_for(tmp_path / "d", drafter=drafter), cluster_id="cl-1", target_file="skills/x/SKILL.md",
                proposal_kind="example", examples=[])
    assert out.status == "needs_human"  # a control is never touched, not even in wording


def test_the_unit_id_includes_the_pins() -> None:
    one = JobUnit(file_fp="fp", sheet="S", record_type="Investors", entity=None, pins={"p": "1"})
    two = JobUnit(file_fp="fp", sheet="S", record_type="Investors", entity=None, pins={"p": "2"})
    assert one.unit_id != two.unit_id


# ----------------------------------------------------------------------------------------------------- tools


def test_two_read_only_tools_behind_the_usual_wrapper(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    book = Workbook()
    sheet = book.active
    sheet.title = "Deals"
    sheet.append(["Deal", "Trade Dt"])
    for deal, dt in (("Deal Alder", "13/04/2024"), ("Deal Birch", "28/12/2023")):
        sheet.append([deal, dt])
    book.save(inbox / "deals.xlsx")
    out = tmp_path / "out"
    ctx = RunContext(sponsor="sponsor-a", gp_id="GP-001", inbox_roots=[inbox], out_dir=out,
                     audit=AuditLog(tmp_path / "audit.jsonl"), stage=Stage(out))

    assert not {"propose_binding", "ask_question"} & {t.name for t in build_tools(ctx)}  # the Coworker's list
    tools = {t.name: t for t in build_tools(ctx, role_tools=True)}
    text = tools["propose_binding"].invoke({"path": "deals.xlsx", "sheet": "Deals", "col": "B",
                                            "field": "Commitment_Date", "why": "decide the date field"})
    got = json.loads(text)
    assert got["rows"] == 2 and got["recommended"] == ""
    assert [o["action"] for o in got["options"]] == ["transform", "transform", "declare_missing"]
    ids = [o["option_id"] for o in got["options"]]

    asked = json.loads(tools["ask_question"].invoke({"field": "Commitment_Date", "option_ids": ids,
                                                     "why": "ask the human about the date order"}))
    assert asked["recommended"] == "" and asked["question"]["kind"] == "unknown_format"
    refused = tools["ask_question"].invoke({"field": "Commitment_Date", "option_ids": ["opt-made-up"],
                                            "why": "try an invented option"})
    assert refused.startswith("ERROR")
    logged = [e["action"] for e in ctx.audit.read()]
    assert logged == ["propose_binding", "ask_question", "ask_question.error"]
    assert not (out / "Deals").exists()  # nothing written
