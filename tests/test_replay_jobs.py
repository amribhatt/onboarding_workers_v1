"""Migration M19: replay records at the write, the engine release check, and
the manifest on every write (ticket 04 R2-R4). Scripted model and fakes only; no live model; fictitious names.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import test_static_clean as F5
from id_decision import decide_ids
from port_fakes import MemoryWorld
from scripted_model import approve
from worker import engine as E
from worker import engine_write as EW
from worker import replay as R
from worker.audit import AuditLog
from worker.console import Console
from worker.ports import FileReader, FileStore, ListAudit, Ports, ProductionSkills

SPONSOR = "sponsor-a"
SCHEME = {"mode": "sequential", "prefix": "GINV-", "width": 4, "start": 1, "pattern": ""}
CSV = "Investor ID,Investor Name,Country\nHS-1,Alpha Fund LP,US\nHS-2,Beta Partners,UK\nHS-3,Gamma Holdings,DE\n"
STEPS = [{"step": "lift", "source": 0, "columns": {"Investor_ID": "A", "Investor_Name": "B", "Country": "C"},
          "header_row": 1, "first_row": 2, "constants": {"GP_ID": SPONSOR}},
         {"step": "assign_ids", "id_field": "Investor_ID", "source_key_field": "Source_Investor_Key"}]


class Rig:
    """One Investors job over real files: inbox, the engine's file store under `state/engine`, records under
    `state/replay`."""

    def __init__(self, tmp: Path, *, audit: Any = None, choices: tuple[E.Choice, ...] = ()) -> None:
        self.inbox = tmp / "inbox"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.src = self.inbox / "investors.csv"
        if not self.src.exists():
            self.src.write_text(CSV, encoding="utf-8")
        self.state = tmp / "state"
        self.root = self.state / "replay"
        self.out = tmp / "out"
        self.world = MemoryWorld(SPONSOR, schemes={"Investors": SCHEME})
        self.audit = audit or ListAudit()
        self.ports = Ports(self.world, FileReader([self.inbox]), self.audit, FileStore(self.state / "engine"),
                           self.out, ProductionSkills(SPONSOR))
        self.spec = E.JobSpec("job-1", SPONSOR, "Investors", None, (E.SourceRef("file", "investors.csv", None, 1),),
                              E.PinSet(), E.Recipe.of(*STEPS), choices, {"gp_id": SPONSOR})

    def approval(self, ev: E.Evaluation, by: str = "reviewer-x") -> E.Approval:
        warns = tuple(f.id for f in ev.findings if f.severity == "WARN")
        return E.Approval("appr-1", by, ev.manifest.eval_key, ev.findings_digest, warns, "2026-10-11T09:00:00Z")

    def write(self) -> E.Evaluation:
        ev = E.evaluate(self.spec, self.ports)
        return E.evaluate(self.spec, self.ports, approval=self.approval(ev), stop_after="write")

    def record(self) -> R.ReplayRecord:
        (path,) = sorted((self.root / "sponsor-a").glob("*.json"))
        return R.ReplayRecord.load(path)


# ---- VOLATILE: the identities of a run ------------------------------------------------------------------------
def test_the_identities_of_a_run_are_volatile_and_everything_else_is_data() -> None:
    """eval_key / stage keys / fingerprints follow the pins and the bytes read (a candidate pin changes them by
    construction; the inputs are checked by sha256 on their own); an approval id, its time and the record checksum
    are fresh per approval. Nothing that was decided is on the list."""
    a = {"manifest": {"eval_key": "a" * 64, "stage_keys": {"read": "x"}, "sources": [{"ref": "f", "fingerprint": "1"}],
                      "replay_record": {"ref": "s/j.json", "record_sha256": "1"}, "pins": {"skills": {"m": "p1"}}},
         "approval_id": "appr-1", "approved_at": "t1", "inputs": {"spec_key": "k", "stage_key": "s"}}
    b = json.loads(json.dumps(a))
    b["manifest"].update(eval_key="b" * 64, stage_keys={"read": "y"}, sources=[{"ref": "f", "fingerprint": "2"}],
                         replay_record={"ref": "s/j.json", "record_sha256": "2"})
    b.update(approval_id="appr-2", approved_at="t2", inputs={"spec_key": "k2", "stage_key": "s2"})
    assert R.canonical(a) == R.canonical(b)
    c = json.loads(json.dumps(a))
    c["manifest"]["pins"]["skills"]["m"] = "p2"  # a pin is what was in force: data
    assert R.canonical(a) != R.canonical(c)
    d = json.loads(json.dumps(a))
    d["manifest"]["replay_record"]["ref"] = "s/other.json"
    assert R.canonical(a) != R.canonical(d)


def test_the_content_hasher_equals_the_sheet_digest() -> None:
    rows = [["A", "B"], ["x", 1], ["", None], ["y", ""]]
    h = R.ContentHasher("Investors")
    for r in rows:
        h.row(r)
    assert h.hexdigest() == R.cells_digest([("Investors", rows)])


# ---- R2: the record, written before the write_upload entry ------------------------------------------------------
class _Watch(ListAudit):
    """Fails the test if the write_upload entry is written before the record exists."""

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self.seen_record = False

    def record_many(self, action: str, items: list[tuple[str, dict | None, dict | None]]) -> list[dict]:
        if action == "write_upload":
            ref = items[0][2]["replay_record"]["ref"]  # type: ignore[index]
            self.seen_record = (self.root / ref).is_file()
            assert self.seen_record, "the write_upload entry came before its replay record"
        return super().record_many(action, items)


def test_a_write_leaves_a_record_that_its_write_upload_entry_names(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.audit = _Watch(r.root)
    r.ports = replace(r.ports, audit=r.audit)
    done = r.write()
    assert r.audit.seen_record and done.produced is not None
    rec = r.record()
    entry = next(e for e in r.audit.entries if e["action"] == "write_upload")
    named = entry["outputs"]["replay_record"]
    assert named == {"ref": "sponsor-a/job-1.json", "record_sha256": rec.checksum()}
    assert entry["outputs"]["manifest"]["replay_record"] == named == dict(done.manifest.replay_record)
    m = rec.manifest
    pins = m["pins"]
    assert pins["engine"] == E.ENGINE_VERSION and pins["target"] and pins["controls"] and pins["golden_coa"]
    assert {"check-data-quality", "map-accounts", "build-static-records"} <= set(pins["skills"])
    assert m["schema_versions"] and "models" in m and "prompts" in m and m["tape_complete"] is True
    assert set(m["skill_trees"]) == set(pins["skills"])
    # the inputs by reference and sha256, never a copy
    (item,) = rec.inputs
    assert item["ref"] == "investors.csv" and item["sha256"] == hashlib.sha256(r.src.read_bytes()).hexdigest()
    assert item["bytes"] == r.src.stat().st_size and Path(item["root"]) == r.inbox.resolve()
    assert not [p for p in r.root.rglob("*") if p.is_file() and p.read_bytes() == r.src.read_bytes()]
    # the approval, signed off by the person, and the expected component digests
    assert rec.signed_off == {"by": "reviewer-x", "at": "2026-10-11T09:00:00Z", "approval_id": "appr-1"}
    assert rec.approval["by"] == "reviewer-x" and rec.approval["eval_key"] == done.manifest.eval_key
    assert set(rec.expected) == set(R.COMPONENTS)
    assert rec.expected["output"]["bytes_sha256"] == done.produced.sha256
    assert rec.expected["output"]["content_sha256"] == R.xlsx_content_digest(done.produced.path)
    assert rec.expected["ids_issued"]["count"] == 3 and rec.holdout is False and m["holdout_pct"] is None
    # the world tape holds what the engine asked; the results tape is empty (no agent ran)
    blobs = R.BlobStore(r.root / "blobs")
    asked = {e["q"] for e in blobs.get_json(rec.blobs["world_tape"])}
    assert {"fingerprint", "store", "id_scheme", "reserve_ids"} <= asked and "commit" not in asked
    assert blobs.get_json(rec.blobs["results_tape"]) == []
    assert (r.root / "retained.jsonl").read_text(encoding="utf-8").count("investors.csv") == 1


def test_choices_are_recorded_verbatim(tmp_path: Path) -> None:
    pick = E.Choice("supply", "field:Country", "NL", "job", "reviewer-x", "the sponsor said every row is NL")
    r = Rig(tmp_path, choices=(pick,))
    r.write()
    rec = r.record()
    got = R.BlobStore(r.root / "blobs").get_json(rec.blobs["choices"]["blob"])
    assert got == [{"kind": "supply", "subject": "field:Country", "value": "NL", "scope": "job", "by": "reviewer-x",
                    "reason": "the sponsor said every row is NL"}]


def test_a_record_that_cannot_be_written_refuses_the_upload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("WORKER_REPLAY_ROOT", str(blocker / "replay"))
    r = Rig(tmp_path)
    ev = E.evaluate(r.spec, r.ports)
    with pytest.raises(E.Refused) as err:
        E.evaluate(r.spec, r.ports, approval=r.approval(ev), stop_after="write")
    assert err.value.code == "audit_unavailable" and "replay record" in err.value.sentence
    assert "write_upload" not in r.audit.actions() and r.world.commits == []
    assert not list(r.out.glob("*_Upload*")) and not list(r.out.glob(".*"))  # no file, no part folder
    assert r.world.counters["Investors"] == 1


# ---- R3: run = evaluate, the tapes, the release check -----------------------------------------------------------
def test_a_recorded_job_replays_identical_through_the_engine(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.write()
    rec = r.record()
    commits, counter = list(r.world.commits), dict(r.world.counters)
    audit = AuditLog(tmp_path / "replay_audit.jsonl")
    before = {p for p in tmp_path.rglob("*") if p.is_file()}
    d = R.replay_job(rec, root=r.root, audit=audit)  # run defaults to the engine
    assert d.identical, d.first_difference
    assert r.world.commits == commits and r.world.counters == counter  # nothing moved
    assert {p for p in tmp_path.rglob("*") if p.is_file()} - before == {tmp_path / "replay_audit.jsonl"}
    assert [e["action"] for e in audit.read()] == ["replay.run"]


def test_a_tape_miss_is_a_difference_and_never_a_live_call(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.write()
    rec = r.record()
    blobs = R.BlobStore(r.root / "blobs")
    tape = [e for e in blobs.get_json(rec.blobs["world_tape"]) if e["q"] != "reserve_ids"]
    rec.blobs["world_tape"] = blobs.put_plain(tape)
    d = R.replay_job(rec, root=r.root)
    assert d.status == "different" and d.component == "tape" and "reserve_ids" in d.first_difference
    # the agents port: a recorded Result plays back, an unrecorded question is a miss
    sink = R.TapeSink()
    sink.begin("map")

    class Pick:
        model = "scripted"

        def rank(self, keys: Any) -> dict[str, str]:
            return {k: "1000" for k in keys}

    rec_rank = R.RecordingRanker(Pick(), sink, "job-1")
    assert rec_rank.rank(["k1"]) == {"k1": "1000"}
    tape_rank = R.TapeRanker(sink.end("map")["results"], "job-1", "scripted")
    assert tape_rank.rank(["k1"]) == {"k1": "1000"}
    with pytest.raises(R.TapeMiss):
        tape_rank.rank(["k1"])  # a second occurrence was never recorded
    with pytest.raises(R.TapeMiss):
        R.TapeRanker([], "job-1").rank(["k2"])


def test_a_changed_or_missing_input_cannot_be_replayed(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.write()
    rec = r.record()
    r.src.write_text(CSV.replace("UK", "FR"), encoding="utf-8")
    d = R.replay_job(rec, root=r.root)
    assert d.status == "cannot_replay" and "source_gone" in d.first_difference
    r.src.unlink()
    assert R.replay_job(rec, root=r.root).status == "cannot_replay"


def test_capability_mode_still_raises_not_built(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.write()
    with pytest.raises(R.NotBuilt):
        R.replay_job(r.record(), mode="capability", root=r.root)


def test_the_release_check_fails_on_a_difference_unless_a_person_accepted_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = Rig(tmp_path)
    r.write()
    audit = AuditLog(tmp_path / "release.jsonl")
    ok = R.replay_all(r.root, audit=audit)
    assert ok.passed and ok.total == 1 and ok.identical == 1
    # an engine "bug fix" that changes the tie-out, released as engine-2
    real = EW._tie_dict
    monkeypatch.setattr(EW, "_tie_dict", lambda t: {**real(t), "ties": False} if t is not None else {})
    bad = R.replay_all(r.root, engine="engine-2", audit=audit)
    assert not bad.passed and bad.different[0]["component"] == "tie_out"
    new = bad.different[0]["detail"]["actual_sha256"]
    accepted = r.root / "accepted"
    accepted.mkdir()
    entry = {"job_id": "job-1", "component": "tie_out", "new_digest": new, "reason": "ties is now strict"}
    (accepted / "engine-2.json").write_text(json.dumps([{**entry, "by": "agent"}]), encoding="utf-8")
    still = R.replay_all(r.root, engine="engine-2", audit=audit)
    assert not still.passed and still.invalid_accepted  # an agent never accepts a difference
    (accepted / "engine-2.json").write_text(json.dumps([{**entry, "by": "reviewer-x"}]), encoding="utf-8")
    expected_before = r.record().expected
    passed = R.replay_all(r.root, engine="engine-2", audit=audit)
    assert passed.passed and len(passed.accepted) == 1
    rec = r.record()
    assert rec.expected == expected_before  # expected is never rewritten
    assert rec.history == [{"engine": "engine-2", "component": "tie_out", "new_digest": new,
                            "accepted_by": "reviewer-x", "reason": "ties is now strict"}]
    actions = [e["action"] for e in audit.read()]
    assert actions.count("replay.release_check") == 4 and "replay.accepted" in actions
    assert R.main(["--all", "--root", str(r.root), "--engine", "engine-2"]) == 0


def test_the_release_check_fails_closed_when_nothing_can_be_replayed(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.write()
    r.src.unlink()
    rep = R.replay_all(r.root)
    assert rep.total == 1 and len(rep.cannot_replay) == 1 and not rep.passed


# ---- the manifest on every write: console and agent ---------------------------------------------------------------
def test_a_console_approval_puts_the_manifest_in_the_job_record_and_replays(tmp_path: Path) -> None:
    c = Console(tmp_path / "inbox")
    c.act("prov|paid")
    j = F5.console_job(c, "Investors")
    c.act("acceptall")
    decide_ids(c)
    c.act("approve", who="reviewer-a")
    (rec_entry,) = [e for e in AuditLog(c.job_dir(j) / "audit.jsonl").read() if e["action"] == "write_upload"]
    named = rec_entry["outputs"]["manifest"]["replay_record"]
    assert named == j["manifest"]["replay_record"] and named["record_sha256"]
    root = c.job_dir(j).parent / "replay"
    rec = R.ReplayRecord.load(root / named["ref"])
    assert rec.checksum() == named["record_sha256"]
    assert rec.facts["headers"] and rec.facts["fields"]  # what a replay needs
    d = R.replay_job(rec, root=root)
    assert d.identical, d.first_difference


def test_an_agent_write_puts_the_manifest_in_the_runs_record_and_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_adapter_parity_agent as A
    from worker import sample as S

    monkeypatch.delenv("WORKER_AUDIT_MANIFEST", raising=False)
    for typ, keep in F5.CLEAN_ROWS.items():
        monkeypatch.setitem(S.CFG[typ], "rows", [x for x in S.CFG[typ]["rows"] if x.i in keep])
    ctx, agent, _last, _source, _sheet = A._static_agent("Vendors", tmp_path)
    approve(agent, ctx, "t1", "checked against the source")
    (entry,) = [e for e in ctx.audit.read() if e["action"] == "write_upload"]
    out = entry["outputs"]
    assert out["manifest"]["pins"]["skills"] and out["approved_by"] and out["approval_id"]
    named = out["manifest"]["replay_record"]
    root = ctx.out_dir.parent / "replay"
    rec = R.ReplayRecord.load(root / named["ref"])
    assert rec.checksum() == named["record_sha256"]
    assert R.replay_job(rec, root=root).identical


# ---- the golden lock: one intended difference --------------------------------------------------------------------
# sha256 (first 16) of each golden file without its decisions digest and result_sha256, taken before M19
OTHER_COMPONENTS_BEFORE_M19 = {
    "gl_clean_fund": "e9e86c75ef0245bb", "gl_clean_lp": "5d1c13f6373bba2b", "gl_dirty_fund": "41b554a290ed0bc4",
    "gl_dirty_lp": "15b5fee0ee2f28aa", "recon_checks": "62bc310e898aa685", "score_set7": "69b63db48cb0ac0f",
    "score_set8": "870694007bb72589",
    "source_acceptance_ledgerline_affiliates_export": "49c2156d266433e4",
    "source_acceptance_ledgerline_investments_export": "51ec8393e616487e",
    "source_acceptance_ledgerline_investors_export": "c13dec5a73ec9036",
    "source_acceptance_ledgerline_vendors_export": "3aaa41044d683359",
    "source_acceptance_sponsor_a_affiliates": "a4e76680cf53562d",
    "source_acceptance_sponsor_a_allocation_file": "61ae9638d1f60724",
    "source_acceptance_sponsor_a_common_investors": "25edd72bfb656749",
    "source_acceptance_sponsor_a_gl_funda_2025q4": "1cc522fc980490b6",
    "source_acceptance_sponsor_a_lp_ledger_funda_2025q4": "cfd0d7efface8e5b",
    "source_acceptance_sponsor_a_roll_forward_funda_2025": "54053d5558d2d9ed",
    "source_acceptance_sponsor_a_vendors": "220860210fe4aa45",
    "source_acceptance_sponsor_b_gl_bfundi_2025q4": "1c1bdb9971e568e2",
    "source_acceptance_sponsor_b_lp_ledger_bfundi_2025q4": "fb1f301c1720860c",
    "source_messy_investors_compact_pivot": "2f4e867613f525e4",
    "source_messy_investors_hierarchical": "8c4802ebc33fd66f",
    "source_messy_investors_horizontal": "3b74572d3a6e47d3",
    "source_messy_investors_multi_sheet": "9f9ea2710a97760e", "source_messy_investors_pivot": "843246a98e10517c",
    "source_messy_investors_side_by_side": "2ad8e41b3bcbc2e1",
    "source_messy_investors_stacked_blocks": "57e768bba70be560",
    "source_messy_investors_vertical_messy": "55a1352fa4a942fc",
    "source_messy_investors_wide_noise": "47d142a32a504ab2",
    "source_set7_commitment_register": "c49a88dc4aa223e5", "source_set7_fund_roster": "31e2d3025f01fbf8",
    "source_set7_gp_ledger": "6a6fc19b8a4dccf8", "source_set7_investor_roster_side_by_side": "9d05a464b580acdf",
    "source_set8_commitment_register": "a250f36400ed5af4", "source_set8_fund_roster": "816b19499da0fcfd",
    "source_set8_gp_ledger": "c5ae5295bf71b69c", "source_set8_investor_roster_multi_sheet": "3dcb41f2ac360509",
    "static_clean_affiliates": "3e3c4a5ba37effcf", "static_clean_commitments": "89ccfc220c56e035",
    "static_clean_investments": "2853b47719e14a55", "static_clean_investors": "fcc9bcf1fabf91d1",
    "static_clean_vendors": "3f100ddc2e1733d8", "static_dirty_affiliates": "10874cf97c99387a",
    "static_dirty_commitments": "5c0473d7d2727246", "static_dirty_investments": "411407a69dcada34",
    "static_dirty_investors": "46f759a242490922", "static_dirty_vendors": "af738584e5a8d964",
}


def _without_decisions(doc: dict[str, Any]) -> dict[str, Any]:
    doc = json.loads(json.dumps(doc))
    for part in [doc.get("console"), doc.get("agent"), doc]:
        if isinstance(part, dict):
            if part is not doc:
                part.pop("result_sha256", None)
            if isinstance(part.get("decisions"), dict):
                part["decisions"].pop("digest", None)
    return doc


def test_only_the_decision_digest_changed_in_the_golden_lock() -> None:
    """Rows, findings, MISSING, tie-out, tally, IDs, the upload digest, the decision count and actions, and the score
    are byte-identical to before M19; only `decisions.digest` (and `result_sha256`, derived from it) moved."""
    golden = Path(__file__).parent / "fixtures" / "golden"
    now = {}
    for p in sorted(golden.glob("*.json")):
        body = json.dumps(_without_decisions(json.loads(p.read_text(encoding="utf-8"))), sort_keys=True,
                          separators=(",", ":"), ensure_ascii=False)
        now[p.stem] = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    assert now == OTHER_COMPONENTS_BEFORE_M19
    for p in sorted(golden.glob("*.json")):  # result_sha256 still follows its components
        doc = json.loads(p.read_text(encoding="utf-8"))
        for side in ("console", "agent"):
            f = doc.get(side)
            if not isinstance(f, dict) or "result_sha256" not in f:
                continue
            shaped = {"output": ({"format": "xlsx", "content_sha256": f["upload"]["content_sha256"]}
                                 if f["upload"] else {}),
                      "findings": f["findings"], "tie_out": f["tie_out"], "missing": f["missing"], "tally": f["tally"],
                      "ids_issued": f["ids_issued"], "decisions": f["decisions"]["digest"]}
            assert R.result_digest(shaped) == f["result_sha256"], p.name
    assert "M19, the ONE intended difference" in (golden / "CHANGES.md").read_text(encoding="utf-8")
