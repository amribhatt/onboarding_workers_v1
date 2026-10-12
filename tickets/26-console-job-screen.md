# 26 Console job screen: side chat, timeline, structured main area

Why: the agreed experience is a coworker like a coding assistant, not a form. The mockup v25
(`docs/mockup/onboarding-console.html`, https://claude.ai/artifact/RgJgknkkKFtwpU9TYAPGGY) is the UI target and the
source for this build. Design: `docs/design-onboarding-assistant.md` section 2 (job screen) and section 10 (scenarios
A, B, D, J, K).

Depends on: 11 (web console), 15, 20.

Do:
- Rebuild the job screen of `worker.web` / `static/` to match the mockup:
  - **worker chat docked right**: collapsible to an icon, a bottom sheet on narrow screens, `/` focuses its box;
  - **main area**: title + "Show working", the **timeline** Read → Map → Convert → Check → Write (done / waiting on you /
    stopped, with one short fact per stage), then the work: plan, issues, output. Finished parts fold to one line.
  - **No prose in the main area**: tables, counts, chips, folds. Explanations live in the chat or behind "why".
- **A job starts in the chat**: a sentence and a file. The worker infers record type, template and route, and asks one
  question at a time with buttons only for what it cannot tell (entities covered, records already in Ledgerline). The main
  area shows "what I understood so far" and can be edited there. Clicking and chatting change the same state.
- **Records already in Ledgerline** question asked in the chat before the plan, once per sponsor and record type (yes →
  upload the export, no, not sure → upload or go ahead marked "not checked"); the plan waits for the answer.
- **Runs report back**: live progress on the timeline; a notification when Check is done, with a link to the job.
- Approve is only a button in the main area; it is refused before a successful run, after a failed run, or while
  anything needs the person.
- Keep clicks at four: Start → Looks right → Accept all → Approve.

Tests: scenarios A1–A5, B1–B5, D1–D5, J1–J4 and K1–K4 of section 10 as end-to-end tests with the scripted model
(no live model), plus a browser check of the layout at desktop and phone width.

Acceptance: green tests (`> .logs/t26.log`), ruff clean, STATUS.md updated, one commit.
