# 28 Reviewing millions of rows: decisions, not rows

Why: a million-row file holds a few thousand distinct values and a few dozen decisions. The screen must show that
reduction and let the person work at the level of decisions, then find any row in their own Excel file in seconds.
Design: `docs/design-onboarding-assistant.md` section 2 ("Millions of rows") and section 10 (scenarios F, G, H, I).

Depends on: 25, 26.

Do:
- **Issues card**: a funnel (rows → distinct records → written by rules · decided by you · to check · left out by rules ·
  for you), causes ranked by impact (blocking, then amount, then rows), a heat strip of where issues cluster (entity, or
  journal/account for GL) that narrows on click.
- **Each cause shows where it is**: file, sheet, row ranges, Excel range for a single row (with copy), the other fields
  of the row as the file names them, and the Excel filter recipe, "copy values", marked copy and just-these-rows from
  ticket 25. A cause over several breakdown values shows one line per value (count, ranges, its own filter and marked
  copy). The per-row table shows the first 20.
- **Find and focus** across every file of the job (source and Ledgerline export): typed in a find box with status and entity
  selectors, or said in the chat ("show errors in Fund B for harbor") and turned into the same structured filters; the
  issue list narrows too; "show all" clears.
- **Sample** sized for 95% confidence (±5%) instead of first/middle/last; all rows when the output is small.
- **Trace** any written value: file, sheet, row, ID rule, name column, checks, decision and who made it, model calls,
  approval.
- **Keyboard triage**: j / k between causes, a to accept, / to the chat.
- Everything is computed by code over distinct keys and streamed; nothing loads all rows into the browser.

Tests: scenarios F1–F3, G1–G6, H1–H3 and I1–I6 of section 10, plus a 1,000,000-row GL fixture with 4 accounts of 10,000
blank-deal lines: the page renders the cause, recipe and breakdown without sending the rows to the browser.

Acceptance: green tests (`> .logs/t28.log`), ruff clean, STATUS.md updated, one commit.
