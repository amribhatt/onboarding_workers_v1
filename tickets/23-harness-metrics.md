# 23 Harness metrics in production units

Why: we improve the harness, never the model. Without metrics in business units we cannot tell real gains from bug
fixes or metric artefacts.

Depends on: 20 (traces), 22 (feedback).

Do:
- `worker.metrics` computes these per skill, sponsor, record type and week, from the audit:
  - correct output cells per 1,000 rows, against answer keys and signed-off jobs;
  - overrides per 1,000 rows and questions to the analyst per 1,000 rows. Track both, so the harness cannot "improve"
    by asking about everything;
  - rows left out per 1,000 rows, with the reasons checked;
  - accept-all rate;
  - share of keys resolved without the model (for example `map-accounts` L0 + L1);
  - first-pass tie-out rate;
  - Ledgerline rejections after upload;
  - time to approve;
  - model calls and tokens per 1,000 rows;
  - success rate: approved with no "needs revision" and no upload rejection;
  - cost per successful job = (model spend, including failed and retried runs, + reviewer time × a configurable hourly
    rate) ÷ successful jobs. Shown per workflow with the breakdown (review, model, retries). On free models the model
    spend is 0 and reviewer time remains;
  - skill usage, seen from the model's side: which skills and scenario files were loaded per job; how often a skill
    was loaded but not needed, or needed but not loaded (description quality); and which context the model leans on
    most.
- Each metric definition is versioned. A gain is always recomputed with the definition pinned at the baseline version.
- Harness bug fixes are tagged separately from improver changes, so a fix is never counted as learning.
- A console "Health" view per sponsor and skill shows trend lines, with the last promoted change marked.

Tests: metrics on a fixture audit match hand-computed values; a definition change does not alter past numbers; bug-fix
tags are excluded from improver gains.

Acceptance: green tests (`> .logs/t23.log`), ruff clean, STATUS.md updated, one commit.
