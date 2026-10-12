# 27 Plan editing: Required? per sponsor and source-column override

Why: the process flows say what the first run needs, but each sponsor has its own mandatory fields, and the worker's
column pick is sometimes wrong. Both must be fixable in the table that already lists every output column. Design:
`docs/design-onboarding-assistant.md` section 8 (decisions 15 and 16) and section 10 (scenarios C, E).

Depends on: 13, 25, 26.

Do:
- Mapping table columns: Template field · **Required?** (checkbox) · **Comes from** (dropdown of the file's columns +
  "not in this file") · Confidence · why.
- Required? defaults from the process flows; a tick or untick is stored per sponsor and record type, audited, applies
  to every job of that sponsor, and shows a small sponsor tag when it differs from the default. Cross-sponsor patterns
  may inform defaults quietly; the UI never says "learned from other sponsors".
- Comes from: changing the column is audited, recorded as feedback (ticket 22), offered as a sponsor rule, and re-runs
  the data-quality checks (ticket 25). Constants from sponsor setup (GP_ID) and generated IDs are shown, not editable.
- IDs (decision 6): investors and commitments always get a new ID from the sponsor counter (the old key kept as the
  source key); vendors, affiliates and investments keep the client's ID or the corrected one; a vendor without one gets
  the ID generated from its name. The same ID shows in the sample, every row, find and trace.

Tests: scenarios C1–C3 and E1–E4 of section 10.

Acceptance: green tests (`> .logs/t27.log`), ruff clean, STATUS.md updated, one commit.
