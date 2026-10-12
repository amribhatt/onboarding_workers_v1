"""Migration M13: the engine's contract at `evaluate`, over the fakes of `worker.ports` (no model, no network).

Audit first, rerun idempotency, a stale approval, an agent named as approver, resume after a stop, the Refused codes,
paging from the spool, the two `required_blank` levels as separate namespaces, and a million-line GL in bounded
memory. Fictitious names only.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from port_fakes import FakeSkills, MemoryReader, MemoryWorld
from worker import engine as E
from worker import engine_stages as ES
from worker import sample as S
from worker.accounts import key_of
from worker.audit import AuditError
from worker.ports import ListAudit, MemoryStore, Ports, ProductionSkills, Progress
from worker.rulefiles import Problem, SchemaError

SPONSOR = "sponsor-a"
INVESTORS = [
    ["Investor ID", "Investor Name", "Country"],
    ["HS-1", "Alpha Fund LP", "US"],
    ["HS-2", "Beta Partners", "UK"],
    ["HS-3", "Gamma Holdings", "DE"],
]
SCHEME = {"mode": "sequential", "prefix": "GINV-", "width": 4, "start": 1, "pattern": ""}


@dataclass
class Rig:
    reader: MemoryReader
    world: MemoryWorld
    audit: ListAudit
    store: MemoryStore
    ports: Ports
    spec: E.JobSpec
    out: Path

    def with_ports(self, **kw: Any) -> Rig:
        return replace(self, ports=replace(self.ports, **kw))


def approval_for(ev: E.Evaluation, by: str = "reviewer-x", approval_id: str = "appr-1") -> E.Approval:
    warns = tuple(f.id for f in ev.findings if f.severity == "WARN")
    return E.Approval(approval_id, by, ev.manifest.eval_key, ev.findings_digest, warns, "2026-10-11T09:00:00Z")


def investor_steps(*, ids: bool = True, quality: bool = False) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = [
        {"step": "lift", "source": 0, "columns": {"Investor_ID": "A", "Investor_Name": "B", "Country": "C"},
         "header_row": 1, "first_row": 2, "constants": {"GP_ID": SPONSOR}}]
    if ids:
        steps.append({"step": "assign_ids", "id_field": "Investor_ID", "source_key_field": "Source_Investor_Key"})
    if quality:
        steps.append({"step": "check_data_quality", "source": 0, "record_type": "Investors",
                      "columns": {"Investor_ID": "Investor ID", "Investor_Name": "Investor Name",
                                  "Country": "Country"}})
    return steps


def rig(tmp_path: Path, rows: list[list[Any]] | None = None, *, steps: list[dict[str, Any]] | None = None,
        record_type: str = "Investors", audit: ListAudit | None = None, choices: tuple[E.Choice, ...] = (),
        skills: Any = None, world: MemoryWorld | None = None, ranker: Any = None, max_retries: int = 0,
        progress: Any = None, files: dict[str, list[list[Any]]] | None = None, fingerprint: str = "") -> Rig:
    reader = MemoryReader(files or {"investors.csv": rows or INVESTORS})
    world = world or MemoryWorld(SPONSOR, schemes={"Investors": SCHEME})
    audit = audit or ListAudit()
    store = MemoryStore()
    ports = Ports(world, reader, audit, store, tmp_path / "out", skills or ProductionSkills(SPONSOR),
                  ranker=ranker, progress=progress, max_retries=max_retries)
    name = next(iter(files)) if files else "investors.csv"
    spec = E.JobSpec("job-1", SPONSOR, record_type, None, (E.SourceRef("file", name, None, 1, fingerprint),),
                     E.PinSet(), E.Recipe.of(*(steps or investor_steps())), choices, {"gp_id": SPONSOR})
    return Rig(reader, world, audit, store, ports, spec, tmp_path / "out")


def uploads(out: Path) -> list[Path]:
    return sorted(out.glob("*_Upload*")) if out.is_dir() else []


# ---- audit first ---------------------------------------------------------------------------------------------
def test_engine_audit_first_a_failing_sink_refuses_before_any_read(tmp_path: Path) -> None:
    r = rig(tmp_path, audit=ListAudit(fail_all=True))
    with pytest.raises(E.Refused) as err:
        E.evaluate(r.spec, r.ports)
    assert err.value.code == "audit_unavailable"
    assert (r.reader.fingerprinted, r.reader.paths, r.reader.pulled) == (0, 0, 0)  # the source was never touched
    assert r.store.data == {} and r.world.commits == [] and r.world.reservations == 0 and not uploads(r.out)


def test_engine_eval_started_is_the_first_record_and_carries_the_pins(tmp_path: Path) -> None:
    r = rig(tmp_path)
    ev = E.evaluate(r.spec, r.ports)
    first = r.audit.entries[0]
    assert first["action"] == "eval.started" and first["seq"] == 1
    assert {"read", "map", "convert", "check"} == {e["inputs"]["stage"] for e in r.audit.entries
                                                   if e["action"] == "stage.done"}
    pins = first["outputs"]["pins"]
    assert pins["engine"] == E.ENGINE_VERSION and pins["target"] and pins["controls"] and pins["golden_coa"]
    assert "check-data-quality" in pins["skills"] and "map-accounts" in pins["skills"]
    assert ev.manifest.pins.skills == pins["skills"] and ev.manifest.pins.schema_versions
    assert list(ev.audit_seqs) == [e["seq"] for e in r.audit.entries]


def test_engine_a_sink_that_fails_when_the_upload_is_recorded_leaves_no_file_and_no_commit(tmp_path: Path) -> None:
    r = rig(tmp_path, audit=ListAudit(fail_on=["write_upload"]))
    first = E.evaluate(r.spec, r.ports)
    with pytest.raises(E.Refused) as err:
        E.evaluate(r.spec, r.ports, approval=approval_for(first), stop_after="write")
    assert err.value.code == "audit_unavailable"
    assert not uploads(r.out) and not list(r.out.glob(".*")) and r.world.commits == []
    assert r.world.counters["Investors"] == 1  # the counter did not move


def test_engine_a_commit_that_fails_removes_the_file_and_records_it(tmp_path: Path) -> None:
    r = rig(tmp_path)
    first = E.evaluate(r.spec, r.ports)
    r.world.fail_commit = True
    with pytest.raises(E.StageFailed) as err:
        E.evaluate(r.spec, r.ports, approval=approval_for(first), stop_after="write")
    assert err.value.stage == "write"
    assert not uploads(r.out)
    assert "write_upload" in r.audit.actions() and "write_upload.failed" in r.audit.actions()


# ---- idempotency ---------------------------------------------------------------------------------------------
def test_engine_rerun_gives_the_same_ids_and_the_counter_moves_once(tmp_path: Path) -> None:
    r = rig(tmp_path)
    first = E.evaluate(r.spec, r.ports)
    assert first.outcome == "complete" and r.world.counters["Investors"] == 1  # nothing moves before approval
    again = E.evaluate(r.spec, r.ports)
    assert again.reused == ("read", "map", "convert", "check")
    appr = approval_for(first)
    w1 = E.evaluate(r.spec, r.ports, approval=appr, stop_after="write")
    assert w1.produced is not None
    assert w1.produced.ids_issued == ("GINV-0001", "GINV-0002", "GINV-0003")
    assert r.world.counters["Investors"] == 4 and len(r.world.commits) == 1
    w2 = E.evaluate(r.spec, r.ports, approval=appr, stop_after="write")  # the same call again
    w3 = E.evaluate(r.spec, r.ports, approval=replace(appr, approval_id="appr-2", by="bao"), stop_after="write")
    assert w2.produced == w1.produced and w3.produced is not None and w3.produced.path == w1.produced.path
    assert "write" in w2.reused and "write" in w3.reused
    assert len(uploads(r.out)) == 1 and not list(r.out.glob("*_v2*"))
    assert r.world.counters["Investors"] == 4 and len(r.world.commits) == 1
    assert r.audit.actions().count("write_upload") == 1 and "eval.reused" in r.audit.actions()


def test_engine_a_second_evaluation_of_the_same_job_reserves_the_same_ids(tmp_path: Path) -> None:
    r = rig(tmp_path)
    a = E.evaluate(r.spec, r.ports)
    other = replace(r.spec, choices=(E.Choice("supply", "field:Investor_Type", "LP", "job", "reviewer-x", "all LPs"),))
    b = E.evaluate(other, r.ports)  # another eval_key: convert runs again, against the same counter
    ids = lambda ev: [o.dest["Investor_ID"] for o in ev.rows("ok")]
    assert ids(a) == ids(b) == ["GINV-0001", "GINV-0002", "GINV-0003"]
    assert r.world.counters["Investors"] == 1


# ---- approval: verified, never made ---------------------------------------------------------------------------
def test_engine_stale_or_missing_approval_is_refused(tmp_path: Path) -> None:
    r = rig(tmp_path)
    first = E.evaluate(r.spec, r.ports)
    good = approval_for(first)
    with pytest.raises(E.Refused) as err:
        E.evaluate(r.spec, r.ports, stop_after="write")
    assert err.value.code == "no_approval"
    with pytest.raises(E.Refused) as err:  # the person looked at other findings
        E.evaluate(r.spec, r.ports, approval=replace(good, findings_digest="0" * 64), stop_after="write")
    assert err.value.code == "stale_approval"
    changed = replace(r.spec, choices=(E.Choice("supply", "field:Investor_Type", "LP", "job", "reviewer-x", "LPs"),))
    with pytest.raises(E.Refused) as err:  # the job changed after they looked: another eval_key
        E.evaluate(changed, r.ports, approval=good, stop_after="write")
    assert err.value.code == "stale_approval" and not uploads(r.out) and r.world.commits == []


def test_engine_an_approval_is_stale_when_the_sponsors_records_changed_after_the_check(tmp_path: Path) -> None:
    r = rig(tmp_path)
    first = E.evaluate(r.spec, r.ports)
    r.world.records["Investors"] = [{"key": "GINV-0099", "name": "Delta Trust", "source_key": "HS-9"}]
    with pytest.raises(E.Refused) as err:
        E.evaluate(r.spec, r.ports, approval=approval_for(first), stop_after="write")
    assert err.value.code == "stale_approval" and not uploads(r.out)


@pytest.mark.parametrize("name", ["agent", "improver", "system", "console", "local", "policy:auto", "gpt-4o-mini",
                                  "Conductor", "key_resolver"])
def test_engine_an_agent_or_reserved_name_as_approver_is_refused(tmp_path: Path, name: str) -> None:
    r = rig(tmp_path)
    first = E.evaluate(r.spec, r.ports)
    with pytest.raises(E.Refused) as err:
        E.evaluate(r.spec, r.ports, approval=approval_for(first, by=name), stop_after="write")
    assert err.value.code == "agent_approver"
    assert not uploads(r.out) and r.world.commits == []
    assert "eval.refused" in r.audit.actions()  # the refusal is on the record


def test_engine_a_choice_made_in_an_agents_name_is_refused(tmp_path: Path) -> None:
    c = E.Choice("supply", "field:Investor_Type", "LP", "job", "agent", "the model decided")
    r = rig(tmp_path, choices=(c,))
    with pytest.raises(E.Refused) as err:
        E.evaluate(r.spec, r.ports)
    assert err.value.code == "agent_approver" and r.reader.fingerprinted == 0


def test_engine_open_err_blocks_the_write_and_a_warn_must_be_acknowledged(tmp_path: Path) -> None:
    dirty = [INVESTORS[0], ["HS-1", "Alpha Fund LP", "US"], ["HS-1", "Alpha Again", "US"], ["", "No Id Ltd", "FR"]]
    r = rig(tmp_path, dirty, steps=investor_steps(ids=False))
    ev = E.evaluate(r.spec, r.ports)
    assert ev.outcome == "needs_human" and any(f.severity == "ERR" for f in ev.findings)
    with pytest.raises(E.Refused) as err:
        E.evaluate(r.spec, r.ports, approval=approval_for(ev), stop_after="write")
    assert err.value.code == "open_err" and not uploads(r.out)
    clean = rig(tmp_path / "c", [INVESTORS[0], ["HS-1", "Alpha Fund LP", "US"]], steps=investor_steps(ids=False))
    cv = E.evaluate(clean.spec, clean.ports)
    warns = [f for f in cv.findings if f.severity == "WARN"]
    assert not [f for f in cv.findings if f.severity == "ERR"]
    if warns:
        with pytest.raises(E.Refused) as err:
            E.evaluate(clean.spec, clean.ports, approval=replace(approval_for(cv), acknowledged=()),
                       stop_after="write")
        assert err.value.code == "no_approval"


# ---- resume --------------------------------------------------------------------------------------------------
class _FailOnNth(ListAudit):
    """Fails when the n-th record of `action` is written (and only then)."""

    def __init__(self, action: str, n: int) -> None:
        super().__init__()
        self.action, self.n, self.seen = action, n, 0

    def record_many(self, action: str, items: list[Any]) -> list[dict[str, Any]]:
        if action == self.action:
            self.seen += 1
            if self.seen == self.n:
                raise AuditError("the sink went away")
        return super().record_many(action, items)


def test_engine_resumes_after_a_mid_run_stop_at_the_last_sealed_stage(tmp_path: Path) -> None:
    r = rig(tmp_path, audit=_FailOnNth("stage.done", 3))  # read and map are sealed, convert is not
    with pytest.raises(E.Refused) as err:
        E.evaluate(r.spec, r.ports)
    assert err.value.code == "audit_unavailable"
    sealed = {k for k, v in r.store.seals.items() if k.endswith("/meta")}
    assert len(sealed) == 2
    audit2 = ListAudit()
    ports2 = replace(r.ports, audit=audit2)
    ev = E.evaluate(r.spec, ports2)
    assert ev.outcome == "complete" and ev.reused == ("read", "map")
    done = [e["inputs"]["stage"] for e in audit2.entries if e["action"] == "stage.done"]
    assert done == ["convert", "check"]  # read and map were not run again
    fresh = rig(tmp_path / "fresh")
    assert E.evaluate(fresh.spec, fresh.ports).manifest.spool_checksum == ev.manifest.spool_checksum


def _gl_rows(lines: int = 30) -> list[list[Any]]:
    rows: list[list[Any]] = [list(S.GLSRC)]
    for n in range(lines // 2):
        for ln, (acct, dr, cr) in enumerate((("1000", 100, None), ("3000", None, 100)), start=1):
            rows.append(["2025-12-31", f"J-{n + 1}", ln, acct, "name", "Capital call", dr, cr, "line", None, None,
                         None, "Deal X", "Fund A", "B1"])
    return rows


def _gl_steps() -> list[dict[str, Any]]:
    return [{"step": "lift_gl_lines", "source": 0, "fund_id": "F-1", "fund_name": "Fund A"},
            {"step": "map_accounts"},
            {"step": "check_data_quality", "source": 0, "record_type": "GL",
             "columns": {"Account": "GL Account", "Deal": "Investment", "Journal": "Journal", "Debit": "Debit",
                         "Credit": "Credit"}}]


def _gl_rig(tmp_path: Path, lines: int = 30, **kw: Any) -> Rig:
    picks = (E.Choice("map_account", f"key:{key_of('', '1000', 'Capital call')}", "11010", "job", "reviewer-x",
                      "the bank account"),
             E.Choice("map_account", f"key:{key_of('', '3000', 'Capital call')}", "31010", "job", "reviewer-x",
                      "partner capital"))
    return rig(tmp_path, steps=_gl_steps(), record_type="GL", choices=picks,
               files={"gl.csv": _gl_rows(lines)}, world=MemoryWorld(SPONSOR), **kw)


def test_engine_a_stage_that_fails_is_retried_then_resumes_from_the_sealed_ones(tmp_path: Path) -> None:
    class Flaky:
        model = "scripted"
        calls = 0

        def rank(self, keys: Any) -> dict[str, str]:
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise OSError("the model service is down")
            return {}

    gl = _gl_rows(10)
    gl.append(["2025-12-31", "J-9", 1, "6150", "name", "Valuation", 5, None, "x", None, None, None, "Deal X", "Fund A",
               "B1"])
    gl.append(["2025-12-31", "J-9", 2, "3000", "name", "Valuation", None, 5, "x", None, None, None, "Deal X", "Fund A",
               "B1"])
    r = rig(tmp_path, steps=_gl_steps(), record_type="GL", files={"gl.csv": gl}, world=MemoryWorld(SPONSOR),
            ranker=Flaky(), max_retries=0)
    with pytest.raises(E.StageFailed) as err:
        E.evaluate(r.spec, r.ports)
    assert err.value.stage == "map" and "stage.failed" in r.audit.actions()
    ev = E.evaluate(r.spec, r.ports)  # the model service is back
    assert ev.reached == "check" and "read" in ev.reused and Flaky.calls == 2
    assert ev.manifest.model == "scripted" and ev.manifest.pins.models == {"small": "scripted"}
    assert any(p["key"] == key_of("", "6150", "Valuation") for p in ev.proposals)  # a proposal, never applied


def test_engine_read_resumes_at_the_last_chunk_not_at_row_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ES, "CHUNK_LINES", 10)
    seen: list[int] = []
    boom = {"on": True}

    def progress(p: Progress) -> None:
        if p.stage == "read" and p.note == "chunk":
            seen.append(p.rows)
            if boom["on"] and p.rows == 20:
                raise RuntimeError("the machine went away")

    r = _gl_rig(tmp_path, 36, progress=progress)
    with pytest.raises(E.StageFailed) as err:
        E.evaluate(r.spec, r.ports)
    assert err.value.stage == "read" and seen == [10, 20]
    boom["on"] = False
    seen.clear()
    ev = E.evaluate(r.spec, r.ports)
    assert seen == [30]  # chunks 10 and 20 were kept: the run went on from line 21
    assert ev.tally["read"] == 36 and ev.outcome == "complete"
    clean = _gl_rig(tmp_path / "clean", 36)
    want = E.evaluate(clean.spec, clean.ports)
    assert ev.manifest.spool_checksum == want.manifest.spool_checksum
    assert [o.dest for o in ev.rows(limit=1000)] == [o.dest for o in want.rows(limit=1000)]


# ---- keys and stages -----------------------------------------------------------------------------------------
def test_engine_eval_key_ignores_the_trace_id_and_stage_keys_cover_only_their_inputs(tmp_path: Path) -> None:
    r = rig(tmp_path)
    a = r.spec
    b = replace(a, context={**a.context, "trace_id": "t-123"})
    assert a.eval_key() == b.eval_key()
    c = replace(a, choices=(E.Choice("supply", "field:Investor_Type", "LP", "job", "reviewer-x", "LPs"),))
    assert a.eval_key() != c.eval_key()
    for stage in ("read", "map"):
        assert a.stage_key(stage) == c.stage_key(stage)  # a changed fill does not re-read or re-map
    for stage in ("convert", "check", "write"):
        assert a.stage_key(stage) != c.stage_key(stage)
    assert a.stage_key("write") != a.stage_key("write", E.Approval("x", "reviewer-x", "e", "d"))
    d = replace(a, context={**a.context, "fund": "Fund B"})
    assert a.stage_key("read") != d.stage_key("read")


def test_engine_a_changed_fill_runs_convert_and_check_again_not_read_and_map(tmp_path: Path) -> None:
    r = rig(tmp_path)
    E.evaluate(r.spec, r.ports)
    r.audit.entries.clear()
    filled = replace(r.spec, choices=(E.Choice("supply", "field:Investor_Type", "LP", "job", "reviewer-x", "LPs"),))
    ev = E.evaluate(filled, r.ports)
    assert ev.reused == ("read", "map")
    assert [e["inputs"]["stage"] for e in r.audit.entries if e["action"] == "stage.done"] == ["convert", "check"]
    assert {o.dest["Investor_Type"] for o in ev.rows()} == {"LP"}
    assert any(e["action"] == "choice.applied" and e["outputs"]["inferred"] is False for e in r.audit.entries)


# ---- the Refused codes -----------------------------------------------------------------------------------------
def test_engine_refuses_a_source_that_changed_since_its_fingerprint(tmp_path: Path) -> None:
    r = rig(tmp_path, fingerprint="0" * 64)
    with pytest.raises(E.Refused) as err:
        E.evaluate(r.spec, r.ports)
    assert err.value.code == "source_changed" and r.reader.paths == 0
    good = replace(r.spec, sources=(replace(r.spec.sources[0], fingerprint=r.reader.fingerprint(r.spec.sources[0])),))
    assert E.evaluate(good, r.ports).outcome == "complete"


def test_engine_refuses_an_answer_key_and_a_file_outside_the_inbox(tmp_path: Path) -> None:
    r = rig(tmp_path)
    key = replace(r.spec, sources=(E.SourceRef("file", "answer_key/investors.xlsx", None, 1, ""),))
    with pytest.raises(E.Refused) as err:
        E.evaluate(key, r.ports)
    assert err.value.code == "path_refused"
    gone = replace(r.spec, sources=(E.SourceRef("file", "nothing.csv", None, 1, ""),))
    with pytest.raises(E.Refused) as err:
        E.evaluate(gone, r.ports)
    assert err.value.code == "path_refused"


def test_engine_refuses_rule_files_that_do_not_load_and_records_it_first(tmp_path: Path) -> None:
    bad = SchemaError([Problem("skills/x/checks.yaml", "rules[0]", "missing_field", "no severity")])
    r = rig(tmp_path, skills=FakeSkills(SPONSOR, refuse=bad))
    with pytest.raises(E.Refused) as err:
        E.evaluate(r.spec, r.ports)
    assert err.value.code == "schema_invalid"
    assert r.audit.actions()[0] == "skill.load_refused" and r.reader.fingerprinted == 0


def test_engine_refuses_a_control_out_of_bounds(tmp_path: Path) -> None:
    bad = SchemaError([Problem("skills/check-data-quality/controls.yaml", "max.id_length", "out_of_bounds", "99 > 30")])
    r = rig(tmp_path, skills=FakeSkills(SPONSOR, refuse=bad))
    with pytest.raises(E.Refused) as err:
        E.evaluate(r.spec, r.ports)
    assert err.value.code == "control_out_of_bounds"


def test_engine_refuses_an_unknown_transform_and_a_pin_that_moved(tmp_path: Path) -> None:
    steps = investor_steps(ids=False)
    steps.append({"step": "transform", "source": 0, "recipe": {
        "schema_version": 1, "id": "r1", "sponsor": SPONSOR,
        "fields": {"Investor_Name": {"from": {"header": "Investor Name"}, "ops": [{"op": "teleport"}]}}}})
    r = rig(tmp_path, steps=steps)
    with pytest.raises(E.Refused) as err:
        E.evaluate(r.spec, r.ports)
    assert err.value.code == "unknown_transform" and r.reader.fingerprinted == 0
    ok = rig(tmp_path / "ok")
    pinned = replace(ok.spec, pins=E.PinSet(skills={"map-accounts": "000000000000"}))
    with pytest.raises(E.Refused) as err:
        E.evaluate(pinned, ok.ports)
    assert err.value.code == "schema_invalid" and "promoted" in err.value.sentence


def test_engine_too_large_is_a_refusal_when_a_cap_is_set_and_no_cap_when_it_is_not(tmp_path: Path) -> None:
    capped = _gl_rig(tmp_path / "a", 30, skills=FakeSkills(SPONSOR, limits={"max_keys": 3}))
    with pytest.raises(E.Refused) as err:
        E.evaluate(capped.spec, capped.ports)
    assert err.value.code == "too_large"
    free = _gl_rig(tmp_path / "b", 30)
    assert E.evaluate(free.spec, free.ports).outcome == "complete"  # unset = no cap


def test_engine_an_audit_log_that_fails_between_stages_refuses_rather_than_continue(tmp_path: Path) -> None:
    r = rig(tmp_path, audit=ListAudit(fail_on=["stage.done"]))
    with pytest.raises(E.Refused) as err:
        E.evaluate(r.spec, r.ports)
    assert err.value.code == "audit_unavailable" and not any(k.endswith("/meta") for k in r.store.seals)


# ---- what comes back -------------------------------------------------------------------------------------------
def test_engine_rows_are_paged_from_the_spool_and_the_digest_is_capped(tmp_path: Path) -> None:
    rows = [INVESTORS[0], *[[f"HS-{n}", f"Investor {n}", "US"] for n in range(1, 41)]]
    r = rig(tmp_path, rows)
    ev = E.evaluate(r.spec, r.ports)
    page = list(ev.rows("ok", offset=10, limit=5))
    assert [o.i for o in page] == [11, 12, 13, 14, 15] and all(o.dest and o.status == "ok" for o in page)
    assert page[0].dest["Investor_ID"] == "GINV-0011" and page[0].src_row == 12
    assert len(list(ev.rows(limit=1000))) == 40 and list(ev.rows("err")) == []
    text = ev.digest(max_chars=120)
    assert len(text) <= 120 and "outcome: complete" in text
    assert ev.tally == {"read": 40, "out": 40, "warn": ev.tally["warn"], "err": 0, "skip": 0}
    assert ev.tie_out is not None and ev.tie_out.rows_out == 40 and ev.tie_out.ties


def test_engine_the_two_required_blank_levels_are_separate_namespaces(tmp_path: Path) -> None:
    rows = [INVESTORS[0], ["HS-1", "Alpha Fund LP", "US"], ["HS-2", "", "UK"]]
    r = rig(tmp_path, rows, steps=investor_steps(ids=False, quality=True))
    ev = E.evaluate(r.spec, r.ports)
    blanks = {f.rule.rule_id: f.severity for f in ev.findings if f.rule.rule_id.endswith("required_blank")}
    assert blanks == {"checks.required_blank": "WARN", "check-data-quality.required_blank": "ERR"}
    q = next(f for f in ev.findings if f.rule.rule_id == "check-data-quality.required_blank")
    assert q.rule.skill == "check-data-quality" and q.rule.pin == ev.manifest.pins.skills["check-data-quality"]
    assert q.rule.file.startswith("skills/check-data-quality/checks.yaml:rules[") and q.family == "completeness"
    assert q.ranges_text == "3" and q.sample == ()
    assert [o.status for o in ev.rows(limit=10)] == ["ok", "err"]  # the blank name is the ERR, from the exact ranges


def test_engine_missing_stays_blank_and_is_reported(tmp_path: Path) -> None:
    r = rig(tmp_path, steps=investor_steps(ids=False))
    ev = E.evaluate(r.spec, r.ports)
    gaps = {m.field: m.reason for m in ev.missing if m.reason != "blank_in_source"}
    assert gaps["Investor_Type"] == "no_source" and gaps["Investor_Status"] == "no_source"
    assert "GP_ID" not in gaps and all(o.dest["Investor_Type"] == "" for o in ev.rows())
    declared = replace(r.spec, choices=(E.Choice("declare_missing", "field:Investor_Type", None, "job", "reviewer-x",
                                                 "the sponsor has no types"),))
    assert {m.field: m.reason for m in E.evaluate(declared, r.ports).missing}["Investor_Type"] == "declared_missing"


def test_engine_exclude_and_correct_are_the_persons_choices_and_are_recorded(tmp_path: Path) -> None:
    r = rig(tmp_path, steps=investor_steps(ids=False))
    base = E.evaluate(r.spec, r.ports)
    src = next(o for o in base.rows() if o.dest["Investor_ID"] == "HS-2").dest["_src"]
    choices = (E.Choice("exclude", f"src:{src}", "incorrect", "job", "reviewer-x", "a test account"),
               E.Choice("correct_id", f"src:{base.staged().__next__()['_src']}|Investor_ID", "HS-001", "job", "reviewer-x",
                        "typo in the source"))
    ev = E.evaluate(replace(r.spec, choices=choices), r.ports)
    got = [o.dest["Investor_ID"] for o in ev.rows() if o.dest is not None]
    assert got == ["HS-001", "HS-3"] and ev.tally["read"] == 3
    skipped = list(ev.rows("skip"))
    assert len(skipped) == 1 and "exclude_incorrect" in skipped[0].why and ev.tally["skip"] == 1
    done = [e for e in r.audit.entries if e["action"] == "choice.applied"]
    assert {e["rationale"] for e in done} >= {"a test account", "typo in the source"}


def test_engine_a_gl_with_unmapped_accounts_is_needs_human_and_spools_err_rows(tmp_path: Path) -> None:
    gl = _gl_rows(10)
    gl.append(["2025-12-31", "J-8", 1, "6150", "x", "Valuation", 5, None, "x", None, None, None, "Deal X", "Fund A", "B1"])
    gl.append(["2025-12-31", "J-8", 2, "3000", "x", "Valuation", None, 5, "x", None, None, None, "Deal X", "Fund A", "B1"])
    r = rig(tmp_path, steps=_gl_steps(), record_type="GL", files={"gl.csv": gl}, world=MemoryWorld(SPONSOR))
    ev = E.evaluate(r.spec, r.ports)
    assert ev.outcome == "needs_human"
    errs = list(ev.rows("err", limit=100))
    assert len(errs) == len(gl) - 1  # every line has an account the check cannot place until a person picks one
    assert any(f.rule.rule_id == "checks.account_unmapped" and f.rule.control for f in ev.findings)
    assert ev.proposals == () or all("account" in p for p in ev.proposals)


def test_engine_checks_every_row_at_any_batch_size_and_has_no_table_bound(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The review is set-based over the whole table: loading it in batches of 7 rows or of 50,000 gives the same
    findings, and no table-level check is ever skipped for size."""
    gl = _gl_rows(20)
    gl.append(["2025-12-31", "J-99", 1, "6150", "x", "Valuation", 5, None, "x", None, None, None, "", "Fund A", "B1"])
    gl.append(["2025-12-31", "J-99", 2, "3000", "x", "Valuation", None, 4, "x", None, None, None, "Deal X", "Fund A",
               "B1"])

    def run(root: Path) -> E.Evaluation:
        r = rig(root, steps=_gl_steps(), record_type="GL", files={"gl.csv": gl}, world=MemoryWorld(SPONSOR))
        return E.evaluate(r.spec, r.ports)

    def facts(ev: E.Evaluation) -> list[tuple[str, int, str]]:
        return sorted((f.rule.rule_id, f.count, f.severity) for f in ev.findings)

    whole = run(tmp_path / "whole")
    monkeypatch.setattr(E, "BATCH_ROWS", 7)
    small = run(tmp_path / "small")
    assert facts(small) == facts(whole)
    assert "check-data-quality.journal_unbalanced" in {f.rule.rule_id for f in small.findings}
    assert not hasattr(E, "TABLE_MAX_ROWS")
    assert not [f for f in small.findings if f.rule.rule_id == "engine.table_checks_skipped"]
    assert {o.status for o in small.rows(limit=1000)} <= {"ok", "warn", "err"}
    assert small.tie_out is not None and small.tie_out.journals == 11


# ---- a million lines in bounded memory -----------------------------------------------------------------------
@pytest.mark.slow
def test_engine_bounded_memory_1m_gl(tmp_path: Path) -> None:
    """One million GL lines through read, map, convert and check: row outcomes spill to disk, so the peak is the
    streaming check's own state and the distinct keys, never the table."""
    script = tmp_path / "big_engine.py"
    script.write_text(textwrap.dedent('''
        import sys
        from pathlib import Path
        sys.path.insert(0, sys.argv[2])
        import large_gl
        from worker import engine as E
        from worker.accounts import key_of
        from worker.audit import AuditLog
        from memprobe import peak_memory_mb
        from worker.hashing import sha256_file
        from port_fakes import MemoryWorld
        from worker.ports import FileReader, FileStore, Ports, ProductionSkills

        root = Path(sys.argv[1])
        inbox = root / "inbox"
        inbox.mkdir()
        src = large_gl.build(inbox / "gl.csv")
        # approved in an earlier job: only 6150 is unmapped
        accounts = {key_of("", a, "Expense"): {"key": key_of("", a, "Expense"), "account": "11010", "source": "earlier"}
                    for a in ("1000", "1500", "1600", "8050", "9010")}
        world = MemoryWorld("sponsor-a", accounts=accounts)
        ports = Ports(world, FileReader([inbox]), AuditLog(root / "out" / "audit.jsonl"), FileStore(root / "engine"),
                      root / "out", ProductionSkills("sponsor-a"))
        steps = [{"step": "lift_gl_lines", "source": 0, "fund_id": "F-1", "fund_name": "Fund A"},
                 {"step": "map_accounts"},
                 {"step": "check_data_quality", "source": 0, "record_type": "GL",
                  "columns": {"Account": "GL Account", "Deal": "Investment", "Journal": "Journal",
                              "Debit": "Debit", "Credit": "Credit"}}]
        spec = E.JobSpec("job-big", "sponsor-a", "GL", None, (E.SourceRef("file", "gl.csv", None, 1, sha256_file(src)),),
                         E.PinSet(), E.Recipe.of(*steps), (), {"gp_id": "sponsor-a"})
        ev = E.evaluate(spec, ports)
        by = {f.rule.rule_id: f.count for f in ev.findings}
        page = list(ev.rows("err", offset=10_000, limit=3))
        print("RESULT", ev.tally["read"], by.get("check-data-quality.account_not_in_chart"),
              by.get("check-data-quality.deal_required"), len(page), page[0].src_row if page else 0,
              ev.reached, ev.outcome, round(peak_memory_mb()))
    '''), encoding="utf-8")
    env = {"SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), "PATH": "", "PYTHONPATH": os.pathsep.join(
        [str(Path("src").resolve()), str(Path("tests").resolve())])}
    done = subprocess.run([sys.executable, str(script), str(tmp_path), str(Path("tests").resolve())],
                          capture_output=True, text=True, timeout=3000, env=env, check=False)
    line = next((x for x in done.stdout.splitlines() if x.startswith("RESULT")), done.stderr[-2500:])
    assert done.returncode == 0, line
    print(line)  # `pytest -rP` shows it: the peak in MB is the last number
    _, read, unmapped, blank, page, first, reached, outcome, peak = line.split()
    assert int(read) == 999_999 and int(unmapped) == 10_214 and int(blank) == 40_000
    assert int(page) == 3 and int(first) > 0 and reached == "check" and outcome == "needs_human"
    assert int(peak) < 800  # the table is on disk (Parquet parts), never in memory: 1M dict rows are > 2 GB; ~150 MB is the Arrow and DuckDB libraries
