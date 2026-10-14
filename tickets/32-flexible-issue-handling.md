# 32: flexible issue handling, re-upload, one experience for every static type

Raised by the owner 2026-10-13. Slice 1 (decide one row at a time, before/after audit) is built; the rest is open.

1. **Per-row decisions (built).** `apply` takes `rows`; the review pane has per-row Set value / Skip / Hold / Use suggestion.
2. **Edit any cell, not only the cause's field.** Today `edit` is supported only where `_commands` knows how (ID causes, blank
   required field, commitment date, GL account/deal). A free cell edit needs the engine to read the person's value *before*
   validation (today `fill` is applied to output rows after the row's status is decided), plus a recorded before/after and an
   undo. Decide: one `cell` slot in the job, consumed by `static_rows`, audited like `fill`.
3. **(built 2026-10-14; live model not tried) Chat understands free text about rows.** `console_issues._match` only matches the option labels. Route anything it does
   not match to the ticket-30 `interpreter` role with the open group and its rows as context; it returns a closed-set proposal
   (row, option, value) that still goes through preview and one Apply. Never a direct write.
4. **(built 2026-10-14) Re-upload as often as wanted, in any order.** A job's source file can be replaced at any stage. Decisions are carried over by
   row key (source key, else row content hash), the ones whose row changed or vanished are re-opened and listed, the audit gets
   `source.replaced` (old/new sha256, rows kept / re-opened / new). Approved jobs stay immutable.
5. **(audited and the clear gaps fixed 2026-10-14: docs/consistency-audit-static-types.md; four items await a decision) Same screens for every static type** (Investors, Commitments, Investments, Vendors, Affiliates). Audit each tab against the
   others and list every difference (options, labels, rows page, plan, output); remove the ones that are not forced by the data.

## Added 2026-10-14

6. **Everything a person does is audited** (built): `Console.act` and a request middleware; every upload numbered. See STATUS.md.
7. **A re-upload resets what comes after it** (built): the new file is classified (same / modified / different); different data
   clears everything decided about the old data and returns the job to the Map phase. Open: resets caused by other upstream
   actions (a column choice or Required? after conversion), real-file measurement of the thresholds.
