"""Fitness functions (migration M23): lints over the source tree that stop the deleted shapes creeping back.

Every lint is a function over source text (or a path), so each has a test that feeds it a synthetic offender in
`tmp_path` and proves it can fail. The lint over the real tree reports `path:line` messages. What is legitimately
code is in `tests/fitness_allow.yaml`, one reason per entry; a stale entry (one that matches nothing) fails too, so
the list cannot rot into a blanket permit.

The seven lints:
  1 no literal rule value in src/worker (account ranges, tolerances, severity tables, ID formats, synonym lists)
  2 nothing but data and prose inside skills/<name>/
  3 `sample` is imported only by console_demo and cli
  4 every tool in tools.build_tools goes through the audited `run`, or is listed read-only-unaudited with a reason
  5 every SKILL.md names only registered tools and states no number that contradicts prompts.py / controls.yaml
  6 every module in src/worker is imported by another module, a console script, or is listed
  7 only money.py decides a balance or a tolerance
"""

from __future__ import annotations

import ast
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "worker"
SKILLS = ROOT / "skills"
ALLOW_FILE = Path(__file__).with_name("fitness_allow.yaml")


# --------------------------------------------------------------------------------------------------- plumbing
@dataclass(frozen=True)
class Offence:
    path: str  # relative to the repository, posix
    line: int
    kind: str
    text: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.kind}] {self.text}"


def _allow() -> dict[str, list[dict[str, Any]]]:
    data = yaml.safe_load(ALLOW_FILE.read_text(encoding="utf-8")) or {}
    for section, entries in data.items():
        for e in entries or []:
            assert str(e.get("reason", "")).strip(), f"{ALLOW_FILE.name}: {section} entry {e} has no reason"
    return {k: list(v or []) for k, v in data.items()}


def _rel(p: Path) -> str:
    return p.resolve().relative_to(ROOT).as_posix()


def _parse(source: str, path: str) -> ast.Module:
    return ast.parse(source, filename=path)


def _src_files() -> list[Path]:
    return sorted(SRC.glob("*.py"))


def _allowed(o: Offence, line_text: str, entries: list[dict[str, Any]], used: set[int]) -> bool:
    for i, e in enumerate(entries):
        if e["file"] != o.path.rsplit("/", 1)[-1]:
            continue
        kinds = e.get("kinds")
        if kinds and o.kind not in kinds:
            continue
        if e.get("contains") and e["contains"] not in line_text:
            continue
        used.add(i)
        return True
    return False


def _filter(found: list[tuple[Offence, str]], entries: list[dict[str, Any]]) -> tuple[list[Offence], list[dict]]:
    """(offences not allowed, allow entries that matched nothing)."""
    used: set[int] = set()
    left = [o for o, text in found if not _allowed(o, text, entries, used)]
    stale = [e for i, e in enumerate(entries) if i not in used]
    return left, stale


# --------------------------------------------------------------------------------------- lint 1: literal rules
SEVERITY_NAMES = {"ERR", "WARN", "ERROR", "REVIEW"}
_RANGE = re.compile(r"^\s*(\d{3,5})\s*-\s*(\d{3,5})\s*$")
_FILL_CALLS = {"mkdtemp", "TemporaryDirectory", "NamedTemporaryFile", "mkstemp", "ThreadPoolExecutor"}
_ID_NAMES = {"prefix", "id_prefix", "pad", "id_pad", "id_limit", "idlimit", "id_max_len", "id_width",
             "namemax", "name_max", "name_limit", "name_max_len"}
_LEGAL = {"inc", "llc", "ltd", "limited", "corp", "corporation", "plc", "lp"}
_SYN_NAME = re.compile(r"syn|alias|header_word|legal_form|legalform", re.IGNORECASE)


def _name_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        s = node.slice
        if isinstance(s, ast.Constant) and isinstance(s.value, str):
            return s.value
        return _name_of(node.value)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_num(node: ast.AST | None) -> bool:
    if node is None:
        return False
    if isinstance(node, ast.Constant):
        return isinstance(node.value, int | float) and not isinstance(node.value, bool)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
        return _is_num(node.operand)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("Decimal", "float", "int"):
        return len(node.args) == 1 and isinstance(node.args[0], ast.Constant) and isinstance(
            node.args[0].value, str | int | float)
    if isinstance(node, ast.Tuple | ast.List):
        return bool(node.elts) and all(_is_num(e) for e in node.elts)
    return False


def _is_sev(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id in SEVERITY_NAMES
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in SEVERITY_NAMES


def _sev_in(node: ast.AST) -> bool:
    if _is_sev(node):
        return True
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return any(_sev_in(e) for e in node.elts)
    return False


def _strs(node: ast.AST) -> list[str] | None:
    if isinstance(node, ast.Tuple | ast.List | ast.Set) and node.elts:
        out = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        return out if len(out) == len(node.elts) else None
    return None


def _docstring_nodes(tree: ast.Module) -> set[int]:
    out: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = n.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def literal_rule_values(source: str, path: str) -> list[Offence]:
    """Offences: a rule value written as a literal in code. See the module docstring for the five kinds."""
    tree = _parse(source, path)
    docs = _docstring_nodes(tree)
    out: list[Offence] = []

    def add(node: ast.AST, kind: str, text: str) -> None:
        out.append(Offence(path, getattr(node, "lineno", 0), kind, text))

    def named_value(name: str | None, value: ast.AST | None, node: ast.AST) -> None:
        if name is None or value is None:
            return
        low = name.lower()
        if _is_num(value) and ("tol" in low or low in ("pass_up_to", "warn_up_to")):
            add(node, "tolerance", f"tolerance value written as a literal: {name} = {ast.unparse(value)}")
        if _sev_in(value) and any(w in low for w in ("level", "severity", "floor")):
            add(node, "severity", f"severity assigned as a literal: {name} = {ast.unparse(value)}")
        if low in _ID_NAMES and isinstance(value, ast.Constant) and value.value not in ("", None, 0, False) \
                and isinstance(value.value, str | int) and not isinstance(value.value, bool):
            add(node, "id_format", f"ID format value written as a literal: {name} = {ast.unparse(value)}")
        if _SYN_NAME.search(name) and isinstance(value, ast.Dict | ast.List | ast.Tuple | ast.Set) \
                and any(isinstance(n, ast.Constant) and isinstance(n.value, str) for n in ast.walk(value)):
            add(node, "synonyms", f"word list written as a literal: {name} = {ast.unparse(value)[:60]}")

    for n in ast.walk(tree):
        # account ranges ------------------------------------------------------------------------------
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
            m = _RANGE.match(n.value)
            if m and int(m.group(1)) <= int(m.group(2)):
                add(n, "account_range", f"account range written as a literal: {n.value!r}")
        if isinstance(n, ast.Tuple | ast.List) and len(n.elts) == 2 and all(
                isinstance(e, ast.Constant) and isinstance(e.value, int) and not isinstance(e.value, bool)
                for e in n.elts):
            lo, hi = n.elts[0].value, n.elts[1].value  # type: ignore[attr-defined]
            if 1000 <= lo <= hi <= 99999:
                add(n, "account_range", f"account range written as a pair of numbers: {ast.unparse(n)}")
        if isinstance(n, ast.Call) and _name_of(n.func) == "range" and len(n.args) == 2 and all(
                isinstance(a, ast.Constant) and isinstance(a.value, int) for a in n.args):
            lo, hi = n.args[0].value, n.args[1].value  # type: ignore[attr-defined]
            if 1000 <= lo <= hi <= 99999:
                add(n, "account_range", f"account range written as range(): {ast.unparse(n)}")
        if isinstance(n, ast.Compare):
            nums = [o for o in [n.left, *n.comparators] if isinstance(o, ast.Constant)
                    and isinstance(o.value, int) and not isinstance(o.value, bool) and 1000 <= o.value <= 99999]
            others = " ".join(ast.unparse(o).lower() for o in [n.left, *n.comparators] if o not in nums)
            if nums and ("acct" in others or "account" in others):
                add(n, "account_range", f"account compared with a literal number: {ast.unparse(n)}")
        # tolerances, severities, ID formats, synonyms by the name they are bound to -------------------
        if isinstance(n, ast.Assign):
            for t in n.targets:
                named_value(_name_of(t), n.value, n)
        elif isinstance(n, ast.AnnAssign):
            named_value(_name_of(n.target), n.value, n)
        elif isinstance(n, ast.keyword) and n.arg:
            named_value(n.arg, n.value, n.value)
        elif isinstance(n, ast.arguments):
            pos = n.posonlyargs + n.args
            for arg, d in zip(pos[len(pos) - len(n.defaults):], n.defaults, strict=True):
                named_value(arg.arg, d, d)
            for arg, kd in zip(n.kwonlyargs, n.kw_defaults, strict=True):
                named_value(arg.arg, kd, kd)
        elif isinstance(n, ast.Dict):
            for k, v in zip(n.keys, n.values, strict=True):
                if k is not None and isinstance(k, ast.Constant) and isinstance(k.value, str):
                    named_value(k.value, v, v)
                if _sev_in(v) and not (isinstance(k, ast.Constant) and isinstance(k.value, str)
                                       and any(w in k.value.lower() for w in ("level", "severity", "floor"))):
                    add(v, "severity", f"severity assigned in a table: {ast.unparse(v)[:60]}")
            vals = [_strs(v) for v in n.values]
            if len(n.values) >= 3 and all(v is not None and len(v) >= 2 for v in vals) and all(
                    isinstance(k, ast.Constant) and isinstance(k.value, str) for k in n.keys):
                add(n, "synonyms", f"a table of word lists (header synonyms?): {ast.unparse(n)[:60]}")
        # ID formats written in a format string or a pad call ---------------------------------------
        if isinstance(n, ast.FormattedValue) and isinstance(n.format_spec, ast.JoinedStr):
            spec = "".join(v.value for v in n.format_spec.values if isinstance(v, ast.Constant)
                           and isinstance(v.value, str))
            if re.fullmatch(r"0\d+d", spec) and len(n.format_spec.values) == 1:
                add(n, "id_format", f"zero-padded number with a literal width: :{spec}")
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("zfill", "rjust") \
                and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, int):
            add(n, "id_format", f"padding with a literal width: .{n.func.attr}({n.args[0].value})")
        if isinstance(n, ast.keyword) and n.arg == "prefix":
            pass  # `prefix=` of tempfile is not an ID prefix; named_value ignores it unless in _ID_NAMES
        # legal-form words -----------------------------------------------------------------------------
        if isinstance(n, ast.Tuple | ast.List | ast.Set):
            words = _strs(n)
            if words and len({w.lower().strip(". ") for w in words} & _LEGAL) >= 3:
                add(n, "synonyms", f"legal-form words written as a literal: {ast.unparse(n)[:60]}")
    # a kwarg in a tempfile call is not an ID prefix: drop those
    keep: list[Offence] = []
    lines = source.splitlines()
    for o in out:
        text = lines[o.line - 1] if 0 < o.line <= len(lines) else ""
        if o.kind == "id_format" and any(c in text for c in _FILL_CALLS) and "prefix=" in text:
            continue
        keep.append(o)
    # one offence per (line, kind): ast.walk meets a keyword and its value twice
    seen: set[tuple[int, str, str]] = set()
    uniq: list[Offence] = []
    for o in keep:
        k = (o.line, o.kind, o.text)
        if k not in seen:
            seen.add(k)
            uniq.append(o)
    return uniq


_SCAN_CACHE: dict[tuple[Any, tuple[Path, ...]], list[tuple[Offence, str]]] = {}


def _scan(fn: Any, files: list[Path]) -> list[tuple[Offence, str]]:
    """Parse every file once per (lint, file set) for the whole session: the parametrized lint1 tests and the
    stale-entry test all scan the same src tree, and each scan costs seconds. Same result, returned as a copy."""
    key = (fn, tuple(files))
    if key not in _SCAN_CACHE:
        _SCAN_CACHE[key] = _scan_uncached(fn, files)
    return list(_SCAN_CACHE[key])


def _scan_uncached(fn: Any, files: list[Path]) -> list[tuple[Offence, str]]:
    found: list[tuple[Offence, str]] = []
    for p in files:
        text = p.read_text(encoding="utf-8")
        lines = text.splitlines()
        for o in fn(text, _rel(p)):
            found.append((o, lines[o.line - 1] if 0 < o.line <= len(lines) else ""))
    return found


LINT1_KINDS = ("account_range", "tolerance", "severity", "id_format", "synonyms")


@pytest.mark.parametrize("kind", LINT1_KINDS)
def test_lint1_no_literal_rule_value_in_src(kind: str) -> None:
    entries = [e for e in _allow().get("literal_values", []) if kind in e["kinds"]]
    found = [(o, t) for o, t in _scan(literal_rule_values, _src_files()) if o.kind == kind]
    left, _ = _filter(found, entries)
    msg = f"literal {kind} values in src/worker (move them to a skill or target file):\n"
    assert not left, msg + "\n".join(str(o) for o in left)


def test_lint1_allow_list_has_no_stale_entry() -> None:
    entries = _allow().get("literal_values", [])
    assert all(set(e["kinds"]) <= set(LINT1_KINDS) for e in entries), "an allow entry names an unknown kind"
    found = _scan(literal_rule_values, _src_files())
    stale = []
    for e in entries:
        hit = any(o.path.endswith("/" + e["file"]) and o.kind in e["kinds"]
                  and (not e.get("contains") or e["contains"] in text) for o, text in found)
        if not hit:
            stale.append(e["file"])
    assert not stale, f"allow entries (literal_values) that match nothing: {stale}"


@pytest.mark.parametrize(
    ("snippet", "kind"),
    [
        ('RANGES = ["1500-1599", "8050-8069"]\n', "account_range"),
        ("DEAL = (1500, 1699)\n", "account_range"),
        ("def f(acct):\n    return 9000 <= acct <= 9999\n", "account_range"),
        ("TOLERANCE = 0.01\n", "tolerance"),
        ("def f(x, tol_pass=1):\n    return x\n", "tolerance"),
        ('rules = {"tolerances": {"money": {"pass_up_to": 1, "warn_up_to": 5}}}\n', "tolerance"),
        ('LEVELS = {"required_blank": "ERR", "name_too_long": "WARN"}\n', "severity"),
        ("severity_floor = ERR\n", "severity"),
        ('PREFIX_TABLE = {}\nid_prefix = "INV-"\n', "id_format"),
        ('def mint(n):\n    return f"X{n:05d}"\n', "id_format"),
        ('def mint(n):\n    return str(n).zfill(6)\n', "id_format"),
        ('SYNONYMS = {"Investor_Name": ["Investor", "LP Name"]}\n', "synonyms"),
        ('FORMS = ("inc", "llc", "ltd")\n', "synonyms"),
    ],
)
def test_lint1_catches_each_kind_in_a_synthetic_file(tmp_path: Path, snippet: str, kind: str) -> None:
    f = tmp_path / "offender.py"
    f.write_text(snippet, encoding="utf-8")
    got = literal_rule_values(f.read_text(encoding="utf-8"), "src/worker/offender.py")
    assert kind in {o.kind for o in got}, f"{kind} not caught in {snippet!r}: {got}"
    assert all(str(o).startswith("src/worker/offender.py:") for o in got)  # path:line in the message


def test_lint1_does_not_flag_ordinary_code() -> None:
    clean = (
        '"""Docstring about 1500-1599 ranges."""\n'
        "import tempfile\n"
        "def f(rows, limit, tolerances):\n"
        "    d = tempfile.mkdtemp(prefix='run-')\n"
        "    return [r for r in rows if r.level == 'ERR'][:limit], tolerances['pass'], d\n"
    )
    assert literal_rule_values(clean, "src/worker/ok.py") == []


# ----------------------------------------------------------------------------------- lint 2: skill folders
SKILL_ALLOWED_SUFFIXES = (".md", ".yaml", ".yml", ".json")
SKILL_FORBIDDEN_DIRS = {"scripts", "bin", "__pycache__"}


def skill_folder_offences(skills_dir: Path, extra_allowed: set[str] | None = None) -> list[Offence]:
    """Every file under skills/<name>/ that is not data or prose, any `scripts` directory, any executable."""
    out: list[Offence] = []
    extra = extra_allowed or set()
    if not skills_dir.is_dir():
        return out
    for p in sorted(skills_dir.rglob("*")):
        rel = p.relative_to(skills_dir).as_posix()
        if rel in extra:
            continue
        parts = set(p.relative_to(skills_dir).parts[1:-1] if p.is_file() else p.relative_to(skills_dir).parts[1:])
        if p.is_dir():
            if p.name in SKILL_FORBIDDEN_DIRS:
                out.append(Offence(f"skills/{rel}", 1, "skill_file", "a skill has no code directory"))
            continue
        if p.parent == skills_dir:
            out.append(Offence(f"skills/{rel}", 1, "skill_file", "a file outside any skill folder"))
        elif parts & SKILL_FORBIDDEN_DIRS:
            out.append(Offence(f"skills/{rel}", 1, "skill_file", "file in a code directory"))
        elif p.suffix.lower() not in SKILL_ALLOWED_SUFFIXES:
            out.append(Offence(f"skills/{rel}", 1, "skill_file", f"{p.suffix or 'no extension'} is not data or prose"))
        elif p.stat().st_mode & 0o111 and not _is_windows_fs():
            out.append(Offence(f"skills/{rel}", 1, "skill_file", "an executable file"))
    return out


def _is_windows_fs() -> bool:
    import os

    return os.name == "nt"


def _git_executables() -> list[str]:
    """Paths under skills/ that git records with the executable bit (works on Windows, where stat cannot say)."""
    try:
        res = subprocess.run(["git", "ls-files", "-s", "--", "skills"], cwd=ROOT, capture_output=True, text=True,
                             timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    return [ln.split("\t", 1)[1] for ln in res.stdout.splitlines() if ln.startswith("100755") and "\t" in ln]


def test_lint2_skill_folders_hold_only_data_and_prose() -> None:
    allow = {e["path"] for e in _allow().get("skill_files", [])}
    bad = skill_folder_offences(SKILLS, allow)
    bad += [Offence(p, 1, "skill_file", "git records it as executable") for p in _git_executables()]
    assert not bad, "files in a skill folder that are not data or prose:\n" + "\n".join(str(o) for o in bad)


def test_lint2_catches_code_scripts_and_executables(tmp_path: Path) -> None:
    sk = tmp_path / "skills"
    (sk / "demo" / "scripts").mkdir(parents=True)
    (sk / "demo" / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
    (sk / "demo" / "map_keys.py").write_text("print(1)\n", encoding="utf-8")
    (sk / "demo" / "scripts" / "go.yaml").write_text("a: 1\n", encoding="utf-8")
    (sk / "demo" / "run").write_text("#!/bin/sh\n", encoding="utf-8")
    (sk / "stray.yaml").write_text("a: 1\n", encoding="utf-8")
    got = {o.path for o in skill_folder_offences(sk)}
    assert {"skills/demo/map_keys.py", "skills/demo/scripts", "skills/demo/scripts/go.yaml",
            "skills/demo/run", "skills/stray.yaml"} <= got
    assert "skills/demo/SKILL.md" not in got


# ----------------------------------------------------------------------------------- lint 3: sample imports
SAMPLE_IMPORTERS = {"console_demo", "cli"}


def sample_imports(source: str, path: str) -> list[Offence]:
    tree = _parse(source, path)
    out: list[Offence] = []
    for n in ast.walk(tree):
        hit = False
        if isinstance(n, ast.ImportFrom):
            mod = n.module or ""
            if n.level == 0 and mod in ("worker.sample",) or (n.level == 1 and mod == "sample"):
                hit = True
            if (n.level == 0 and mod == "worker") or (n.level == 1 and not mod):
                hit = hit or any(a.name == "sample" for a in n.names)
        elif isinstance(n, ast.Import):
            hit = any(a.name == "worker.sample" for a in n.names)
        elif isinstance(n, ast.Call) and _name_of(n.func) in ("import_module", "__import__") and n.args:
            a0 = n.args[0]
            hit = isinstance(a0, ast.Constant) and a0.value == "worker.sample"
        if hit:
            out.append(Offence(path, n.lineno, "sample_import", "imports worker.sample (fixture data, not config)"))
    return out


def test_lint3_only_console_demo_and_cli_import_sample() -> None:
    bad: list[Offence] = []
    for p in _src_files():
        if p.stem in SAMPLE_IMPORTERS or p.stem == "sample":
            continue
        bad += sample_imports(p.read_text(encoding="utf-8"), _rel(p))
    assert not bad, "imports of worker.sample outside console_demo and cli:\n" + "\n".join(str(o) for o in bad)


@pytest.mark.parametrize("snippet", ["from worker import sample\n", "from worker.sample import CFG\n",
                                     "import worker.sample as S\n", "from . import sample\n",
                                     "def f():\n    from worker import sample as S\n    return S\n",
                                     "import importlib\nm = importlib.import_module('worker.sample')\n"])
def test_lint3_catches_every_import_form(snippet: str) -> None:
    assert sample_imports(snippet, "src/worker/engine.py"), snippet


def test_lint3_ignores_other_modules() -> None:
    assert sample_imports("from worker import samples_dir, money\nimport worker.glline\n", "x.py") == []


# --------------------------------------------------------------------------------------- lint 4: tool audit
def _decorated_tools(fn: ast.FunctionDef) -> dict[str, ast.FunctionDef]:
    out: dict[str, ast.FunctionDef] = {}
    for n in fn.body:
        if isinstance(n, ast.FunctionDef):
            for d in n.decorator_list:
                dn = _name_of(d.func if isinstance(d, ast.Call) else d)
                if dn == "tool":
                    out[n.name] = n
    return out


def _listed(value: ast.AST | None) -> list[str]:
    """Tool names of a list literal (`[a, b]`), of `[by_name["a"]]` and of `[by_name[n] for n in ("a", "b")]`."""
    elts: list[ast.expr] = []
    if isinstance(value, ast.List):
        elts = value.elts
    elif isinstance(value, ast.ListComp):
        elts = getattr(value.generators[0].iter, "elts", [])
    out: list[str] = []
    for e in elts:
        if isinstance(e, ast.Name):
            out.append(e.id)
        elif isinstance(e, ast.Constant) and isinstance(e.value, str):
            out.append(e.value)
        elif isinstance(e, ast.Subscript) and isinstance(e.slice, ast.Constant):
            out.append(str(e.slice.value))
    return out


def _registered(fn: ast.FunctionDef) -> list[str]:
    """Names in the `tools` list of the function and in every `tools += [...]`."""
    names: list[str] = []
    for n in ast.walk(fn):
        value = None
        if isinstance(n, ast.AnnAssign) and _name_of(n.target) == "tools" or isinstance(n, ast.Assign) and any(_name_of(t) == "tools" for t in n.targets) or isinstance(n, ast.AugAssign) and _name_of(n.target) == "tools":
            value = n.value
        names += _listed(value)
    return names


def _calls_run(fn: ast.FunctionDef, name: str) -> bool:
    """The tool body calls `run("<name>", ...)`: the audited wrapper, under its own action name."""
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "run" and n.args:
            a0 = n.args[0]
            if isinstance(a0, ast.Constant) and a0.value == name:
                return True
    return False


def tool_audit_offences(
    source: str, path: str, unaudited: dict[str, str], families: Sequence[str] = ()
) -> list[Offence]:
    """`source` holds `build_tools` and the audited wrapper; `families` are the tool modules' sources, whose
    top-level functions hold the `@tool` closures that `build_tools` registers."""
    tree = _parse(source, path)
    build = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_tools"), None)
    if build is None:
        return [Offence(path, 1, "tool_audit", "no build_tools function found")]
    out: list[Offence] = []
    tools = _decorated_tools(build)
    for fam in families:
        for fn in _parse(fam, path).body:
            if isinstance(fn, ast.FunctionDef):
                tools.update(_decorated_tools(fn))
    registered = _registered(build)
    for name in registered:
        fn = tools.get(name)
        if fn is None:
            out.append(Offence(path, build.lineno, "tool_audit", f"registered `{name}` is not a @tool of build_tools"))
        elif name in unaudited:
            continue
        elif not _calls_run(fn, name):
            out.append(Offence(path, fn.lineno, "tool_audit",
                               f"tool `{name}` does not go through the audited run(\"{name}\", ...) and is not "
                               "listed as read-only-unaudited"))
    # the wrapper itself: run -> _execute -> audit.record, and a failed record stops the action
    inner = {n.name: n for n in build.body if isinstance(n, ast.FunctionDef)}
    run_fn, exe = inner.get("run"), inner.get("_execute")
    if run_fn is None or exe is None:
        out.append(Offence(path, build.lineno, "tool_audit", "the audited `run` / `_execute` wrapper is missing"))
    else:
        called = {_name_of(c.func) for c in ast.walk(run_fn) if isinstance(c, ast.Call)}
        if "_execute" not in called:
            out.append(Offence(path, run_fn.lineno, "tool_audit", "`run` no longer calls `_execute`"))
        records = [c for c in ast.walk(exe) if isinstance(c, ast.Call) and _name_of(c.func) == "record"]
        if not records:
            out.append(Offence(path, exe.lineno, "tool_audit", "`_execute` writes no audit record"))
        if not any(isinstance(h.type, ast.Name) and h.type.id == "AuditError"
                   for h in ast.walk(exe) if isinstance(h, ast.ExceptHandler)):
            out.append(Offence(path, exe.lineno, "tool_audit", "`_execute` does not handle a failed audit record"))
    return out


def test_lint4_every_registered_tool_is_audited() -> None:
    allow = _allow().get("unaudited_tools", [])
    unaudited = {e["tool"]: e["reason"] for e in allow}
    src = (SRC / "tools.py").read_text(encoding="utf-8")
    families = [p.read_text(encoding="utf-8") for p in sorted(SRC.glob("tools_*.py"))]
    bad = tool_audit_offences(src, "src/worker/tools.py", unaudited, families)
    assert not bad, "\n".join(str(o) for o in bad)
    build = next(n for n in _parse(src, "tools.py").body if isinstance(n, ast.FunctionDef) and n.name == "build_tools")
    stale = sorted(set(unaudited) - set(_registered(build)))
    assert not stale, f"unaudited_tools entries that are not registered tools: {stale}"
    assert len(_registered(build)) >= 20  # the walk really found the registered list


_TOOLS_SNIPPET = '''
def build_tools(ctx):
    def run(action, why, inputs, fn):
        return _execute(action, why, inputs, fn)
    def _execute(action, why, inputs, fn):
        try:
            ctx.audit.record(action, why, inputs, {})
        except AuditError:
            return "ERROR"
    @tool
    def audited(why):
        return run("audited", why, {}, lambda: 1)
    @tool
    def sneaky(why):
        return "did a thing with no record"
    @tool
    def peek(why):
        return "read only"
    tools = [audited, sneaky, peek]
    return tools
'''


def test_lint4_catches_an_unwrapped_tool_and_accepts_a_listed_one() -> None:
    got = tool_audit_offences(_TOOLS_SNIPPET, "src/worker/tools.py", {})
    names = " ".join(o.text for o in got)
    assert "`sneaky`" in names and "`peek`" in names and "`audited`" not in names
    listed = tool_audit_offences(_TOOLS_SNIPPET, "src/worker/tools.py",
                                 {"sneaky": "r", "peek": "read-only"})
    assert listed == []


def test_lint4_catches_a_wrapper_that_stops_recording() -> None:
    broken = _TOOLS_SNIPPET.replace("ctx.audit.record(action, why, inputs, {})", "pass")
    got = tool_audit_offences(broken, "src/worker/tools.py", {"sneaky": "r", "peek": "r"})
    assert any("writes no audit record" in o.text for o in got)


# ------------------------------------------------------------------------------------------ lint 5: SKILL.md
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                 "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14}
_UNITS = ("characters", "chars", "rows", "calls", "tokens", "places", "decimals", "cents", "files", "tools")
_LIMIT_WORDS = re.compile(r"toleran|band|ceiling|limit|\bfloor\b|\bwithin\b|\bat most\b|\bup to\b", re.IGNORECASE)
_TOKEN = re.compile(r"`([^`\n]+)`")


def registered_tool_names() -> set[str]:
    src = (SRC / "tools.py").read_text(encoding="utf-8")
    build = next(n for n in _parse(src, "tools.py").body if isinstance(n, ast.FunctionDef) and n.name == "build_tools")
    return set(_registered(build))


def _strip_front_matter(text: str) -> tuple[str, int]:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            skipped = text[: end + 4].count("\n")
            return text[end + 4:], skipped
    return text, 0


def tool_names_in(text: str, known: set[str], not_tools: set[str]) -> list[tuple[int, str]]:
    """(line, name) for each backticked snake_case token or `name(args)` that is not a file, a field or a
    skill word. A token that is not in `known` is an unknown tool."""
    body, skipped = _strip_front_matter(text)
    out: list[tuple[int, str]] = []
    for i, line in enumerate(body.splitlines(), start=skipped + 1):
        for m in _TOKEN.finditer(line):
            tok = m.group(1).strip()
            call = re.fullmatch(r"([a-z][a-z0-9]*(?:_[a-z0-9]+)*)\(.*\)", tok)
            bare = re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+", tok)
            name = call.group(1) if call else (bare.group(0) if bare else None)
            if name and name not in not_tools:
                out.append((i, name))
    return out


def _numbers(text: str) -> list[tuple[int, str, float]]:
    """(line, context sentence, number) for plain digit tokens and number words in prose, front matter and
    table index cells excluded."""
    body, skipped = _strip_front_matter(text)
    out: list[tuple[int, str, float]] = []
    for i, line in enumerate(body.splitlines(), start=skipped + 1):
        if re.match(r"^\s*(\||\d+\.\s)", line):  # table rows (numbered checks) and list markers
            if line.lstrip().startswith("|"):
                continue
            line = re.sub(r"^\s*\d+\.\s", "", line)
        scrub = _TOKEN.sub(" ", line)
        for m in re.finditer(r"(?<![\w./-])(\d+(?:\.\d+)?)(?![\w/-])", scrub):
            out.append((i, line, float(m.group(1))))
        for m in re.finditer(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", scrub, re.IGNORECASE):
            out.append((i, line, float(_NUMBER_WORDS[m.group(1).lower()])))
    return out


def _flat_numbers(obj: Any) -> set[float]:
    out: set[float] = set()
    if isinstance(obj, bool):
        return out
    if isinstance(obj, int | float):
        out.add(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            out |= _flat_numbers(v)
    elif isinstance(obj, list | tuple):
        for v in obj:
            out |= _flat_numbers(v)
    elif isinstance(obj, str):
        for m in re.finditer(r"(?<![\w.])\d+(?:\.\d+)?(?![\w])", obj):
            out.add(float(m.group(0)))
    return out


def prompt_unit_numbers() -> dict[str, set[float]]:
    """{unit word: numbers stated with it} over the string constants of prompts.py."""
    tree = _parse((SRC / "prompts.py").read_text(encoding="utf-8"), "prompts.py")
    text = " ".join(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str))
    return unit_numbers(text)


def unit_numbers(text: str) -> dict[str, set[float]]:
    out: dict[str, set[float]] = {}
    pat = re.compile(r"\b(\d+(?:\.\d+)?|" + "|".join(_NUMBER_WORDS) + r")\s+(" + "|".join(_UNITS) + r")\b", re.IGNORECASE)
    for m in pat.finditer(text):
        tok = m.group(1).lower()
        out.setdefault(m.group(2).lower(), set()).add(float(_NUMBER_WORDS.get(tok, tok) if tok in _NUMBER_WORDS
                                                           else tok))
    return out


def skill_text_offences(
    skill_md: Path, known_tools: set[str], not_tools: set[str], controls_numbers: set[float],
    prompt_units: dict[str, set[float]], recon_check_count: int | None, skill_path: str | None = None,
) -> list[Offence]:
    path = skill_path or _rel(skill_md)
    text = skill_md.read_text(encoding="utf-8")
    out: list[Offence] = []
    # (a) tools it names exist
    for line, name in tool_names_in(text, known_tools, not_tools):
        if name not in known_tools:
            out.append(Offence(path, line, "skill_text", f"names `{name}`, which is not a registered tool"))
    # (b) a number stated next to a limit word is a value some controls.yaml holds
    for line, ctx, num in _numbers(text):
        if _LIMIT_WORDS.search(ctx) and num not in controls_numbers and num not in (0.0, 1.0):
            out.append(Offence(path, line, "skill_text",
                               f"states {num:g} next to a limit word, but no controls.yaml value is {num:g}"))
    # (c) a number stated with a unit word agrees with prompts.py's number for the same unit
    body, skipped = _strip_front_matter(text)
    for i, line in enumerate(body.splitlines(), start=skipped + 1):
        for unit, nums in unit_numbers(line).items():
            theirs = prompt_units.get(unit)
            if theirs and not nums <= theirs:
                out.append(Offence(path, i, "skill_text",
                                   f"states {sorted(nums)} {unit}; prompts.py says {sorted(theirs)} {unit}"))
    # (d) "the eleven checks" is the number of checks in specs.yaml
    if recon_check_count is not None:
        for i, line in enumerate(text.splitlines(), start=1):
            for m in re.finditer(r"\b(\d+|" + "|".join(_NUMBER_WORDS) + r")\s+checks\b", line, re.IGNORECASE):
                tok = m.group(1).lower()
                n = _NUMBER_WORDS.get(tok, int(tok) if tok.isdigit() else -1)
                if n != recon_check_count:
                    out.append(Offence(path, i, "skill_text",
                                       f"says {m.group(1)} checks; specs.yaml defines {recon_check_count}"))
    return out


def _controls_numbers() -> set[float]:
    out: set[float] = set()
    for p in SKILLS.glob("*/controls.yaml"):
        out |= _flat_numbers(yaml.safe_load(p.read_text(encoding="utf-8")))
    # the account-range floors are strings like "1500-1599": their bounds are controls values too
    return out


def _recon_check_count() -> int:
    data = yaml.safe_load((SKILLS / "reconcile-gl" / "specs.yaml").read_text(encoding="utf-8"))
    return len(data["checks"])


def test_lint5_skill_text_names_real_tools_and_states_no_contradicting_number() -> None:
    not_tools = set(_allow().get("skill_text_not_tools", [{}])[0].get("names", [])) if _allow().get(
        "skill_text_not_tools") else set()
    known = registered_tool_names()
    assert known, "no registered tools found in tools.build_tools"
    bad: list[Offence] = []
    files = sorted(SKILLS.glob("*/SKILL.md"))
    assert len(files) >= 4
    for md in files:
        bad += skill_text_offences(md, known, not_tools, _controls_numbers(), prompt_unit_numbers(),
                                   _recon_check_count() if md.parent.name == "reconcile-gl" else None)
    assert not bad, "\n".join(str(o) for o in bad)


def _write_skill(tmp_path: Path, body: str) -> Path:
    md = tmp_path / "SKILL.md"
    md.write_text("---\nname: t\n---\n" + body, encoding="utf-8")
    return md


def test_lint5_catches_a_tool_that_does_not_exist(tmp_path: Path) -> None:
    md = _write_skill(tmp_path, "Call `map_accounts(stage_id)` then `invent_account(x)` and `supply_value`.\n")
    got = skill_text_offences(md, {"map_accounts", "supply_value"}, set(), set(), {}, None, "skills/t/SKILL.md")
    assert [o.text for o in got] == ["names `invent_account`, which is not a registered tool"]
    assert got[0].line == 4 and str(got[0]).startswith("skills/t/SKILL.md:4:")


def test_lint5_catches_a_number_no_control_holds(tmp_path: Path) -> None:
    md = _write_skill(tmp_path, "A difference within 7 is a pass.\nA difference within 5 is a warning.\n")
    got = skill_text_offences(md, set(), set(), {5.0}, {}, None, "skills/t/SKILL.md")
    assert len(got) == 1 and "7" in got[0].text and got[0].line == 4


def test_lint5_catches_a_unit_number_that_contradicts_the_prompt(tmp_path: Path) -> None:
    md = _write_skill(tmp_path, "Show at most 12 rows.\n")
    got = skill_text_offences(md, set(), set(), {12.0}, {"rows": {40.0}}, None, "skills/t/SKILL.md")
    assert any("rows" in o.text for o in got)


def test_lint5_catches_a_wrong_check_count(tmp_path: Path) -> None:
    md = _write_skill(tmp_path, "The twelve checks of the guide.\n")
    got = skill_text_offences(md, set(), set(), set(), {}, 11, "skills/t/SKILL.md")
    assert any("specs.yaml defines 11" in o.text for o in got)


# --------------------------------------------------------------------------------------- lint 6: dead modules
def module_imports(source: str, mods: set[str]) -> set[str]:
    """Module names (of src/worker) the source imports, by any import form, at any depth."""
    out: set[str] = set()
    for n in ast.walk(_parse(source, "m.py")):
        if isinstance(n, ast.ImportFrom):
            if n.level == 0 and n.module == "worker" or n.level == 1 and not n.module:
                out |= {a.name for a in n.names} & mods
            elif n.level == 0 and n.module and n.module.startswith("worker."):
                out.add(n.module.split(".")[1])
            elif n.level == 1 and n.module:
                out.add(n.module.split(".")[0])
        elif isinstance(n, ast.Import):
            out |= {a.name.split(".")[1] for a in n.names if a.name.startswith("worker.")}
        elif isinstance(n, ast.Call) and _name_of(n.func) in ("import_module", "__import__") and n.args:
            a0 = n.args[0]
            if isinstance(a0, ast.Constant) and isinstance(a0.value, str) and a0.value.startswith("worker."):
                out.add(a0.value.split(".")[1])
    return out & mods


def unused_modules(src_dir: Path, entry_modules: set[str]) -> list[str]:
    files = sorted(p for p in src_dir.glob("*.py") if p.stem != "__init__")
    mods = {p.stem for p in files}
    used: set[str] = set(entry_modules)
    for p in files:
        used |= module_imports(p.read_text(encoding="utf-8"), mods) - {p.stem}
    return sorted(mods - used)


def _console_script_modules() -> set[str]:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"\[project\.scripts\](.*?)(?:\n\[|\Z)", text, re.DOTALL)
    out: set[str] = set()
    for m in re.finditer(r'=\s*"worker\.(\w+)', block.group(1) if block else ""):
        out.add(m.group(1))
    return out


def test_lint6_no_unused_module_in_src() -> None:
    entries = _allow().get("unused_modules", [])
    allowed = {e["module"]: e["reason"] for e in entries}
    scripts = _console_script_modules()
    assert "cli" in scripts  # the pyproject block was read
    dead = unused_modules(SRC, scripts)
    left = [m for m in dead if m not in allowed]
    stale = sorted(set(allowed) - set(dead))
    assert not left, "modules nothing imports (no console script, not in the allow-list):\n" + "\n".join(
        f"src/worker/{m}.py:1" for m in left)
    assert not stale, f"unused_modules entries that are used (or gone): {stale}"


def test_lint6_catches_a_module_nobody_imports(tmp_path: Path) -> None:
    src = tmp_path / "worker"
    src.mkdir()
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "cli.py").write_text("from worker import engine\nfrom worker.money import total\n", encoding="utf-8")
    (src / "engine.py").write_text("from . import helper\n", encoding="utf-8")
    (src / "money.py").write_text("", encoding="utf-8")
    (src / "helper.py").write_text("def f():\n    import worker.late\n", encoding="utf-8")
    (src / "late.py").write_text("", encoding="utf-8")
    (src / "orphan.py").write_text("import json\n", encoding="utf-8")
    (src / "circle_a.py").write_text("from worker import circle_a\n", encoding="utf-8")  # imports only itself
    assert unused_modules(src, {"cli"}) == ["circle_a", "orphan"]


# ----------------------------------------------------------------------- lint 7: one balance implementation
_BAL_WORDS = re.compile(r"toler|tol_|tolpass|tolwarn|tol\b|pass_up_to|warn_up_to|within_warn", re.IGNORECASE)


def balance_decisions(source: str, path: str) -> list[Offence]:
    """A comparison that decides pass / warn / balanced: against a tolerance, `abs(a - b)` against a number, or the
    debits against the credits."""
    tree = _parse(source, path)
    out: list[Offence] = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Compare):
            continue
        operands = [n.left, *n.comparators]
        texts = [ast.unparse(o) for o in operands]
        ordered = any(isinstance(op, ast.Lt | ast.LtE | ast.Gt | ast.GtE) for op in n.ops)
        if ordered and any(_BAL_WORDS.search(t) for t in texts):
            out.append(Offence(path, n.lineno, "balance_decision",
                               f"compares against a tolerance outside money.py: {ast.unparse(n)[:70]}"))
            continue
        if ordered and any(isinstance(o, ast.Call) and _name_of(o.func) == "abs" and o.args
                           and isinstance(o.args[0], ast.BinOp) and isinstance(o.args[0].op, ast.Sub)
                           for o in operands) and any(_is_num(o) for o in operands):
            out.append(Offence(path, n.lineno, "balance_decision",
                               f"compares a difference with a number outside money.py: {ast.unparse(n)[:70]}"))
            continue
        low = [t.lower() for t in texts]
        if len(operands) == 2 and any(isinstance(op, ast.Eq | ast.NotEq) for op in n.ops) and (
                ("debit" in low[0] and "credit" in low[1]) or ("credit" in low[0] and "debit" in low[1])):
            out.append(Offence(path, n.lineno, "balance_decision",
                               f"compares debits with credits outside money.py: {ast.unparse(n)[:70]}"))
    return out


def test_lint7_only_money_decides_a_balance() -> None:
    entries = _allow().get("balance_decisions", [])
    files = [p for p in _src_files() if p.stem != "money"]
    left, stale = _filter(_scan(balance_decisions, files), entries)
    assert not left, "balance / tolerance decisions outside money.py:\n" + "\n".join(str(o) for o in left)
    assert not stale, f"stale allow-list entries (balance_decisions): {stale}"
    assert "def balance_status" in (SRC / "money.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("snippet", [
    "def f(diff, tol_pass):\n    return abs(diff) <= tol_pass\n",
    "def f(a, b):\n    return abs(a - b) < 0.005\n",
    "def f(debits, credits):\n    return sum(debits) == sum(credits)\n",
    "def f(r):\n    return r['diff'] > r['pass_up_to']\n",
])
def test_lint7_catches_a_second_balance_implementation(snippet: str) -> None:
    assert balance_decisions(snippet, "src/worker/other.py"), snippet


def test_lint7_ignores_calls_into_money() -> None:
    ok = "from worker import money\ndef f(d, c, tp, tw):\n    return money.balance_status(d, c, tp, tw)[0] == 'ok'\n"
    assert balance_decisions(ok, "src/worker/other.py") == []
