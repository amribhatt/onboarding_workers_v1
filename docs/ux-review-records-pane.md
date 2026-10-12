# UX review: the records pane (right-side chat, Review on an Issues or Reconcile group)

Question asked: when a person presses Review, does the pane open the actual records for the affected rows, is it
friendly to a first-time user, and does it avoid scrolling (inside the pane, to find the records, to find Apply)?

Method. Real console (`onboarding-worker demo`, offline demo model, state dir outside the repo), own headless Chromium
driven by Node Playwright scripts (not committed). Jobs: Investors with the 2-row group (`Sponsor_A_Common_Investors`
sample, "IDs longer than Ledgerline allows"), Investors with a 50-row group (synthetic file, same cause), GL Fund (4-line
group "Journal does not balance"), and a 100,000-row group (see Limits). Viewports 1280x720, 1366x768, 1536x864,
1920x1080, 390x844 at 100% and 125% (125% = CSS viewport divided by 1.25, device scale 1.25). Review, chip, Send were
driven exactly as a person does. Screenshots: `.logs/ux/before/` and `.logs/ux/after/` (gitignored). No answer key was
read.

Limits, stated plainly.
- **100,000 rows.** An xlsx with 100,020 rows was accepted by the upload, but the Start act (the demo engine) had not
  returned after 20 minutes and I stopped it (not investigated; it is not the pane). The 100,000-row case is therefore the
  real 50-row job with the rows endpoint answered by a Playwright route that serves 100,000 synthetic rows and exact
  total (so the pager, jump box and fitted page size are real; the preview's count is the real job's 50). Server-side
  slicing of a million-row group is covered by `test_the_first_page_of_a_million_row_group_is_a_slice` (under 100 ms).
- **Read-aloud test.** I cannot run people. Below I list the exact text visible at first paint and rate each line
  against the heuristics; that is a simulation, not user evidence. The research doc's five-user protocol still stands.
- The chat history above the card in the "before" runs is longer than a new user's (my scripts sent several previews);
  this only moves the scroll position, not the visible-row counts at the pane's bottom.

## Sources (fetched 2026-10-12 unless marked)

- NN/g, 10 usability heuristics: https://www.nngroup.com/articles/ten-usability-heuristics/ (names and definitions of
  #1 visibility of system status, #6 recognition rather than recall, #8 minimalist design).
- NN/g, scrolling and scrollbars: https://www.nngroup.com/articles/scrolling-and-scrollbars/ ("Display all important
  information above the fold"; users allocate about 20% of attention below it; custom in-page scrollers are often
  missed). The page does not address nested scrolling directly; I apply the above-the-fold and "missed scroller" points.
- NN/g, progressive disclosure: https://www.nngroup.com/articles/progressive-disclosure/ (core options first, rest on
  request, at most two levels).
- WCAG 2.2 SC 2.5.8 Target Size (Minimum): https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html (24 by 24
  CSS px; exceptions: spacing, inline links, equivalent control, user-agent default).
- WCAG 2.2 SC 1.4.10 Reflow: https://www.w3.org/WAI/WCAG22/Understanding/reflow.html (320 CSS px; data tables exempt but
  surrounding pagination and controls are not).
- WCAG 2.2 SC 2.4.3 Focus Order and 2.1.1 Keyboard (understanding pages, same host).
- Carbon data table usage: https://carbondesignsystem.com/components/data-table/usage/ (five row heights, 24 to 64 px;
  pagination at the bottom of the table with items per page and jump to page; long text truncated with the full text in
  a tooltip).
- Primer DataTable: https://primer.style/product/components/data-table/ (condensed density "maximizes data visibility in
  a small area"; the fetched part did not cover pagination, truncation or sticky headers).
- GOV.UK error summary / error message and NN/g progressive disclosure are also cited by
  `docs/research-data-quality-ux.md`, fetched 2026-10-11; not re-fetched here.
- **UNVERIFIED, not cited as evidence:** "F-pattern" reading claims; WCAG 2.4.7 focus visible and 1.4.3 contrast were not
  fetched (contrast was not measured).

## What the person saw at first paint (before), 1280 x 720, 100%

The pane was 400 x 518 px and scrolled to the bottom of the message list, so the card's title, cause line and first
record were above the visible area. Text actually visible, in order:

| Visible text (before) | Read-aloud rating |
|---|---|
| `10  HS-REF-0000000000000077  Cedar Hollow Partners  0000000000000077` (tail of a table; the first record, the headers and the card title are scrolled away) | Fails "what is wrong": no cause, no count, no header. NN/g #1, #6; above the fold. |
| `rows 1-2 of 2` `‹ Prev` `Next ›` `go to page` `Go` | Pager for two rows takes a line; fine but noise at this size. |
| `Suggestions: click one to put it in the box, edit it (a note after the colon is kept), then Send. Nothing changes until you press Apply.` | Three clauses of procedure before the first choice; the person must hold four steps in mind. NN/g #8, #6. |
| `Update to the shortened IDs SUGGESTED` `Leave these out` `Park this until the client answers` | Good: outcome-labelled, one marked. |
| `Deciding as [your name]` | Needed once; sits between the choices and the links. |
| `Why` `Open the source` `Where it is` `Next group needing you` | Plain links, good; 20 px tall (fails 2.5.8). |

Time to understand, simulated: the person cannot say what is wrong or which rows from this screen without scrolling up
inside a 374 px list that is itself inside a 518 px pane. Not met within 5 seconds.

After the change (same viewport, 50-row group):

| Visible text (after) | Rating |
|---|---|
| `IDs longer than Ledgerline allows · 50 rows [Blocks upload]` | What is wrong, how many, and that it blocks. NN/g #1. |
| `HS-REF- + 16 characters · Ledgerline would reject them as they are.` | One line of cause and consequence in plain words. |
| `ROW  RECORD  WAS  BECOMES` then 7 rows, e.g. `12  State Investment Council long 0  HS-REF-0…0000000  0000000000000000` | Which rows and what will change, side by side, no horizontal scroll. The tail digits that tell IDs apart are kept. |
| `rows 1-7 of 50  ‹ Prev  Next ›  page [ ] Go  Copy this page` | Exact total, jump box, page copy as a link. |
| `Pick one, then press Send. Nothing changes until you press Apply.` + three chips | One short instruction; the choices. |
| `Why  Open the source  Where it is  Next group  Back to chat` | Explanation behind a link (two levels, NN/g). |

What to press is still three steps (chip, Send, Apply); see "What remains".

## Findings

Severity: High = the user cannot do the task without scrolling or cannot see the thing they must act on. Medium =
slows or excludes some users. Low = polish.

| # | Sev | Finding (evidence) | Screenshot | Criterion | Fix | Status |
|---|---|---|---|---|---|---|
| 1 | High | The review card is the last item of the scrolling message list and the list is scrolled to the bottom, so title, cause line and first records are cut off. Rows fully visible at first paint: 0 to 5, median 1, of 40 combinations (1 at 1280x720 100%, 0 at 125%). | `before/medium_1280x720_z100_review.png`, `before/small_1280x720_z125_review.png` | NN/g above the fold, #1, #6 | The pane is the group: title, cause line, records, pager, action bar; the message list is replaced while a review is open ("Back to chat" returns it). | Fixed |
| 2 | High | After a chip and Send, the preview with the exact count and Apply is a message in the history above the card; the next render snaps the list to the bottom (card), so Apply and the count were not visible in 40 of 40 combinations. | `before/medium_1280x720_z100_preview.png`, `before/gl_1280x720_z125_preview.png` | #1 visibility, #3 control; above the fold | The worker's answer to what was just sent (preview with its one Apply, else the reply) is the first thing in a bar at the bottom of the pane; the chips return under "Other choices". | Fixed |
| 3 | High | Fixed 50 rows a page, and every cell wraps: a 23-character ID wraps to three lines at 400 px, so one record is about 60 px high and one fits per screen. | `before/small_1280x720_z100_review.png` | Carbon/Primer density; NN/g #8 | One line a row (30 px), rows per page computed from the pane height (resize and zoom refit), long values keep start and end with the whole value as tooltip, "Copy this page" link. | Fixed |
| 4 | High | Nested scrolling: the pane's message list plus a horizontal scroller around the table (idle at 100%, scrolling at 390 px 125%: 6 px), plus the source viewer in its own 260 px scroller. Reconcile: the six-column table scrolled sideways by 142 px, 0 to 6 of 11 records visible. | `before/medium_390x844_z125_review.png`, `before/recon_1280x720_z100.png` | NN/g scrolling (missed scrollers), 1.4.10 | No scroll container in the review pane (0 measured in all 40 combinations); the table is `table-layout:fixed` in a clipped region. | Fixed (source viewer: see 14) |
| 5 | Medium | Keyboard: Enter on Review re-draws the page, focus falls to `<body>`; the first stop in the pane is the 15th Tab; Esc did nothing while focus was in the name or message box; on close focus stayed on `<body>`. | `before/keyboard.json` | 2.4.3, 2.1.1 | Opening puts focus on the pane title, the first Tab goes into the pane; Esc closes from any box; focus returns to the Review button. Focus is kept across re-draws for the title, jump box and name box. | Fixed |
| 6 | Medium | Column labels and order: `Row, Value, Name, Proposed`: "Value" and "Proposed" say nothing about change; the name was in the middle. | `before/small_1280x720_z100_review.png` | #2 match with the real world | `Row, Record, Was, Becomes` (Record hidden under 520 px so Was and Becomes stay readable). | Fixed |
| 7 | Medium | Explanation first: a three-line consequence and a three-clause instruction above the options; the cause sentence and the records were below it. | `before/gl_1280x720_z100_review.png` | NN/g progressive disclosure, #8 | One line of cause under the title; the full consequence, "Where it is" and the source are behind links that take the place of the rows ("Show rows" returns). | Fixed |
| 8 | Medium | Target size: Why 27x20, Open the source 97x20, Where it is 63x20, Next group needing you 144x20, Go 17x20 (five targets under 24 px, not inline links). | `before/results-review-small.json` (under24Names) | 2.5.8 | Links 24 px high and 24 px wide minimum; 0 targets under 24 px measured after. | Fixed |
| 9 | Medium | Reconcile review: six columns, report box with two buttons and a paragraph, "Decide these on the Issues tab" as a button; with no client report the Client and Difference columns are empty and take the width. | `before/recon_1280x720_z100.png` | 1.4.10 (pagination/controls reflow), #8 | Same pane; Client and Difference columns only when there is a report; the report line and its two actions are links. | Fixed |
| 10 | Low | Small text: table headers 10.5 px, tags and eyebrow 10.5 to 11.5 px, "Press / to type here" 11.5 px. | `before/results-review-small.json` (fontSizes) | Carbon/Primer density (no hard minimum cited) | Headers 12 px, hints 12 px, eyebrow and tag 11 px. | Partly (11 px remains for the two labels) |
| 11 | Low | Three steps to a decision: chip fills the box, Send, then Apply. | `after/medium_1280x720_z100_preview.png` | #7 efficiency | Not changed: a chip only fills the box by design (the person may add a note); proposal below. | Open |
| 12 | Low | At 125% on a 720 p screen (1024 x 576 CSS px) a 50-row group shows 6 rows and a 390 px phone at 125% shows 3. Chrome that is always needed (title, pager, bar, input) takes about 380 px. | `after/medium_390x844_z125_preview.png` | Carbon density | Header goes to one line and the one-line cause and hint are hidden under 640 px height; the rest is structural. | Mitigated |
| 13 | Low | Start did not finish for a 100,020-row file in 20 minutes (demo engine), unrelated to the pane. | n/a | n/a | Not investigated. | Open |
| 14 | Low | The source viewer has up to 12 columns and still scrolls sideways inside the region; its page size is fitted. | `after/` (open the source) | 1.4.10 data-table exemption | Left: a spreadsheet viewer needs the columns. | Open |

## Measurements (before and after)

All 40 combinations per run (4 jobs x 5 viewports x 2 zooms). "Rows visible" = records fully inside the pane's visible
area with no scrolling at first paint, for the 2-row, 50-row, 100,000-row and 4-line GL groups. "Apply + count" = after a
chip and Send, both the exact-count line and the single Apply button fully visible without scrolling.

| Viewport | Zoom | Rows visible 2 / 50 / 100,000 / GL-4 (before) | after | Apply + count visible, of 4 jobs (before / after) | Pager + "rows a-b of N" + jump visible, of 4 (before / after) | Scroll containers in pane (before / after) |
|---|---|---|---|---|---|---|
| 1280 x 720 | 100% | 1 / 1 / 1 / 1 | 2 / 7 / 6 / 4 | 0 / 4 | 4 / 4 | 2 (1 scrolling) / 0 |
| 1280 x 720 | 125% | 0 / 0 / 0 / 0 | 2 / 6 / 4 / 4 | 0 / 4 | 3 / 4 | 2 (1 scrolling) / 0 |
| 1366 x 768 | 100% | 2 / 2 / 1 / 1 | 2 / 9 / 9 / 4 | 0 / 4 | 4 / 4 | 2 (1 scrolling) / 0 |
| 1366 x 768 | 125% | 0 / 0 / 0 / 0 | 2 / 7 / 5 / 4 | 0 / 4 | 4 / 4 | 2 (1 scrolling) / 0 |
| 1536 x 864 | 100% | 2 / 3 / 2 / 2 | 2 / 12 / 12 / 4 | 0 / 4 | 4 / 4 | 2 (1 scrolling) / 0 |
| 1536 x 864 | 125% | 1 / 1 / 0 / 1 | 2 / 7 / 5 / 4 | 0 / 4 | 4 / 4 | 2 (1 scrolling) / 0 |
| 1920 x 1080 | 100% | 2 / 3 / 3 / 3 | 2 / 15 / 15 / 4 | 0 / 4 | 4 / 4 | 2 (1 scrolling) / 0 |
| 1920 x 1080 | 125% | 2 / 3 / 2 / 2 | 2 / 12 / 12 / 4 | 0 / 4 | 4 / 4 | 2 (1 scrolling) / 0 |
| 390 x 844 | 100% | 2 / 5 / 5 / 4 | 2 / 10 / 10 / 4 | 0 / 4 | 4 / 4 | 2 (1 scrolling) / 0 |
| 390 x 844 | 125% | 1 / 1 / 1 / 1 | 2 / 3 / 3 / 4 | 0 / 4 | 4 / 4 | 2 (2 scrolling) / 0 |

- (b) Source row, value and proposed result without horizontal scroll: before, the three columns were all visible only
  when the table was scrolled into view (header row fully visible in 5 of 40; the table overflowed sideways by 6 px at
  390 x 844 125%). After: 0 px overflow in 40 of 40; at 390 px the Record column is hidden so Row, Was, Becomes remain.
- (d) Pager and jump box: after, "rows a-b of N", Prev, Next and the jump box are in the pane in 40 of 40 (they sit
  directly under the table and do not scroll away). Before they were in view in 39 of 40 (they are at the end of the
  card, which is where the list is scrolled to) and off-screen in one (100,000 rows at 1280 x 720 125%).
- (e) Nested scroll containers: before 2 (the message list and the table wrapper; both scrolling at 390 px 125%), after 0.
  Exception: the source viewer's horizontal scroller (finding 14).
- (f) Click on Review to records painted: median 114 ms (max 209) before, median 81 ms (max 143) after, against a local
  server. Both are well under the 1 s a person notices; the server's page slice is the cost (the 1,000,000-row test).
- (g) Keyboard (1280 x 720): before, Enter on Review left focus on `<body>`; the 15th Tab was the first stop in the pane;
  Esc did not close the pane from the name box; after Esc focus was on `<body>`. After, focus is on the pane title
  after Enter, the first Tab is in the pane (jump box, Go, Copy, chips, name, links), Esc closes from anywhere in it and
  focus is on the Review button. Files: `.logs/ux/before/keyboard.json`, `.logs/ux/after/keyboard.json`.
- (h) Text and targets: smallest text 10.5 px before (table headers), 11 px after (eyebrow and the "suggested" tag; table
  headers and hints 12 px). Targets under 24 x 24 CSS px in the pane: 5 before, 0 after.

Reconcile pane (GL Fund, 11 records, no client report): before, 0 to 6 of 11 records visible and 142 to 152 px of
sideways scroll inside the pane at every viewport; after 8 / 8 / 11 / 11 visible (1280 x 720 at 100%, 1280 x 720 at
125%, 390 x 844, 1920 x 1080), 0 px overflow, pager in the pane. Files: `.logs/ux/{before,after}/recon_*.png`.

## What changed in the code

`src/worker/static/console.js` (`PANE`: fitted row count, middle truncation, pager, `closeChat` with focus return; the
chat renders a review as a pane when a module hands it `{since, html}`), `console.html` (the `.rv*` rules; pane width
`clamp(440px, 34vw, 620px)` docked or floating; shorter at small heights), `issues.js` and `recon.js` (the review view;
the old records card and the source bubble are gone). The data, the preview, Apply, Undo and the audit are unchanged:
the page still computes nothing and the worker still answers every sentence. No button was added; Why, Show rows, Open
the source, Where it is, Copy this page, Other choices, Next group and Back to chat are plain text links. Tests: the
three Node stub-DOM suites updated and two added in `tests/test_console_shell_node.py`; ruff clean.

## What remains

1. **Chip, Send, Apply is three steps.** Options that need no note could send on click and go straight to the preview
   (Apply stays the one decision). Needs a product decision because a chip is documented as "fills the box".
2. **Source viewer** still scrolls sideways; consider showing only the columns named by the cause plus the row.
3. **Phone, 125%**: three rows a page for big groups; the "Copy this page" link wraps. Acceptable, but a bottom-sheet
   instead of full-screen would not help; the chrome is the limit.
4. **Eyebrow and tag text is 11 px**; raising it needs a design-token change in the shell.
5. **Not measured:** colour contrast (1.4.3), visible focus ring (2.4.7), screen-reader output of the pane. The pane
   title takes focus and the preview bar is `role="status"`; neither was heard with a screen reader.
6. **A 100,000-row upload** did not finish Start in 20 minutes on the demo engine (finding 13).
7. **Real read-aloud test** with five first-time users per `docs/research-data-quality-ux.md` section 3.
