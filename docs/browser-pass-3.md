# Browser pass 3 (2026-10-12)

Real Chromium 153 (Playwright, headless, Python), the console at `/console`, the demo model (`WORKER_MODEL_PROVIDER=demo`),
fictitious data only. The scripts were not committed (they lived in `.logs/`, which is ignored).

**Setup.** PostgreSQL 17 from the compose project (`onboarding-worker-postgres-1`), a **separate database** `ow_pass3` made
by the owner role in that cluster and migrated to revision 0003 (`onboarding-worker db migrate`). The app ran in a container
from `onboarding-worker:local` on the compose network with this worktree's `src/`, `skills/` and `templates/` copied in,
reaching the database through `DATABASE_URL_FILE` (a rewritten copy of the mounted secret with only the database name
changed; no value was printed). Port 18765 on 127.0.0.1. The files backend (the reshape and the empty-upload cases) ran on
the host from the same source with a short absolute state folder. Every server, container and browser started here was
stopped at the end.

## Matrix: record type x viewport x backend

Each cell is one complete job, start to approval, on the sample file.

Steps in each job: the sample button (no file chooser), Start, the question about records already in Ledgerline, the plan,
the ID decision, Convert, the Issues review with one Undo, Reconcile for GL, Output, and the approval. Sponsor A ran at
1280x720 with the mouse; Sponsor C ran at 390x844 with touch input (every action a tap).

| Type | Read, written, left out | File rows | 1280x720, PostgreSQL | 390x844 touch, PostgreSQL | Files backend |
|---|---|---|---|---|---|
| Investors | 15, 14, 1 | 14 | pass | pass | pass (reshaped pivot: 43, 43, 0; 43 rows) |
| Commitments | 8, 7, 1 | 7 | pass | pass | not run in this pass |
| Investments | 8, 8, 0 | 8 | pass | pass | not run in this pass |
| Vendors | 8, 8, 0 | 8 | pass | pass | not run in this pass |
| Affiliates | 8, 6, 2 | 6 | pass | pass | not run in this pass |
| GL Fund | 18 lines | 4 (net 0.00) | pass | pass | not run in this pass |
| GL LP | 5 lines | 6 (net 0.00) | pass | pass | not run in this pass |

- The GL types need the five static types approved first, so they ran last on the same sponsor.
- Reconcile on GL Fund (1280): the tab lists 9 to 11 checks. Review, the choice "Leave these out of the check", the note
  after the colon, Send, a preview with exact figures and one Apply, Apply, Undo; the same by touch on the phone. Each
  preview took 3 to 6 s (30 s once, with the test suite running beside it): each preview recomputes the GL.
- `onboarding-worker db verify --anchor-dir /anchors`: **OK (959 entries in 31 streams)** after all runs, with a fresh
  anchor written first (a first verify said `anchor_stale`, which is only the age of the last anchor).

## Scope items

| # | Item | Result | Evidence |
|---|---|---|---|
| 1 | Seven types end to end on PostgreSQL, then `db verify` | pass | the matrix above |
| 2a | 1280x720 and 390x844 | pass | the matrix; no horizontal page scroll on the job tabs at either size |
| 2b | 125% zoom, launcher not over content | pass after one fix | CSS viewport 1024x576 and 312x675 at device scale 1.25 (125% of 1280x720 and of a 390 phone). With every tab scrolled to its end, no visible control, link, cell or card overlapped the closed launcher on Issues, Reconcile, Output and Activity (closed `<details>` excluded). The Reconcile tab was 7 px wider than a 312 px screen: fixed (defect 4). Layout was measured, whole flows were not run at 125%. |
| 2c | Touch: Review, chips, Apply, Prev/Next, jump box, pager | pass | the whole phone matrix by tap. The pager on a 300-row group (the rows endpoint answered by a Playwright route, as in `docs/ux-review-records-pane.md`): Next, Next, Prev, jump to page 4 gave "rows 34-44 of 300", then Copy this page. Controls in the pane are 24 to 38 px high; the smallest are the text links and Go at 24x24. |
| 3 | An unknown source format through the Reshape card | pass | `investors_pivot.xlsx` on the files backend with the real sandbox image `ow-sandbox:1`; the program came from a stub OpenAI-compatible endpoint (no live model). The card said "I cannot place investors_pivot.xlsx as a table" and showed 171 cells, 104 used, 67 set aside, 43 rows, 52 subtotals proven, one WARN to tick (rounded subtotals; Approve stayed disabled until ticked), the before and after tables, a reason box, the scope "this file only" and the name typed into the card's own "Deciding as" box. After Approve: "Approved by Priya Shah", the job read 43 rows, was approved, and the downloaded file has 43 rows. The proposal survives a reload. |
| 4a | Empty upload refused | pass | A job with every row left out: Output says every row is left out, the Approve button is disabled, and a direct approve call answers 409 "Every one of the 15 rows is left out ... Nothing is written ..." (audited as `approve.refused_empty`). Real file chooser cases: a zero-byte file ("the file is empty"), a non-workbook ("cannot be read as an Excel workbook"), a blank workbook ("has no header row ..."). A header-only workbook was accepted and read 0 rows (defect 7). |
| 4b | ID decision gate | pass | Approve before the decision: "The ID decision for Sponsor A Investors has not been made ...", nothing written; Save without a name or a reason is refused with the reason; after the decision the approval goes through. |
| 4c | Approval blocked while rows are open or on hold | pass | Open rows: Output says "3 still need you" and has no Approve button. Only a hold left: "2 on hold. Decide or release them ..." with no button, and a direct approve call answers 409. |
| 4d | Hold note audited | pass | Park without a note: "Putting rows on hold needs a note ... Nothing changed." With a note: the rows are on hold, and the job's Activity tab lists the time, decision, cause, rows, "Priya Shah" and the note; Release asks for a note too. |
| 5a | Keyboard only | pass, one gap | below |
| 5b | Contrast, computed | pass after one fix | below |
| 5c | Screen reader | **not verified** | no screen reader was run; the accessibility tree of the pane is below |

### Keyboard-only pass of the records pane (1280x720, Issues tab, chat closed)

- Tab from the page: 12 navigation buttons, the page tabs, the status chips, the search box, then the first group's
  source link and **Review**: 25 stops. There is no skip link (open).
- Enter on Review: focus moves to the pane heading (`#rvh`, `tabindex=-1`); the pane is the chat dialog in review mode.
- Tab order inside the pane: page jump box, Go, Copy this page, the choices (suggested first), Why, Open the source, Where
  it is, Next group, Back to chat, then the message box. Prev and Next are disabled with one page, so skipped.
- Escape: the pane closes and focus returns to the same group's Review button. Repeated once with the same result.

### Contrast (WCAG 2.1 AA, 4.5:1 for text under 24 px, computed from the rendered colours)

Light: ink on page 16.3, sub on page 6.9, link on card 10.1, primary button 14.8, green button 5.0, pills 4.6 to 5.5.
Failing before the fix: the grey `faint` text (`#6b7280`) was 4.43 on the page, 4.24 on the navigation and 3.98 on a
selected row (small labels, group headings, eyebrows), and the warning pill 4.45. After: `faint` `#5f6773` (4.53 to 5.7 on
the four surfaces) and warning `#8f5206` (5.3). Dark: only `faint` on the muted pill background was 4.28; `#99a4b9` is
4.96 or more. In the browser, light and dark, no text on the page or in the pane is under AA except the disabled Prev
button (3.4; a disabled control is exempt). `tests/test_console_layout_css.py` checks the token pairs.

### Accessibility tree of the records pane (Review on a held group, Playwright `aria_snapshot`)

```
- dialog "Chat with the worker":
  - button "Pin"
  - button "Close the chat"
  - region "Review":
    - text: <cause> - 2 rows, <pattern>, <consequence>
    - table: columnheader Row, Record, Was, Becomes; one row per record
    - text: rows 1-2 of 2
    - button "Prev" [disabled]   button "Next" [disabled]
    - spinbutton "Go to page, of 1"   button "Go"   button "Copy this page"
    - text: On hold, Park this until the client answers, by <name> <time>, reason: <note>
    - button "Release"   button "Why"   button "Open the source"   button "Where it is"   button "Back to chat"
  - textbox "Message the worker"   button "Send"
```

Before the fixes the stage list read `Read, Map, Convert, Check, 5 Write` (state by colour and tick only) and every row's
button was named just "Review". Both are fixed (defect 6). Whether a screen reader reads this well was not tested.

## Defects found and fixed (each with a test)

1. **Plan tab, ID card: no way to give a name.** On a job that had run, Save said "type your name in the Who is deciding
   box" and that tab has no such box (it lives in the Questions card, shown only while questions are open). The card now
   carries a "Deciding as" box while no name is known (`idgen.js`). Test: `test_the_id_card_asks_for_the_name_...`.
2. **Output tab, Approve: no way to give a name.** Where approval needs a named person the call is refused ("name
   yourself") and the tab had no box. Added "Approving as" beside the notes box (`console.js`), kept through a redraw.
   Test: `test_the_output_tab_asks_for_the_name_when_none_is_known`.
3. **Copy this page always said "Copied".** A clipboard refusal (permission denied, or no clipboard on a plain-HTTP
   origin) was an unhandled rejection after a false toast. Now "Copied" only on success, else "Could not copy: ...".
   Test: `test_copy_says_copied_only_when_the_browser_copied`.
4. **Reconcile tab 7 px too wide at 312 px** (a 390 phone at 125%): a card laid out as a grid sized its single column to
   its widest child, so the page scrolled sideways. `.card{grid-template-columns:minmax(0,1fr)}`. Test: `test_console_layout_css.py`.
5. **Contrast below AA** (numbers above). Tokens changed in `console.html`; the pairs are tested.
6. **Screen reader: stage states, and which group a Review button opens.** The progress list now says done, in progress,
   stopped or not started (visually hidden) and marks the current step; each Review button is named "Review: <cause>".
   Test: `test_a_screen_reader_hears_the_stage_states_...`.
7. **A header-only workbook was announced as "ready for your approval"** (0 rows read). The toast and the worker's message
   now say no rows were read and there is nothing to approve (`console.py`, `console_screens.py`). Test in `test_console_empty_upload.py`.
8. **The ID-gate refusal quoted an agent tool** ("set_id_scheme"). On the console it now says "ID generation logic on the
   Plan tab". `idgen.py` is untouched; the wording is replaced in `Console.approve`. Test in `test_id_decision_gate.py`.

## Found and not fixed

- **The console cannot be used where an approver credential is required** (the compose `worker` service sets
  `WORKER_REQUIRE_TOKEN=1`). Verified on a host server started with `WORKER_APPROVER_TOKEN`: every console action answers
  401 "an approver token is required", and `console.js` has no field and never sends the `X-Approver-Token` header (only the
  older agent page `index.html` does). Left for the owner, because it decides where a credential is typed and kept in the
  browser. An edit that made the console send it was started here and stopped by the permission check; it was not retried.
- **Console state is one document on the server.** The current sponsor, job and tab are shared by every browser: two tabs
  steer each other (my desktop and phone runs, started together, did). Demo-grade and known; it makes parallel manual
  testing invalid.
- **Reconcile preview latency**: 3 to 6 s per sentence (30 s once, under load); each preview recomputes the
  reconciliation. Not investigated.
- **No skip link**: 25 Tab stops before the first page control.
- **Every Apply logs one to four 409s** in the browser console (the row fetch of a group that has just been decided).
  Noise; known from pass 2.
- **On a phone the chat is a full-screen dialog and stays open after Apply**; the page behind is not reachable until
  "Close the chat". By design (a sheet over a narrow screen), but it costs a tap per group.
- **Approving with no name on a server with no approver credential records "local reviewer"** (the documented demo mode).
  The Sponsor A Investors job was approved that way; the others carried a name.
- **The shared `ow_app` role password drifted while this pass ran**: new connections to the compose database with the
  secrets file's URL were refused ("password authentication failed for user ow_app") after the first migrate. Another
  process re-ran a migration with a different app password, or changed the role; not investigated. This pass used the
  owner URL for `db anchor` and `db verify`. If the compose `worker` or `verifier` restarts they will fail until
  `db migrate` is run again with the current secrets.
- **Two Review buttons with the same cause label** are named alike; the pattern is not in the name.

## Not verified

- Screen-reader output (NVDA, VoiceOver, TalkBack): no screen reader was run; the accessibility tree is above.
- A real phone or tablet, real touch hardware, Safari and Firefox: Chromium emulation only.
- Real browser zoom (Ctrl and plus): the 125% cells use the equivalent CSS viewport and a 1.25 device scale.
- Whole flows at 125% (layout only), and the six other record types on the files backend in this pass.
- The compose `worker` service as deployed (approver credential, read-only filesystem, Neo4j projector).
- Fixes 7 and 8 live (Python): the container was not restarted, to avoid re-running `db migrate` against the shared role.
  They are covered by unit tests.
- A live model: the reshape program came from a stub endpoint that returns the repository's pivot program.
- A field recipe of the person's own (a column of their own with a reason) in this pass: verified in M24c; the Questions
  card of the pivot job offered it ("a column of your own for Investor_Name").
