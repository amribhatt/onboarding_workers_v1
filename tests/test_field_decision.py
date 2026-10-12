"""Migration M16: field-decision learning records (ticket 05 D8, the parts this step owns).

Record and recipe, privacy, clustering by sponsor, the learned-proposal guard, the reversible recipe migrator and
revert cards. Replay (`SynonymReplay`) and metric v2 belong to later steps (M19, metrics v2).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from worker import feedback as F
from worker import learnindex as L
from worker import transforms as T
from worker.audit import AuditError, AuditLog
from worker.decide import Table
from worker.improver import (
    LEARNING_DEFAULTS,
    ImprovementStore,
    WriteRefused,
    cluster,
    cluster_decisions,
    draft_learning_card,
    file_learning_card,
    guard,
    learning_controls,
    revert_cards,
)
from worker.recipes import (
    FieldRecipes,
    RecipeChangeRefused,
    decide_field,
)

ROOT = Path(__file__).resolve().parents[1]
KEY = b"test-learning-key"
DATES = [f"2021-08-{d:02d}" for d in range(1, 11)]
DATE_OPS = [{"op": "date_parse", "patterns": ["YYYY-MM-DD"]}]
AMOUNT_OPS = [{"op": "number_parse", "decimal": "."}]
PINS = {"skills": {"build-static-records": "sha256:aa"}, "controls": "sha256:cc", "target": {"Commitments": "sha256:dd"},
        "recipe_before": None, "model": "scripted", "prompt": "sha256:ff", "mapper_handoff": "h-1"}


class FailOn(AuditLog):
    """An audit log that cannot write one action (or any, with None)."""

    def __init__(self, path: Path, action: str | None = None) -> None:
        super().__init__(path)
        self.fail = action

    def record_many(self, action: str, items: list[Any]) -> list[dict[str, Any]]:
        if self.fail is None or action == self.fail:
            raise AuditError("disk full")
        return super().record_many(action, items)


def rows(dates: list[str] = DATES, header: str = "Commit Date") -> list[T.Row]:
    return [T.Row(f"Commitment Register!{i + 5}", ("Fund", "Commit Ref", header, "Amount"),
                  ("F1", f"C-{i:03d}", d, 1000 + i)) for i, d in enumerate(dates)]


def date_option(oid: str = "o1", conform: int = 10, total: int = 10, header: str = "Commit Date",
                proposed_by: str = "mapper", **extra: Any) -> dict[str, Any]:
    return {"option_id": oid, "action": "transform", "proposed_by": proposed_by,
            "payload": {"from": header, "ops": DATE_OPS, **extra.pop("payload_extra", {})},
            "preview": {"conform": conform, "total": total, "sample_ref": "p-2#c3"}, **extra}


def missing_option(total: int = 10) -> dict[str, Any]:
    return {"option_id": "o2", "action": "declare_missing", "proposed_by": "mapper", "payload": {},
            "preview": {"conform": 0, "total": total}}


def learned_option(sid: str) -> dict[str, Any]:
    return date_option("o3", proposed_by="learned", suggestion_id=sid)


def decision(sponsor: str = "sponsor-a", principal: Any = "analyst-1", *, field: str = "Commitment_Date",
             header: str = "Commit Date", values: list[Any] | None = None, options: list[dict[str, Any]] | None = None,
             chosen: dict[str, Any] | None = None, scope: str = "sponsor", job: str = "job-1",
             **extra: Any) -> dict[str, Any]:
    return {"job": job, "unit_id": "u-1", "sponsor": sponsor, "record_type": "Commitments", "target_field": field,
                "question": {"question_id": "q-1", "kind": "unknown_field"},
                "source": {"file_fp": "sha256:9b0e", "sheet": "Commitment Register", "header": header, "col_id": "c3",
                        "profile_id": "p-2"},
                "values": DATES if values is None else values,
                "sample_ref": {"kind": "profile", "id": "p-2", "checksum": "sha256:01"},
                "options_shown": options or [date_option(header=header), missing_option()],
                "chosen": chosen or {"option_id": "o1"}, "scope": scope, "principal": principal, "pins": PINS,
                "conformance": {"before": {"bound": False, "conform": 0, "total": 10},
                             "after": {"bound": True, "conform": 10, "total": 10}}} | extra


def recipes(tmp: Path, sponsor: str = "sponsor-a") -> FieldRecipes:
    return FieldRecipes(tmp / "sponsors", sponsor, record_types=["Commitments", "Investors"])


def log(tmp: Path, sponsor: str, n: int = 1, **kw: Any) -> list[dict[str, Any]]:
    """Record `n` decisions for `sponsor` in its own log and return the log's field decisions."""
    store = F.FeedbackStore(tmp / "fb", sponsor)
    for i in range(n):
        F.record(AuditLog(tmp / f"audit-{sponsor}.jsonl"), store, kind=F.FIELD_DECISION,
                 **decision(sponsor, job=f"job-{sponsor}-{i}", **kw))
    return store.list(kind=F.FIELD_DECISION)


def index(tmp: Path, sponsors: list[str]) -> list[dict[str, Any]]:
    by = {s: F.FeedbackStore(tmp / "fb", s).list(kind=F.FIELD_DECISION) for s in sponsors}
    undone = {s: F.FeedbackStore(tmp / "fb", s).undone() for s in sponsors}
    return L.build_index(by, key=KEY, undone=undone)


# ---- record and recipe ------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("change", "why"), [
    ({"target_field": ""}, "target field"),
    ({"principal": ""}, "principal"),
    ({"principal": "agent"}, "not a person"),
    ({"principal": "improver"}, "not a person"),
    ({"principal": {"name": "mapper"}}, "not a person"),
    ({"unit_id": None}, "unit"),
])
def test_a_record_missing_its_target_field_or_principal_is_refused(change: dict[str, Any], why: str) -> None:
    with pytest.raises(F.FeedbackError, match=why):
        F.make_field_decision(**{**decision(), **change})


def test_code_fills_shape_header_recommendation_and_accept_and_never_copies_a_value() -> None:
    rec = F.make_field_decision(**decision())
    assert (rec["kind"], rec["action"], rec["recommended"]) == ("field_decision", "accept", "o1")
    assert rec["source"]["header_norm"] == "commit date"
    assert rec["samples"] | {} == {"sample_ref": {"kind": "profile", "id": "p-2", "checksum": "sha256:01"},
                                   "rows_seen": 10, "shape_sig": "date:%Y-%m-%d", "shape_version": 1}
    assert rec["principal"] == {"name": "analyst-1", "auth": "self-declared-demo"}
    assert rec["feedback_id"].startswith("fb-") and len(rec["feedback_id"]) == 15
    assert "2021-08" not in json.dumps(rec)  # values are referenced, never copied
    assert F.make_field_decision(**decision(chosen={"option_id": "o2"}, reason="no date"))["action"] == "override"
    with pytest.raises(F.FeedbackError, match="recommended option is chosen by code"):
        F.make_field_decision(**decision(recommended="o2"))
    with pytest.raises(F.FeedbackError, match="decided by code"):
        F.make_field_decision(**decision(action="accept", chosen={"option_id": "o2"}, reason="x"))
    with pytest.raises(F.FeedbackError, match="unknown field-decision fields"):
        F.make_field_decision(**decision(shape_sig="text"))
    with pytest.raises(F.FeedbackError, match="header_norm is computed by code"):
        F.make_field_decision(**{**decision(), "source": {**decision()["source"], "header_norm": "other"}})


def test_a_custom_option_is_closed_human_and_reasoned() -> None:
    custom = {"option_id": "c1", "action": "transform", "proposed_by": "human",
              "payload": {"from": "Commit Date", "ops": DATE_OPS}, "preview": {"conform": 10, "total": 10}}
    rec = F.make_field_decision(**decision(chosen={"custom": custom}, reason="the ISO form"))
    assert rec["action"] == "override" and rec["chosen"]["custom"]["option_id"] == "c1"
    with pytest.raises(F.FeedbackError, match="needs a reason"):
        F.make_field_decision(**decision(chosen={"custom": custom}))
    with pytest.raises(F.FeedbackError, match="not a valid option"):
        F.make_field_decision(**decision(chosen={"custom": {**custom, "action": "run_python"}}, reason="x"))
    with pytest.raises(F.FeedbackError, match="proposed by the human"):
        F.make_field_decision(**decision(chosen={"custom": {**custom, "proposed_by": "mapper"}}, reason="x"))
    with pytest.raises(F.FeedbackError, match="required field missing needs a reason"):
        F.make_field_decision(**decision(chosen={"option_id": "o2"}, field_required=True))


def test_the_answer_is_audited_first_then_logged_then_the_recipe_is_written(tmp_path: Path) -> None:
    audit, store, rcp = AuditLog(tmp_path / "audit.jsonl"), F.FeedbackStore(tmp_path / "fb", "sponsor-a"), \
        recipes(tmp_path)
    out = decide_field(audit, store, rcp, rows(), **decision())
    entries = audit.read()
    assert [e["action"] for e in entries] == ["feedback", "recipe.changed"]
    assert entries[0]["outputs"]["target"] == "recipe"
    logged = store.list(kind=F.FIELD_DECISION)[0]
    assert logged["decided_at"] == entries[0]["ts"] and logged["feedback_id"] == out["record"]["feedback_id"]
    assert rcp.load("Commitments")["fields"] == {"Commitment_Date": {"from": "Commit Date", "ops": DATE_OPS}}
    assert out["record"]["recipe_after"] == rcp.pin("Commitments") == entries[1]["inputs"]["pin_after"]
    assert entries[1]["inputs"]["verify"] == {"conform": 10, "total": 10, "missing": 0, "failed": {}}
    assert [e["action"] for e in rcp.history("Commitments").read()] == ["recipe.changed"]


def test_a_failed_audit_leaves_the_recipe_byte_identical(tmp_path: Path) -> None:
    store, rcp = F.FeedbackStore(tmp_path / "fb", "sponsor-a"), recipes(tmp_path)
    decide_field(AuditLog(tmp_path / "a.jsonl"), store, rcp, rows(), **decision())
    before = rcp.path("Commitments").read_bytes()
    amount = decision(field="Commitment_Amount", header="Amount", values=[1000 + i for i in range(10)],
                      options=[{"option_id": "o1", "action": "transform", "proposed_by": "mapper",
                                "payload": {"from": "Amount", "ops": AMOUNT_OPS},
                                "preview": {"conform": 10, "total": 10}}])
    with pytest.raises(AuditError):  # the decision itself cannot be audited: nothing is written anywhere
        decide_field(FailOn(tmp_path / "b.jsonl"), store, rcp, rows(), **amount)
    assert rcp.path("Commitments").read_bytes() == before and len(store.list()) == 1
    with pytest.raises(AuditError):  # the decision is recorded, but recipe.changed cannot be: the file stays
        decide_field(FailOn(tmp_path / "c.jsonl", "recipe.changed"), store, rcp, rows(), **amount)
    assert rcp.path("Commitments").read_bytes() == before
    assert [e["action"] for e in rcp.history("Commitments").read()] == ["recipe.changed"]


def test_the_answer_moves_the_next_jobs_pin_not_the_current_jobs(tmp_path: Path) -> None:
    rcp = recipes(tmp_path)
    job_pin = rcp.pin("Commitments")  # recorded at the job's start
    audit, store = AuditLog(tmp_path / "audit.jsonl"), F.FeedbackStore(tmp_path / "fb", "sponsor-a")
    first = decide_field(audit, store, rcp, rows(), **decision(pins={**PINS, "recipe_before": job_pin}))
    amount = decision(field="Commitment_Amount", header="Amount", values=[1000 + i for i in range(10)],
                      pins={**PINS, "recipe_before": job_pin},
                      options=[{"option_id": "o1", "action": "transform", "proposed_by": "mapper",
                                "payload": {"from": "Amount", "ops": AMOUNT_OPS},
                                "preview": {"conform": 10, "total": 10}}])
    second = decide_field(audit, store, rcp, rows(), **amount)
    # both answers of the running job carry the pin it started with; the recipe moved twice for the next job
    assert first["record"]["pins"]["recipe_before"] is None and second["record"]["pins"]["recipe_before"] is None
    assert first["record"]["recipe_after"] != second["record"]["recipe_after"] == rcp.pin("Commitments")
    assert set(rcp.load("Commitments")["fields"]) == {"Commitment_Date", "Commitment_Amount"}


def test_replacing_a_step_needs_an_explicit_replace_and_job_scope_records_only(tmp_path: Path) -> None:
    audit, store, rcp = AuditLog(tmp_path / "a.jsonl"), F.FeedbackStore(tmp_path / "fb", "sponsor-a"), \
        recipes(tmp_path)
    first = decide_field(audit, store, rcp, rows(), **decision())
    other = {"option_id": "c1", "action": "transform", "proposed_by": "human",
             "payload": {"from": "Commit Date", "ops": [{"op": "date_parse", "patterns": ["YYYY-DD-MM"]}]},
             "preview": {"conform": 0, "total": 10}}
    again = decision(chosen={"custom": other}, reason="day before month here", supersedes=first["record"]["feedback_id"])
    with pytest.raises(RecipeChangeRefused, match="explicit replace"):
        decide_field(audit, store, rcp, rows(), **again)
    out = decide_field(audit, store, rcp, rows(), replace=True, **again)
    assert out["recipe"]["old"] == {"from": "Commit Date", "ops": DATE_OPS} and out["record"]["supersedes"]
    pin = rcp.pin("Commitments")
    job_only = decide_field(audit, store, rcp, rows(), **decision(scope="job"))
    assert job_only["recipe"] is None and job_only["record"]["recipe_after"] is None
    assert rcp.pin("Commitments") == pin and audit.read()[-1]["outputs"]["target"] == "feedback_only"


def test_undo_audits_first_restores_and_appends_never_deleting(tmp_path: Path) -> None:
    audit, store, rcp = AuditLog(tmp_path / "a.jsonl"), F.FeedbackStore(tmp_path / "fb", "sponsor-a"), \
        recipes(tmp_path)
    fid = decide_field(audit, store, rcp, rows(), **decision())["record"]["feedback_id"]
    with pytest.raises(AuditError):
        rcp.revert("Commitments", fid, principal="analyst-1", reason="wrong column", audit=FailOn(tmp_path / "x"))
    assert rcp.load("Commitments") is not None  # the failed record changed nothing
    rcp.revert("Commitments", fid, principal="analyst-1", reason="wrong column", audit=audit, feedback_store=store)
    assert [e["action"] for e in audit.read()][-1] == "recipe.reverted"
    assert rcp.load("Commitments") is None  # the only field is gone again; its history is not
    assert [e["action"] for e in rcp.history("Commitments").read()] == ["recipe.changed", "recipe.reverted"]
    assert store.undone() == {fid} and [r["feedback_id"] for r in store.list(kind=F.FIELD_DECISION)] == [fid]
    with pytest.raises(RecipeChangeRefused, match="still in force"):
        rcp.revert("Commitments", fid, principal="analyst-1", reason="again", audit=audit)


# ---- privacy and suggestions --------------------------------------------------------------------------------------
def test_the_index_and_a_card_hold_no_sponsor_slug_principal_or_value(tmp_path: Path) -> None:
    for s, who in (("sponsor-a", "analyst-1"), ("sponsor-b", "analyst-2"), ("sponsor-c", "analyst-3")):
        log(tmp_path, s, principal=who)
    idx = index(tmp_path, ["sponsor-a", "sponsor-b", "sponsor-c"])
    card = draft_learning_card(idx[0])
    assert card is not None
    text = json.dumps(idx) + json.dumps(card)
    for secret in ("sponsor-", "analyst", "2021-08", "C-00", "9b0e"):
        assert secret not in text
    assert card["evidence"]["sponsors"] == ["S1", "S2", "S3"] and card["evidence"]["sample_values"] == "none"
    assert all(s.startswith("k:") for s in idx[0]["sponsors"])
    with pytest.raises(L.LearningError, match="no learning key"):
        L.build_index({}, key=b"")


def test_constant_crosswalk_and_typed_literal_steps_are_never_suggested(tmp_path: Path) -> None:
    supply = {"option_id": "c1", "action": "supply", "proposed_by": "human", "payload": {"value": "2021-01-01"},
              "preview": {"conform": 10, "total": 10}}
    walk = {"option_id": "c2", "action": "transform", "proposed_by": "human",
            "payload": {"from": "Commit Date", "ops": [{"op": "crosswalk", "pairs": {"Open": "O"}}]},
            "preview": {"conform": 10, "total": 10}}
    prefix = {"option_id": "c3", "action": "transform", "proposed_by": "human",
              "payload": {"from": "Commit Date", "ops": [{"op": "strip_prefix", "text": "ACME-"}]},
              "preview": {"conform": 10, "total": 10}}
    for s, custom in (("sponsor-a", supply), ("sponsor-b", walk), ("sponsor-c", prefix)):
        log(tmp_path, s, field="Commitment_Ref" if custom is not supply else "Commitment_Date",
            chosen={"custom": custom}, reason="typed by hand")
    idx = index(tmp_path, ["sponsor-a", "sponsor-b", "sponsor-c"])
    assert idx and not any(e["eligible"] for e in idx)
    text = json.dumps(idx)
    assert "2021-01-01" not in text and "Open" not in text and "ACME" not in text
    for fld in ("Commitment_Date", "Commitment_Ref"):
        assert L.suggest(idx, sponsor="sponsor-d", record_type="Commitments", target_field=fld,
                         header="Commit Date", rows=rows(), key=KEY) == []


def test_a_suggestion_is_one_option_recomputed_on_my_rows_and_never_applied_without_a_click(tmp_path: Path) -> None:
    for s in ("sponsor-a", "sponsor-b"):
        log(tmp_path, s, principal=f"analyst-{s[-1]}")
    idx = index(tmp_path, ["sponsor-a", "sponsor-b"])
    rcp, audit = recipes(tmp_path, "sponsor-c"), AuditLog(tmp_path / "c.jsonl")
    mine = rows([f"2022-01-{d:02d}" for d in range(1, 21)])
    opts = L.suggest(idx, sponsor="sponsor-c", record_type="Commitments", target_field="Commitment_Date",
                     header="Commit Date", rows=mine, key=KEY)
    assert len(opts) == 1
    o = opts[0]
    assert (o.proposed_by, o.action, o.preview.conform, o.preview.total) == ("learned", "transform", 20, 20)
    assert o.payload == {"from": "Commit Date", "ops": [{"op": "date_parse", "patterns": ["YYYY-MM-DD"]}]}
    assert o.note == "Decided the same way at 2 other sponsors"
    assert rcp.pin("Commitments") is None and audit.read() == [] and not (tmp_path / "sponsors").exists()
    assert L.recommend([*opts], idx) == o.option_id  # complete preview: code may recommend it ...
    bad = rows([f"2022-01-{d:02d}" for d in range(1, 21)] + ["2022-13-01"])  # same shape, one row does not read
    weak = L.suggest(idx, sponsor="sponsor-c", record_type="Commitments", target_field="Commitment_Date",
                     header="Commit Date", rows=bad, key=KEY)
    assert weak[0].preview.conform == 20 and weak[0].preview.total == 21
    assert L.recommend(weak, idx) is None  # ... but never when conform < total
    # sponsor-a asking again sees only sponsor-b: the count is hidden at 1
    assert L.suggest(idx, sponsor="sponsor-a", record_type="Commitments", target_field="Commitment_Date",
                     header="Commit Date", rows=mine, key=KEY)[0].note == ""
    # the click: sponsor-c chooses it; the record says it came through the suggestion
    shown = [missing_option(20), {**o.to_dict(), "option_id": "o3"}]
    rec = decide_field(audit, F.FeedbackStore(tmp_path / "fb", "sponsor-c"), rcp, mine,
                       **decision("sponsor-c", "analyst-c", values=[r.cells[2] for r in mine], options=shown,
                                  chosen={"option_id": "o3"}))
    assert rec["record"]["via_suggestion"] == o.suggestion_id and rcp.pin("Commitments") is not None
    after = index(tmp_path, ["sponsor-a", "sponsor-b", "sponsor-c"])[0]
    assert len(after["sponsors"]) == 3 and after["independent"] == 2  # sponsor-c saw the suggestion


# ---- clustering by sponsor ----------------------------------------------------------------------------------------
def test_one_sponsor_three_times_gives_no_card(tmp_path: Path) -> None:
    log(tmp_path, "sponsor-a", n=3)
    idx = index(tmp_path, ["sponsor-a"])
    assert len(idx[0]["sponsors"]) == 1 and idx[0]["decisions"] == 1  # newer ones supersede, each sponsor once
    assert draft_learning_card(idx[0]) is None and not cluster_decisions(idx)[0]["ready"]


def test_three_sponsors_with_one_independent_give_no_card_and_with_two_a_ready_card(tmp_path: Path) -> None:
    log(tmp_path, "sponsor-a", principal="analyst-1")
    sid = index(tmp_path, ["sponsor-a"])[0]["suggestion_id"]
    shown = [date_option(), missing_option(), learned_option(sid)]
    log(tmp_path, "sponsor-b", principal="analyst-2", options=shown)
    log(tmp_path, "sponsor-c", principal="analyst-3", options=shown)
    idx = index(tmp_path, ["sponsor-a", "sponsor-b", "sponsor-c"])
    assert (len(idx[0]["sponsors"]), idx[0]["independent"]) == (3, 1)
    assert draft_learning_card(idx[0]) is None
    log(tmp_path, "sponsor-d", principal="analyst-4")
    idx = index(tmp_path, ["sponsor-a", "sponsor-b", "sponsor-c", "sponsor-d"])
    card = draft_learning_card(idx[0], synonyms={"types": {"Commitments": {"Commitment_Date": ["Closing Date"]}}})
    assert card is not None and card["status"] == "ready" and card["kind"] == "synonym"
    assert card["diff"] == {"op": "add", "path": "types.Commitments.Commitment_Date[-1]", "value": "Commit Date"}
    assert card["evidence"]["independent_sponsors"] == 2 and len(card["evidence"]["feedback"]) == 4
    assert draft_learning_card(idx[0], synonyms={"types": {"Commitments": {"Commitment_Date": ["commit_date"]}}}) \
        is None  # already a synonym: nothing to add


def test_three_sponsors_with_one_principal_give_no_card(tmp_path: Path) -> None:
    for s in ("sponsor-a", "sponsor-b", "sponsor-c"):
        log(tmp_path, s, principal="analyst-1")
    facts = cluster_decisions(index(tmp_path, ["sponsor-a", "sponsor-b", "sponsor-c"]))[0]
    assert not facts["ready"] and any("distinct people" in r for r in facts["reasons"])


def test_a_contest_blocks_suggestions_and_cards(tmp_path: Path) -> None:
    for i, s in enumerate(("sponsor-a", "sponsor-b", "sponsor-c")):
        log(tmp_path, s, principal=f"analyst-{i}")
    log(tmp_path, "sponsor-d", principal="analyst-9", field="Effective_Date")  # same header and shape, other field
    idx = index(tmp_path, ["sponsor-a", "sponsor-b", "sponsor-c", "sponsor-d"])
    assert all(e["contested"] and e["contested_by"] for e in idx)
    assert all(draft_learning_card(e) is None for e in idx)
    assert L.suggest(idx, sponsor="sponsor-e", record_type="Commitments", target_field="Commitment_Date",
                     header="Commit Date", rows=rows(), key=KEY) == []


def _promoted(tmp: Path) -> tuple[dict[str, Any], list[str]]:
    sponsors = ["sponsor-a", "sponsor-b", "sponsor-c"]
    for i, s in enumerate(sponsors):
        log(tmp, s, principal=f"analyst-{i}")
    card = draft_learning_card(index(tmp, sponsors)[0])
    assert card is not None
    return {**card, "status": "promoted"}, sponsors


def test_an_undo_below_n_files_a_revert_card_that_removes_exactly_one_header(tmp_path: Path) -> None:
    card, sponsors = _promoted(tmp_path)
    assert revert_cards([card], index(tmp_path, sponsors), {}, key=KEY) == []
    fid = F.FeedbackStore(tmp_path / "fb", "sponsor-a").list(kind=F.FIELD_DECISION)[0]["feedback_id"]
    F.FeedbackStore(tmp_path / "fb", "sponsor-a").add_undo(fid, "analyst-0", "that column was the trade date")
    out = revert_cards([card], index(tmp_path, sponsors), {}, key=KEY)
    assert len(out) == 1 and out[0]["kind"] == "synonym_revert" and out[0]["reverts"] == card["id"]
    assert out[0]["diff"] == {"op": "remove", "path": "types.Commitments.Commitment_Date", "value": "Commit Date"}
    assert out[0]["trigger"]["below_n"] and out[0]["trigger"]["evidence_sponsors"] == 2


def test_overrides_at_two_sponsors_file_a_revert_card_and_it_is_audited_first(tmp_path: Path) -> None:
    card, sponsors = _promoted(tmp_path)
    via = date_option(payload_extra={"via_card": card["id"]})
    shown = [via, {**missing_option(), "option_id": "o2"}]
    by = {"sponsor-d": log(tmp_path, "sponsor-d", options=shown, chosen={"option_id": "o2"}, reason="not a date")}
    assert revert_cards([card], index(tmp_path, sponsors), by, key=KEY) == []  # one sponsor is not enough
    by["sponsor-e"] = log(tmp_path, "sponsor-e", options=shown, chosen={"option_id": "o2"}, reason="not a date")
    out = revert_cards([card], index(tmp_path, sponsors), by, key=KEY)
    assert len(out) == 1 and out[0]["trigger"]["override_sponsors"] == 2
    assert "sponsor-" not in json.dumps(out[0])
    audit = AuditLog(tmp_path / "improver.jsonl")
    filed = file_learning_card(ImprovementStore(tmp_path / "imp", audit), out[0])
    assert filed["status"] == "awaiting_replay" and audit.read()[0]["action"] == "improver.filed"
    with pytest.raises(AuditError):
        file_learning_card(ImprovementStore(tmp_path / "imp2", FailOn(tmp_path / "f.jsonl")), out[0])
    assert not (tmp_path / "imp2" / "cards").exists()


def test_thresholds_are_controls_with_a_code_bound(tmp_path: Path) -> None:
    assert learning_controls(ROOT / "skills" / "build-static-records") == LEARNING_DEFAULTS
    (tmp_path / "controls.yaml").write_text("learning:\n  min_sponsors: 4\n", encoding="utf-8")
    assert learning_controls(tmp_path)["min_sponsors"] == 4
    with pytest.raises(ValueError, match="below the code bound"):
        learning_controls(data={"min_sponsors": 1})
    with pytest.raises(ValueError, match="unknown learning controls"):
        learning_controls(data={"auto_promote": 1})
    with pytest.raises(ValueError, match="cannot exceed"):
        learning_controls(data={"min_sponsors": 2, "min_independent": 3})


def test_the_old_cluster_ignores_field_decisions(tmp_path: Path) -> None:
    recs = log(tmp_path, "sponsor-a", chosen={"option_id": "o2"}, reason="no")
    legacy = {"action": "override", "lineage": "y", "shown": "1", "chosen": "2", "sponsor": "sponsor-a", "job": "j"}
    assert [g["key"] for g in cluster([*recs, legacy])] == ["y"]


# ---- the learned-proposal guard -----------------------------------------------------------------------------------
@pytest.mark.parametrize(("kind", "target", "change"), [
    ("synonym", "skills/map-accounts/controls.yaml", {"op": "add", "value": "x"}),
    ("table_row", "templates/Commitments.target.yaml", {"path": "controls.required", "row": {"id": "r"}}),
    ("synonym", "skills/build-static-records/cases/headers.json", {"op": "add", "value": "x"}),
    ("threshold", "skills/map-accounts/tables/coa-cascade.yaml", {}),
    ("control", "skills/map-accounts/tables/coa-cascade.yaml", {}),
    ("golden_mapping", "templates/standards/golden-mappings", {}),
    ("know_how", "skills/build-static-records/SKILL.md", {}),
    ("synonym", "templates/sponsors/sponsor-a/recipes/Commitments.yaml", {"op": "add", "value": "x"}),
    ("example", "src/worker/transforms.py", {}),
    ("synonym", "skills/build-static-records/synonyms.yaml", {"op": "remove", "value": "Commit Date"}),
    ("synonym", "skills/build-static-records/synonyms.yaml", {"op": "add", "path": "controls.x", "value": "y"}),
])
def test_learned_proposals_touching_controls_cases_thresholds_or_code_are_refused(
        kind: str, target: str, change: dict[str, Any]) -> None:
    with pytest.raises(WriteRefused):
        guard(kind, target, learned=True, change=change)


def test_learned_table_rows_never_touch_a_control_row_or_narrow_a_one_way_table() -> None:
    cascade = Table.load(ROOT / "skills" / "map-accounts" / "tables" / "coa-cascade.yaml")
    l0 = next(r for r in cascade.rows if r["id"] == "L0")
    with pytest.raises(WriteRefused, match="control row"):
        guard("table_row", "skills/map-accounts/tables/coa-cascade.yaml", learned=True,
              change={"row": {**l0, "then": {**l0["then"], "confidence": 0.5}}}, table=cascade)
    deal = Table.load(ROOT / "skills" / "map-accounts" / "tables" / "deal-required.yaml")
    with pytest.raises(WriteRefused, match="narrows a one-way table|never narrows"):
        guard("table_row", "skills/map-accounts/tables/deal-required.yaml", learned=True,
              change={"row": {"id": "r1500-1599", "when": {"at_least": ">=1500", "at_most": "<=1549"},
                              "then": {"required": True}}}, table=deal)
    with pytest.raises(WriteRefused, match="table is needed"):
        guard("table_row", "skills/map-accounts/tables/deal-required.yaml", learned=True,
              change={"row": {"id": "new", "then": {"required": True}}})
    # widening a one-way table with a new row, and adding one header, are learnable
    guard("table_row", "skills/map-accounts/tables/deal-required.yaml", learned=True,
          change={"row": {"id": "r7000-7099", "when": {"at_least": ">=7000", "at_most": "<=7099"},
                          "then": {"required": True}}}, table=deal)
    guard("synonym", "skills/build-static-records/synonyms.yaml", learned=True,
          change={"op": "add", "path": "types.Commitments.Commitment_Date[-1]", "value": "Commit Date"})
    guard("table_row", "skills/map-accounts/tables/coa-cascade.yaml")  # the old guard is unchanged
