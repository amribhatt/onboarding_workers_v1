# Making the test loop fast (research, diagnosis, measurements)

Measured on the developer machine (Windows 11, 8 logical cores, 15.6 GB RAM, repo and `.venv` under OneDrive), while
other agents were running 30-46 Python processes and leaving 0.6-2 GB RAM free. Wall times therefore swing 30-100%
between identical runs; every number below is the min and the median of 3 interleaved rounds unless it says "1 run".

## 1. What is slow here (diagnosis)

| Question | Finding |
|---|---|
| Does bare `pytest` walk `.claude/worktrees`? | No. `testpaths = ["tests"]` was already set, and pytest's default `norecursedirs` contains `.*`, which already skips `.claude`, `.venv`, `.logs`, `.worker_state`, `.git`. `pytest .` and an IDE that passes `.` walk the other root folders instead. |
| How big is the root? | `scripts/walk_count.py`: 85,250 directories and 945,242 files, 80,370 `test_*.py` files, 284 s just to `os.walk` it. 920,493 files are under `.claude/` (59 worktree copies, each with its own `.venv`). `tests/` is 43 dirs, 375 files. |
| Where does collection time go? | Not the walk and not `conftest.py`. cProfile of `--collect-only`: ~4,760 module imports (langchain, deepagents, pydantic, pandas, pyarrow, duckdb, sqlalchemy, langsmith) cost 30-45 s of `read()`/`open_code` I/O under load. `--noconftest` saves nothing (32.4 s vs 34.9 s min). `python -X importtime`: `langsmith` pytest plugin alone 5.5-9 s of self time. The first test module that imports `worker.*` pays for all later ones (94 s of 160 s in one loaded run). |
| Collection cost in numbers | 1,878 tests selected (70 deselected): 27-40 s calm, 60-170 s under contention, about 18 ms per test (the awesome-pytest-speedup checklist calls 1-3 ms per test good). Source of the slowness is the import stack of `worker`, which belongs to the source modules, not to pytest config. |
| Execution | Tests are heavy end-to-end runs. Slowest (serial, loaded): `test_console_upload::test_F1_*` 53-59 s each, `test_cli` 14-41 s, `test_fitness::lint1[*]` 13-26 s each x 6 (each re-parsed all of `src`), `test_rowloops` 15 s, `test_web` 13-20 s. A 446-test selection took 907 s serial. The full default suite (1,878 tests) was not run to completion: it was at 3% after 25 minutes under contention and was stopped. |

## 2. Sources

- pytest `norecursedirs`, `testpaths`, `--ignore`, `collect_ignore`: https://docs.pytest.org/en/stable/example/pythoncollection.html ; https://docs.pytest.org/en/stable/reference/customize.html . The default `norecursedirs` list was read from the installed `_pytest/main.py` (`*.egg`, `.*`, `_darcs`, `build`, `CVS`, `dist`, `node_modules`, `venv`, `{arch}`); the docs page text itself did not show it (UNVERIFIED in docs, verified in source). Setting `norecursedirs` replaces the defaults.
- Collection starts from `testpaths` or the current directory: https://docs.pytest.org/en/stable/explanation/goodpractices.html (also `--import-mode`: `prepend` default, `importlib` recommended for new projects).
- Disable plugins `-p no:NAME`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD`, `--trace-config`: https://docs.pytest.org/en/stable/how-to/plugins.html
- pytest-xdist worker count and `--dist` modes: https://pytest-xdist.readthedocs.io/en/stable/distribution.html (`-n auto` = physical cores; `load`, `loadscope`, `loadfile`, `loadgroup`, `worksteal`; the page says nothing specific about session-scoped fixtures).
- Python `-X importtime`, `PYTHONDONTWRITEBYTECODE`, `PYTHONPYCACHEPREFIX`: https://docs.python.org/3/using/cmdline.html
- uv: `uv run` makes sure the project environment is up to date before running; `--no-sync`, `--frozen`, `--locked`, `--offline`: https://docs.astral.sh/uv/concepts/projects/run/ and https://docs.astral.sh/uv/reference/cli/#uv-run
- Windows Dev Drive and Defender performance mode (source, package caches and build output on a trusted Dev Drive): https://learn.microsoft.com/en-us/windows/dev-drive/
- Checklist: https://github.com/zupo/awesome-pytest-speedup (section 3).
- UNVERIFIED (could not fetch a primary page): pytest-testmon behaviour and its xdist support (PyPI page did not load), pytest-split, pytest-timeout, OneDrive "Files On-Demand" scanning cost, xdist spawn cost on Windows beyond what was measured here, Docker/DuckDB/Postgres fixture reuse guidance.

## 3. The awesome-pytest-speedup checklist, item by item

| Item | Status | Evidence |
|---|---|---|
| Hardware is fast | Not applicable to the repo; machine is RAM-starved | 0.6-2 GB free of 15.6 GB with 30-46 python processes. Memory, not cores, caps `-n`: use `-n 3`, not `auto`. |
| Collection is fast: `--collect-only`, `norecursedirs`, `testpaths`, `--noconftest`, `-X importtime` | Applied (config), measured, no gain on bare `pytest` | `norecursedirs` added (protects `pytest .`, subdirectory and IDE runs). Collect-only, 3 interleaved rounds, same session: bare 40.2 s min / 50.0 s med; `.` with defaults 26.5 / 38.5; `.` with the new list 37.4 / 39.9. Differences are inside the noise; set of test ids identical (1,878 = 1,878, `diff` empty, for both `pytest` and `pytest .`). `--noconftest` 32.4 / 37.0 vs base 34.9 / 36.2: no gain. `--import-mode=importlib` 32.8 / 63.6 vs 34.9 / 36.2: no gain, not applied. |
| `PYTHONDONTWRITEBYTECODE=1` | Measured, rejected | min 8.3 s was a crashed run (exit 0xC0000409); median 34.6 vs 36.2 base. No gain, and it stops pytest's assertion-rewrite cache from being reused. |
| Built-in plugins disabled (`-p no:...`) | Measured, no gain, not applied | `-p no:langsmith_plugin -p no:anyio`: 37.4 / 48.5 vs 34.9 / 36.2. `-p no:cacheprovider` on top: 39.9 / 47.6. Nothing in `tests/` or `src/` uses either plugin, so it is safe to add if a quieter machine shows a gain; `-p no:cacheprovider` would also remove `--lf`, which the checklist lists as an extra tip. Not measured separately: `pastebin`, `nose`, `doctest`. |
| Only a subset of tests is executed | Applied | `scripts/test_fast.py` (import-graph selection from `git diff main...HEAD`, plus guards and ruff). `slow` excluded in the fast lane. testmon: not measured (UNVERIFIED; needs coverage tracing, its first run is a full run); pytest-skip-slow: not needed, `-m "not slow"` does the same; pytest-incremental: not measured. |
| Network access disabled | Already true, not applicable | Tests use the scripted model; live calls are `-m live`, off by default. pytest-socket not measured. |
| Disk access disabled | Rejected | The product writes upload files and a hash-chained audit log; tests assert on those files. An in-memory filesystem would test something else. Temp is on local `C:`, not OneDrive. |
| Database access optimized | Already true | `db` fixture is SQLite in memory; PostgreSQL parity tests are marked `integration` and off by default; each pg test gets its own database (needed for isolation). |
| Tests run in parallel | Applied for the fast lane, measured; default config unchanged | See section 4. `-n 3`: 475 s -> 219 s median on the same 26-file selection (-54%). Needs a short temp root on Windows (below). `pytest-split`: for CI runners, not measured. |
| Measure first | Done | `scripts/ab_pytest.py` (interleaved A/B, min and median), `--durations`, cProfile, `-X importtime`. hyperfine not installed; pyinstrument not used. |
| `--lf` | Available, unchanged | `cacheprovider` kept on. |
| `config.scan()` (Pyramid) | Not applicable | |

## 4. Before / after

| Change | Before (min / median) | After (min / median) | Verdict |
|---|---|---|---|
| `test_fitness.py` guard file (`_scan` cached per session; same assertions) | 87.7 s / 95.6 s | 29.9 s / 31.2 s | Applied. -67% on a file the fast lane always runs. 6 full re-parses of `src` became 1. |
| Fast lane vs full suite on a branch that changes `console_reshape.py` | full default suite: not completed (see section 1) | 26 files, 446 tests: 460.8 s / 475.3 s serial | Applied (`scripts/test_fast.py`). |
| Fast lane with xdist `-n 3` | 460.8 s / 475.3 s | 199.1 s / 218.8 s | Applied in the script when xdist is importable. All tests passed, serial and parallel. |
| `norecursedirs` | 26.5 s / 38.5 s (`pytest .`, defaults) | 37.4 s / 39.9 s | No measurable gain on a calm walk; kept as protection (pytest `.` over the repo root). Test ids identical. |
| Plugins off, no cache provider, `--noconftest`, `importlib`, no bytecode | 34.9 s / 36.2 s | 32-40 s / 35-64 s | Rejected: noise or slower. |
| venv off OneDrive (`UV_PROJECT_ENVIRONMENT` under `%LOCALAPPDATA%`) | 61.1 s (collect-only, same session, warm) | 58.3-60.1 s | No gain once files are warm; the first, cold run was 164 s in both places. Not applied. |
| xdist `-n 3` on the first attempt | 907 s serial | 652 s, 5 failures | Cause: Windows MAX_PATH. xdist adds `popen-gwN` to every `tmp_path`; the long test names in `test_flex_reshape` overflowed 260 characters. Fixed by a short temp root (`PYTEST_DEBUG_TEMPROOT=C:\owtmp`, set by the script). `test_flex_reshape.py -n 2`: 15 passed. |

Isolation review for xdist: every test uses `tmp_path`/`monkeypatch`; the only session-scoped fixture is the
sandbox/Docker one (`test_flex_sandbox`, off by default); `WORKER_CACHE_DIR` is a per-process temp dir. No fixed ports
are bound by the default suite. Not proven for the whole suite: the full default suite was not run under xdist.

## 5. What was applied

- `pyproject.toml`: `norecursedirs` (the defaults repeated, plus `outputs graphify-out docs skills sandbox scripts templates tickets src`). Default markers unchanged.
- `tests/test_fitness.py`: `_scan` memoised per session (behaviour identical).
- `scripts/test_fast.py`: the branch fast lane. `scripts/ab_pytest.py`, `scripts/measure_pytest.py`, `scripts/walk_count.py`: re-measure tools.
- xdist is not added to the lockfile (it would change `uv.lock`); use `uv run --with pytest-xdist ...`.

## 6. Commands

```bash
# the branch loop: only what the branch can affect + fitness + rowloops + ruff, parallel when xdist is available
uv run --with pytest-xdist python scripts/test_fast.py
uv run python scripts/test_fast.py --list            # what it would run
uv run python scripts/test_fast.py --files src/worker/web.py --list   # what-if

# skip uv's environment check (the interpreter directly)
.venv\Scripts\python -m pytest -q -m "not slow"
.venv\Scripts\python -m pytest -q -n 3 -m "not slow" --basetemp=C:\owtmp   # xdist; short temp root on Windows

# before merge: the default suite (includes slow), then the slow tier on demand
uv run --with pytest-xdist pytest -q -n 3
uv run pytest -q -m slow
uv run pytest -q -m perf          # million-row budgets
uv run pytest -q -m sandbox       # needs Docker + ow-sandbox image
```

`--basetemp` with xdist: pytest appends `popen-gwN`; set `PYTEST_DEBUG_TEMPROOT` instead when using `-n` on Windows.

## 7. Re-measuring

```bash
python scripts/ab_pytest.py -n 3 \
  "base=PY -m pytest --collect-only -q" \
  "variant=PY -m pytest --collect-only -q -p no:anyio"
python scripts/walk_count.py .          # how much a bare walk would cost
```

`PY` is replaced by the interpreter that runs the script, so start it with the interpreter you want to measure.
The CPU columns stay `-` on a uv venv on Windows (the launcher shim is the started process); use wall time.

## 8. Recommendations (in order of expected value)

1. Make the `worker` import graph lazy: `import worker.agent`/pandas/duckdb/pyarrow/langchain at call time in the
   modules other agents own. Collection is import-bound (~4,700 modules); everything pytest-side was within noise.
2. Keep `console_upload` F1 tests and the CLI tests in the default suite but share one built world per module (module
   or session fixture): three of them cost 53-59 s each.
3. Run branch work with `scripts/test_fast.py`; run the full default suite plus `-m slow` once before merge.
4. Close other heavy processes while testing: at 0.6 GB free the machine pages, and no pytest option fixes that.
5. If the OneDrive client keeps burning CPU on `.claude/worktrees` (945k files), exclude that folder from sync.
