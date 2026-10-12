# 25 Data-quality skill: one place that defines what "a problem in the data" is

Why: blank mandatory fields, bad IDs, duplicates and broken references show up in every flow (investors, commitments,
investments, vendors, affiliates, GL). Today each case is coded on its own. A single skill that defines the checks as
data, and how each problem is found again in the source file, keeps every flow consistent and lets business users add
a check without a developer. Design: `docs/design-onboarding-assistant.md` section 3 ("data-quality skill") and section
10 (scenarios D, F, G, L).

Depends on: 16, 20, 21.

Do:
- Skill `skills/check-data-quality/` in the Agent Skills format of ticket 21: `SKILL.md` (know-how: how to explain a
  problem and what to suggest), `checks.yaml` (the rules, evaluated by code), `scripts/` (execute, do not read),
  `cases/`.
- Check families, each rule with id, record types, field(s), severity (ERR/WARN), message and suggested fix:
  - **completeness**: a required field is blank on a row. Required comes from the process flows per record type and
    the sponsor's Required? choices (ticket 27). Conditional requirements are allowed (GL: DEPT_ID/deal required on
    1500s, 1600s, 8050s, 9000s). An ID field that is generated when blank is never a completeness error. A required
    field with no column in the file is reported once, plainly ("This file has no “Currency” column").
  - **validity**: length limits (ID, name), allowed characters, dates, amounts, enumerations.
  - **uniqueness**: duplicates within the file and against the sponsor's existing-records store (ticket 15).
  - **consistency**: cross-file references (a commitment's investor, GL investor/vendor/affiliate/investment
    crosswalks, accounts and transaction types against the COA and mapping).
  - **balance**: journals balance; LP lines plus offsets net to zero.
- One cause per rule and field: rows that fail the same rule on the same field are one issue, never one issue per row.
- Every cause carries its **locator**: the source file, sheet, row numbers as ranges, and the Excel filter that finds the
  rows (the column(s) the rule reads, with values or "(Blanks)"), plus an optional breakdown dimension (GL account for GL,
  entity for static files). Code computes the locator; the model never writes it.
- Writers for **marked copy** (the user's file untouched + a "Worker check" column naming the cause on affected rows,
  rows highlighted, AutoFilter on; optionally for one breakdown value) and **just these rows** (affected rows with their
  original row numbers). Both stream; a million-row file must not be loaded into memory at once.
- A blank value can be supplied by the user for one row or for all rows of a cause; it is audited as "supplied by the
  person, never inferred" and the check re-runs.

Tests: each family fires on a fixture; blank Investor_Name, Commitment_Date and Affiliate_Name each become one cause with
the recipe "column … = (Blanks)"; ticking Required? on a field without a column reports it once and makes every row ERR;
a cause of 10,214 GL lines on account 6150 yields "column D “GL Account” = 6150 → 10,214 lines" and three row ranges; a
blank-deal cause over 4 accounts of 10,000 lines yields the two-condition recipe and a 4-line breakdown; the marked copy
keeps every original cell byte-identical and adds only the Worker check column; streaming writers stay under a memory
limit on a 1,000,000-row file.

Acceptance: green tests (`> .logs/t25.log`), ruff clean, STATUS.md updated, one commit.
