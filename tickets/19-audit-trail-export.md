# 19 Audit trail that reads in ten years

Do:
- Every user instruction, tool action, gate decision and per-row exclusion or correction already goes through
  `audit.record`; make a human-readable reason mandatory for each (empty reason = action refused).
- Job downloads: the upload file, `<Sponsor>_Rules.md` (the recipe and settings used, with their labels and versions)
  and `<Sponsor>_AuditTrail.csv` (time, who, action, record, before, after, reason), as in the POC.
- Each approved output records the template, overlay, recipe and ID-scheme versions it used; later config changes
  never alter a finished job and are listed in the trail.
- A `verify` command re-checks the hash chain and reports the first break.

Tests: a missing reason is refused; AuditTrail.csv lists every excluded row with its reason; version stamps are
present; tampering with a line is detected.

Acceptance: green tests (`> .logs/t19.log`), STATUS.md updated, one commit.
