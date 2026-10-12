# Planted-set scorecard: 8 entities, pilot FUND-00001

## Against the answer key (matched by location)
- Recall on errors: 4/4 (100%); missed none
- Expected gaps flagged as errors: 0 of 2
- Warnings flagged: 5/5; fixes that remove the finding: none; no fix yet: ['gl_blank_vendor', 'gl_fund_name_variant']
- Findings that match no entry: 2 [{'fund': 'FUND-00005', 'code': 'SCHEDULE_COST_TIES_TO_THE_LEDGER', 'count': 1}, {'fund': 'FUND-00005', 'code': 'ROLLFORWARD_COST_TIES_TO_THE_LEDGER', 'count': 1}]
- Entities left clean: 2/2

## The four autonomy measures
- Rows reaching sign-off with no human edit: 1,000 of 4,000 (25.0%)
- Questions per job: pilot 1 in total; rollout 0 (0.0 per entity)
- Second sponsor, same source system: 1 pilot questions against 1 (drop 0)
- Seconds from upload to the first reviewable file: 6.55

## First-slice acceptance

## Layout variants
- 6 variants: mapped 6, asked 0, mapped wrongly 0
  - VAR-01 vocabulary_gl (FUND-00006): mapped
  - VAR-02 preamble_gl (FUND-00015): mapped
  - VAR-03 vocabulary_rollforward (FUND-00021): mapped
  - VAR-04 two_row_header_soi (FUND-00031): mapped
  - VAR-05 split_tabs_soi (FUND-00039): mapped
  - VAR-06 pivot_tab_soi (FUND-00011): mapped

## First-slice acceptance
- [ok] 1 identifies the ledger, schedule and rollforward without being told (['documentation', 'gl', 'rollforward', 'soi', 'summary'])
- [ok] 2 chooses the ledger as the source, gives its reasoning and asks once (investments_from_gl_soi_and_rollforward)
- [ok] 3 lists the stale investments as expected gaps and flags none as errors (2 gaps, 0 false alarms)
- [ok] 4 flags every planted error (4/4)
- [NOT MET] 4b proposes a fix for every kind of warning (fixes for []; none yet for ['gl_blank_vendor', 'gl_fund_name_variant'])
- [covered by tests] 5 a bulk fix previews, applies as one batch, reverts and is saved as a suggested rule (tests/contract/test_bulk_fix.py, test_remediation_rules.py)
- [ok] 6 on the rollout it asks nothing the playbook already answers (0 questions)
- [ok] 7 clean entities form a cohort signed in one action, a record and audit entry each (2 signed)
- [ok] 8 every state change has an audit record with a rationale and the chain verifies (9 records, chain ok=True; a failed write stops the change: tests/contract/test_audit_chain.py)
