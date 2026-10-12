# Research: making data-quality issues easy to understand and resolve

Scope: why the Issues and Check views read as "accept all" to a first-time user, and what primary sources say to do
instead. Pain points come from the handoff and the two screenshots (repeated file/sheet/range text on every card, an
Excel filter recipe shown by default, a fix that is a sentence not an action, one suggestion per issue, one global
"Accept all suggestions"). Every claim below was fetched from the cited page on 2026-10-11. Anything I could not
verify at the source is marked UNVERIFIED.

## 1. Principles

1. **Tell the user what is wrong and what to do, in plain words, and nothing else by default.** GOV.UK: "explain what
   went wrong and how to fix it", plain English, positive language
   (https://design-system.service.gov.uk/components/error-message/). NN/g adds: avoid jargon, be precise, be
   constructive, "offer some potential remedies" (https://www.nngroup.com/articles/error-message-guidelines/).
2. **Name the specific problem, not a category.** GOV.UK: "General errors are not helpful to everyone"; say "Enter
   your first name", not "This field is required" (same GOV.UK page). Here: "2 IDs are too long for Ledgerline" not
   "ID too long".
3. **No codes or tool-specific internals in the main message.** NN/g: hide or minimise obscure codes, show them
   "for technical diagnostic purposes only" (error-message-guidelines page); heuristic 9 asks for "plain language (no
   error codes)" (https://www.nngroup.com/articles/ten-usability-heuristics/). A cell range such as `Investor!A9:L10`
   and an Excel filter recipe are exactly this. Power Query keeps reason/message/detail behind a "Show details"
   button (https://learn.microsoft.com/en-us/power-query/dealing-with-errors).
4. **Progressive disclosure, two levels only.** Show a few core options first, defer the rest on request; NN/g warns
   that three or more levels "tend to lose users", and that the path to the deeper level must be visible and labelled
   (https://www.nngroup.com/articles/progressive-disclosure/). Map: level 1 = card (cause, count, choices);
   level 2 = "Why" and "Show rows".
5. **Summarise first, then fix.** GOV.UK always shows a summary at the top that links to each problem and uses the
   same wording as the item itself (https://design-system.service.gov.uk/components/error-summary/). Here the
   header bar already counts rows; make each count a jump link to its group.
6. **Group by cause and show the count; act per group.** OpenRefine clustering groups similar values and lets the
   user pick one value for the whole cluster or leave it alone, and "always requires the user to approve each
   suggested edit" (https://openrefine.org/docs/manual/cellediting). Flatfile sheet actions can target the whole
   sheet, a filtered view or selected records, with a confirmation modal by default
   (https://flatfile.com/docs/guides/using-actions.md). Bulk scope should be the group, never the whole file.
7. **Give a small set of explicit choices, one marked recommended.** Linear's triage queue offers accept, decline,
   mark duplicate and snooze per item, each with a one-key shortcut
   (https://linear.app/docs/triage). Power Query offers remove, replace or keep for rows with errors
   (Power Query page above). Both show that "fix it", "drop it" and "decide later" are normal sibling options.
8. **Include a "decide later / ask someone" option.** Linear's snooze hides an item until you choose, and to get
   information from the creator you comment and leave it in triage (Linear page above). Our equivalent is "Ask client",
   which becomes a tracked question, not a silent skip.
9. **Label buttons by their outcome, not Yes/No/OK.** NN/g on dialogs: "Delete file" / "Keep file", not Yes/No; avoid
   generic "Are you sure?" (https://www.nngroup.com/articles/confirmation-dialog/). GitHub Primer likewise asks for
   active verbs with the item named (https://primer.style/product/ui-patterns/saving/).
10. **Prefer undo to confirmation dialogs.** NN/g: "try your best to offer undo"; reserve confirmations for serious,
    hard-to-reverse actions, otherwise people click through (confirmation-dialog page). Heuristic 3 recommends
    visible undo/redo and cancel (ten-usability-heuristics page). Primer: confirm destructive submissions, offer undo
    for risky changes (Primer page above).
11. **Say what will change before it changes.** Flatfile's confirm modal text is a question describing the action
    (using-actions page); OpenRefine shows the cluster and the chosen value before applying (cellediting page).
    Preview = before to after for one example row plus the count.
12. **Order by severity and say what blocks.** Carbon: define high/medium/low attention levels, high = immediate
    action; when statuses are grouped use the highest-attention colour for the group; never colour alone, pair with
    icon and label; more than five or six indicator types overloads the UI
    (https://carbondesignsystem.com/patterns/status-indicator-pattern/). Put "blocks the upload" above "worth
    checking".
13. **Keep the user's work when something fails and put help next to the problem.** NN/g: show an actionable message
    "below or next to the problem field" (https://www.nngroup.com/articles/errors-forms-design-guidelines/); Primer:
    keep entered data in the form on failure (Primer page above). So the "type a value" input lives inside the card.
14. **Disable an action that cannot apply instead of letting it fail.** Flatfile action constraints (`hasAllValid`,
    `hasSelection`, `hasData`) disable an action under those conditions (using-actions page). NN/g heuristic 5:
    prevention beats good messages. Here: "Write upload" stays disabled until blocking groups are decided, with the
    reason in one line.
15. **Design the empty/complete state.** Atlassian empty state: header is the only required part, add only useful
    description, "one primary action ... the best next step"
    (https://atlassian.design/components/empty-state/examples). Success states are not covered on that page
    (UNVERIFIED for Atlassian). Complete state here: "Nothing needs you. 15 rows ready" + one button.
16. **Do not nag early.** GOV.UK: validate when the user tries to move on, not on every field, and "minimise your
    chances of needing to show an error message in the first place"
    (https://design-system.service.gov.uk/patterns/validation/). For us: let rules and past decisions settle what
    they can before a human sees a card.

Not verified at source: Flatfile and OneSchema end-user error UI (their docs pages fetched describe only developer
validators), Great Expectations Data Docs layout, Shopify Polaris bulk actions (page redirected), Gmail/Stripe review
queues, Material 3 beyond the one-line "say what happened and how to fix". Do not cite these as evidence.

## 2. Redesign of two cards

Rules for every card: line 1 = cause with count, line 2 = the plain consequence, line 3 = choices. Provenance (file,
sheet, range, filter recipe, "copy values") never appears collapsed. Status pill is replaced by position (blocking
group first) and one small icon+label (principle 12).

### Card A: "ID too long"

Before (current, about 14 lines): title, "Needs you" pill, "Source key is 23 characters, limit is 20", file name,
sheet and rows, `Investor!A9:L10`, "copy", lightbulb Excel filter recipe, "copy values", "the 2 rows" table, side
column "Drop 'HS-REF-' from every long ID like this one".

After (collapsed, 3 lines):

```
[!] 2 IDs are longer than Ledgerline allows (23 characters, limit 20)        Blocks upload
    HS-REF-0000000000000042  ->  0000000000000042     (drop the "HS-REF-" prefix)
    [Shorten both - recommended]  [Edit value]  [Leave out]  [Ask client]        Why   Show rows
```

Option labels and what each does:

| Label | Effect | Notes |
|---|---|---|
| Shorten both (recommended) | Apply the prefix rule to the 2 rows in this group only | Shows the before to after example inline. Disabled with reason if the shortened IDs collide with existing ones. |
| Edit value | Opens one input per row, prefilled with the proposed ID | Confirms with "Use this ID". |
| Leave out | Rows are excluded from the upload file, reason recorded | Result line: "2 left out". |
| Ask client | Creates a question "Which ID should these two investors use?" and parks the rows | Group moves to "Waiting on client". |

Behind "Why" (one short paragraph, shown on request only): "Ledgerline keys are at most 20 characters. Every other ID
in this file already follows the same pattern after the prefix, so dropping it keeps them unique. Nothing is
invented; the digits are copied from the file."

Behind "Show rows": a 2-row table with columns Row, ID as in the file, Proposed ID, Investor, Entity, with a one-line
footer "Investor sheet, rows 9-10" plus a small "Copy cell range" link. No Excel recipe unless the user clicks
"Open in Excel steps" inside this panel (principle 3).

### Card B: "Same key under another name"

Before (about 12 lines): the title is the investor name, a "Needs you" pill, "Same key on file under another name",
file, sheet, row, range, "copy", lightbulb recipe, "copy values", monospace row dump, and a side sentence "Leave it
out. It is INV-0007 ... already in Ledgerline".

After (collapsed, 3 lines):

```
[!] INV-0007 is already in Ledgerline under a different name          Worth checking
    File: "Individual Holder One and Two"   Ledgerline: "Individual Holder One & Two"
    [Same investor, leave out - recommended]  [Different investor, keep]  [Edit key]  [Ask client]   Why  Show rows
```

| Label | Effect | Notes |
|---|---|---|
| Same investor, leave out (recommended) | Row is not written; link to the existing record is remembered for this sponsor | Offered as recommended only when names differ by punctuation/spacing, otherwise no default is marked. |
| Different investor, keep | Row is written as new; asks for a new key in the same card | Needed because the key must be unique. |
| Edit key | One input | Validates uniqueness in place. |
| Ask client | Question "Is X the same investor as Y?" | Parks the row. |

Behind "Why": "The key INV-0007 is already used in Ledgerline for 'Individual Holder One & Two'. The names differ only
in 'and' versus '&'. Writing the row would overwrite or duplicate that investor." Behind "Show rows": side-by-side
file row and Ledgerline record, differences highlighted, with sheet/row as a small caption.

The reverse case ("Same name under another key") uses the same card with labels "Same investor, leave out" /
"Different investor, keep" / "Use the Ledgerline key" / "Ask client".

### Replacing "Accept all suggestions"

Per group header (above the cards in that group), not in the Worker panel:

```
Blocks upload - 2 groups, 5 rows
[Accept recommended for this group (2)]   [Review one by one]
```

- "Accept recommended for this group (N)" applies only to rows in that group that have a recommended option, shows
  the count in the label, runs the same preview as a single card, and ends with an "Undo" toast for the batch.
- Rows with no recommendation are excluded from the count and remain open.
- "Review one by one" steps through cards with Next/Previous and one key per option (Linear pattern, principle 7).
- There is no global accept. The panel button becomes "Next group needing you" and, when empty, "Nothing needs you.
  Write the upload file" (principle 15). Writing the upload keeps its existing human approval gate.

Line budget: collapsed card 3 lines; "Why" at most 3 sentences; group header 2 lines; the page for 5 issues fits above
the fold at 1440 px.

## 3. Risks and what to test with a first-time user

Risks
- A recommended default can become "accept all" in disguise. Recommendations must be visibly different per card and
  sometimes absent (principle 7), or users learn to ignore the choice.
- "Leave out" is silent data loss from the user's point of view. The result line must say who is affected and where
  they can be restored; every choice is audited with its reason.
- "Ask client" can park rows indefinitely. Needs a visible "Waiting on client" count and a way to resume.
- Hiding the file/sheet/range reduces trust for expert users. Keep one-click access and a persistent footer line
  inside "Show rows".
- Plain language for finance terms (key, entity, fund group) may still be opaque; label the vocabulary by testing,
  not by assumption.
- Per-group bulk apply can hide one bad row inside a good group. Preview must show 2-3 example rows and a count, and
  the "Review one by one" path must be one click away.
- Two levels only (principle 4): resist adding a third panel for "details of details".

Tests (5 users who have never seen the tool, no coaching)
1. Read-aloud: ask the user to read one collapsed card aloud and say in their own words what is wrong and what the
   buttons will do. Pass: they can paraphrase both without opening "Why".
2. First-click: "Fix the long IDs." Record whether they pick the recommended option, another option, or open "Why"
   first, and what they expected would happen.
3. Prediction: before pressing "Accept recommended for this group (2)", ask what will change. Compare with the
   preview.
4. Recovery: ask them to undo it and find the rows afterwards.
5. Counter-test: ask them to leave out one row and ask the client about the other. Pass: they find both within one
   screen without help.
6. Time to a clean file and number of clicks, compared to today's "Accept all suggestions".
7. Complete state: do they know what to do next when nothing needs them?
