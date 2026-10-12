"""M11b: a table-row proposal that touches a control row (an output that passes something unseen, such as the cascade
rows L0 and L1) is filed `awaiting_controller` (ticket 07): never `ready`, never auto-promoted, a controller approves
it and the record is written first."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from worker.accounts import Coa, TypeMap, Versions, golden_versions, key_of
from worker.audit import AuditLog
from worker.improver import (
    AccountReplay,
    ImprovementStore,
    ReplayCase,
    control_row_hits,
    fast_loop,
)
from worker.skills import load_skills
from worker.templates import standards_dir

TARGET = "skills/map-accounts/tables/coa-cascade.yaml"
VAL = {"account_type": "", "account": "6150 Valuation adjustment", "transaction_type": "Valuation", "rows": 4}
VKEY = key_of(VAL["account_type"], VAL["account"], VAL["transaction_type"])


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Any]:
    base = standards_dir()
    table = load_skills()["map-accounts"].table("coa-cascade")
    return {
        "table": table,
        "replay": AccountReplay(Coa.load(base / "Golden_COA.csv"), TypeMap.load(base / "Transaction_Type_Mapping.csv"),
                                table),
        "store": ImprovementStore(tmp_path / "improvements", AuditLog(tmp_path / "audit.jsonl")),
        "golden": golden_versions(tmp_path / "accounts"),
        "overlay": Versions(tmp_path / "overlay"),
    }


def _cases() -> list[ReplayCase]:
    return [ReplayCase(f"{s}:{VKEY}", s, "GL", VKEY, VAL, "42020") for s in ("sponsor-a", "sponsor-b")]


def _proposal(row: dict[str, Any], by: str = "casey") -> dict[str, Any]:
    return {"kind": "table_row", "target": TARGET, "scope": "sponsor", "change": {"row": row}, "evidence": {},
            "proposed_by": by, "speed": "fast", "summary": f"{by} edits row {row['id']}"}


L0 = {"id": "L0", "when": {"historic": True}, "then": {"tier": "L0 Historic", "action": "apply", "confidence": 0.9}}
L1_SOFTER = {"id": "L1", "when": {"pinned": True}, "then": {"tier": "L1 Transaction type", "action": "suggest",
                                                           "confidence": 0.85}}
NEW_APPLY = {"id": "L2x", "when": {"tt_known": True, "score": ">=0.9"},
             "then": {"tier": "L2 Close text", "action": "apply", "confidence": 0.8}}
L3_TEXT = {"id": "L3", "when": {"tt_known": True, "score": ">=0.5"}, "then": {"tier": "L3 Hybrid text", "action": "suggest"}}


def test_the_control_rows_are_found_by_their_unseen_outputs(world: dict[str, Any]) -> None:
    table = world["table"]
    assert control_row_hits(table, "L0", L0) and control_row_hits(table, "L1", L1_SOFTER)
    assert control_row_hits(table, "L2x", NEW_APPLY)  # a new row that would apply without a person looking
    assert control_row_hits(table, "L3", L3_TEXT) == []  # suggest: a person sees it
    assert control_row_hits(table, "L9", {"id": "L9", "then": {"tier": "x", "action": "needs_you"}}) == []


@pytest.mark.parametrize("row", [L0, L1_SOFTER, NEW_APPLY], ids=["L0", "L1", "new apply row"])
def test_a_row_that_touches_a_control_row_waits_for_a_controller(world: dict[str, Any], row: dict[str, Any]) -> None:
    store, overlay = world["store"], world["overlay"]
    card = store.file(_proposal(row), _cases(), world["replay"], overlay, golden=world["golden"])
    assert card["status"] == "awaiting_controller"
    assert card["replay"]["info_only"] is True and card["proposal_sha256"]
    assert card["control"]["unseen"] and card["control"]["table"] == "coa-cascade"
    with pytest.raises(PermissionError, match="only another controller"):
        store.promote(card["id"], "blake", overlay, admin=True, reason="ok")
    assert overlay.version == 0
    # the fast loop files it and never promotes it, even with auto-promotion on
    again = fast_loop(store, [_proposal(row)], _cases(), world["replay"], overlay, golden=world["golden"],
                      auto_promote_sponsor=True, approver="owner-1")
    assert [c["status"] for c in again] == ["awaiting_controller"] and overlay.version == 0


def test_a_row_that_touches_no_control_row_is_filed_as_before(world: dict[str, Any]) -> None:
    card = world["store"].file(_proposal(L3_TEXT), _cases(), world["replay"], world["overlay"],
                               golden=world["golden"])
    assert card["status"] in ("ready", "blocked") and "control" not in card


def test_a_controller_approves_a_control_row_and_it_is_recorded_first(world: dict[str, Any]) -> None:
    store, overlay = world["store"], world["overlay"]
    card = store.file(_proposal(L0, by="casey"), _cases(), world["replay"], overlay, golden=world["golden"])
    for who in ("casey", "drew", "improver"):  # the proposer, a non-controller, a reserved name
        with pytest.raises(PermissionError):
            store.approve_control(card["id"], who, controllers=["blake", "casey"], reason="ok", versions=overlay)
    assert overlay.version == 0
    done = store.approve_control(card["id"], "blake", controllers=["blake", "casey"], reason="the guide says 0.9",
                                versions=overlay)
    assert done["status"] == "promoted" and overlay.version == 1
    rec = [x for x in store.audit.read() if x["action"] == "control.changed"][-1]  # type: ignore[union-attr]
    assert rec["inputs"]["control_id"] == "coa-cascade.L0" and rec["inputs"]["approved_by"] == "blake"
    assert rec["inputs"]["bound"]["rule"].startswith("a row that passes something unseen")
    assert rec["outputs"]["version"] == 1
