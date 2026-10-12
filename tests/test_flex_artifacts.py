"""Step 1 of ticket 11: content-addressed, immutable, sponsor-scoped transform artifacts, audited before they
take effect. One contract suite for the file adapter and the SQL adapter (SQLite always, PostgreSQL under
`integration`)."""

from __future__ import annotations

import json
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from worker.audit import AuditError
from worker.db.core import Db
from worker.engine import Refused
from worker.flex.artifacts import Artifact, Artifacts, FileArtifactStore, SqlArtifactStore, sha256_text


def _artifact(sponsor: str = "sponsor-a", **over: Any) -> Artifact:
    src = over.pop("source_text", "def build(book):\n    return {}\n")
    base: dict[str, Any] = {
        "sponsor": sponsor, "record_type": "Commitments", "harness": "layout-harness/1",
        "image_digest": "sha256:" + "a" * 64, "source_sha256": sha256_text(src), "input_sig": "b" * 64,
        "output_schema": (("Investor", "dimension"), ("Commitment", "measure")), "controls_hash": "c" * 64,
        "limits": {"cpu": 1, "memory_mb": 512, "pids": 64, "seconds": 60}, "parent": "", "source_text": src,
        "case": "transposed", "proposed_by": {"role": "layout_drafter", "model_id": "scripted"}}
    base.update(over)
    return Artifact(**base)


class Recorder:
    """An audit sink that remembers what was written, and can be made to fail."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, dict]] = []
        self.fail = False

    def record(self, action: str, rationale: str, inputs: dict | None = None, outputs: dict | None = None) -> dict:
        if self.fail:
            raise AuditError("sink down")
        self.rows.append((action, dict(inputs or {})))
        return {"seq": len(self.rows)}

    def record_many(self, action: str, items: list) -> list[dict]:  # pragma: no cover  (port completeness)
        return [self.record(action, *item) for item in items]


@pytest.fixture(params=["file", "sqlite", pytest.param("postgres", marks=pytest.mark.integration)])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    if request.param == "file":
        yield FileArtifactStore(tmp_path / "flex")
    elif request.param == "sqlite":
        d = Db("sqlite://")
        d.create_all()
        yield SqlArtifactStore(d)
        d.dispose()
    else:
        yield SqlArtifactStore(request.getfixturevalue("pg").app)


@pytest.fixture
def book(store: Any) -> tuple[Artifacts, Recorder]:
    audit = Recorder()
    return Artifacts(store, audit), audit


def test_the_id_is_the_hash_of_the_identity_and_changes_with_any_field() -> None:
    a = _artifact()
    assert a.artifact_id == _artifact().artifact_id and a.artifact_id.startswith("ta_")
    assert len(a.artifact_id) == 3 + 64
    for field, value in [("sponsor", "sponsor-b"), ("record_type", "Investors"), ("image_digest", "sha256:" + "d" * 64),
                         ("input_sig", "e" * 64), ("controls_hash", "f" * 64), ("parent", "ta_" + "1" * 64),
                         ("limits", {"cpu": 2}), ("harness", "layout-harness/2")]:
        assert _artifact(**{field: value}).artifact_id != a.artifact_id, field
    # what is not identity (the label of the case, who proposed it) does not move the id
    assert _artifact(case="other", proposed_by={"role": "x"}).artifact_id == a.artifact_id


def test_create_then_load_round_trips_and_is_idempotent(book: tuple[Artifacts, Recorder]) -> None:
    arts, audit = book
    a = _artifact()
    assert arts.create(a, "first use of this layout") == a.artifact_id
    assert arts.create(a, "same again") == a.artifact_id  # content addressed: the same id, no second record
    got = arts.load("sponsor-a", a.artifact_id)
    assert got == a and got.source_text.startswith("def build")
    assert [r[0] for r in audit.rows].count("flex.artifact.created") == 1


def test_audit_comes_first_and_a_failing_sink_creates_nothing(book: tuple[Artifacts, Recorder]) -> None:
    arts, audit = book
    audit.fail = True
    a = _artifact()
    with pytest.raises(AuditError):
        arts.create(a, "why")
    audit.fail = False
    assert arts.store.get_artifact("sponsor-a", a.artifact_id) is None
    with pytest.raises(Refused):
        arts.load("sponsor-a", a.artifact_id)


def test_audit_precedes_the_store_write(tmp_path: Path) -> None:
    order: list[str] = []
    audit = Recorder()
    inner = FileArtifactStore(tmp_path / "flex")

    class Spy:
        def __getattr__(self, name: str) -> Any:
            return getattr(inner, name)

        def insert_artifact(self, record: dict) -> None:
            order.append(f"store after {len(audit.rows)} audit rows")
            inner.insert_artifact(record)

    Artifacts(Spy(), audit).create(_artifact(), "why")  # type: ignore[arg-type]
    assert order == ["store after 1 audit rows"]


def test_artifacts_are_sponsor_scoped(book: tuple[Artifacts, Recorder]) -> None:
    arts, _ = book
    a = _artifact("sponsor-a")
    arts.create(a, "why")
    assert arts.store.get_artifact("sponsor-b", a.artifact_id) is None
    with pytest.raises(Refused):
        arts.load("sponsor-b", a.artifact_id)
    assert arts.store.artifacts("sponsor-b", "Commitments") == []
    assert [r["artifact_id"] for r in arts.store.artifacts("sponsor-a", "Commitments")] == [a.artifact_id]


def test_an_approval_is_a_separate_record_and_never_by_an_agent(book: tuple[Artifacts, Recorder]) -> None:
    arts, audit = book
    a = _artifact()
    arts.create(a, "why")
    bound = {"input_grid_sha256": "1" * 64, "output_table_sha256": "2" * 64, "verification_report_sha256": "3" * 64}
    for agent in ("agent", "layout_drafter", "system", "  "):
        with pytest.raises(Refused):
            arts.approve("sponsor-a", a.artifact_id, by=agent, scope="sponsor", bound=bound, acknowledged=(),
                         reason="ok")
    assert arts.store.decisions("sponsor-a", a.artifact_id) == []
    rec = arts.approve("sponsor-a", a.artifact_id, by="Priya Rao", scope="sponsor", bound=bound,
                       acknowledged=("misaligned",), reason="checked the preview")
    assert rec["kind"] == "approval" and rec["approval_id"].startswith("tap_")
    assert [d["kind"] for d in arts.store.decisions("sponsor-a", a.artifact_id)] == ["approval"]
    assert arts.load("sponsor-a", a.artifact_id) == a  # the artifact did not change
    assert audit.rows[-1][0] == "flex.artifact.approved"
    with pytest.raises(Refused):  # unknown scope
        arts.approve("sponsor-a", a.artifact_id, by="Priya Rao", scope="org", bound=bound, acknowledged=(), reason="r")


def test_the_same_person_approving_the_same_thing_again_is_not_an_error(book: tuple[Artifacts, Recorder]) -> None:
    """Found in a real browser: replaying a file with a saved program and approving it with the same reason gave the
    same content id, and the second insert raised FileExistsError (a 500)."""
    arts, audit = book
    a = _artifact()
    arts.create(a, "why")
    bound = {"input_grid_sha256": "1" * 64, "output_table_sha256": "2" * 64, "verification_report_sha256": "3" * 64}
    first = arts.approve("sponsor-a", a.artifact_id, by="Priya Rao", scope="sponsor", bound=bound, acknowledged=(),
                         reason="checked")
    again = arts.approve("sponsor-a", a.artifact_id, by="Priya Rao", scope="sponsor", bound=bound, acknowledged=(),
                         reason="checked")
    assert again["approval_id"] == first["approval_id"]
    assert len(arts.store.decisions("sponsor-a", a.artifact_id)) == 1
    assert [r[0] for r in audit.rows].count("flex.artifact.approved") == 2  # both acts are on the audit trail


def test_retirement_ends_the_offer_and_an_edit_is_a_child(book: tuple[Artifacts, Recorder]) -> None:
    arts, _ = book
    parent = _artifact()
    arts.create(parent, "why")
    bound = {"input_grid_sha256": "1" * 64, "output_table_sha256": "2" * 64, "verification_report_sha256": "3" * 64}
    arts.approve("sponsor-a", parent.artifact_id, by="Priya Rao", scope="sponsor", bound=bound, acknowledged=(),
                 reason="r")
    assert [h.artifact_id for h in arts.heads("sponsor-a", "Commitments", parent.input_sig)] == [parent.artifact_id]
    child = _artifact(parent=parent.artifact_id, source_text="def build(book):\n    return {'rows': []}\n")
    arts.create(child, "an edit")
    arts.approve("sponsor-a", child.artifact_id, by="Priya Rao", scope="sponsor", bound=bound, acknowledged=(),
                 reason="r")
    assert [h.artifact_id for h in arts.heads("sponsor-a", "Commitments", parent.input_sig)] == [child.artifact_id]
    arts.retire("sponsor-a", child.artifact_id, by="Priya Rao", reason="wrong layout")
    # the child is retired; the parent is not the head any more, and is not offered again by accident
    assert arts.heads("sponsor-a", "Commitments", parent.input_sig) == []
    assert arts.heads("sponsor-a", "Commitments", "9" * 64) == []  # exact shape match only


def test_the_ports_have_no_update_or_delete() -> None:
    for cls in (FileArtifactStore, SqlArtifactStore, Artifacts):
        names = {n for n in dir(cls) if not n.startswith("_")}
        assert not {n for n in names if n.startswith(("update", "delete", "remove", "replace", "set_"))}, cls


def test_the_file_adapter_creates_exclusively_read_only_and_reloads_with_a_hash_check(tmp_path: Path) -> None:
    audit = Recorder()
    st = FileArtifactStore(tmp_path / "flex")
    arts = Artifacts(st, audit)
    a = _artifact()
    arts.create(a, "why")
    path = tmp_path / "flex" / "sponsor-a" / "artifacts" / f"{a.artifact_id}.json"
    assert path.is_file() and not path.stat().st_mode & stat.S_IWUSR
    with pytest.raises(FileExistsError):  # no in-place edit, even by the same code
        st.insert_artifact({**json.loads(path.read_text("utf-8")), "case": "other"})
    # a hand edit (the file made writable again) is found on load: never run, and audited
    path.chmod(0o666)
    doc = json.loads(path.read_text("utf-8"))
    doc["source_text"] = "def build(book):\n    return {'forged': 1}\n"
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(Refused) as exc:
        arts.load("sponsor-a", a.artifact_id)
    assert exc.value.code == "artifact_tampered"
    assert audit.rows[-1][0] == "flex.artifact.tampered"


def test_the_sql_table_refuses_update_and_delete() -> None:
    d = Db("sqlite://")
    d.create_all()
    arts = Artifacts(SqlArtifactStore(d), Recorder())
    a = _artifact()
    arts.create(a, "why")
    arts.approve("sponsor-a", a.artifact_id, by="Priya Rao", scope="job", bound={}, acknowledged=(), reason="r")
    with d.begin() as conn, pytest.raises(sa.exc.DBAPIError):
        conn.execute(sa.text("UPDATE flex_artifacts SET record_type = 'X'"))
    with d.begin() as conn, pytest.raises(sa.exc.DBAPIError):
        conn.execute(sa.text("DELETE FROM flex_decisions"))
    d.dispose()


def test_the_store_is_never_under_skills(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        FileArtifactStore(tmp_path / "skills" / "x")


def test_a_bad_sponsor_or_id_cannot_become_a_path(tmp_path: Path) -> None:
    st = FileArtifactStore(tmp_path / "flex")
    assert st.get_artifact("sponsor-a", "ta_" + "0" * 64) is None
    for bad in ("../x", "a/b", ""):
        with pytest.raises(ValueError):
            st.get_artifact(bad, "ta_" + "0" * 64)
    with pytest.raises(ValueError):
        st.get_artifact("sponsor-a", "../../etc/passwd")
