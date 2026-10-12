"""Skills in the open Agent Skills layout: `skills/<name>/SKILL.md` plus scenario files, tables and checks.

Progressive disclosure:
  level 1  the frontmatter (`name`, `description`, `version`) is all that sits in the system prompt;
  level 2  the body of SKILL.md is read only when the model calls `use_skill(skill)`;
  level 3  a scenario file (`bank-accounts.md`, ...) is read only when its case occurs, `use_skill(skill, scenario)`.
Each skill splits know-how (what the model reads) from rules (`tables/*.yaml`, `checks.yaml`, which code evaluates).
Skills come only from this repository's `skills/` folder and are reviewed like code.

Every rule file of a skill (tables, checks, controls, specs, cases) is read through the strict loader
(`worker.rulefiles`): a skill whose files are refused does not load. `require_valid_skills` is what a job calls
before it starts: a refused pin is written to the audit log as `skill.load_refused` and the job does not start.

Sponsor tables live at `templates/sponsors/<sponsor>/skills/<skill>/tables/<id>.yaml` and nowhere else. The earlier
place, `templates/sponsors/<sponsor>/tables/<id>.yaml`, is not read: a sponsor folder that still holds table files
there is refused by name (`LegacySponsorTables`, never silently ignored): move the files by hand.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from worker import rulefiles
from worker.decide import Table, TableError

_NAME = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,60}\.md$")
_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)
MAX_DESCRIPTION = 600


@functools.cache
def bundled_skills_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "skills"  # resolved once: a resolve is a file system call


def skills_dir() -> Path:
    override = os.environ.get("WORKER_SKILLS_DIR")
    return Path(override) if override else bundled_skills_dir()


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    version: str
    path: Path

    @property
    def body(self) -> str:
        return _split(self.path / "SKILL.md")[1]

    def scenarios(self) -> list[str]:
        return sorted(p.name for p in self.path.glob("*.md") if p.name != "SKILL.md")

    def scenario(self, name: str) -> str:
        if not _FILE.match(name) or name == "SKILL.md":
            raise ValueError(f"no scenario file {name!r}; this skill has: {self.scenarios()}")
        path = self.path / name
        if not path.is_file():
            raise ValueError(f"no scenario file {name!r}; this skill has: {self.scenarios()}")
        return path.read_text(encoding="utf-8")

    @property
    def pin(self) -> rulefiles.Pin:
        """Every rule file of this skill, read strictly (kept while the files do not change). Raises
        `rulefiles.SchemaError` listing every problem."""
        return pin_of(self.path)

    def _sponsor_places(self, sponsor_dir: Path | None) -> list[Path]:
        """`<sponsor>/skills/<skill>/tables` of the sponsor's own folder. Table files still in `<sponsor>/tables`
        are refused by name: their overrides would otherwise vanish quietly."""
        if sponsor_dir is None:
            return []
        if sponsor_dir.name == "tables":
            raise ValueError(f"{sponsor_dir} is a tables folder: pass the sponsor's own folder")
        old = sponsor_dir / "tables"
        stray = sorted(p.name for p in old.glob("*.yaml")) if old.is_dir() else []
        if stray:
            raise LegacySponsorTables(
                f"{old} holds {', '.join(stray)} in the earlier place, which is no longer read; move them to "
                f"{sponsor_dir / 'skills' / '<skill>' / 'tables'}")
        return [sponsor_dir / "skills" / self.name / "tables"]

    def table(self, name: str, sponsor_dir: Path | None = None,
              overlay_rows: Sequence[Mapping[str, Any]] | None = None) -> Table:
        """A decision table. Rows promoted through the improvement gate (`overlay_rows`) replace organisation rows
        by id, then a sponsor's `<name>.yaml` overrides rows by id. The sponsor file is looked for at
        `<sponsor>/skills/<skill>/tables/` first, then in `sponsor_dir` (the earlier place). The organisation's
        inputs and outputs stand: a sponsor file never changes them."""
        base = Table.load(self.path / "tables" / f"{name}.yaml")
        if overlay_rows:
            base = base.with_override({"id": base.id, "rows": list(overlay_rows)})
        for place in self._sponsor_places(sponsor_dir):
            path = place / f"{name}.yaml"
            if path.is_file():
                return base.with_override(self._sponsor_rows(name, path))
        return base

    def _sponsor_rows(self, name: str, path: Path) -> dict[str, Any]:
        """A sponsor table file read as an overlay of the organisation's table, or TableError naming every problem."""
        key = next((k for k in self.pin.files if k.endswith(f"/tables/{name}.yaml")), None)
        if key is None:
            raise TableError(f"{self.name} has no table {name!r} for {path} to refine")
        text = v1_overlay_text(path.read_text(encoding="utf-8"))
        try:
            got = rulefiles.load_text(f"tables/{name}.yaml", text, skill=self.name, base=self.pin.files[key])
        except rulefiles.SchemaError as exc:
            raise TableError(f"{path}: " + "; ".join(f"{p.message} ({p.pointer or '$'})" for p in exc.problems)) from exc
        out: dict[str, Any] = {"id": got.data["id"], "rows": [dict(r) for r in got.data["rows"]]}
        if "description" in got.data:
            out["description"] = got.data["description"]
        return out

    def content_hash(self, sponsor_dir: Path | None = None,
                     overlay: Mapping[str, Sequence[Mapping[str, Any]]] | None = None) -> str:
        """Identity of what actually ran: a hash of every file of the skill folder, the sponsor's table overrides and
        the promoted overlay rows. The human label (`version`) says what it is called; this says what it was."""
        h = hashlib.sha256()
        files = sorted(p for p in self.path.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
        for p in files:
            h.update(p.relative_to(self.path).as_posix().encode())
            h.update(b"\0" + p.read_bytes().replace(b"\r\n", b"\n") + b"\0")  # same hash on every checkout
        for place in self._sponsor_places(sponsor_dir):
            if place.is_dir():
                for p in sorted(place.glob("*.yaml")):
                    h.update(b"sponsor/" + p.name.encode() + b"\0" + p.read_bytes().replace(b"\r\n", b"\n") + b"\0")
        # the sponsor's other overlay files of this skill (added header words, checks): what ran includes them
        root = sponsor_dir
        for name in rulefiles.OVERLAY_FILES:
            p = root / "skills" / self.name / name if root is not None else None
            if p is not None and p.is_file():
                h.update(b"sponsor-file/" + name.encode() + b"\0" + p.read_bytes().replace(b"\r\n", b"\n") + b"\0")
        for name in sorted(overlay or {}):
            h.update(b"overlay/" + name.encode() + b"\0")
            h.update(json.dumps(list(overlay[name]), sort_keys=True).encode())
        return h.hexdigest()[:12]

    def checks(self) -> dict[str, Any]:
        """The skill's `checks.yaml` as the strict loader read it (empty when the skill has none)."""
        for loaded in self.pin.files.values():
            if loaded.kind == "checks" and not loaded.overlay:
                return dict(loaded.data)
        return {}

    def controls(self) -> dict[str, Any]:
        """The skill's `controls.yaml` values as the strict loader read them (empty when the skill has none):
        `severity_floor`, `max_ceiling`, `ranges_floor`, `balance`."""
        for loaded in self.pin.files.values():
            if loaded.kind == "controls" and not loaded.overlay:
                return dict(loaded.controls)
        return {}

    @property
    def stamp(self) -> str:
        return f"{self.name}@{self.version}"


def _split(path: Path) -> tuple[dict[str, Any], str]:
    m = _FRONT.match(path.read_text(encoding="utf-8"))
    if not m:
        raise ValueError(f"{path} has no YAML frontmatter")
    meta = yaml.safe_load(m.group(1)) or {}
    return meta, m.group(2).strip()


# ---- strict reading of a skill folder ---------------------------------------------------------------------
_PINS: dict[tuple[str, str], rulefiles.Pin] = {}


def _signature(path: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(p for p in path.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        h.update(p.relative_to(path).as_posix().encode())
        h.update(b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


def pin_of(path: Path) -> rulefiles.Pin:
    """The strict reading of one skill folder: every rule file loaded, or `rulefiles.SchemaError` listing every
    problem. A reading is kept while the files do not change, so asking again is cheap."""
    key = (str(path.resolve()), _signature(path))
    got = _PINS.get(key)
    if got is None:
        got = rulefiles.load_skill(path, repo_root=path.parent.parent)
        if len(_PINS) >= 64:
            _PINS.clear()
        _PINS[key] = got
    return got


def _read_skill(path: Path) -> Skill:
    """The skill folder's frontmatter, checked. The rule files are not read here."""
    meta, _ = _split(path / "SKILL.md")
    name = str(meta.get("name", ""))
    if name != path.name or not _NAME.match(name):
        raise ValueError(f"skill folder {path.name!r} must match its frontmatter name {name!r}")
    for key in ("description", "version"):
        if not meta.get(key):
            raise ValueError(f"skill {name}: frontmatter needs {key!r}")
    if len(str(meta["description"])) > MAX_DESCRIPTION:
        raise ValueError(f"skill {name}: description is over {MAX_DESCRIPTION} characters")
    return Skill(name, " ".join(str(meta["description"]).split()), str(meta["version"]), path)


def find_skill(name: str, directory: Path | None = None) -> Skill | None:
    """One skill by name from a folder of skills, its frontmatter checked and its rule files NOT yet read (use
    `Skill.pin`, which refuses a skill whose files are refused). None when the folder has no such skill."""
    path = (directory or skills_dir()) / name
    return _read_skill(path) if (path / "SKILL.md").is_file() else None


def load_skills(directory: Path | None = None) -> dict[str, Skill]:
    """Every skill of the folder. Frontmatter is checked, then every rule file goes through the strict loader and
    table ids must be unique across skills; one refused file refuses the load (`rulefiles.SchemaError`)."""
    base = directory or skills_dir()
    out: dict[str, Skill] = {}
    if not base.is_dir():
        return out
    problems: list[rulefiles.Problem] = []
    owner: dict[str, str] = {}
    for path in sorted(p for p in base.iterdir() if (p / "SKILL.md").is_file()):
        skill = _read_skill(path)
        try:
            pin = pin_of(path)
        except rulefiles.SchemaError as exc:
            problems.extend(exc.problems)
            continue
        for f in pin.files.values():
            if f.kind == "table":
                if f.id in owner and owner[f.id] != skill.name:
                    problems.append(rulefiles.Problem(
                        f.path, "id", "duplicate_id",
                        f"table id {f.id!r} is also used by skill {owner[f.id]}; table ids are unique across skills"))
                owner.setdefault(f.id, skill.name)
        out[skill.name] = skill
    if problems:
        raise rulefiles.SchemaError(problems)
    return out


class LegacySponsorTables(ValueError):
    """A sponsor folder keeps table files in `<sponsor>/tables`, the place that is no longer read."""


class SkillLoadRefused(RuntimeError):
    """A job may not start: its skills were refused by the strict loader (`problems` lists every line)."""

    def __init__(self, message: str, problems: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.problems = tuple(problems)


def require_valid_skills(audit: Any, directory: Path | None = None) -> dict[str, Skill]:
    """The skills a job runs with. A skill file the engine reads by name that is not there, or a pin the strict
    loader refuses, is said here, at the start: `skill.load_refused` is written FIRST (reason and every problem line)
    and `SkillLoadRefused` is raised, so the job does not start (and never meets a FileNotFoundError half way). If
    the record cannot be written (no audit log, or it fails) the job does not start either. Nothing is half-loaded."""
    from worker.audit import AuditError
    from worker.engine_console import missing_skill_files

    lines = [f"skills/{m}: the engine reads this file by name and it is not there" for m in missing_skill_files(directory)]
    codes: list[str] = ["missing_file"] * len(lines)
    if not lines:
        try:
            return load_skills(directory)
        except rulefiles.SchemaError as exc:
            lines, codes = list(exc.lines), list(exc.codes)
    shown = "; ".join(lines[:3])
    if audit is None:
        raise SkillLoadRefused("the skills were refused and there is no audit log to record it; the job does "
                               f"not start: {shown}", lines)
    try:
        audit.record("skill.load_refused", f"{len(lines)} problem(s) in the rule files; the job does not start",
                     {"problems": lines, "codes": codes}, {"started": False})
    except (AuditError, OSError, ValueError, TypeError) as err:  # the record is the precondition: no record, no job
        raise SkillLoadRefused(f"the skills were refused and the audit record could not be written ({err}); "
                               "the job does not start", lines) from err
    raise SkillLoadRefused(f"the skills were refused ({len(lines)} problems); the job does not start: {shown}", lines)


# ---- sponsor tables: a header-less file -----------------------------------------------------------------
def v1_overlay_text(text: str) -> str:
    """A sponsor table file as a v1 overlay. A file that has not been migrated (no `schema_version`) gets the header
    in memory and loses `version`, `inputs` and `outputs`: the organisation's inputs and outputs stand."""
    data = yaml.safe_load(text)
    if not isinstance(data, dict) or "schema_version" in data or "kind" in data:
        return text
    body = {k: v for k, v in data.items() if k not in ("version", "inputs", "outputs")}
    return yaml.safe_dump({"schema_version": 1, "kind": "table", **body}, sort_keys=False, allow_unicode=True)


def catalog(skills: Sequence[Skill]) -> str:
    """The only skill text in the system prompt: one line per skill (level 1)."""
    if not skills:
        return ""
    lines = [f"- {s.name} (v{s.version}): {s.description}" for s in skills]
    return (
        "\nSkills you can load with use_skill(skill) when one fits the job (the body is not loaded until you do; "
        "scenario files are listed in it):\n" + "\n".join(lines) + "\n"
    )
