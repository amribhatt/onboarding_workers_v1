"""Controller record and audit-first order (ticket 07 sections 2 to 6; identity stays demo-grade and is labelled).

Every change that takes effect writes its audit record first; when the record cannot be written nothing moves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from worker.accounts import Coa, TypeMap, golden_versions, key_of
from worker.audit import AuditError, AuditLog
from worker.improver import AccountReplay, ImprovementStore, ReplayCase, propose_golden_mapping
from worker.skills import load_skills
from worker.templates import standards_dir

VAL = {"account_type": "", "account": "6150 Valuation adjustment", "transaction_type": "Valuation", "rows": 4}
VKEY = key_of(VAL["account_type"], VAL["account"], VAL["transaction_type"])


class BrokenAudit(AuditLog):
    """An audit log whose writes fail: the action must not happen."""

    def record_many(self, action: str, items: list[tuple[str, dict | None, dict | None]]) -> list[dict]:
        raise AuditError("disk full")


def _replay() -> AccountReplay:
    base = standards_dir()
    return AccountReplay(Coa.load(base / "Golden_COA.csv"), TypeMap.load(base / "Transaction_Type_Mapping.csv"),
                         load_skills()["map-accounts"].table("coa-cascade"))


def _cases() -> list[ReplayCase]:
    return [ReplayCase(f"{s}:{VKEY}", s, "GL", VKEY, VAL, "42020") for s in ("sponsor-a", "sponsor-b")]


def _app(tmp_path: Path, **env: str) -> Any:
    from worker.web import create_app

    return create_app(tmp_path / "state", environ={"WORKER_MODEL_PROVIDER": "demo",
                                                   "WORKER_ENV_FILE": str(tmp_path / "t.env"), **env})


def _record_cases(manager: Any) -> None:
    from worker.accounts import AccountStore

    for sp in ("sponsor-a", "sponsor-b"):
        AccountStore(manager.state_dir / "accounts", sp).add(VKEY, "42020", source="approved", reason="signed off")


def _ready_card(store: ImprovementStore, golden: Any) -> dict[str, Any]:
    cause = {"key": VKEY, "chosen": "42020", "count": 2, "sponsors": ["sponsor-a", "sponsor-b"]}
    card = store.file(propose_golden_mapping(cause, proposed_by="casey"), _cases(), _replay(), golden)
    assert card["status"] == "ready"
    return card


# ---- audit before the change ---------------------------------------------------------------------------------
def test_promote_writes_the_audit_before_the_version_moves(tmp_path: Path) -> None:
    golden = golden_versions(tmp_path / "accounts")
    good = ImprovementStore(tmp_path / "imp", AuditLog(tmp_path / "audit.jsonl"))
    card = _ready_card(good, golden)
    broken = ImprovementStore(tmp_path / "imp", BrokenAudit(tmp_path / "audit.jsonl"))
    with pytest.raises(AuditError):
        broken.promote(card["id"], "alex", golden, admin=True, reason="two sponsors agree")
    assert golden.version == 0 and not (golden.dir / "CURRENT").exists()
    assert broken.load(card["id"])["status"] == "ready"
    done = good.promote(card["id"], "alex", golden, admin=True, reason="two sponsors agree")
    entry = [e for e in good.audit.read() if e["action"] == "improver.promoted"][-1]  # type: ignore[union-attr]
    assert entry["outputs"]["golden_version"] == done["version"] == golden.version == 1


def test_promote_and_rollback_refuse_without_an_audit_log(tmp_path: Path) -> None:
    golden = golden_versions(tmp_path / "accounts")
    good = ImprovementStore(tmp_path / "imp", AuditLog(tmp_path / "audit.jsonl"))
    card = _ready_card(good, golden)
    bare = ImprovementStore(tmp_path / "imp")
    with pytest.raises(AuditError, match="needs an audit log"):
        bare.promote(card["id"], "alex", golden, admin=True, reason="agreed")
    assert golden.version == 0
    good.promote(card["id"], "alex", golden, admin=True, reason="agreed")
    with pytest.raises(AuditError, match="needs an audit log"):
        bare.rollback(golden, "alex", "premature")
    assert golden.version == 1


def test_rollback_writes_the_audit_before_the_pointer_moves(tmp_path: Path) -> None:
    golden = golden_versions(tmp_path / "accounts")
    good = ImprovementStore(tmp_path / "imp", AuditLog(tmp_path / "audit.jsonl"))
    good.promote(_ready_card(good, golden)["id"], "alex", golden, admin=True, reason="agreed")
    broken = ImprovementStore(tmp_path / "imp", BrokenAudit(tmp_path / "audit.jsonl"))
    with pytest.raises(AuditError):
        broken.rollback(golden, "alex", "premature")
    assert golden.version == 1 and (golden.dir / "CURRENT").read_text(encoding="utf-8") == "1"
    good.rollback(golden, "alex", "premature")
    last = good.audit.read()[-1]  # type: ignore[union-attr]
    assert last["action"] == "improver.rolled_back" and last["outputs"]["golden_version"] == 0 == golden.version


def test_reject_is_audited_first_on_both_paths(tmp_path: Path) -> None:
    from worker.console_harness import harness_act

    m = _app(tmp_path).state.manager
    _record_cases(m)
    first = m.propose_improvement(VKEY, "42020", "sponsor", "casey")
    m.decide_improvement(first["id"], "reject", "no cases yet", "dana", False)
    second = m.propose_improvement(VKEY, "42020", "sponsor", "casey")
    harness_act(m, {"action": "reject", "card": second["id"], "reason": "not now"}, "dana", False)
    store = m._improvement_parts()[1]
    rejected = [e for e in store.audit.read() if e["action"] == "improver.rejected"]
    assert [e["inputs"]["card"] for e in rejected] == [first["id"], second["id"]]
    assert all(e["inputs"]["by"] == "dana" for e in rejected)
    third = m.propose_improvement(VKEY, "42020", "sponsor", "casey")
    broken = ImprovementStore(store.root, BrokenAudit(store.audit.path))
    with pytest.raises(AuditError):
        broken.reject(third["id"], "dana", "no")
    assert store.load(third["id"])["status"] == "ready"


def test_threshold_is_not_an_improvable_kind() -> None:
    from worker.improver import ALLOWED_KINDS, WriteRefused, guard

    assert "threshold" not in ALLOWED_KINDS
    with pytest.raises(WriteRefused):
        guard("threshold", "skills/map-accounts/tables/coa-cascade.yaml")  # refused even on a learnable surface


def test_the_unaudited_golden_shortcut_is_gone() -> None:
    from worker.accounts import AccountStore

    assert not hasattr(AccountStore, "promote")


def test_confirm_account_mappings_leaves_the_store_unchanged_when_the_audit_fails(tmp_path: Path) -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    from scripted_model import ScriptedModel, call, say
    from test_accounts import _ctx, _lift
    from worker.accounts import AccountStore
    from worker.agent import build_agent, decide, pending_gate, start

    class FailsOnConfirm(AuditLog):
        def record_many(self, action: str, items: list[tuple[str, dict | None, dict | None]]) -> list[dict]:
            if action == "account.confirmed":
                raise AuditError("disk full")
            return super().record_many(action, items)

    ctx = _ctx(tmp_path)
    ctx.audit = FailsOnConfirm(ctx.audit.path)
    key = key_of("", "3000 Contributions", "Capital call")
    script = [
        say("lift", _lift()),
        say("map", call("map_accounts", stage_id="s1", why="map the GL accounts")),
        say("confirm", call("confirm_account_mappings", stage_id="s2", picks={key: "31010"}, why="human chose")),
        say("done"),
    ]
    agent = build_agent(ctx, ScriptedModel(script=script), InMemorySaver())
    assert [g["name"] for g in pending_gate(start(agent, ctx, "map", "t1"))] == ["confirm_account_mappings"]
    decide(agent, ctx, "t1", {"type": "approve"}, "approved the picks", {"reviewer": "reviewer-a"})
    assert AccountStore(ctx.accounts_root, "sponsor-a").entries() == {}  # type: ignore[arg-type]
    assert not any(e["action"] == "account.confirmed" for e in ctx.audit.read())


# ---- approving a control ---------------------------------------------------------------------------------------
SKILLS = Path(__file__).resolve().parents[1] / "skills"
FIELDS = ("proposal_id", "proposal_sha256", "proposed_by", "proposed_at", "approved_by", "approver_roles",
          "control_id", "old", "new", "bound", "direction", "evidence", "replay", "pin_before", "pin_after",
          "controllers_list_sha256")


def _skills(tmp_path: Path) -> Path:
    import shutil

    root = tmp_path / "skills"
    shutil.copytree(SKILLS, root)
    return root


def _wider(skill_dir: Path, warn: float = 10) -> dict[str, Any]:
    import yaml

    new = yaml.safe_load((skill_dir / "controls.yaml").read_text(encoding="utf-8"))
    new["tolerances"]["money-tiered"]["warn_up_to"] = warn
    return new


def _control_card(store: ImprovementStore, skill_dir: Path, by: str = "alex", warn: float = 10) -> dict[str, Any]:
    return store.file_control({"kind": "control", "target": "skills/reconcile-gl/controls.yaml",
                               "change": {"controls": _wider(skill_dir, warn)}, "proposed_by": by,
                               "evidence": {"cases": ["known-gl:tb"], "feedback": ["fb-1"]},
                               "summary": f"{by} proposes a wider WARN band"}, skill_dir)


def test_the_control_record_has_every_field_and_the_pin_it_names(tmp_path: Path) -> None:
    from worker.improver import controllers_sha256
    from worker.skills import Skill

    skill_dir = _skills(tmp_path) / "reconcile-gl"
    store = ImprovementStore(tmp_path / "imp", AuditLog(tmp_path / "audit.jsonl"))
    card = _control_card(store, skill_dir)
    assert card["status"] == "awaiting_controller" and card["replay"]["info_only"] is True
    before = Skill("reconcile-gl", "", "", skill_dir).content_hash()
    done = store.approve_control(card["id"], "blake", controllers=["alex", "blake"], reason="auditors accept 10",
                                 skill_dir=skill_dir)
    assert done["status"] == "promoted" and done["approved_by"] == "blake"
    entry = next(e for e in store.audit.read() if e["action"] == "control.changed")  # type: ignore[union-attr]
    rec = entry["inputs"]
    assert set(FIELDS) <= set(rec)
    assert rec["proposal_id"] == card["id"] and rec["proposal_sha256"] == card["proposal_sha256"]
    assert rec["proposed_by"] == "alex" and rec["approved_by"] == "blake" and rec["approver_roles"] == ["controller"]
    assert rec["control_id"] == "tolerances.money-tiered.warn_up_to"
    assert rec["old"] == {"tolerances.money-tiered.warn_up_to": 5}
    assert rec["new"] == {"tolerances.money-tiered.warn_up_to": 10}
    assert rec["bound"]["tolerances.money-tiered.warn_up_to"]["max"] == "25" and rec["direction"] == "looser"
    assert rec["evidence"]["cases"] == ["known-gl:tb"] and rec["replay"]["info_only"] is True
    assert rec["pin_before"] == before != rec["pin_after"]
    assert rec["pin_after"] == Skill("reconcile-gl", "", "", skill_dir).content_hash()
    assert rec["controllers_list_sha256"] == controllers_sha256(["blake", "alex"])
    assert store.audit.verify()[0]  # type: ignore[union-attr]


def test_a_stricter_change_is_recorded_as_stricter(tmp_path: Path) -> None:
    skill_dir = _skills(tmp_path) / "reconcile-gl"
    store = ImprovementStore(tmp_path / "imp", AuditLog(tmp_path / "audit.jsonl"))
    card = _control_card(store, skill_dir, warn=3)
    store.approve_control(card["id"], "blake", controllers=["alex", "blake"], reason="tighter", skill_dir=skill_dir)
    rec = next(e for e in store.audit.read() if e["action"] == "control.changed")["inputs"]  # type: ignore[union-attr]
    assert rec["direction"] == "stricter"


def test_a_non_controller_cannot_approve_and_the_file_is_unchanged(tmp_path: Path) -> None:
    skill_dir = _skills(tmp_path) / "reconcile-gl"
    raw = (skill_dir / "controls.yaml").read_bytes()
    store = ImprovementStore(tmp_path / "imp", AuditLog(tmp_path / "audit.jsonl"))
    card = _control_card(store, skill_dir)
    with pytest.raises(PermissionError, match="not a controller"):
        store.approve_control(card["id"], "casey", controllers=["alex", "blake"], reason="ok", skill_dir=skill_dir)
    with pytest.raises(PermissionError, match="no controllers are configured"):
        store.approve_control(card["id"], "blake", controllers=[], reason="ok", skill_dir=skill_dir)
    with pytest.raises(PermissionError, match="proposer cannot approve"):
        store.approve_control(card["id"], "Alex", controllers=["alex", "blake"], reason="ok", skill_dir=skill_dir)
    assert (skill_dir / "controls.yaml").read_bytes() == raw
    assert store.load(card["id"])["status"] == "awaiting_controller"


def test_a_failing_audit_leaves_controls_yaml_byte_identical(tmp_path: Path) -> None:
    skill_dir = _skills(tmp_path) / "reconcile-gl"
    raw = (skill_dir / "controls.yaml").read_bytes()
    good = ImprovementStore(tmp_path / "imp", AuditLog(tmp_path / "audit.jsonl"))
    card = _control_card(good, skill_dir)
    broken = ImprovementStore(tmp_path / "imp", BrokenAudit(tmp_path / "audit.jsonl"))
    with pytest.raises(AuditError):
        broken.approve_control(card["id"], "blake", controllers=["alex", "blake"], reason="ok", skill_dir=skill_dir)
    assert (skill_dir / "controls.yaml").read_bytes() == raw
    assert good.load(card["id"])["status"] == "awaiting_controller"


def test_a_file_that_does_not_read_back_is_put_back_and_recorded(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    from worker import recon as R

    skill_dir = _skills(tmp_path) / "reconcile-gl"
    raw = (skill_dir / "controls.yaml").read_bytes()
    store = ImprovementStore(tmp_path / "imp", AuditLog(tmp_path / "audit.jsonl"))
    card = _control_card(store, skill_dir)
    real = Path.write_bytes
    calls: list[int] = []

    def torn(self: Path, data: bytes) -> int:  # the first write is cut short, as on a full disk
        calls.append(1)
        return real(self, data[: len(data) // 2] if len(calls) == 1 else data)

    monkeypatch.setattr(Path, "write_bytes", torn)
    with pytest.raises(R.RulesError, match="did not read back"):
        store.approve_control(card["id"], "blake", controllers=["alex", "blake"], reason="ok", skill_dir=skill_dir)
    monkeypatch.undo()
    assert (skill_dir / "controls.yaml").read_bytes() == raw
    actions = [e["action"] for e in store.audit.read()]  # type: ignore[union-attr]
    assert actions[-2:] == ["control.changed", "control.write_failed"]


def test_a_tampered_or_stale_proposal_is_refused(tmp_path: Path) -> None:
    skill_dir = _skills(tmp_path) / "reconcile-gl"
    store = ImprovementStore(tmp_path / "imp", AuditLog(tmp_path / "audit.jsonl"))
    card = _control_card(store, skill_dir)
    with pytest.raises(ValueError, match="changed since it was shown"):
        store.approve_control(card["id"], "blake", controllers=["alex", "blake"], reason="ok", skill_dir=skill_dir,
                              seen_sha256="0" * 64)
    stored = store.load(card["id"])
    stored["proposal"]["change"]["controls"]["tolerances"]["money-tiered"]["warn_up_to"] = 25
    store.save(stored)
    with pytest.raises(ValueError, match="changed since it was shown"):
        store.approve_control(card["id"], "blake", controllers=["alex", "blake"], reason="ok", skill_dir=skill_dir)


def _console(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, admins: str = "alex,blake") -> tuple[Any, Path]:
    """A web app whose skills folder is a scratch copy, so a control change never touches the repository."""
    root = _skills(tmp_path)
    monkeypatch.setenv("WORKER_SKILLS_DIR", str(root))
    return _app(tmp_path, WORKER_ADMINS=admins), root / "reconcile-gl"


# ---- routing -----------------------------------------------------------------------------------------------------
def test_a_control_change_over_http_needs_another_controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    app, skill_dir = _console(tmp_path, monkeypatch, admins="alex,blake,agent")
    c = TestClient(app)
    raw = (skill_dir / "controls.yaml").read_bytes()
    filed = c.post("/api/console/harness/act", headers={"x-reviewer": "alex"},
                   json={"action": "propose_control", "controls": _wider(skill_dir), "reason": "wider WARN band"})
    assert filed.status_code == 200 and filed.json()["status"] == "awaiting_controller"
    card = filed.json()
    url = f"/api/improvements/{card['id']}/decision"
    for who, code in (("casey", 403), ("alex", 403), ("agent", 403), ("improver", 403), ("policy:owner", 403)):
        r = c.post(url, headers={"x-reviewer": who}, json={"verdict": "approve", "reason": "ok"})
        assert r.status_code == code, (who, r.text)
        assert (skill_dir / "controls.yaml").read_bytes() == raw
    stale = c.post(url, headers={"x-reviewer": "blake"},
                   json={"verdict": "approve", "reason": "ok", "proposal_sha256": "0" * 64})
    assert stale.status_code == 409
    ok = c.post(url, headers={"x-reviewer": "blake"},
                json={"verdict": "approve", "reason": "auditors accept 10", "proposal_sha256": card["proposal_sha256"]})
    assert ok.status_code == 200 and ok.json()["status"] == "promoted"
    assert (skill_dir / "controls.yaml").read_bytes() != raw


def test_promote_refuses_a_control_card_and_reserved_names(tmp_path: Path) -> None:
    from worker.improver import reserved_name

    skill_dir = _skills(tmp_path) / "reconcile-gl"
    store = ImprovementStore(tmp_path / "imp", AuditLog(tmp_path / "audit.jsonl"))
    golden = golden_versions(tmp_path / "accounts")
    card = _control_card(store, skill_dir)
    with pytest.raises(PermissionError, match="only another controller"):
        store.promote(card["id"], "blake", golden, admin=True, reason="ok")
    assert golden.version == 0
    ready = _ready_card(store, golden)
    for name in ("agent", "improver", "console", "system", "policy:owner-1", "openai/gpt-4o", "claude-opus-5-5"):
        assert reserved_name(name)
        with pytest.raises(PermissionError, match="not a person"):
            store.promote(ready["id"], name, golden, admin=True, reason="ok")
    assert not reserved_name("alex") and golden.version == 0
    with pytest.raises(PermissionError, match="named person"):
        _control_card(store, skill_dir, by="improver")


def _narrowing(manager: Any, row: dict[str, Any]) -> tuple[dict[str, Any], Any, Any, Any]:
    from worker import console_harness as H

    skill = load_skills()["reconcile-gl"]
    base = skill.table("account-series")
    proposal = {"kind": "table_row", "target": "skills/reconcile-gl/tables/account-series.yaml", "scope": "sponsor",
                "change": {"row": row}, "evidence": {"skill": "reconcile-gl", "table": "account-series",
                                                     "sponsor": "sponsor-a"},
                "proposed_by": "alex", "speed": "fast", "summary": f"alex edits row {row['id']} of account-series"}
    return proposal, H.ReconReplay(manager, base), H._recon_cases(manager), H._overlay(manager, "reconcile-gl",
                                                                                         "account-series")


def test_fast_loop_never_promotes_a_control_or_a_narrowing_proposal(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    from worker.improver import fast_loop

    app, skill_dir = _console(tmp_path, monkeypatch)
    m = app.state.manager
    store = m._improvement_parts()[1]
    narrow = {"id": "interest", "when": {"series": "interest"}, "then": {"fsl": "Interest cost"}}
    proposal, replay, cases, overlay = _narrowing(m, narrow)
    control = {"kind": "control", "target": "skills/reconcile-gl/controls.yaml", "scope": "sponsor",
               "change": {"controls": _wider(skill_dir)}, "proposed_by": "improver", "summary": "loosen"}
    cards = fast_loop(store, [control, proposal], cases, replay, overlay, auto_promote_sponsor=True,
                      approver="owner-1")
    assert [c["status"] for c in cards] == ["awaiting_controller"]
    assert cards[0]["control"]["narrows"] == ["fsl loses Accrued interest, Accrued interest payable"]
    assert overlay.version == 0
    with pytest.raises(PermissionError, match="only another controller"):
        store.promote(cards[0]["id"], "blake", overlay, reason="ok")
    widen = {"id": "interest", "when": {"series": "interest"},
             "then": {"fsl": "Interest cost, Accrued interest payable, Accrued interest, Interest payable"}}
    wider = store.file(_narrowing(m, widen)[0], cases, replay, overlay)
    assert wider["status"] in ("ready", "blocked") and "control" not in wider


def test_a_controller_approves_a_narrowing_row_and_it_is_recorded_first(tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    from worker.console_harness import HarnessError, harness_act

    app, _skill_dir = _console(tmp_path, monkeypatch)
    m = app.state.manager
    store = m._improvement_parts()[1]
    narrow = {"id": "interest", "when": {"series": "interest"}, "then": {"fsl": "Interest cost"}}
    proposal, replay, cases, overlay = _narrowing(m, narrow)
    card = store.file(proposal, cases, replay, overlay)
    with pytest.raises(HarnessError) as e:
        harness_act(m, {"action": "promote", "card": card["id"], "reason": "ok"}, "alex", True)
    assert e.value.status == 403 and overlay.version == 0
    done = harness_act(m, {"action": "promote", "card": card["id"], "reason": "the guide drops accruals"}, "blake",
                       True)
    assert done["status"] == "promoted" and overlay.version == 1
    rec = [x for x in store.audit.read() if x["action"] == "control.changed"][-1]
    assert rec["inputs"]["control_id"] == "account-series.interest" and rec["inputs"]["direction"] == "looser"
    assert rec["inputs"]["approved_by"] == "blake" and rec["outputs"]["version"] == 1


# ---- lone admin ----------------------------------------------------------------------------------------------------
def test_a_lone_controller_is_refused_until_a_second_controller_exists(tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    app, skill_dir = _console(tmp_path, monkeypatch, admins="alex")
    raw = (skill_dir / "controls.yaml").read_bytes()
    c = TestClient(app)
    card = c.post("/api/console/harness/act", headers={"x-reviewer": "alex"},
                  json={"action": "propose_control", "controls": _wider(skill_dir)}).json()
    url = f"/api/improvements/{card['id']}/decision"
    lone = c.post(url, headers={"x-reviewer": "alex"}, json={"verdict": "approve", "reason": "only me"})
    assert lone.status_code == 403
    assert lone.json()["error"] == "you are the only controller; another controller must approve"
    store = app.state.manager._improvement_parts()[1]
    assert store.load(card["id"])["status"] == "awaiting_controller"
    assert (skill_dir / "controls.yaml").read_bytes() == raw
    second = TestClient(_app(tmp_path, WORKER_ADMINS="alex,blake"))  # a second controller is added
    ok = second.post(url, headers={"x-reviewer": "blake"}, json={"verdict": "approve", "reason": "agreed"})
    assert ok.status_code == 200 and ok.json()["status"] == "promoted"
    act = "/api/console/harness/act"
    body = {"action": "rollback", "surface": "control", "reason": "incident"}
    assert second.post(act, headers={"x-reviewer": "casey"}, json=body).status_code == 403
    back = second.post(act, headers={"x-reviewer": "alex"}, json=body)  # any controller, even the proposer
    assert back.status_code == 200 and (skill_dir / "controls.yaml").read_bytes() == raw


# ---- control rollback and out-of-band edits ------------------------------------------------------------------------
def _changed(tmp_path: Path) -> tuple[ImprovementStore, Path, bytes]:
    skill_dir = _skills(tmp_path) / "reconcile-gl"
    raw = (skill_dir / "controls.yaml").read_bytes()
    store = ImprovementStore(tmp_path / "imp", AuditLog(tmp_path / "audit.jsonl"))
    card = _control_card(store, skill_dir)
    store.approve_control(card["id"], "blake", controllers=["alex", "blake"], reason="wider", skill_dir=skill_dir)
    return store, skill_dir, raw


def test_control_rollback_refuses_a_non_controller_and_a_mismatched_pin(tmp_path: Path) -> None:
    store, skill_dir, raw = _changed(tmp_path)
    changed = (skill_dir / "controls.yaml").read_bytes()
    for who in ("casey", "agent", "improver"):
        with pytest.raises(PermissionError):
            store.rollback_control(who, "incident", controllers=["alex", "blake"], skill_dir=skill_dir)
    t = skill_dir / "tables" / "trans-type-words.yaml"
    t.write_text(t.read_text(encoding="utf-8").replace("exit", "exit, sell-down"), encoding="utf-8")
    with pytest.raises(ValueError, match="edited outside the system"):
        store.rollback_control("alex", "incident", controllers=["alex", "blake"], skill_dir=skill_dir)
    assert (skill_dir / "controls.yaml").read_bytes() == changed != raw


def test_a_valid_control_rollback_restores_old_with_the_record_written_first(tmp_path: Path) -> None:
    store, skill_dir, raw = _changed(tmp_path)
    broken = ImprovementStore(store.root, BrokenAudit(store.audit.path))  # type: ignore[union-attr]
    changed = (skill_dir / "controls.yaml").read_bytes()
    with pytest.raises(AuditError):
        broken.rollback_control("alex", "incident", controllers=["alex", "blake"], skill_dir=skill_dir)
    assert (skill_dir / "controls.yaml").read_bytes() == changed
    out = store.rollback_control("alex", "incident: band too wide", controllers=["alex", "blake"], skill_dir=skill_dir)
    assert (skill_dir / "controls.yaml").read_bytes() == raw
    log = store.audit.read()  # type: ignore[union-attr]
    first = next(e for e in log if e["action"] == "control.changed")
    last = log[-1]
    assert last["action"] == "control.rolled_back" and last["inputs"]["reverses_seq"] == first["seq"] == out[
        "reverses_seq"]
    assert last["inputs"]["old"] == first["inputs"]["new"] and last["inputs"]["new"] == first["inputs"]["old"]
    assert last["inputs"]["pin_after"] == first["inputs"]["pin_before"]
    assert store.load(first["inputs"]["proposal_id"])["status"] == "rolled_back"
    with pytest.raises(ValueError, match="only the latest change"):
        store.rollback_control("blake", "again", controllers=["alex", "blake"], skill_dir=skill_dir)


def test_an_out_of_band_edit_and_a_new_controller_are_recorded_at_start(tmp_path: Path,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    app, skill_dir = _console(tmp_path, monkeypatch, admins="alex")
    log = app.state.manager._improvement_parts()[1].audit
    assert [e["action"] for e in log.read()] == ["controllers.changed", "control.baseline"]
    _app(tmp_path, WORKER_ADMINS="alex")  # same list, same controls: a restart is quiet
    assert len(log.read()) == 2
    p = skill_dir / "controls.yaml"
    p.write_text(p.read_text(encoding="utf-8").replace("warn_up_to: 5", "warn_up_to: 20"), encoding="utf-8")
    _app(tmp_path, WORKER_ADMINS="alex,mallory")
    tail = log.read()[-2:]
    assert [e["action"] for e in tail] == ["controllers.changed", "control.unaudited_change"]
    assert tail[0]["inputs"]["controllers"] == ["alex", "mallory"] and tail[0]["inputs"]["previous"] == ["alex"]
    _app(tmp_path, WORKER_ADMINS="alex,mallory")  # recorded once per edit, not on every start
    assert len(log.read()) == 4
    assert log.verify()[0]


def test_an_audited_change_is_not_reported_as_out_of_band(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    app, skill_dir = _console(tmp_path, monkeypatch)
    c = TestClient(app)
    card = c.post("/api/console/harness/act", headers={"x-reviewer": "alex"},
                  json={"action": "propose_control", "controls": _wider(skill_dir)}).json()
    c.post(f"/api/improvements/{card['id']}/decision", headers={"x-reviewer": "blake"},
           json={"verdict": "approve", "reason": "agreed"})
    _app(tmp_path, WORKER_ADMINS="alex,blake")
    actions = [e["action"] for e in app.state.manager._improvement_parts()[1].audit.read()]
    assert "control.changed" in actions and "control.unaudited_change" not in actions


def test_the_page_says_identity_is_demo_grade(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    page = TestClient(_app(tmp_path)).get("/inbox").text
    assert "Identity is self-declared; this is not production authentication." in page


def test_a_control_proposal_outside_the_code_bounds_is_never_filed(tmp_path: Path) -> None:
    from worker import recon as R

    skill_dir = _skills(tmp_path) / "reconcile-gl"
    store = ImprovementStore(tmp_path / "imp", AuditLog(tmp_path / "audit.jsonl"))
    with pytest.raises(R.RulesError):
        _control_card(store, skill_dir, warn=500)
    assert store.cards() == []
