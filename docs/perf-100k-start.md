# Start on a 100,020-row upload

Defect: an Investors xlsx of 100,020 rows, uploaded through the console, did not get past Start in 20 minutes on the
demo engine (`docs/ux-review-records-pane.md`, finding 13). The 1M-row budgets elsewhere were met, so something in
the Start path scaled with rows in a way the set-based paths do not.

## What was measured

Generated fictitious Investors files (`Row Labels, Investor, Type, Entity, Country, Contact Email, Notes`), the same
functions the web handlers call (`Console.ask_attach_bytes`, `act("ask_go")`, `state_view`), and the HTTP handlers
through `TestClient`. A throwaway state folder each time. Scratch scripts are not committed; the committed
regression tests are `tests/test_start_scale.py` and `test_start_on_100k_rows` in `tests/perf/test_perf.py`.

The machine was shared and loaded the whole time (other agents' suites), so wall time is up to twice the process CPU
time. CPU time (`time.process_time`) is the comparable figure; both are given.

Start is `ask_go` for a sponsor with a saved workflow (the demo's Sponsor B): the job is created and run at once.
For a sponsor without one, `ask_go` returns in under a second and the same work is done by the next act (`go`); it
costs the same.

## Cause

Not one call and not an unbounded loop. Start reads the sheet and checks every row, and the check did per-row work
that is constant per file, in a path that is already a Python loop over the rows:

| # | Where | Per row, what it did | Per file, what it needs |
|---|-------|----------------------|-------------------------|
| 1 | `engine_console.required_blank` and the optional-field loop in `eval_static` | asked the world `required(type, field, default)` for every plan field of every row (12 questions per row). The engine records each question on the replay tape: the question was turned into a key by a recursive `_plainish` + `json.dumps` each time (about 0.2 ms), only to find it was already on the tape | one question per field |
| 2 | `engine._console_check` -> `_console_finding_id` | for every row of an error group, resolved the skills folder (`Path.resolve`, a file system call, 0.3 to 0.6 ms on this OneDrive path), stat-ed the `causes.yaml`, and hashed a finding id | one lookup per cause |
| 3 | `Words.detect` | rebuilt the header lookup (`_pick`) for every field of every row | one lookup per (field, headers) |
| 4 | `read_static_records` | the sheet was read into row dicts three times (attach, the plan screens, the engine's read stage) | once while the file is unchanged |
| 5 | `eval_static` and `static_template_rows` | both built the same list of `Src` row objects | once |
| 6 | `static_rows` | decoded the table spool for each outcome although only the detail is used | outcomes only |
| 7 | `spool.encode/decode` | `json.dumps(..., default=...)` and `json.loads(..., object_hook=...)` build an encoder and decoder per call; the hook ran for every nested object of every row | one encoder and decoder; no hook when a line has no typed value |
| 8 | `static_template_rows` | scanned all typed-in fills for every row (rows x fills) | one index by row |
| 9 | `evaluation` | two requests (a state fetch while Start runs) each began their own full evaluation, because the result was stored only when the first finished | one evaluation; the second waits and gets it |

Items 1 and 2 are the big ones. Item 9 is a hazard, not something measured as a cost on a quiet server: a page that
fetches the state while a 100k-row Start is running would have doubled the work, each copy slower for the GIL it
shares. I could not reproduce a 20-minute Start; the baseline measured here was about 2 minutes of wall time at
100,000 rows on a loaded machine (below). So the 20 minutes is explained only in part: two minutes of work, a machine
under load, a browser, and duplicate evaluations. Item 9 is a guard against the multiplication, not a proven cause.

## Fix (no new row loops; `tests/rowloops_baseline.yaml` unchanged)

Per item above, in `src/worker/`: `engine_console.py` (1, 3, 4, 5, 8, plus a string fast path in `cell_text`),
`engine.py` (2, 5, 6), `skills.py` (the skills folder is resolved once), `spool.py` (7), `console_engine.py` (4, 6, 9),
`console.py` (the attach reads the records the job will read, once). Outputs are byte-identical: the spool encoding
is unchanged, tape contents and their order are unchanged (`required` is asked for the same fields in the same
order, once).

## Numbers

Investors, Sponsor B (saved workflow), Start = `ask_go`, in process, warm rule files (first Start in a process
pays about 2 s of YAML parse once).

| Rows | Before, wall | Before, CPU | After, wall | After, CPU |
|------|--------------|-------------|-------------|------------|
| 20,000 | 24 s | 15.2 s | 7.0 s | 5.0 s |
| 100,000 | 126 s | not taken | 36 to 42 s | 23 to 24 s |
| 100,020 (perf test, suite running beside it) | | | 46 s | 28.2 s |

- The first 100,000-row run of the old code: attach 32 s, `ask_go` 126 s. After: `ask_go` 36 to 42 s wall, 23 s CPU,
  state 0.4 s, issues 0.1 s, source viewer 0.3 s (20k rows).
- Target "Start + first state under 30 s": met on CPU time (23 to 28 s); not met on wall time on this loaded
  machine (36 to 46 s). On an idle machine wall is close to CPU. The assertion in the perf test is CPU time under
  twice the target (60 s): the old code is about 75 s of CPU at that size (15.2 s at 20,000 rows, scaled; not run at 100,000), so it fails; the new code passes with room.
- The remainder is a per-row Python loop and JSON spooling of rows (about 0.2 ms per row): the row check
  (`eval_static`, 55 us per row), three spool writes and reads, and the group build. Taking it below 10 s would mean
  keeping the rows in a column store through the check, which is a redesign of the stage boundary, not a fix.
- Attach (the upload, not Start): the sheet is now read once for the whole flow. 100,000 rows take 10 to 30 s,
  almost all of it calamine's xlsx parse in child processes (the 24 / 35 s 1M-row figure is the same code).
- 1M-row budgets: untouched code paths (`columnar`, `setops`, `setrecipe`, `accuracy`). `tests/perf` ran for the recipe
  test only (below); the others were not run here.

## The 3-field recipe on 1M rows (21 s against 3 s): measured floor, not fixed

Measured again here, same shared machine: 23.4 s (old code), 27.8 s with a shared process pool tried and removed.

- The reference (`transforms.apply_field`) runs once per distinct tuple of operand cells: Journal 500,000 +
  Debit about 50,000 + Account 40. One reference call is 22 us on a quiet core, 44 us loaded (10 / 20 s single
  process for the Journal field alone).
- The pool runs it in 4 processes when 20,000 or more tuples. Measured: a fresh pool costs 10 to 15 s on this loaded
  Windows machine for 25,000 to 100,000 tuples against 2.5 to 4.6 s in process; at 500,000 tuples the pool is 14.7 s
  against 19.6 s in process. A pool shared across fields and a higher cold threshold were tried: no gain at 1M rows
  (27.8 s against 23.4 s, inside the noise of this machine), so the change was reverted.
- Reaching 3 s needs the ops compiled to SQL (about 2 us per tuple). That is a second implementation of the rule,
  which the repo keeps out on purpose. The floor with one implementation is the reference call times the distinct
  tuples divided by the cores: about 3 to 5 s of pure compute on an idle 8-core machine, plus the pool start and
  the pickling of 500,000 tuples each way, which is what the 21 s is mostly spent on.

## Tests

- `tests/test_start_scale.py` (default run, about 2 minutes, counts not seconds): the world is asked once per
  field for 20 and for 400 rows; a cause is looked up once for 15 and for 300 error rows; the sheet is read once for
  attach, plan and engine; the skills folder is resolved once; a spool line decodes the same with and without the hook;
  three threads asking for the evaluation of one job start one evaluation. Of the first five, four fail on the old
  code (the spool test passes on both, it guards the fast path); the thread test was written after and was not run
  against the old code.
- `tests/perf/test_perf.py::test_start_on_100k_rows` (`-m perf`): 100,020 rows (the last 20 repeat a key), Start and the
  first state, CPU time asserted under 60 s, both times printed.
