"""Fast lane for branch work: run only the tests that can be affected by what changed, plus the cheap guards.

    uv run python scripts/test_fast.py                 # changed since `main` (merge-base) + working tree
    uv run python scripts/test_fast.py --base HEAD~3   # another base
    uv run python scripts/test_fast.py --list          # show the selection, run nothing
    uv run python scripts/test_fast.py -- -k audit     # extra pytest args after --

Selection (plain static analysis, no model, no plugin):
  1. changed files = `git diff --name-only <base>...HEAD` + uncommitted + untracked.
  2. a changed tests/test_*.py is selected itself.
  3. a changed src/worker/**.py is mapped to a dotted module; every src module that imports it, transitively, is
     affected; every tests/test_*.py that imports any affected module is selected.
  4. always on: tests/test_fitness.py, tests/test_rowloops.py and `ruff check` of the changed python files.
  5. a change this script cannot map (pyproject, conftest, tests helpers, non-python files under src, lockfile)
     selects the whole default suite, and says so. Never a silent partial run.

It only narrows WHICH files run; the default markers still apply (no live/integration/perf/sandbox), and `slow`
is left out here (`-m "not slow"`). Run the full default suite plus the slow tier before merging, see
docs/research-pytest-speed.md.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
TESTS = ROOT / "tests"
GUARDS = ["tests/test_fitness.py", "tests/test_rowloops.py"]
IMPORT_RE = re.compile(r"^\s*(?:from\s+(worker(?:\.[\w.]+)?)\s+import\s+(.+)|import\s+(worker(?:\.[\w.]+)?))", re.MULTILINE)


def git(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout
    return [line.strip().replace("\\", "/") for line in out.splitlines() if line.strip()]


def changed_files(base: str) -> list[str]:
    files: set[str] = set()
    merge_base = git("merge-base", base, "HEAD")
    files.update(git("diff", "--name-only", f"{merge_base[0] if merge_base else base}...HEAD"))
    files.update(git("diff", "--name-only", "HEAD"))
    files.update(git("ls-files", "--others", "--exclude-standard"))
    return sorted(files)


def module_name(path: Path) -> str:
    rel = path.relative_to(SRC).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def imports_of(text: str) -> set[str]:
    """Dotted names a file may depend on: `from worker.a import b, c` -> worker.a, worker.a.b, worker.a.c."""
    found: set[str] = set()
    for m in IMPORT_RE.finditer(text):
        if m.group(3):
            found.add(m.group(3))
            continue
        base = m.group(1)
        found.add(base)
        # multi-line parenthesised imports: take names up to the closing paren
        names = m.group(2)
        if "(" in names and ")" not in names:
            tail = text[m.end(): m.end() + 600]
            names += tail.split(")")[0]
        for n in re.findall(r"[A-Za-z_]\w*", names.replace("(", " ")):
            if n not in {"as"}:
                found.add(f"{base}.{n}")
    return found


def select(changed: list[str]) -> tuple[set[str], str | None]:
    """Return (test files, reason for a full run or None)."""
    modules = {module_name(p): p for p in SRC.rglob("*.py")}
    deps = {m: imports_of(p.read_text(encoding="utf8", errors="ignore")) for m, p in modules.items()}
    tests = {p: imports_of(p.read_text(encoding="utf8", errors="ignore")) for p in TESTS.glob("test_*.py")}
    # reverse graph: who imports m
    users: dict[str, set[str]] = {m: set() for m in modules}
    for m, ds in deps.items():
        for d in ds:
            if d in users and d != m:
                users[d].add(m)
    affected: set[str] = set()
    picked: set[str] = set()
    for f in changed:
        if f.startswith("tests/test_") and f.endswith(".py"):
            picked.add(f)
        elif f.startswith("src/worker/") and f.endswith(".py"):
            affected.add(module_name(ROOT / f))
        elif f.startswith(("docs/", "tickets/", "scripts/")) or f in {"STATUS.md", "README.md", "CLAUDE.md", "CONTEXT.md"}:
            continue  # no test depends on these
        elif f.startswith(("tests/fixtures/", "tests/perf/")):
            return set(), f"{f} is shared test data"
        else:
            return set(), f"{f} cannot be mapped to specific tests"
    stack = list(affected)
    while stack:
        m = stack.pop()
        for u in users.get(m, ()):
            if u not in affected:
                affected.add(u)
                stack.append(u)
    for path, imps in tests.items():
        if imps & affected:
            picked.add(path.relative_to(ROOT).as_posix())
    return picked, None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="main")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--files", nargs="+", help="pretend these files changed (for what-if selection)")
    ap.add_argument("--no-ruff", action="store_true")
    ap.add_argument("--serial", action="store_true", help="do not pass -n auto even when xdist is installed")
    ap.add_argument("pytest_args", nargs="*")
    a = ap.parse_args()

    changed = sorted(a.files) if a.files else changed_files(a.base)
    picked, full_reason = select(changed)
    py_changed = [f for f in changed if f.endswith(".py") and (ROOT / f).exists()]
    print(f"changed files: {len(changed)}")
    if full_reason:
        print(f"FULL default suite: {full_reason}")
        targets: list[str] = []
    else:
        targets = sorted(picked | set(GUARDS))
        print(f"selected {len(targets)} test files ({len(picked)} mapped + guards):")
        for t in targets:
            print("  " + t)
    if a.list:
        return 0

    status = 0
    if not a.no_ruff and py_changed:
        r = subprocess.run([sys.executable, "-m", "ruff", "check", *py_changed], cwd=ROOT)
        status |= r.returncode
    marker = ["-m", "not slow and not live and not integration and not perf and not sandbox"]
    xdist = []
    if not a.serial:
        try:
            import xdist as _x  # noqa: F401

            xdist = ["-n", "auto"]
        except ImportError:
            pass
    env = dict(os.environ)
    if xdist and sys.platform == "win32" and "PYTEST_DEBUG_TEMPROOT" not in env:
        # xdist adds `popen-gwN` to every tmp_path; with the long test-name folders that overflows MAX_PATH (260)
        # under %TEMP% (5 test_flex_reshape tests failed that way). A short temp root avoids it.
        short = Path(Path.home().anchor) / "owtmp"
        short.mkdir(exist_ok=True)
        env["PYTEST_DEBUG_TEMPROOT"] = str(short)
    cmd = [sys.executable, "-m", "pytest", "-q", "--tb=short", *marker, *xdist, *targets, *a.pytest_args]
    status |= subprocess.run(cmd, cwd=ROOT, env=env).returncode
    return 1 if status else 0


if __name__ == "__main__":
    sys.exit(main())
