# 24 Improver with replay gates (harness self-improvement, Level 1)

Why: corrections should recursively improve the harness (skills, decision tables, synonyms, know-how, bounded config)
while the model stays frozen. The value is in the gate, not the proposer. Reference: harness-level recursive
self-improvement (signal → diagnose → propose → evaluate → gate → promote), with two speeds and an external verifier.

Depends on: 21, 22, 23.

Do:
- An improver, separate from the worker:
  - clusters feedback by cause;
  - drafts the smallest change to one surface;
  - files it as an Improvement card showing the change, its evidence, the predicted effect, and the replay result.
- Write guard: an allow-list of surfaces the improver may edit — decision-table rows, synonyms, skill know-how
  (`SKILL.md` body and scenario files), skill `description` (to fix wrong or missed triggering), examples, and
  bounded thresholds. It may not add or change skill scripts or dependencies; those go to an engineer as a ticket.
  This is the gated version of the article's "agents create and edit their own skills". It can never edit:
  - `score.py` or the answer keys;
  - the replay set;
  - the ERR/WARN severity floor, or the never-invent rule;
  - the audit, or the output and memory gates.
- External verifier:
  - private holdout: answer-key sets plus signed-off jobs the improver never reads (same path guard as the file tools);
  - dominance: no regression on any split (sponsor, record type, static vs GL), and a real gain on at least one;
  - any change to a signed-off output blocks the change;
  - budget parity: a gain bought with more model calls or more questions is rejected;
  - noise band: slow-loop candidates run at least 3 times, and the paired gain must exceed run-to-run variance.
- Fast loop, nightly, no model calls:
  - covers table rows, synonyms and mappings;
  - replays every recorded job for the scope deterministically;
  - may auto-promote within one sponsor after passing the gates (configurable, off by default);
  - organisation scope always needs an admin.
- Slow loop, weekly:
  - covers know-how and prompt edits, one surface at a time;
  - re-runs on the holdout, then goes to human approval.
- Promote and rollback:
  - every promotion is a new version pinned to future jobs, and finished work never changes;
  - one-step rollback;
  - predicted vs observed is tracked for 2 weeks after release, with an alert if they diverge.
- Business users maintain the system without developers. In the console they can:
  - edit decision-table rows;
  - add column names to the semantic layer;
  - upload a new template, Golden COA or transaction-type mapping;
  - add a new template (the worker maps its columns to known concepts and reuses existing skills).
  Each of these becomes an Improvement card and passes the same replay gates before approval. Only a new script (a new
  kind of logic) needs a developer.
- The improver never approves its own change. Improving the improver is out of scope until Level 1 is shown on the
  holdout.

Tests:
- a table-row change that fixes 3 cases and changes no signed-off output passes;
- one that changes a signed-off output is blocked;
- an attempt to edit `score.py` is refused and audited;
- a gain within noise is rejected;
- rollback restores the previous version exactly.

Acceptance: green tests (`> .logs/t24.log`), ruff clean, STATUS.md updated, one commit.
