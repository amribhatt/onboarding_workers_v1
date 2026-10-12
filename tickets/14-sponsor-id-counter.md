# 14 Sponsor-level ID generation that continues

Decision: IDs are decided once per sponsor and record type, one counter that continues across funds and batches
(fund 1 investors GINV-1..GINV-59, fund 2's new investors start at GINV-60). Modes as in the POC screens: use source
ID as is, sequential numeric (prefix, zero-pad width, start), custom pattern (PREFIX, SEQ:3, YEAR tokens).

Do:
- `src/worker/idgen.py`: `IdScheme(sponsor, record_type)` stored per sponsor under state, with `next_ids(n)` that
  reserves a block and persists the new counter only when the upload is approved (a rejected run reserves nothing).
- Source-ID mode: flag any ID over the template's `id_max_len` (Ledgerline 20 chars) as ERR; never truncate.
- Changing a scheme, or resetting a counter, is a gated action with a reason; the audit shows old and new values.
- Existing-record IDs (ticket 15) are never reissued: the counter skips any ID already present for the sponsor.
- ID assignment is plain Python; the model never types an ID.

Tests: two funds, same sponsor, counter continues; rejected run does not advance it; collision with an existing ID is
skipped; over-length source ID is ERR; scheme change needs approval and is audited.

Acceptance: green tests (`> .logs/t14.log`), STATUS.md updated, one commit.
