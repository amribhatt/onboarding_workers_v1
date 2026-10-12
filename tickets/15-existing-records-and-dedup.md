# 15 Sponsor-level existing records and duplicate handling

Static records (investor, investment, commitment, affiliate, vendor) are unique per sponsor, loaded per fund, and
deduplicated against everything the sponsor already has: earlier batches, other funds, and records loaded outside the
app and supplied as an export. Those IDs are existing Ledgerline IDs, used as is, never reconciled.

Do:
- Extend `registry.py`: store key, name, source (`app batch` | `outside export` | `quick entry`), fund, batch, time.
- Match each candidate row against the registry, after normalising name and key:
  - same key, same name: excluded from the build, reason `already onboarded` (counted and listed);
  - same key, different name: **ERR**;
  - same name, different key: **ERR**.
- Resolution for an ERR row (human decides, audited with a free-text reason): `exclude as duplicate`,
  `exclude as incorrect`, or `correct` (edit the value, re-check). The final file never contains excluded rows.
- Within the file: `distinct_rows` collapse stays; one key under two names is a conflict, never merged silently.
- No old ID in the source: normalised name is the key, probable merges (spelling variants) go to a human, and the
  crosswalk records the key kind (`source id` | `name`).
- One audit record per excluded or changed row: row number, key, name, matched record, rule, decision, reason, who.
- Import an existing-records export (xlsx/csv) into the registry; the import is an audited, gated action.

Tests: all three match cases; exclusion reasons appear per row in the audit; resolution actions; the output has no
excluded row; counts reconcile (in = out + collapsed + excluded); export import is gated.

Acceptance: green tests (`> .logs/t15.log`), STATUS.md updated, one commit.
