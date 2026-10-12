"""Step 3 of ticket 11: an approved layout program enters the engine as a `derived` source; reuse is an exact shape
match within one sponsor with one click each time; replay plays the stored table back without Docker; every
execution, refusal and violation is audited. FakeRunner stands in for the container (no Docker, no model)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from port_fakes import FakeRunner
from test_engine import SPONSOR, approval_for, investor_steps, rig
from worker import engine as E
from worker.audit import AuditError
from worker.engine import Refused
from worker.flex.artifacts import Artifacts, FileArtifactStore
from worker.flex.grid import grid_from_rows, layout_sig
from worker.flex.runner import RunResult
from worker.flex.service import BOUND, DerivedReader, Flex, derived_ref
from worker.replay import BlobStore

IMAGE = "sha256:" + "1" * 64


def investors_grid(country: str = "US", extra_title_number: bool = False) -> dict:
    rows = [["Field", "#1", "#2", "#3"],
            ["Investor ID", "HS-1", "HS-2", "HS-3"],
            ["Investor Name", "Alpha Fund LP", "Beta Partners", "Gamma Holdings"],
            ["Country", country, "UK", "DE"]]
    if extra_title_number:
        rows[0].append(2024)
    return grid_from_rows({"Sheet": rows})


def program_output(extra_label: bool = False, schema: tuple[str, ...] = ("Investor ID", "Investor Name", "Country")
                   ) -> bytes:
    cols = [{"name": n, "role": "key" if i == 0 else "dimension"} for i, n in enumerate(schema)]
    rows = [{"cells": [{"ref": f"Sheet!{c}{r}"} for r in (2, 3, 4)]} for c in "BCD"]
    dropped = [{"ref": f"Sheet!{c}1", "reason": "header"} for c in "ABCD"] + \
        [{"ref": f"Sheet!A{r}", "reason": "header"} for r in (2, 3, 4)]
    if extra_label:
        dropped.append({"ref": "Sheet!E1", "reason": "label"})
    return json.dumps({"columns": cols, "rows": rows, "dropped": dropped, "case": "transposed"}).encode()


class Sink:
    def __init__(self, fail_on: str = "") -> None:
        self.rows: list[tuple[str, dict, dict]] = []
        self.fail_on = fail_on

    def record(self, action: str, rationale: str, inputs: dict | None = None, outputs: dict | None = None) -> dict:
        if action == self.fail_on:
            raise AuditError("sink down")
        assert rationale.strip()
        self.rows.append((action, dict(inputs or {}), dict(outputs or {})))
        return {"seq": len(self.rows)}

    def actions(self) -> list[str]:
        return [r[0] for r in self.rows]


class Rig:
    def __init__(self, tmp: Path, sponsor: str = SPONSOR, payload: Any = None, fail_on: str = "",
                 shared: tuple[Any, Any] | None = None) -> None:
        self.audit = Sink(fail_on)
        self.blobs = BlobStore(tmp / "blobs")
        self.store = FileArtifactStore(tmp / "flex")
        self.runner = FakeRunner(payload or (lambda kind, prog, ins, seed: program_output(
            extra_label=b'"2024"' in ins["grid.json"])), image=IMAGE)
        self.flex = Flex(sponsor, Artifacts(self.store, self.audit), self.blobs, self.runner, self.audit,
                         controls_hash="c" * 64)

    def other(self, sponsor: str) -> Flex:
        return Flex(sponsor, Artifacts(self.store, self.audit), self.blobs, self.runner, self.audit)


def bound(p: dict) -> dict:
    return {k: p[k] for k in BOUND}


def proposed(r: Rig, grid: dict | None = None, **kw: Any) -> dict:
    return r.flex.propose("Investors", grid or investors_grid(), "def build(book):\n    return {}\n",
                          "transposed investor register", proposed_by={"role": "layout_drafter"}, **kw)


def approved(r: Rig, scope: str = "sponsor") -> tuple[dict, dict]:
    p = proposed(r)
    done = r.flex.approve(p["proposal_id"], by="Priya Rao", scope=scope, bound=bound(p), acknowledged=(),
                          reason="checked the preview")
    return p, done


# ---- the engine reads an approved table ---------------------------------------------------------------------
def test_an_approved_program_enters_evaluate_as_a_derived_source(tmp_path: Path) -> None:
    r = Rig(tmp_path / "flex")
    p, done = approved(r)
    assert p["verified"].ok and done["table_sha256"] == p["output_table_sha256"] and done["artifact_id"].startswith("ta_")
    base = rig(tmp_path / "job")
    src = derived_ref(done["table_sha256"])
    reader = DerivedReader(base.reader, r.blobs, tmp_path / "csv")
    ports = replace(base.ports, reader=reader)
    spec = replace(base.spec, sources=(src,), recipe=E.Recipe.of(*investor_steps()))
    ev = E.evaluate(spec, ports)
    assert ev.manifest.sources[0].kind == "derived" and ev.manifest.sources[0].fingerprint == src.ref
    assert ev.manifest.as_record()["derived_sources"][0]["artifact_id"] == done["artifact_id"]
    assert ev.manifest.as_record()["derived_sources"][0]["approval_id"] == done["approval_id"]
    approval = approval_for(ev)
    out = E.evaluate(spec, ports, approval=approval, stop_after="write")
    up = sorted(base.out.glob("*_Upload*"))
    assert out.produced is not None and len(up) == 1 and out.produced.rows == 3
    assert not any(a.startswith("flex.") for a in base.audit.actions())  # the engine never saw the program


def test_a_derived_table_changed_on_disk_is_never_read(tmp_path: Path) -> None:
    r = Rig(tmp_path / "flex")
    _p, done = approved(r)
    reader = DerivedReader(rig(tmp_path / "job").reader, r.blobs, tmp_path / "csv")
    blob = r.blobs._path(done["table_sha256"])
    blob.write_bytes(blob.read_bytes().replace(b"Alpha Fund LP", b"Forged Fund LP"))
    with pytest.raises(OSError):
        reader.fingerprint(derived_ref(done["table_sha256"]))


# ---- approval ----------------------------------------------------------------------------------------------
def test_approval_refusals_each_audited(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    p = proposed(r)
    for by in ("layout_drafter", "agent", "system"):
        with pytest.raises(Refused) as exc:
            r.flex.approve(p["proposal_id"], by=by, scope="sponsor", bound=bound(p), acknowledged=(), reason="r")
        assert exc.value.code == "agent_cannot_decide"
    stale = {**bound(p), "verification_report_sha256": "0" * 64}
    with pytest.raises(Refused) as exc:
        r.flex.approve(p["proposal_id"], by="Priya Rao", scope="sponsor", bound=stale, acknowledged=(), reason="r")
    assert exc.value.code == "stale_approval"
    assert r.audit.actions().count("flex.refused") == 4
    assert r.flex.artifacts.store.artifacts(SPONSOR, "Investors") == []  # nothing was created by a refusal


def test_a_block_cannot_be_approved_by_anyone(tmp_path: Path) -> None:
    r = Rig(tmp_path, payload=lambda *a: json.dumps({"columns": [], "rows": [], "dropped": []}).encode())
    p = proposed(r)
    assert p["blocks"] and not p["output_table_sha256"]
    with pytest.raises(Refused) as exc:
        r.flex.approve(p["proposal_id"], by="Priya Rao", scope="sponsor", bound=bound(p), acknowledged=(), reason="r")
    assert exc.value.code == "blocked"


def test_every_warn_must_be_ticked(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    p = proposed(r, investors_grid(extra_title_number=True))
    assert p["warns"] == ["label_numbers_dropped"] and p["output_table_sha256"]
    with pytest.raises(Refused) as exc:
        r.flex.approve(p["proposal_id"], by="Priya Rao", scope="job", bound=bound(p), acknowledged=(), reason="r")
    assert exc.value.code == "warn_unacknowledged"
    done = r.flex.approve(p["proposal_id"], by="Priya Rao", scope="job", bound=bound(p),
                          acknowledged=("label_numbers_dropped",), reason="the year is in the title")
    assert done["approval_id"].startswith("tap_")
    assert r.flex.artifacts.store.decisions(SPONSOR, done["artifact_id"])[0]["acknowledged"] == [
        "label_numbers_dropped"]


def test_audit_failure_on_approval_stores_nothing(tmp_path: Path) -> None:
    r = Rig(tmp_path, fail_on="flex.artifact.created")
    p = proposed(r)
    with pytest.raises(AuditError):
        r.flex.approve(p["proposal_id"], by="Priya Rao", scope="sponsor", bound=bound(p), acknowledged=(), reason="r")
    assert r.store.artifacts(SPONSOR, "Investors") == []
    assert not (r.blobs.root / "derived").exists()


# ---- reuse -------------------------------------------------------------------------------------------------
def test_reuse_is_offered_on_an_exact_match_and_applied_with_one_click(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    _p, done = approved(r)
    new = investors_grid(country="FR")  # same shape, different values
    assert layout_sig(new) == layout_sig(investors_grid())
    offered = r.flex.offer("Investors", new)
    assert [a.artifact_id for a in offered] == [done["artifact_id"]]
    res = r.flex.reuse(done["artifact_id"], "Investors", new, by="Priya Rao", why="same register, new month")
    assert res["status"] == "approved" and res["table_sha256"]
    tab = json.loads(r.blobs.get(res["table_sha256"]))
    assert tab["rows"][0][2] == "FR"  # the host copied this file's value
    decisions = r.store.decisions(SPONSOR, done["artifact_id"])
    assert [(d["kind"], d["scope"]) for d in decisions] == [("approval", "sponsor"), ("approval", "job")]
    assert "flex.reuse.offered" in r.audit.actions() and "flex.reuse.accepted" in r.audit.actions()
    # one click each time: a second file is not applied by the first approval
    with pytest.raises(Refused):
        r.flex.reuse(done["artifact_id"], "Investors", new, by="agent", why="again")


def test_reuse_is_never_fuzzy_cross_sponsor_or_cross_record_type(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    _p, done = approved(r)
    other_shape = grid_from_rows({"Sheet": [["Field", "#1"], ["Investor ID", "HS-1"], ["Legal name", "X"]]})
    assert r.flex.offer("Investors", other_shape) == []
    assert r.flex.offer("Commitments", investors_grid()) == []  # another record type
    assert r.other("sponsor-b").offer("Investors", investors_grid()) == []  # another sponsor
    with pytest.raises(Refused) as exc:
        r.other("sponsor-b").reuse(done["artifact_id"], "Investors", investors_grid(), by="Priya Rao", why="x")
    assert exc.value.code == "not_offered"
    assert "flex.refused" in r.audit.actions()


def test_a_job_scope_approval_is_never_offered(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    approved(r, scope="job")
    assert r.flex.offer("Investors", investors_grid()) == []


def test_reuse_with_a_warn_opens_the_full_card_and_a_drift_fails(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    _p, done = approved(r)
    with_title = investors_grid(extra_title_number=True)
    res = r.flex.reuse(done["artifact_id"], "Investors", with_title, by="Priya Rao", why="same register")
    assert res["status"] == "needs_card" and [w["code"] for w in res["warns"]] == ["label_numbers_dropped"]
    assert len(r.store.decisions(SPONSOR, done["artifact_id"])) == 1  # no approval was written for the warn
    r.runner.respond = lambda *a: program_output(schema=("ID", "Name", "Country"))
    res = r.flex.reuse(done["artifact_id"], "Investors", investors_grid("FR"), by="Priya Rao", why="again")
    assert res["status"] == "failed" and res["blocks"][0]["code"] == "schema_drift"
    assert "flex.reuse.failed" in r.audit.actions()


def test_a_retired_artifact_is_not_offered_and_a_fork_shows_both_heads(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    _p, done = approved(r)
    parent = r.flex.artifacts.load(SPONSOR, done["artifact_id"])
    from dataclasses import replace as rep

    for i in (1, 2):  # two children of the same parent: a fork
        src = f"def build(book):\n    return {{'v': {i}}}\n"
        from worker.flex.artifacts import sha256_text

        child = rep(parent, parent=parent.artifact_id, source_text=src, source_sha256=sha256_text(src))
        r.flex.artifacts.create(child, "an edit")
        r.flex.artifacts.approve(SPONSOR, child.artifact_id, by="Priya Rao", scope="sponsor", bound={},
                                 acknowledged=(), reason="r")
    heads = r.flex.offer("Investors", investors_grid())
    assert len(heads) == 2 and parent.artifact_id not in {h.artifact_id for h in heads}
    r.flex.artifacts.retire(SPONSOR, heads[0].artifact_id, by="Priya Rao", reason="wrong")
    assert [h.artifact_id for h in r.flex.offer("Investors", investors_grid())] == [heads[1].artifact_id]


def test_the_image_must_be_the_one_it_was_approved_in(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    _p, done = approved(r)
    r.runner.image = "sha256:" + "2" * 64
    with pytest.raises(Refused) as exc:
        r.flex.reuse(done["artifact_id"], "Investors", investors_grid("FR"), by="Priya Rao", why="x")
    assert exc.value.code == "image_mismatch"


# ---- replay ------------------------------------------------------------------------------------------------
def test_the_derived_reader_plays_back_the_table_without_a_runner(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    _p, done = approved(r)
    calls = r.runner.calls
    reader = DerivedReader(rig(tmp_path / "job").reader, r.blobs, tmp_path / "csv")
    rows = list(reader.rows(derived_ref(done["table_sha256"])))
    assert r.runner.calls == calls  # no container, no program
    assert rows[0] == (2, ["HS-1", "Alpha Fund LP", "US"])


def test_a_derivation_file_name_is_short_and_the_full_hashes_stay_inside(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    _p, done = approved(r)
    names = [f.name for f in (r.blobs.root / "derived").glob("*.json")]
    assert len(names) == 1 and len(names[0]) <= 48  # was 140: too long for a deep Windows path
    doc = json.loads((r.blobs.root / "derived" / names[0]).read_text("utf-8"))
    assert doc["output_table_sha256"] == done["table_sha256"] and doc["approval_id"] == done["approval_id"]
    assert len(doc["output_table_sha256"]) == 64 and doc["approval_id"].startswith("tap_")
    reader = DerivedReader(rig(tmp_path / "job").reader, r.blobs, tmp_path / "csv")
    assert reader.derivation(derived_ref(done["table_sha256"]))["approval_id"] == done["approval_id"]


def test_a_derivation_written_with_the_long_name_is_still_found(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    _p, done = approved(r)
    folder = r.blobs.root / "derived"
    short = next(folder.glob("*.json"))
    long = folder / f"{done['table_sha256']}.{done['approval_id']}.json"
    short.rename(long)
    assert len(long.name) > 130
    reader = DerivedReader(rig(tmp_path / "job").reader, r.blobs, tmp_path / "csv")
    assert reader.derivation(derived_ref(done["table_sha256"]))["approval_id"] == done["approval_id"]
    assert reader.derivation(derived_ref("0" * 64)) is None


# ---- the sandbox is off ------------------------------------------------------------------------------------
def test_without_a_sandbox_tiers_two_and_three_are_off_and_it_is_audited(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.flex.runner = None
    with pytest.raises(Refused) as exc:
        proposed(r)
    assert exc.value.code == "no_sandbox"
    assert r.audit.rows[-1][0] == "flex.refused" and r.audit.rows[-1][2]["reason"] == "no_sandbox"
    assert r.runner.calls == 0


def test_a_sandbox_violation_is_audited_as_a_refusal(tmp_path: Path) -> None:
    r = Rig(tmp_path, payload=lambda *a: RunResult("violation", None, None, "image_mismatch", 0.0, "", "e"))
    p = proposed(r)
    assert p["blocks"] == ["limits"]
    assert "flex.refused" in r.audit.actions()
    assert r.audit.actions()[:3] == ["flex.exec.started", "flex.exec.finished", "flex.refused"]
