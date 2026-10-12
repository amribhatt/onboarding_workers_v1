# 16 ERR / WARN levels and the review screen

Do:
- Each check has a level, set per organisation with sponsor override: **ERR** blocks the approval; **WARN** needs an
  explicit acknowledge. The default mapping lives in the template sidecar; checks already in `worker.checks` are
  classified, none removed.
- The approval card (web console and CLI) shows: rows read, ERR count, WARN count, then the ERR list, the WARN list,
  and flagged rows with `Keep | Exclude` and a DNI flag, as in the POC screens (Keep all, Exclude all, Mark all DNI).
- Easy audited workaround: correct a value inline, exclude a row with a reason, acknowledge a WARN with a reason.
  Every action is an audit record with its reason.
- The gate refuses approval in code while any ERR is unresolved (the approver resolves it, cannot override it).
  AI prepares, human approves; the approver identity is stored with the decision.
- Final reconciliation note: input rows, output rows, collapsed by dedup, excluded by existing records, excluded by
  analyst; plus re-checks on the final output (ID uniqueness, length, required fields).

Tests: an unresolved ERR blocks `write_upload`; WARN acknowledged without a reason is refused; resolution changes the
output and the audit; reconciliation numbers add up.

Acceptance: green tests (`> .logs/t16.log`), STATUS.md updated, one commit.
