# Consistency audit: the five static record types

Date: 2026-10-14 · Ticket 32, item 5 · Data: the built-in sample files of Sponsor A, all entities.
Types: Investors, Commitments, Investments, Vendors, Affiliates. GL Fund and GL LP are out of scope.

## Method

The same flow was driven in a real browser (Chromium, 1280 px) for each type, through the console as a signed-in person:
start the job from a chat sentence, answer the records-in-Ledgerline question with "No", convert, accept all suggestions,
make the ID decision, approve. At each stage (asked, planned, converted, accepted, approved) every tab (Plan, Issues,
Reconcile, Output, Activity) was captured twice: as text (headings, buttons, table columns, notes, pills, inputs, helper
text; numbers, file names and record names normalised away, then diffed across the five types) and as a screenshot (one
labelled montage per screen, five types side by side). Code was read where a screen differed.

## What is identical (no action)

For all five types, at every stage: the job header, the verdict line and progress bar, the five stages, the tab set
(Plan, Issues, Reconcile, Output, Activity), the Issues table (columns, filters, search, paging, the Review pane and its
per-row controls), the Reconcile table, the Output card (Written, download, Next, what I learned, Reconcile, Feedback,
Rulebook), and the Activity tab (decisions, the Show-working tabs, Every row with the same columns and counts).
`tests/test_static_consistency.py` pins that the job view has the same keys for every type.

## Differences that the data forces (left as they are)

- The template fields (names, number, Required? defaults from the process flows) and therefore the Plan rows.
- The causes in the Issues table and their examples (an Investments file has "In the GL but not in the roll forward", a
  Vendors file "first word shared", an Affiliates file "differs only by punctuation", Commitments "investor not on file"
  and "date is blank").
- Commitments has one more Reconcile check ("Every investor is on file") because it depends on Investors.
- "Left out" counts and pills appear only where a sample had rows left out.
- The "Next: ..." button follows the workflow order Investors, Commitments, Investments, Vendors, Affiliates, GL Fund.

## Inconsistencies found

| # | Finding | Status |
|---|---|---|
| 1 | After approval every type showed "Source file [object Object]" in the job header: the new Replace file line read `file`, which the approved view overwrites with the written upload. | Fixed: the view carries `source_file`. (A regression of this branch.) |
| 2 | The ID decision, which every type that generates IDs needs before approval, was shown three ways: a standalone card above the plan (Commitments), a row inside the Plan table that is folded once the job runs (Investors, Investments, Vendors, Affiliates), and not at all after conversion for those four. Approve was enabled and then failed with a 409. | Fixed: one flag (`id_pending`) for every type; the same note on the Plan and Output tabs; Approve disabled until decided; the plan fold opens while the decision is pending. |
| 3 | The standalone ID card (Commitments) had no heading, so it did not say what it was. | Fixed: it is headed "<Type> IDs - how each new ID is made". |

## Open, needs a decision (not changed)

1. **Different defaults for the ID decision.** Investors starts on "Generate new IDs" (prefix, digits, start at); Investments,
   Vendors and Affiliates start on "use the source column" with a "Source ID confirmed: column ... Confirm" step; Commitments
   starts on "Generate". It was not checked whether this follows only from the sample data or from a rule per type. Same
   decision, different first impression: one default, or an explicit reason shown for each, would be more consistent.
2. **Commitments' ID is not a row of the Plan table**, while the other four types keep theirs there. The card now has a
   heading, but it is still a different place. Adding the ID as a row would make the five Plan tables alike.
3. **The Reconcile footer reads "reconcile-gl@1.0#..." on static types.** It is the engine's skill name; it is shown for
   all five, so it is consistent, but it names a GL skill on a non-GL job.
4. **Required? defaults differ by type** (Affiliate_ID and Vendor_ID start unchecked, Investor_ID checked). They come from
   the process flows; whether the difference is intended was not checked against them.

## Not covered

GL Fund / GL LP; the "Yes, I'll upload the export" path of the records-in-Ledgerline question; one fund or some entities
instead of all; failure and resume states; a phone-width pass; the Harness, Records and Standards pages; any file other
than the built-in samples (a real client file may take other paths); other sponsors.
