# 29 Acceptance suite: every scenario in section 10, run on every change

Why: the mockup is the source for the build and its flow was made credible number by number. The build must reproduce
the same numbers. Design: `docs/design-onboarding-assistant.md` section 10.

Depends on: 25, 26, 27, 28 (write the suite's skeleton first and fill it in as those land).

Do:
- Fixtures that mirror the mockup's sample data exactly (`CFG`, `GLD`, `REG0`, `EXPORTS` in
  `docs/mockup/onboarding-console.html`): Sponsor A investor, commitment, investment, vendor and affiliate files; the
  Ledgerline exports; Sponsor B's seeded state (B Fund I static approved); the GL Fund (18 lines) and GL LP (5 lines)
  files. Put them in `tests/fixtures/acceptance/`.
- One test per scenario id (A1 … L4), named after it, asserting the exact numbers in section 10 (e.g. Investors with
  the export: 15 read = 7 written + 3 left out + 5 need you; Alder Co LLC at `Investor!A8:L8`).
- A large-data fixture generator (not committed data): 1,000,000 GL lines with 10,214 lines on an unmapped account and
  4 accounts × 10,000 blank-deal lines, for G1, G2 and the streaming limits.
- Uses the scripted model only; never a live model.

Tests: the suite itself; it must pass in CI.

Acceptance: every scenario has a passing test (`> .logs/t29.log`), ruff clean, STATUS.md lists any scenario still
failing as a blocker, one commit.
