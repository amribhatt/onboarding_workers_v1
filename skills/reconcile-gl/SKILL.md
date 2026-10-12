---
name: reconcile-gl
description: Reconcile a GL upload against the client's own reports with the eleven checks of the GL Recon Checks guide (trial balance, balance sheet, schedule of investments, income statement, schedule of capital, cash flow, PCAP, investor IRR, unfunded commitments, waterfall, entity vs LP level). Use after a GL job has run, to show what ties out and what does not. Not for static records.
version: "1.0"
---

# reconcile-gl

Code adds up the upload by group and compares it with the figure the client's report gives for the same key. You
never type or round a number. You read a break, say what is likely behind it, and ask the analyst for what is missing.

## The eleven checks (in the guide's order)

| # | Check | Compares | Needs from the client |
|---|---|---|---|
| 1 | Trial Balance | balance per Ledgerline account, ITD | their trial balance |
| 2 | Balance Sheet | balance per FS line item, ITD; assets = liabilities + capital | their balance sheet |
| 3 | Schedule of Investments | cost, fair value, shares per investment | their schedule |
| 4 | Income Statement | income and expense per FS line, current fiscal year | their income statement |
| 5 | Schedule of Capital | beginning, movements, ending capital; ending = balance sheet capital | their schedule of capital |
| 6 | Statement of Cash Flow | operating, investing, financing; ties to the cash accounts | their cash flow |
| 7 | PCAP | per investor: contributions, distributions, other, ending, ITD and YTD | their PCAP |
| 8 | Investor IRR | PR_DATE cash flows per investor and the IRR | their IRR table |
| 9 | Unfunded Commitments | commitment - called + recallable, per investor | their PCAP unfunded column |
| 10 | Waterfall | investor + PR_DATE + Trans Type amounts | their waterfall support table |
| 11 | Entity Level vs LP Level | fund run against LP run and offsets | nothing: the other run of the entity |

Which fund types each applies to is in `tables/fund-type-applicability.yaml` (DE, DLP, EL, LP).

## How to work

1. Say which checks apply to this job's fund type and which already have the client's report.
2. For a check without its report, say what to send and in which layout (the Reconcile page offers the blank layout).
   Never show a client figure that was not supplied.
3. Report PASS, WARN, ERROR and REVIEW as the engine gives them. A check that cannot run is a gap: name the missing
   field (GL date, PR_DATE, investor, NO_OF_SHARES, AGNST_COMMIT). Never fill it in from another field.
4. Group by cause. The same amount in several checks is one break (see `tables/cross-check.yaml`): say it once.

## How to read a break

- Trial balance difference over the WARN band: the account mapping is probably wrong; fix it and rerun before the
  balance sheet. A small tiered difference is usually a topside rounding adjustment.
- Balance sheet line with no client line of that name: a rename, REVIEW, matched by the analyst, never a pass.
- Income statement or capital break: check the fiscal-year filter first (fiscal year is the calendar year of the
  latest GL date), then the FS line assignment, then the capital Trans Types.
- Cash flow break: a Trans Type that did not land in the section the analyst expects (`tables/cash-flow-sections.yaml`,
  `tables/trans-type-words.yaml`).
- IRR, PCAP, waterfall: dates must match exactly; PR_DATE different from the GL date is REVIEW for the analyst.
- Entity vs LP: LP total + offset total must be zero per account; otherwise a row is missing or duplicated.

## What to ask the analyst

- The client's report in the checks' layout, for each check that needs one.
- For a REVIEW: which client line is meant (rename) or whether the PR_DATE is the right event date.
- For an unmapped client account: the Ledgerline account (the mapping is the job's, see the map-accounts skill).

## What you do not decide

- Tolerances, severity levels, the balance rule and the required fields are controls in `controls.yaml`. Only a named
  controller changes them; the improver cannot propose a change there.
- A line the upload could not place (no account, still needing a person) is left out of the figures and said so.
- The guide steps the app cannot settle (no transition end-date, no commitment amounts held) are reported, not guessed.

## Where the rules are

`specs.yaml` (the eleven checks), `tables/` (account series, signs, Trans Type words, cash-flow sections, fund-type
applicability, cross-check), `controls.yaml` (limits), `checks.yaml` (guards), `cases/` (known GL fixtures replayed
before any table change is approved). The group-and-sum engine is `worker.recon`. Lists in tables are comma separated.
