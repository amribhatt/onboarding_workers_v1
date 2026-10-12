# 17 Saved workflow (recipe) with source variants

Builds on ticket 06 (sponsor memory, approved cases, replay).

Do:
- After an approved run, save a **recipe** keyed by sponsor + record type: deterministic steps and parameters (source
  sheet/columns to target fields, joins, distinct keys, constants, dedup policy, ID scheme reference, checks) plus the
  user's instruction. Versioned, human-readable (`recipes/<sponsor>/<record_type>/v<N>.json` and `.md`).
- A recipe holds several **source variants** (common file, GL-derived extraction, horizontal layout, ...). The fund is a
  run parameter (GP_ID, Fund_ID, entity name), not part of the key.
- Matching a new file: exact layout -> apply and go straight to the approval card; close match (renamed or added
  columns, transposed table) -> propose synonym mappings with a visible diff and ask; no match -> fresh conversation.
  A confirmed new layout is stored as another variant. With three or more variants, pick the closest and ask for review.
- Mappings transfer across the sponsor's funds; a field that differs is shown as `variation` or `missing`.
- A field absent in a later batch (for example bank accounts) stays blank, is reported MISSING, and the job still
  completes. Nothing is invented.
- A per-run instruction can switch the variant ("no common file, extract the remaining investors from the GL").
- Re-applying a recipe runs code, not a model, and is always gated.

Tests: save and re-apply on an identical layout with no model call; a renamed-column file proposes a diff; a second
variant is stored; a missing optional field yields blank + MISSING; fund 2 re-uses fund 1's mapping; replay stays green.

Acceptance: green tests (`> .logs/t17.log`), STATUS.md updated, one commit.
