# Build prompt 2: finish the GL and the static files (everything except live model calls)

Read `CLAUDE.md`, then `STATUS.md`, then `docs/build-prompt.md` (the rules still apply), then this file. Work unattended
until every item below is done or blocked. This is a finishing pass: no new screens, no new concepts.

## Why this pass exists

The console engine and the agent path were built separately. Today the console's "Approve and download" records the
approval and updates counters but **writes no upload workbook and has no download**. The console store of existing records
is separate from `worker.registry`. Agent-path staged tables do not survive a restart. Close those gaps for the five
static types (Investors, Commitments, Investments, Vendors, Affiliates) and both GL types (GL Fund, GL LP).

## Out of scope

- Any live model call. Tests and browser runs use the scripted model or the console's built-in sample world.
- New UI design. Keep the mockup's styling and the existing console screens.
- Pushing, amending, or touching history.

## Rules that do not bend

- Code decides, the model proposes. Values, totals, balancing, IDs and files are plain Python. Never invent: a field the
  source cannot provide stays blank and is reported.
- Gates are code. Approval is refused while an ERR is open or a WARN has no reason. Nothing is written before approval.
- Everything is audited through `audit.record`. If the record cannot be written, the action does not happen.
- Names: only `sponsor-a` style names in code and fixtures; the booking systems are the fictitious Ledgerline and
  Fundbook. Never the real product names.
- Never read answer keys. Never commit `.logs/`, `.worker_state/`, outputs or secrets.
- No shortcuts: no stubs, no `pass`, no "TODO later", no output that only looks written. If something cannot be done,
  record the blocker in `STATUS.md` and say so plainly.
- Per item: tests first, implement, full suite and ruff, update `STATUS.md`, one commit `Finish NN: ...`. Stop on the same
  failure twice and record the blocker.

## Items (in this order)

### F1. One writer for the console and the agent path

- Approving a console job must produce the real upload workbook through the **same writer** the agent path uses
  (`write_upload`, template fields from the sponsor's effective template). No second implementation.
- Output: header equals the template's fields in order; rows are exactly the written rows (the funnel's "written");
  generated IDs are the ones stored at approval (`j["ids"]`); left-out rows are not in the file; nothing is invented.
- GL Fund and GL LP: lines, offsets and crosswalks are in the file as the engine counted them (LP read 5 / out 8 / err 1 /
  5 offsets on the sample). Balanced where the template requires it, with the tie-out in the audit.
- Large GL: write streaming (write-only workbook or CSV per the template's declared format), peak memory bounded; the
  existing 1,000,000-row load test must go through the writer too.
- Add `GET /api/console/jobs/{job}/download` (and the "Approve and download" button actually downloads). Only approved
  jobs download. The file path never leaves the job's output directory.
- The approved job's tally and output facts stay frozen (see test J4b).
- Tests: for each of the 7 types, approve a clean sample and open the written file with openpyxl: header, row count,
  key column, a spot-checked value traced back to its source cell, and the audit chain verifies.

### F2. One store of existing records

- The console store (`S["reg"]`) and `worker.registry` become one source of truth. Approving a console job registers its
  records in the registry with provenance (`job`, `source`, `batch`); the agent path's `match_existing` and dependency
  checks read the same data. Loading an export (`import_existing_records`) shows up in both.
- Migration: existing console state files load without loss.
- Tests: approve Investors in the console, then a Commitments job on the agent path sees those investors; the reverse
  also holds. Re-running an approved file reports exact matches, not new records.

### F3. Survive a restart

- Agent-path staged tables, the pending gate, open questions and the job's decisions persist across a process restart
  (the checkpointer is already sqlite; the `Stage` tables and the `specs_by_stage` are not).
- Tests: stage, restart (new process objects), the stage id still resolves and approval still works exactly once.

### F4. GL on the agent path, clean file, end to end

- A clean GL file (no unmapped accounts, no blank deals where required) goes through
  `map_accounts` -> `confirm_account_mappings` -> `check_data_quality` -> `write_upload` and the written file equals the
  console's output for the same file (same rows, same offsets, same totals). Scripted model only.
- A dirty GL file (the G1/G2 shapes) is refused at the gate with the locators and a marked copy offered.
- Tests: the two files above, plus the 1,000,000-row run through the agent path (marked slow, run by default in the
  full suite as today).

### F5. Static types, clean path on the agent path

- Today four of five sample files are refused at the gate (correct) but the clean path is only proven for Vendors.
  Add a clean fixture per type (generated in the test from `worker.sample`, not committed binary data) and prove:
  gate passes, file written, header/rows/keys right, Commitments resolves its investors through the shared store.
- The dirty sample files must still be refused with the same reasons (keep `tests/test_static_types.py`).

### F6. Improver: the slow loop's evaluator

- Implement the evaluator for know-how edits (ticket 24's slow loop): replay the sponsor's saved cases offline with the
  scripted model, same gates as the fast loop (write guard, replay gates, holdout counts only, noise gate, one-step
  rollback). It never reads answer keys and never changes a rule without a human approval.
- Tests: an edit that improves replay passes; one that regresses is refused; rollback restores the previous version.

### F7. Browser verification (all seven types)

- Start the server on a **fresh empty state directory**. For each of the 7 types drive the console in the browser:
  start in chat, question, plan, convert, accept suggestions, approve, **download the file and open it**.
- Check at desktop and phone width. Check the keyboard path (j / k / a / `/`) and that the find box keeps focus.
- Record per type: rows read, written, left out, and the downloaded file's name and row count. Fix what you find;
  add a test for each real bug.

## Done means

- Every one of the seven types has: a real file written on approval from the console, the same file from the agent
  path, a test that opens it, and a browser run that downloaded it.
- One registry; restart-safe agent path; the slow-loop evaluator exists and is tested.
- Full suite green, ruff clean, `STATUS.md` updated with what is real and what is not, and an honest "not built" list
  (live model runs remain not done and must be listed as such).
- Final message: a run summary of **five lines or fewer**, nothing else.
