# onboarding_worker

An agent that acts like an onboarding knowledge worker. A human drops a file and says "build me the Ledgerline sheet".
The agent opens the file like a person would, works out what it is, suggests what to do, continues on its own, is loud
about what is missing, shows what ties out, and asks the human to check. Humans approve; the agent proposes.

Read `STATUS.md` first, then only the ticket you were given in `tickets/`.

## Rules

- **Code decides, the model proposes.** Copying cell values, totals, balancing, duplicate detection and writing upload
  files are plain Python. The model never retypes a number or an ID.
- **Gates are code.** `write_upload` and any memory write pause for a human decision via `interrupt_on`. The agent can
  never approve its own action.
- **Never invent.** A field the source cannot provide stays blank and is reported as MISSING.
- **Never read answer keys.** Paths containing `answer_key` or ending `.answer.json` are refused by the file tools. Only
  `score.py` and tests read them.
- **Everything is audited** through `audit.record` (append-only, hash-chained, with the reason). If the record cannot
  be written, the action does not happen.
- **No real client or person names** in code, fixtures or docs. Use `sponsor-a`.
- Models: configurable by env (`WORKER_MODEL`, `WORKER_MODEL_PROVIDER` = `openai` or `openrouter`). Tests use the
  scripted model in `tests/scripted_model.py`; they never call a live model.

## Commands

```bash
uv sync                                   # install
uv run pytest -q --tb=short > .logs/test.log 2>&1; tail -15 .logs/test.log
uv run ruff check src tests
uv run python -m worker.cli run <file> --sponsor sponsor-a --gp-id GP-001
uv run python -m worker.score <out_dir> <answer_key_dir>
```

## Context discipline (important for unattended runs)

- Redirect test/build/script output to `.logs/<name>.log` and read only the tail or the summary line.
- Never print whole workbooks; sample by range and row count.
- Finish a ticket: tests green, `STATUS.md` updated, one small commit. Never push.
- If a check fails twice, write the blocker into `STATUS.md` and stop.
