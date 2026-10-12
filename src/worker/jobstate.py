"""What an agent run keeps in memory besides its staged tables, written to disk so a restart loses nothing.

`Stage` persists the tables themselves. This file holds the rest of the tool-side bookkeeping: how each stage was
built (specs and constants, for the saved workflow), the ID reservations that have not been committed, the values a
person supplied, and the corrections recorded for the replay case. The checkpoint (sqlite) holds the conversation
and the paused gate. Written after every tool call, loaded only when a run is restored.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from worker import persist
from worker.fsutil import atomic_write_text
from worker.idgen import Reservation
from worker.quality import Supplied

FILE = "job_state.json"


class JobState:
    def __init__(self, out_dir: Path) -> None:
        self.path = Path(out_dir) / FILE
        self.constants: dict[str, dict[str, str]] = {}
        self.specs: dict[str, dict[str, Any]] = {}
        self.recipe: dict[str, Any] = {}
        self.skills: dict[str, str] = {}
        self.supplied = Supplied()

    def restore(self, ctx: Any) -> bool:
        """Load the saved state into this object and the run context; False when nothing was saved."""
        if not self.path.is_file():
            return False
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.constants.update(data["constants"])
        self.specs.update(data["specs"])
        self.recipe.update(data["recipe"])
        self.skills.update(data["skills"])
        self.supplied.rows.update({(int(r), f): v for r, f, v in data["supplied"]["rows"]})
        self.supplied.all.update({(r, f): v for r, f, v in data["supplied"]["all"]})
        self.supplied.log.extend(data["supplied"]["log"])
        ctx.job_feedback[:] = data["job_feedback"]
        ctx.instruction = data["instruction"] or ctx.instruction
        for sid, r in data["reservations"].items():
            if ctx.id_root is None:
                continue
            spec = ctx.stage.templates[r["record_type"]]
            scheme = persist.id_scheme(ctx.id_root, ctx.sponsor, r["record_type"], spec.id_generation)
            ctx.reservations[sid] = (scheme, Reservation.from_state(r["state"]), r["id_field"], r["source_key_field"])
        return True

    def save(self, ctx: Any) -> None:
        reservations = {}
        for sid, (scheme, rsv, id_field, source_key_field) in ctx.reservations.items():
            reservations[sid] = {"record_type": scheme.record_type, "state": rsv.state(), "id_field": id_field,
                                 "source_key_field": source_key_field}
        data = {
            "constants": self.constants, "specs": self.specs, "recipe": self.recipe, "skills": self.skills,
            "supplied": {"rows": [[r, f, v] for (r, f), v in self.supplied.rows.items()],
                         "all": [[r, f, v] for (r, f), v in self.supplied.all.items()], "log": self.supplied.log},
            "job_feedback": list(ctx.job_feedback), "instruction": ctx.instruction, "reservations": reservations,
        }
        atomic_write_text(self.path, json.dumps(data, ensure_ascii=False, default=str))
