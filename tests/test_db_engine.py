"""The one engine over the database's ports gives what it gives over the fakes (SQLite, and PostgreSQL under
`integration`): same IDs, same upload, same audit actions, an idempotent commit, and a chain that verifies."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import openpyxl
import pytest

from port_fakes import MemoryReader, MemoryWorld
from test_engine import INVESTORS, SCHEME, SPONSOR, approval_for, investor_steps, uploads
from worker import engine as E
from worker.db.core import Db, SqlAuditLog
from worker.db.stores import SqlStore, SqlWorld
from worker.ports import ListAudit, MemoryStore, Ports, ProductionSkills
from worker.templates import templates_for


def _spec() -> E.JobSpec:
    return E.JobSpec("job-1", SPONSOR, "Investors", None, (E.SourceRef("file", "investors.csv", None, 1, ""),),
                     E.PinSet(), E.Recipe.of(*investor_steps()), (), {"gp_id": SPONSOR})


def _approved(ports: Ports) -> E.Evaluation:
    first = E.evaluate(_spec(), ports)
    return E.evaluate(_spec(), ports, approval=approval_for(first), stop_after="write")


def _cells(files: list[Path]) -> list[list[tuple[Any, ...]]]:
    """The cell values of each file (the workbook's bytes carry a creation time)."""
    out = []
    for f in files:
        book = openpyxl.load_workbook(f, read_only=True)
        out.append([tuple(r) for ws in book.worksheets for r in ws.iter_rows(values_only=True)])
        book.close()
    return out


def _templates() -> dict[str, Any]:
    spec = templates_for(SPONSOR)["Investors"]
    return {"Investors": type("T", (), {"id_generation": SCHEME, "required": spec.required})()}


def test_the_engine_over_sql_ports_matches_the_fakes(db: Db, tmp_path: Path) -> None:
    fake_audit = ListAudit()
    fake = Ports(MemoryWorld(SPONSOR, schemes={"Investors": SCHEME}), MemoryReader({"investors.csv": INVESTORS}),
                 fake_audit, MemoryStore(), tmp_path / "fake", ProductionSkills(SPONSOR))
    sql_audit = SqlAuditLog(db, "runs/job-1/engine_audit.jsonl")
    world = SqlWorld(db, SPONSOR, templates=_templates())
    sql = Ports(world, MemoryReader({"investors.csv": INVESTORS}), sql_audit, SqlStore(db), tmp_path / "sql",
                ProductionSkills(SPONSOR))

    want, got = _approved(fake), _approved(sql)

    assert got.produced is not None and want.produced is not None
    assert got.produced.rows == want.produced.rows == 3
    assert [e["action"] for e in sql_audit.read()] == fake_audit.actions()
    assert sql_audit.verify() == (True, len(fake_audit.actions()))
    assert [e["key"] for e in world.store("Investors")] == [e["key"] for e in fake.world.store("Investors")]
    assert world.counter("Investors") == fake.world.counter("Investors") == 4
    assert _cells(uploads(tmp_path / "sql")) == _cells(uploads(tmp_path / "fake"))


def test_a_second_job_of_the_sponsor_continues_the_numbers(db: Db, tmp_path: Path) -> None:
    world = SqlWorld(db, SPONSOR, templates=_templates())
    audit = SqlAuditLog(db, "runs/a/engine_audit.jsonl")
    ports = Ports(world, MemoryReader({"investors.csv": INVESTORS}), audit, SqlStore(db), tmp_path / "out",
                  ProductionSkills(SPONSOR))
    _approved(ports)
    second = [INVESTORS[0], ["HS-9", "Delta Trust", "FR"]]
    spec = E.JobSpec("job-2", SPONSOR, "Investors", None, (E.SourceRef("file", "second.csv", None, 1, ""),),
                     E.PinSet(), E.Recipe.of(*investor_steps()), (), {"gp_id": SPONSOR})
    ports2 = Ports(world, MemoryReader({"second.csv": second}), audit, SqlStore(db), tmp_path / "out2",
                   ProductionSkills(SPONSOR))
    first = E.evaluate(spec, ports2)
    E.evaluate(spec, ports2, approval=approval_for(first, approval_id="appr-2"), stop_after="write")
    assert [e["key"] for e in world.store("Investors")][-1] == "GINV-0004"


def test_a_failing_audit_sink_refuses_before_the_source_is_read(db: Db, tmp_path: Path) -> None:
    class Broken(SqlAuditLog):
        def record_many(self, action: str, items: list) -> list:
            from worker.audit import AuditError

            raise AuditError("down")

    reader = MemoryReader({"investors.csv": INVESTORS})
    ports = Ports(SqlWorld(db, SPONSOR, templates=_templates()), reader, Broken(db, "s"), SqlStore(db),
                  tmp_path / "out", ProductionSkills(SPONSOR))
    with pytest.raises(E.Refused) as err:
        E.evaluate(_spec(), ports)
    assert err.value.code == "audit_unavailable" and reader.pulled == 0
