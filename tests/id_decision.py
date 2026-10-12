"""The sponsor's ID decision for a console job, as a test helper: approval waits for it (`Console.approve`)."""

from __future__ import annotations

from typing import Any

from worker import console_idgen as G
from worker import record_facts as RF


def decide_ids(c: Any, typ: str | None = None, sponsor: str | None = None, job_id: str | None = None) -> None:
    """A named person keeps the target file's own format for the current job's generating type, unless one is already
    decided (a no-op for any other type)."""
    j = c.job()
    typ = typ or (j or {}).get("type", "")
    sponsor = sponsor or (j or {}).get("sp", "Sponsor A")
    if typ in RF.id_decision_types() and not c.id_decision(sponsor, typ):
        G.decide(c, {"sponsor": sponsor, "record_type": typ, "reason": "the sponsor's standard numbering",
                     "scheme": dict(RF.target(typ).id_generation)}, "reviewer-a", job_id or (j or {}).get("id"))


def decide_agent_ids(ids_root: Any, typ: str, sponsor: str = "sponsor-a") -> None:
    """The same decision for the agent path's store (`<state>/ids`), as a named person's change."""
    from worker import persist
    from worker.templates import TEMPLATES

    persist.id_scheme(ids_root, sponsor, typ, TEMPLATES[typ].id_generation).change(
        dict(TEMPLATES[typ].id_generation), by="reviewer-a", reason="the sponsor's standard numbering",
        record=lambda *a: None)
