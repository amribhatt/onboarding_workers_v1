"""Strict loader for rule files: one schema per kind, closed error codes, three layers of control.

A rule file is data that code evaluates. This module decides whether a file may be read at all, and pins a skill
folder. The kind schemas live next to it: `rule_schema` (errors, parse, shared validators), `rule_checks` (checks and
controls), `rule_specs` (table, recon-specs and cases), `rule_target` (target and synonyms), `rule_recipe`.

* Eight kinds (`KINDS`). Every file carries `schema_version` and `kind`. Unknown fields are refused, not passed on.
* One `SchemaError` per refused load lists every problem of the whole pin as
  `<repo-relative path>:<pointer>: <code>: <sentence>`. Nothing is partly loaded.
* Three layers of a control: the engine's code bounds (`CONTROL_BOUNDS`) sit above `controls.yaml` (controller only),
  which sits above the learnable files. A learnable value may only be at least as strict as its control. Where the
  repo states no strictness order, the field is locked (a controller changes it in both directions).
* `controls_hash` is SHA-256 over the canonical JSON of the parsed control values. A comment edit changes the pin,
  not this hash.
* YAML duplicate keys, anchors, aliases and non-plain tags are refused (PyYAML keeps the last duplicate silently).
* A file of a version newer than `LATEST[kind]` is refused (`unsupported_schema_version`); every kind is at 1.

Two choices the closed code list forced: a YAML or JSON syntax error, an anchor, an alias or a tag is `wrong_type`
(the text is not plain data); a key that appears twice in one mapping is `duplicate_id`. A file whose name does not
match its own id, or a recipe filed under another sponsor, is `layout`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any

from worker.rule_checks import (
    checks_overlay,
    checks_vs_controls,
    control_ids_exist,
    validate_checks,
    validate_controls,
)
from worker.rule_recipe import validate_recipe
from worker.rule_schema import Context, Loaded, Pin, Problem, Rep, SchemaError, at, is_int, parse
from worker.rule_specs import controls_refs, resolve_specs, validate_cases, validate_specs, validate_table
from worker.rule_target import validate_synonyms, validate_target

KINDS: tuple[str, ...] = ("target", "table", "checks", "recon-specs", "synonyms", "cases", "controls", "recipe")
LATEST: dict[str, int] = {k: 1 for k in KINDS}
SKILL_FILES = ("checks.yaml", "controls.yaml", "specs.yaml", "synonyms.yaml")
OVERLAY_FILES = ("checks.yaml", "synonyms.yaml")


def _version_ok(rep: Rep, kind: str, version: Any) -> bool:
    if not is_int(version) or version < 1:
        rep.add("schema_version", "wrong_type", f"schema_version is a whole number from 1, not {version!r}")
        return False
    if version > LATEST[kind]:
        rep.add("schema_version", "unsupported_schema_version",
                f"{kind} files are read up to version {LATEST[kind]}; this one is version {version}, from a newer worker")
        return False
    return True


def expected_kind(rel: str) -> str | None:
    """The kind a repo-relative path stands for, or None when the path is not a rule file."""
    p = PurePosixPath(rel)
    if p.name.endswith(".target.yaml"):
        return "target"
    if "recipes" in p.parts and p.suffix == ".yaml":
        return "recipe"
    if "tables" in p.parts and p.suffix == ".yaml":
        return "table"
    if "cases" in p.parts and p.suffix == ".json":
        return "cases"
    return {"checks.yaml": "checks", "controls.yaml": "controls", "specs.yaml": "recon-specs",
            "synonyms.yaml": "synonyms"}.get(p.name)


def _skill_of(rel: str) -> str | None:
    parts = PurePosixPath(rel).parts
    if "skills" in parts:
        i = len(parts) - 1 - parts[::-1].index("skills")
        if i + 1 < len(parts) - 1:
            return parts[i + 1]
    return None


def _load_text(rel: str, text: str, sink: list[Problem], *, skill: str | None, ctx: Context,
               base: Loaded | None, overlay: bool) -> Loaded | None:
    rep = Rep(rel, sink)
    n0 = len(sink)
    data = parse(text, rel, rep)
    if data is None and len(sink) > n0:
        return None
    if not isinstance(data, dict):
        rep.add("", "wrong_type", "the file must hold one mapping of fields")
        return None
    want = expected_kind(rel)
    missing = [k for k in ("schema_version", "kind") if k not in data]
    for k in missing:
        rep.add(k, "missing_field", f"every rule file starts with {k}")
    if missing:
        return None
    kind = data["kind"]
    if kind not in KINDS:
        rep.add("kind", "unknown_kind", f"{kind!r} is not a kind of rule file; the engine knows {list(KINDS)}")
        return None
    if want is not None and kind != want:
        rep.add("kind", "wrong_type", f"a file at this place holds kind {want!r}, not {kind!r}")
        return None
    if not _version_ok(rep, kind, data["schema_version"]):
        return None
    stem = PurePosixPath(rel).name.split(".")[0]
    skill = skill or _skill_of(rel)
    if base is not None and kind not in ("table", "checks", "synonyms", "cases", "target"):
        rep.add("", "overlay_forbidden", f"a sponsor overlay has no {kind} file; the organisation's {kind} stands")
        return None
    if kind == "controls":
        ctl = validate_controls(rep, data, skill)
    elif kind == "table":
        ctl = validate_table(rep, data, stem, base)
    elif kind == "checks":
        ctl = validate_checks(rep, data)
        if base is not None and not any(p.path == rel for p in sink[n0:]):
            checks_overlay(rep, data, base)
    elif kind == "recon-specs":
        ctl = validate_specs(rep, data)
    elif kind == "synonyms":
        ctl = validate_synonyms(rep, data, base, ctx)
    elif kind == "cases":
        ctl = validate_cases(rep, data, base)
    elif kind == "target":
        stem = PurePosixPath(rel).name.removesuffix(".target.yaml")
        ctl = validate_target(rep, data, stem, base, ctx)
    else:
        ctl = validate_recipe(rep, data, stem, rel, ctx)
    if len(sink) > n0:
        return None
    return Loaded(rel, kind, int(data["schema_version"]), str(data.get("id", stem)), data, ctl, overlay)


def _relative(path: Path, root: Path | None) -> str:
    if root is not None:
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            pass
    return path.as_posix()


def load_text(rel_path: str, text: str, *, skill: str | None = None,
              context: Context | None = None, base: Loaded | None = None) -> Loaded:
    """Load one file from its text. `rel_path` is the repo-relative path (it decides the kind and goes in errors).
    With `base`, the file is a sponsor overlay of that organisation file. Raises SchemaError listing every problem."""
    sink: list[Problem] = []
    got = _load_text(rel_path, text, sink, skill=skill, ctx=context or Context(), base=base,
                     overlay=base is not None)
    if sink or got is None:
        raise SchemaError(sink)
    return got


def load_file(path: str | Path, *, repo_root: str | Path | None = None,
              skill: str | None = None, context: Context | None = None, base: Loaded | None = None) -> Loaded:
    p = Path(path)
    rel = _relative(p, Path(repo_root) if repo_root else None)
    return load_text(rel, p.read_text(encoding="utf-8"), skill=skill, context=context, base=base)


def _walk(folder: Path) -> Iterator[Path]:
    for p in sorted(folder.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            yield p


def _layout_ok(rel: PurePosixPath, names: tuple[str, ...], *, md: bool = False) -> bool:
    """A file at the top of the folder named in `names` (or prose, when `md`), or one under tables/ or cases/."""
    parts, s = rel.parts, rel.as_posix()
    if len(parts) == 1:
        return parts[0] in names or (md and s.endswith(".md"))
    return len(parts) == 2 and ((parts[0] == "tables" and s.endswith(".yaml")) or (parts[0] == "cases" and s.endswith(".json")))


def _load_skill(skill_dir: Path, root: Path | None, overlay_dir: Path | None, ctx: Context,
                sink: list[Problem]) -> Pin | None:
    skill = skill_dir.name
    files: dict[str, Loaded] = {}
    n0 = len(sink)
    for p in _walk(skill_dir):
        rel_in = PurePosixPath(p.relative_to(skill_dir).as_posix())
        rel = _relative(p, root)
        if not _layout_ok(rel_in, SKILL_FILES, md=True):
            sink.append(Problem(rel, "", "layout",
                                f"this file is not part of a skill folder; allowed: SKILL.md, scenario .md, tables/*.yaml, "
                                f"{', '.join(SKILL_FILES)}, cases/*.json"))
            continue
        if rel_in.suffix in (".yaml", ".json"):
            got = _load_text(rel, p.read_text(encoding="utf-8"), sink, skill=skill, ctx=ctx, base=None,
                             overlay=False)
            if got is not None:
                files[rel] = got
    by_kind: dict[str, list[Loaded]] = {}
    for f in files.values():
        by_kind.setdefault(f.kind, []).append(f)
    controls = next(iter(by_kind.get("controls", [])), None)
    checks = next(iter(by_kind.get("checks", [])), None)
    specs = next(iter(by_kind.get("recon-specs", [])), None)
    tables = {t.id: t for t in by_kind.get("table", [])}
    # Cross-file checks run only over files that loaded: a file already refused would only add noise.
    if checks is not None and controls is not None and skill != "reconcile-gl":
        checks_vs_controls(Rep(checks.path, sink), checks, controls)
        control_ids_exist(Rep(controls.path, sink), controls,
                           {r["id"] for r in checks.data.get("rules", []) if isinstance(r, Mapping)})
    elif (controls is not None and skill in ("check-data-quality", "map-accounts")
          and not (skill_dir / "checks.yaml").exists()):
        control_ids_exist(Rep(controls.path, sink), controls, set())
    if specs is not None:
        resolve_specs(Rep(specs.path, sink), specs, tables, controls)
        if controls is not None:
            controls_refs(Rep(controls.path, sink), controls, {c["id"] for c in specs.data["checks"]})
    for case in by_kind.get("cases", []):
        if specs is not None and case.data.get("runner") == "reconcile-gl.check":
            ids = {c["id"] for c in specs.data["checks"]}
            for i, c in enumerate(case.data["cases"]):
                if c.get("check") not in ids:
                    sink.append(Problem(case.path, at(at("cases", i), "check"), "unknown_reference",
                                        f"{c.get('check')!r} is not a check of specs.yaml"))
    if overlay_dir is not None and overlay_dir.is_dir():
        for p in _walk(overlay_dir):
            rel_in = PurePosixPath(p.relative_to(overlay_dir).as_posix())
            rel = _relative(p, root)
            name = rel_in.as_posix()
            if name in ("controls.yaml", "specs.yaml"):
                sink.append(Problem(rel, "", "overlay_forbidden",
                                    f"a sponsor overlay cannot hold {name}; only a controller changes it"))
                continue
            if not _layout_ok(rel_in, OVERLAY_FILES):
                sink.append(Problem(rel, "", "layout", "this file is not an overlay file; allowed: tables/*.yaml, "
                                    "checks.yaml, synonyms.yaml, cases/*.json"))
                continue
            want = expected_kind(rel_in.as_posix())
            base = (tables.get(rel_in.stem) if want == "table" else
                    checks if want == "checks" else
                    next(iter(by_kind.get("synonyms", [])), None) if want == "synonyms" else
                    files.get(_relative(skill_dir / rel_in, root)) if want == "cases" else None)
            if base is None:
                sink.append(Problem(rel, "", "unknown_reference",
                                    f"there is no organisation file of {skill} for this overlay to refine"))
                continue
            got = _load_text(rel, p.read_text(encoding="utf-8"), sink, skill=skill, ctx=ctx, base=base,
                             overlay=True)
            if got is not None:
                files[rel] = got
                if got.kind == "checks" and controls is not None:
                    checks_vs_controls(Rep(rel, sink), got, controls)
    if len(sink) > n0:
        return None
    return Pin(skill, files, controls_hash(files.values()), schema_versions(files.values()))


def load_skill(skill_dir: str | Path, *, repo_root: str | Path | None = None, overlay_dir: str | Path | None = None,
               context: Context | None = None) -> Pin:
    """Load every rule file of one skill folder (and a sponsor overlay folder) or raise one SchemaError that lists
    every problem of the pin."""
    sink: list[Problem] = []
    pin = _load_skill(Path(skill_dir), Path(repo_root) if repo_root else None,
                      Path(overlay_dir) if overlay_dir else None, context or Context(), sink)
    if sink or pin is None:
        raise SchemaError(sink)
    return pin


def load_skills(skills_root: str | Path, *, repo_root: str | Path | None = None,
                context: Context | None = None) -> dict[str, Pin]:
    """Every skill under `skills_root`. Table ids must be unique across all skills."""
    sink: list[Problem] = []
    pins: dict[str, Pin] = {}
    root = Path(repo_root) if repo_root else None
    for d in sorted(p for p in Path(skills_root).iterdir() if p.is_dir()):
        pin = _load_skill(d, root, None, context or Context(), sink)
        if pin is not None:
            pins[d.name] = pin
    owner: dict[str, str] = {}
    for name, pin in pins.items():
        for f in pin.files.values():
            if f.kind == "table":
                if f.id in owner:
                    sink.append(Problem(f.path, "id", "duplicate_id",
                                        f"table id {f.id!r} is also used by skill {owner[f.id]}; table ids are unique "
                                        "across all skills"))
                owner.setdefault(f.id, name)
    if sink:
        raise SchemaError(sink)
    return pins


def _canon_default(v: Any) -> Any:
    if isinstance(v, Decimal):
        s = format(v.normalize(), "f")
        return "0" if s in ("-0", "0") else s
    raise TypeError(f"{type(v).__name__} is not canonical JSON")


def canonical_json(value: Any) -> str:
    """Sorted keys, no whitespace, Decimals as normalised strings: the form every control hash is taken over."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_canon_default)


def controls_hash(files: Iterable[Loaded]) -> str:
    """SHA-256 over the canonical JSON of the parsed control values in force. Comments, messages and other learnable
    text are not in it, so editing them changes the pin but not this hash."""
    body = {f.path: f.controls for f in sorted(files, key=lambda x: x.path) if f.controls}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def schema_versions(files: Iterable[Loaded]) -> dict[str, int]:
    """`{kind: schema_version}` for every kind loaded, as the manifest records it."""
    return {k: LATEST[k] for k in sorted({f.kind for f in files})}
