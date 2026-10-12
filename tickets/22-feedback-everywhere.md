# 22 Feedback everywhere

Why: every human correction is data. Feedback must be one click from anywhere the worker made a choice, attached to
what produced it, so it can be routed to the right skill, rule or prompt.

Depends on: 21 (skills and decision tables to attach feedback to).

Do:
- A `feedback` record, written through `audit.record`, holding:
  - what was shown;
  - the action: accept, override, reject, flag, or comment;
  - the reason (optional text);
  - the intended scope: row, job, sponsor, or organisation;
  - the job, sponsor and record type;
  - skill@version, the decision-table row id, the model and prompt version;
  - the input fingerprint and source-cell lineage.
- Overrides and rejections are recorded as feedback even when no comment is given.
- Capture points:
  - plan line or mapping;
  - suggestion;
  - output row or cell flag;
  - whole-job approve-with-notes or "needs revision";
  - post-upload outcomes (Ledgerline rejection, reconciliation break), entered by hand for now;
  - chat messages that correct the worker.
- Routing is done by code from the attachment, and the model only drafts the wording:
  - data rule → a proposed table row;
  - concept → a proposed synonym;
  - skill behaviour → a proposed know-how edit or example;
  - tool or check gap → a ticket with the failing case attached.
- Every corrected job is stored as a replay case: input fingerprint, approved output, reason. Answer keys are never
  stored here.
- Console: a small flag on every line, with the reason typed in the message box. A read-only "Feedback" view per sponsor
  and skill.

Tests: an override with no comment creates a record; routing picks the right target for each attachment type; the
record is hash-chained; scope defaults to the job when not stated.

Acceptance: green tests (`> .logs/t22.log`), ruff clean, STATUS.md updated, one commit.
