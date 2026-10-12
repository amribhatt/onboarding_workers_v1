# Autonomous build prompt

Start a fresh Claude Code session in this folder (any model) and say:
"Read docs/build-prompt.md and follow it exactly."

---

You are building the onboarding_worker app end to end, unattended. Work until every open ticket is done or recorded as
blocked. Do not ask questions: make the conventional choice, write it under "Decisions" in STATUS.md, and continue.

## 1. Sources of truth (read in this order, and only what you need)

1. `CLAUDE.md`: project rules. They apply to everything you do.
2. `STATUS.md`: what is done, what is next, decisions and blockers. This is your memory between tickets and after any
   context reset. Re-read it whenever you are unsure where you are.
3. `docs/design-onboarding-assistant.md`: the product. Binding: section 2 (UX, the job screen, millions of rows),
   section 3 (architecture, skills, the data-quality skill), section 8 (decisions 1-16), section 10 (acceptance
   scenarios A-L). Read a section when a ticket needs it, not the whole file every time.
4. `docs/mockup/onboarding-console.html`: the UI target and the reference for behaviour and sample numbers (published
   as v26 at https://claude.ai/artifact/RgJgknkkKFtwpU9TYAPGGY). It is about 190 KB: never read it whole. Use Grep to
   find a function or data block (`CFG`, `GLD`, `REG0`, `EXPORTS`, `SRCCOLS`, `evalStatic`, `xlFilter`, `jobView`) and
   read only those lines.
5. `docs/process-flows.md`: domain rules, read by section.
6. The ticket you are working on in `tickets/`. Do not read other tickets unless this one depends on them.

Precedence when they disagree: CLAUDE.md > the ticket > the design doc > the mockup. Record any disagreement in
STATUS.md under "Decisions" and follow the higher source.

## 2. Build order

1. Commit the planning work already in the tree (`docs/`, `tickets/25-29`, `STATUS.md`) as one commit. Leave the ticket
   13 source changes out of it.
2. Ticket 13 is half done and uncommitted (`src/worker/memory.py`, `service.py`, `staging.py`, `templates.py`,
   `tools.py`, new `registry.py`). Read `git diff` for those files, compare with the ticket, finish it, test, commit.
3. Then 14 → 19, then 20 → 24, then 25 → 29, one ticket at a time. You may write ticket 29's test skeleton early and
   fill it in as 25-28 land.

## 3. The loop for every ticket

1. **Orient (small reads).** Read the ticket. Read the STATUS.md lines for it. Grep for the modules it names; read
   only the functions you will change and their tests. List what already exists before writing anything new.
2. **Plan in five lines or fewer** in your head or scratch: the files to touch, the tests to add, the acceptance check.
3. **Tests first.** Write or extend the tests named in the ticket (and the matching section-10 scenario ids) so they
   fail for the right reason. Run only those tests first.
4. **Implement** the smallest change that makes them pass. Reuse existing helpers. Match the surrounding style,
   naming and comment density. No dead code, no speculative options, no TODOs left behind.
5. **Verify.** Run the ticket's tests, then the full suite and ruff:
   `uv run pytest -q --tb=short > .logs/tNN.log 2>&1` then read the tail (`tail -15 .logs/tNN.log`);
   `uv run ruff check src tests > .logs/ruff.log 2>&1` then read the tail.
   For UI work, check the screen against the mockup (layout, wording, numbers); use a browser if one is available,
   otherwise test the rendered HTML.
6. **Re-read the ticket's "Tests" and "Acceptance" lines** and confirm each one against real output. If any is not met,
   go back to step 4.
7. **Record.** Update STATUS.md: one line under "Done" (what, test count), decisions taken, next step. Keep it short.
8. **Commit** once: `git add` only the files of this ticket, message `Ticket NN: <what it delivers>`. Never push, never
   force, never amend an earlier commit, never commit `.logs/`, `.worker_state/`, outputs or secrets.

## 4. Do not hallucinate

- **Never state something you have not checked in this session.** "Done", "passing", "works" only after you have read
  the tool output that shows it. Quote the summary line (e.g. `142 passed`) in STATUS.md.
- **Read before you edit.** Never edit a function you have not read in its current form. Never assume a file, function,
  fixture, column or config key exists: Grep or Glob for it first.
- **Library APIs:** use the versions pinned in `pyproject.toml` / `uv.lock`. Before using an API you are not certain of,
  check the installed package (`uv run python -c "import x, inspect; print(inspect.signature(x.f))"`) or its docs. If
  still unsure, write a 3-line probe and run it. Never invent parameters, imports or return shapes.
- **Data:** never type, guess or "fix up" a number, ID or name. Expected numbers in tests come from section 10 of the
  design doc or from the mockup's data blocks. If they disagree with what the code produces, find out why before
  changing either; never loosen an assertion to make it pass.
- **Never read answer keys** (`*answer_key*`, `*.answer.json`) except from `score.py` and tests, as CLAUDE.md says.
- **No fake progress:** never skip, xfail, comment out or delete a failing test to get green. Never mock the code under
  test. Never hard-code a test's expected output into the implementation.
- **Unknowns:** if a requirement is ambiguous, choose the option closest to the design doc and the mockup, write it
  under "Decisions" with one line of reasoning, and move on. If it is truly unknowable (e.g. a client rule), leave the
  value blank, report it as MISSING, and note it. Never invent business rules.
- **Cite.** When STATUS.md or a commit message refers to code, give `path:line` or the function name.

## 5. Keep the context short (token budget)

- Search, then read a range. Grep/Glob first; Read with offset/limit. Never read a whole large file to find one thing.
- Never print workbooks, logs, the mockup or lockfiles in full. Redirect long output to `.logs/<name>.log` and read the
  tail or a grep of it. Sample workbooks by range and row count.
- Do not re-read a file you just edited to "check" it; the edit tool reports failures. Do not re-run a passing test
  without a change in between.
- Run independent reads, greps and checks in parallel in one step.
- Use a sub-agent (Explore) for broad questions ("where is X used across the repo"), and keep only its conclusion.
- Keep your own messages terse: what you did, what you found, what is next. No restating the plan, no pasting diffs or
  large code back.
- STATUS.md is the memory, not the conversation. After each ticket, everything needed to continue must be in STATUS.md,
  the code, and the commit, so a context reset or compaction loses nothing.
- One ticket in focus at a time. Do not pre-read future tickets.

## 6. Engineering rules (from CLAUDE.md and the design doc)

- **Code decides, the model proposes.** Copying cell values, totals, balancing, duplicate detection, IDs, the
  data-quality checks, locators and writing files are plain Python. The model never retypes a number or an ID.
- **Gates are code.** Writing the output and any memory write pause for a human via `interrupt_on`. The agent can never
  approve its own action. Approve is refused before a successful run, after a failed run, or while anything needs you.
- **Never invent.** A field the source cannot provide stays blank and is reported as MISSING.
- **Everything is audited** through `audit.record` (append-only, hash-chained, with the reason). If the record cannot be
  written, the action does not happen.
- **No real client or person names** in code, fixtures or docs. Use `sponsor-a`, "Sponsor A".
- **Models** come from env (`WORKER_MODEL`, `WORKER_MODEL_PROVIDER` = `openai` or `openrouter`, roles per design
  section 3). Tests use `tests/scripted_model.py` and never call a live model or the network.
- **Large data:** compute over distinct keys; stream files; never load a million rows into memory or into the browser
  (tickets 25 and 28 set memory limits; respect them).
- **Lean codebase:** no new dependency unless the ticket needs it; pin it in `pyproject.toml`. No new docs beyond what a
  ticket asks for.
- **Environment:** Windows, repo under OneDrive, `uv` with `link-mode = "copy"`. Use forward slashes in paths in code.
  Write files as UTF-8.

## 7. Failure, budget and stop rules

- If the same check fails twice after a real fix attempt, stop working on that point: write the blocker in STATUS.md
  (what fails, the exact error line, what you tried, your best hypothesis), and continue with the next ticket that does
  not depend on it. This overrides CLAUDE.md's "stop" for this run.
- Never loop on retries. No more than three attempts at the same command without changing something.
- Do not "fix" unrelated code you happen to see. Note it in STATUS.md under "Next" instead.
- Never run destructive commands (`rm -rf` outside `.logs/`, `git reset --hard`, `git clean`, force pushes), never
  touch files outside this repository, never delete user data or fixtures.
- Stop when every ticket is committed or blocked.

## 8. Definition of done for the run

- Tickets 13-29 committed, or listed in STATUS.md as blocked with the reason.
- `uv run pytest -q` and `uv run ruff check src tests` green (quote the summary lines).
- Ticket 29's acceptance suite has one test per scenario id in section 10 (A1 … L7) asserting the exact numbers given
  there. Any scenario that does not pass is a blocker in STATUS.md.
- STATUS.md ends with a short run summary: tickets done, test count, scenarios passing / total, blockers, next step.
- Your final message: the same summary in five lines or fewer, nothing else.
