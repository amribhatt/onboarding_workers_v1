"""Schema tests for the typed hand-off contracts (migration step M4). No runtime is touched."""

from __future__ import annotations

import dataclasses
import json

import pytest

from worker import contracts as c
from worker.contracts import ContractError

PINS = {"controls": "sha256:cc", "skills.map-accounts": "sha256:aa", "target.Commitments": "sha256:dd"}


def unit(sheet: str = "Commitment Register", **kw) -> c.JobUnit:
    base = {"file_fp": "sha256:9b0e", "sheet": sheet, "record_type": "Commitments", "entity": "fund-1", "pins": PINS}
    base.update(kw)
    return c.JobUnit(**base)


def preview(conform: int = 49, total: int = 49) -> c.Preview:
    return c.Preview(conform=conform, total=total, sample_ref="p-2#c6")


def option(option_id: str = "o1", action: str = "transform", **kw) -> c.Option:
    base = {
        "option_id": option_id,
        "action": action,
        "proposed_by": "mapper",
        "preview": preview(),
        "payload": {"from": "Commit Date", "transforms": [{"op": "date_parse", "pattern": "%Y-%m-%d"}]},
    }
    base.update(kw)
    return c.Option(**base)


def question(options=None, **kw) -> c.Question:
    base = {
        "question_id": "q-17",
        "unit_id": unit().unit_id,
        "kind": "unknown_format",
        "subject": (c.Subject(kind="field", ref="Commitment_Date"),),
        "options": options if options is not None else (option("o1"), option("o2", "declare_missing", preview=preview(0, 49), payload={})),
        "blocking": ("map",),
    }
    base.update(kw)
    return c.Question.build(**base)


def brief(**kw) -> c.Brief:
    base = {
        "unit_id": unit().unit_id,
        "role": "mapper",
        "objective": "Bind each Commitments field to a column, a transform, a supply or missing",
        "inputs": (c.ArtifactRef(kind="profile", id="p-2", checksum="sha256:11"),),
        "skill_slice": ("build-static-records@sha256:aa#synonyms.yaml",),
        "tools": ("column_profile", "find_recipe"),
        "output_schema": "MappingPlan@1",
        "budget": c.Budget(model_calls=3, tool_calls=10, tokens=20000, seconds=60),
        "stop_when": "every target field has a binding",
    }
    base.update(kw)
    return c.Brief(**base)


def plan() -> c.MappingPlan:
    return c.MappingPlan(
        plan_id="mp-1",
        record_type="Commitments",
        bindings=(
            c.Binding(
                field="Commitment_Date",
                action="transform",
                col_id="c6",
                recipe=(c.Transform(op="date_parse", args={"pattern": "%Y-%m-%d"}),),
                reason="header matches, values are ISO dates",
                confidence=0.86,
            ),
            c.Binding(field="Commitment_Amount", action="map", col_id="c7", reason="header match", confidence=1),
            c.Binding(field="Fund_Id", action="supply", value_from="setup", reason="from setup", confidence=0.9),
            c.Binding(field="Doc_Ref", action="missing", reason="no source column", confidence=1.0),
        ),
    )


def profile() -> c.SourceProfile:
    return c.SourceProfile(
        profile_id="p-2",
        file_fp="sha256:9b0e",
        sheet="Commitment Register",
        header_row=4,
        layout="flat",
        columns=(
            c.ProfileColumn(col_id="c1", header="Fund", kind="idpat:A-999"),
            c.ProfileColumn(col_id="c6", header="Commit Date", kind="date:%Y-%m-%d", sample_ref="p-2#c6"),
        ),
        record_type_candidates=(c.RecordTypeCandidate(type="Commitments", confidence=0.9, evidence="headers"),),
    )


def handoff(event: str = c.HANDOFF_SENT, **kw) -> c.Handoff:
    base = {
        "event": event,
        "brief_id": brief().brief_id,
        "unit_id": unit().unit_id,
        "role": "mapper",
        "model_id": "model-x",
        "prompt_checksum": "sha256:ff",
        "skill_pins": ("build-static-records@sha256:aa",),
        "input_checksums": ("sha256:11",),
        "trace_id": "t-1",
    }
    base.update(kw)
    return c.Handoff(**base)


def every_contract() -> list[c.Contract]:
    q = question()
    order = c.WorkOrder(
        order_id="wo-1",
        request_text="build the Commitments sheet",
        units=(unit(), unit("Other", depends_on=(unit().unit_id,))),
        proposed_by="coworker",
        confirmed_by="analyst-1",
    )
    answer = c.Answer(question_id="q-17", option_id="o1", scope="sponsor", reason="", principal="analyst-1")
    custom = c.Answer(
        question_id="q-17",
        custom=option("c1", proposed_by="human"),
        scope="job",
        reason="the file uses day first",
        principal="analyst-1",
    )
    return [
        c.ArtifactRef(kind="plan", id="mp-1", checksum="sha256:22"),
        c.Subject(kind="artifact", ref="p-2", checksum="sha256:11"),
        c.Budget(model_calls=1, tool_calls=1, tokens=10, seconds=1),
        unit(),
        order,
        brief(),
        profile(),
        plan(),
        c.KeyDecisions(batch_id="k-1", picks={"1000 Cash": "cand-1", "9999 Odd": None}, voter=2),
        c.Explanation(cause_id="cause-1", likely_cause="The LP run is missing one fund.", cites=("row-12",), ask=q),
        option(),
        q,
        answer,
        custom,
        c.Result(
            brief_id=brief().brief_id, role="mapper", status="ok", summary="Bound 4 fields.", confidence=0.8,
            output=plan(),
        ),
        c.Result(
            brief_id=brief().brief_id, role="profiler", status="needs_human", summary="Unsure.", confidence=0.2,
            output=profile(), questions=(q,),
        ),
        handoff(),
        handoff(c.HANDOFF_RETURNED, output_checksum="sha256:33", schema_valid=True, verify_result="pass",
                tokens_in=10, tokens_out=5, seconds=1.5, tool_calls=2),
        handoff(c.HANDOFF_REJECTED, output_checksum="sha256:33", schema_valid=False),
    ]


# ---- round trip and hash stability -------------------------------------------------------------------------


@pytest.mark.parametrize("obj", every_contract(), ids=lambda o: type(o).__name__)
def test_json_round_trip_is_equal_and_stable(obj):
    text = obj.to_json()
    again = type(obj).from_json(text)
    assert again == obj
    assert again.to_json() == text
    assert again.digest() == obj.digest()


def test_canonical_json_ignores_key_order_and_spacing():
    a = option(payload={"b": 1, "a": 2})
    b = option(payload={"a": 2, "b": 1})
    assert a.to_json() == b.to_json()
    assert a.digest() == b.digest()
    assert " " not in json.dumps(json.loads(a.to_json()), separators=(",", ":")).replace("Commit Date", "")


def test_hash_is_stable_across_constructions_and_changes_with_content():
    assert plan().digest() == plan().digest()
    other = c.MappingPlan(plan_id="mp-2", record_type="Commitments", bindings=plan().bindings)
    assert other.digest() != plan().digest()


def test_digest_value_is_pinned():
    ref = c.ArtifactRef(kind="plan", id="mp-1", checksum="sha256:22")
    assert ref.to_json() == '{"checksum":"sha256:22","id":"mp-1","kind":"plan"}'
    assert ref.digest() == c.sha256_hex('{"checksum":"sha256:22","id":"mp-1","kind":"plan"}')


def test_from_dict_rejects_unknown_and_missing_keys():
    data = c.ArtifactRef(kind="plan", id="x", checksum="y").to_dict()
    with pytest.raises(ContractError, match="unknown keys"):
        c.ArtifactRef.from_dict({**data, "extra": 1})
    del data["checksum"]
    with pytest.raises(ContractError, match="missing checksum"):
        c.ArtifactRef.from_dict(data)
    with pytest.raises(ContractError, match="not JSON"):
        c.ArtifactRef.from_json("{nope")


def test_from_dict_revalidates_so_a_tampered_record_is_refused():
    data = question().to_dict()
    data["options"] = data["options"] * 3
    with pytest.raises(ContractError):
        c.Question.from_dict(data)
    tampered = unit().to_dict()
    tampered["unit_id"] = "u-0000000000000000"
    with pytest.raises(ContractError, match="does not match"):
        c.JobUnit.from_dict(tampered)


# ---- ids ---------------------------------------------------------------------------------------------------


def test_unit_id_includes_the_pins():
    assert unit().unit_id == unit().unit_id
    assert unit().unit_id != unit(pins={**PINS, "controls": "sha256:other"}).unit_id
    assert unit().unit_id != unit(entity="fund-2").unit_id
    assert unit().unit_id.startswith("u-")


def test_brief_id_is_derived_from_content():
    assert brief().brief_id == brief().brief_id
    assert brief().brief_id != brief(objective="something else").brief_id


# ---- refusals at construction ------------------------------------------------------------------------------


def test_summary_over_600_characters_is_refused():
    kw = {"brief_id": "b-1", "role": "explainer", "status": "failed", "confidence": 0.5}
    c.Result(summary="x" * 600, **kw)
    with pytest.raises(ContractError, match="601"):
        c.Result(summary="x" * 601, **kw)


def test_more_than_four_options_is_refused():
    opts = tuple(option(f"o{i}", "pick", payload={"candidate": f"c{i}"}) for i in range(5))
    with pytest.raises(ContractError, match="1 to 4"):
        question(options=opts)
    assert len(question(options=opts[:4]).options) == 4
    with pytest.raises(ContractError, match="1 to 4"):
        question(options=())


@pytest.mark.parametrize(
    "payload",
    [
        {"control": "tolerance"},
        {"set": {"controls": {"tb": 1}}},
        {"edit": [{"control_id": "c11"}]},
        {"file": "skills/reconcile-gl/controls.yaml"},
    ],
)
def test_an_option_that_offers_a_control_is_refused(payload):
    with pytest.raises(ContractError, match="control"):
        option(payload=payload)


@pytest.mark.parametrize("who", ["mapper", "Coworker", "agent-7", "system", "key_resolver", "llm"])
def test_an_agent_as_approver_is_refused(who):
    with pytest.raises(ContractError, match="only a human"):
        c.Answer(question_id="q", option_id="o1", scope="job", reason="", principal=who)
    with pytest.raises(ContractError, match="only a human"):
        c.WorkOrder(order_id="w", request_text="", units=(unit(),), proposed_by="human", confirmed_by=who)


def test_a_human_principal_is_accepted():
    answer = c.Answer(question_id="q", option_id="o1", scope="job", reason="", principal="analyst-1")
    assert answer.principal == "analyst-1"
    assert not c.is_agent_principal("analyst-1")


def test_organisation_scope_is_never_offered():
    with pytest.raises(ContractError, match="organisation"):
        question(scopes=("job", "organisation"))
    with pytest.raises(ContractError, match="organisation"):
        c.Answer(question_id="q", option_id="o1", scope="organisation", reason="", principal="analyst-1")
    with pytest.raises(ContractError, match="at least one scope"):
        question(scopes=())


def test_recommended_is_chosen_by_code_not_supplied():
    q = question()
    assert q.recommended == "o1"
    base = q.to_dict()
    base["recommended"] = "o2"
    with pytest.raises(ContractError, match="chosen by code"):
        c.Question.from_dict(base)


def test_recommended_needs_every_row_to_conform_and_is_never_declare_missing():
    partial = question(options=(option("o1", preview=preview(40, 49)),))
    assert partial.recommended is None
    only_missing = question(options=(option("o2", "declare_missing", preview=preview(49, 49), payload={}),))
    assert only_missing.recommended is None
    learned = option("o3", proposed_by="learned", preview=preview(30, 49))
    assert question(options=(learned, option("o1"))).recommended == "o1"
    assert c.choose_recommended(()) is None


def test_recommended_prefers_map_then_transform_then_position():
    m = option("m", "map", payload={"from": "Amount"})
    t = option("t", "transform")
    assert c.choose_recommended((t, m)) == "m"
    assert c.choose_recommended((t, option("t2", "transform"))) == "t"


def test_option_actions_are_the_closed_set_and_preview_is_ordered():
    with pytest.raises(ContractError, match="action"):
        option(action="run_python")
    with pytest.raises(ContractError):
        c.Preview(conform=5, total=4)
    with pytest.raises(ContractError):
        option(proposed_by="intern")


def test_custom_answer_needs_a_reason_and_a_human_option():
    with pytest.raises(ContractError, match="needs a reason"):
        c.Answer(question_id="q", custom=option("c", proposed_by="human"), scope="job", reason=" ", principal="a-1")
    with pytest.raises(ContractError, match="proposed by the human"):
        c.Answer(question_id="q", custom=option("c"), scope="job", reason="why", principal="a-1")
    with pytest.raises(ContractError, match="exactly one"):
        c.Answer(question_id="q", scope="job", reason="", principal="a-1")
    with pytest.raises(ContractError, match="exactly one"):
        c.Answer(question_id="q", option_id="o1", custom=option("c", proposed_by="human"),
                 scope="job", reason="r", principal="a-1")


def test_answer_is_checked_against_its_question():
    q = question(scopes=("job",))
    ok = c.Answer(question_id="q-17", option_id="o1", scope="job", reason="", principal="analyst-1")
    ok.check_against(q)
    with pytest.raises(ContractError, match="scope"):
        c.Answer(question_id="q-17", option_id="o1", scope="sponsor", reason="", principal="a-1").check_against(q)
    with pytest.raises(ContractError, match="no option"):
        c.Answer(question_id="q-17", option_id="zz", scope="job", reason="", principal="a-1").check_against(q)
    with pytest.raises(ContractError, match="different question"):
        c.Answer(question_id="q-99", option_id="o1", scope="job", reason="", principal="a-1").check_against(q)


def test_brief_never_hands_out_a_gated_tool_or_tools_to_structured_roles():
    with pytest.raises(ContractError, match="gated"):
        brief(tools=("column_profile", "write_upload"))
    with pytest.raises(ContractError, match="no tools"):
        brief(role="key_resolver", tools=("find",))
    brief(role="key_resolver", tools=())
    with pytest.raises(ContractError, match="name@version"):
        brief(output_schema="MappingPlan")
    with pytest.raises(ContractError, match="skill@checksum#file"):
        brief(skill_slice=("build-static-records",))


def test_binding_never_invents_a_value():
    with pytest.raises(ContractError, match="col_id"):
        c.Binding(field="F", action="map", reason="", confidence=1)
    with pytest.raises(ContractError, match="where the value comes from"):
        c.Binding(field="F", action="supply", reason="", confidence=1)
    with pytest.raises(ContractError):
        c.Binding(field="F", action="supply", value_from="42", reason="", confidence=1)
    with pytest.raises(ContractError, match="must not name a column"):
        c.Binding(field="F", action="missing", col_id="c1", reason="", confidence=1)
    with pytest.raises(ContractError, match="at least one transform"):
        c.Binding(field="F", action="transform", col_id="c1", reason="", confidence=1)
    with pytest.raises(ContractError, match="same field twice"):
        c.MappingPlan(plan_id="p", record_type="T", bindings=plan().bindings[:1] * 2)


def test_result_status_and_output_must_agree():
    kw = {"brief_id": "b", "summary": "s", "confidence": 0.5}
    with pytest.raises(ContractError, match="carries its MappingPlan"):
        c.Result(role="mapper", status="ok", **kw)
    with pytest.raises(ContractError, match="does not return"):
        c.Result(role="mapper", status="ok", output=profile(), **kw)
    with pytest.raises(ContractError, match="at least one Question"):
        c.Result(role="mapper", status="needs_human", **kw)
    with pytest.raises(ContractError, match="no output"):
        c.Result(role="mapper", status="failed", output=plan(), **kw)
    with pytest.raises(ContractError, match="between 0 and 1"):
        c.Result(brief_id="b", summary="s", confidence=1.5, role="mapper", status="failed")
    with pytest.raises(ContractError):
        c.Result(brief_id="b", summary="s", confidence=float("nan"), role="mapper", status="failed")


def test_work_order_rules():
    a, b = unit(), unit("Two")
    with pytest.raises(ContractError, match="at least one unit"):
        c.WorkOrder(order_id="w", request_text="", units=(), proposed_by="human")
    with pytest.raises(ContractError, match="twice"):
        c.WorkOrder(order_id="w", request_text="", units=(a, a), proposed_by="human")
    with pytest.raises(ContractError, match="same pins"):
        c.WorkOrder(order_id="w", request_text="", units=(a, unit("Two", pins={"controls": "x"})), proposed_by="human")
    with pytest.raises(ContractError, match="outside the order"):
        c.WorkOrder(order_id="w", request_text="", units=(unit(depends_on=("u-nope",)),), proposed_by="human")
    with pytest.raises(ContractError, match="cycle"):
        c.WorkOrder(
            order_id="w",
            request_text="",
            units=(unit(depends_on=(b.unit_id,)), unit("Two", depends_on=(a.unit_id,))),
            proposed_by="human",
        )
    with pytest.raises(ContractError, match="proposed_by"):
        c.WorkOrder(order_id="w", request_text="", units=(a,), proposed_by="mapper")
    assert not c.WorkOrder(order_id="w", request_text="", units=(a,), proposed_by="human").confirmed


def test_handoff_action_names_and_state_rules():
    assert c.HANDOFF_EVENTS == ("handoff.sent", "handoff.returned", "handoff.rejected")
    assert handoff().action == "handoff.sent"
    with pytest.raises(ContractError, match="event"):
        handoff("handoff.other")
    with pytest.raises(ContractError, match="no output"):
        handoff(output_checksum="sha256:33")
    with pytest.raises(ContractError, match="schema-valid and verified"):
        handoff(c.HANDOFF_RETURNED, output_checksum="sha256:33", schema_valid=True, verify_result="fail")
    with pytest.raises(ContractError, match="records a schema or verification failure"):
        handoff(c.HANDOFF_REJECTED)
    assert handoff(c.HANDOFF_REJECTED, verify_result="fail").verify_result == "fail"


def test_contracts_are_frozen():
    ref = c.ArtifactRef(kind="plan", id="x", checksum="y")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ref.id = "z"  # type: ignore[misc]


def test_payload_is_copied_so_later_mutation_cannot_change_a_hash():
    source = {"from": "Amount"}
    opt = option(payload=source)
    before = opt.digest()
    source["from"] = "Other"
    assert opt.digest() == before
