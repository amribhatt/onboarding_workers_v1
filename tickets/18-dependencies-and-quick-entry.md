# 18 Record dependencies and the quick "existing record" entry

Order: investors before commitments; all static records before GL (GL conversion itself is the next increment).

Do:
- Blocking check (ERR) listing every missing reference, for example commitment rows whose Investor_ID is not in the
  sponsor's registry or the current batch.
- `add_existing_record` tool, gated: the user says "this one exists, it was loaded outside the app" and supplies the
  existing Ledgerline ID and name (more fields optional). Stored in the registry with source `quick entry`; used as is,
  no reconciliation, no new ID issued. The blocked job re-checks and continues without re-running the investor flow.
- Bulk form: several missing items in one reply.

Tests: a commitment with an unknown investor blocks and lists it; quick entry unblocks; the entry is audited with its
reason and the registry shows its source; a later investor batch dedups against it.

Acceptance: green tests (`> .logs/t18.log`), STATUS.md updated, one commit.
