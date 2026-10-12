# Onboarding assistant: design direction

Agreed in conversation on 2026-10-10. This is the source of truth for the product direction; tickets 20–24 implement
it. Mockup: https://claude.ai/artifact/RgJgknkkKFtwpU9TYAPGGY (v26). Section 10 lists the acceptance scenarios to test
the build against; tickets 25–29 build what tickets 13–24 do not cover (data-quality skill, console job screen, plan
editing, large-data review, acceptance suite). The source is in `docs/mockup/onboarding-console.html`.

## 1. What we are building

An onboarding assistant that works the way a coding assistant works with a developer: a coworker, not a form wizard.

**The core loop for every job:**
1. The user says what to build and provides the source file. The output template is known by default.
2. Based on the record type (investor, commitment, investment, vendor, affiliate, GL), the system proposes the
   transformation rules. They come from the organisation standard, the sponsor's history and the process flows.
3. The user confirms the rules or steers them in conversation.
4. Code converts the input.
5. The worker reports data issues, each with a suggested fix. The user accepts the fix or overrides it.
6. The fixes and the rules applied produce the output workbook, which the human approves.

**Long-term goal: an autonomous agent.** It is not possible yet, because the system must first learn the rules of the
game. **The current phase is an apprenticeship** (section 6).

## 2. UX principles (user requirements)

- **KISS and YAGNI, "think Google homepage".** One box to start (a sentence and a file). Minimal clicks: about 4 from
  file to output (Start → Looks right → Accept all → Approve). No steep learning curve for a new analyst.
  Premium, calm, never dreadful.
- **The job screen (agreed 2026-10-10, mockup v21): the work in the main area, the conversation in a side chat.**
  - **The worker chat is docked on the right**, like a coding assistant's side panel. It is always there, can be
    collapsed to an icon, becomes a bottom sheet on narrow screens, and `/` jumps to its message box.
  - **A job starts in the chat, not in a dialog.** The user types a sentence and drops the file there. The worker infers
    the record type, template and route, and asks one question at a time, with buttons, only for what it cannot tell
    (which entities the file covers, records already in Ledgerline). The main area shows what it understood so far and can
    be edited there too. Start, or "go".
  - **Both paths write the same state.** Anything can be done by clicking in the main area or by saying it in the chat
    (answer a question, change a rule, pick a column, ask why or how far). Code still decides: the plan waits for
    answers, and **Approve is only a button in the main area**, so the agent can never approve its own work.
  - **The timeline is the checklist Read → Map → Convert → Check → Write** at the top of the main area. It shows where
    the job is at a glance: done, waiting on you (e.g. "Check · 2 need you"), or stopped, with a short fact per stage.
    It is a progress view, not a new set of gates, and the job is not a rigid step-by-step wizard.
  - **No long scroll.** The main area shows what is current; a finished part (plan, check) folds to one line that can be
    opened. Finished chat exchanges fold the same way. The worker posts a one-line status in the chat with the same
    button the main area has.
- **Transforming messy files from different sponsors should feel easy, not a burden of clicks.**
- **The "why" is always available on request and never clutters the main area.** Use "why" toggles, "Show working" and
  hover details.
- **Natural language in the chat, structured visuals in the main area.** People like to discuss changes in plain words
  but read results as structure. The main area has no "Worker says" blocks or prose: it shows tables, counts, chips and
  one-line folds. Explanations live in the chat or behind "why".
- **Millions of rows: review decisions, not rows.** The main area makes the reduction visible and lets people work at
  that level:
  - a **funnel** on the issues card: rows → distinct records → written by rules · decided by you · to check · left out
    by rules · for you;
  - **find and focus** across every file of the job (source and Ledgerline export): typed in the find box, or said in the
    chat ("show errors in Fund B for harbor"), which becomes structured filters (text, status, entity) you can change;
    the issue list narrows to the focus;
  - issues ranked by **impact** (rows and amounts), biggest first;
  - every issue says **where it is in the source file**: file name, sheet, the actual row number(s) and the Excel range
    (e.g. `Investor!A8:L8`, with a copy button for Go To), plus the other fields on that row as the file has them, so
    the person can open the file and find it; a cause with several rows gives its row numbers as ranges and a small
    table of the first 20 rows;
  - **thousands of rows behind one cause (e.g. 10,000 GL lines on one account in a million-line file)**: rows that share
    a cause share a value, and that value is how they are found. Every cause gives three ways in:
    - a **filter recipe for the person's own file**: "In Excel: Data → Filter, column D “GL Account” = 6150 →
      10,214 lines", with "copy values" for the filter search box; no download needed;
    - a **marked copy**: their file untouched, plus a "Worker check" column naming the cause on each affected row,
      those rows highlighted and the filter already on;
    - **just these rows**: an extract of only the affected rows, each with its row number in the original file, for
      files too big to open comfortably.
    Row numbers are compacted into ranges ("rows 1204–7203, 256000–258999 and 1 more range").
    When a cause needs two conditions (e.g. deal name blank on several accounts, each with 10,000 lines), the recipe
    combines them ("column M “Investment” = (Blanks), and column D “GL Account” = 1500, 1600, 8050 and 1 more →
    40,000 lines") and the cause is broken down by account, each line with its own count, row ranges, filter and
    marked copy, so the user can work through one account at a time.
  - a **heat strip** showing where issues cluster (by entity, or by journal for GL); a click narrows to it;
  - a **sample sized for 95% confidence (±5%)** instead of a fixed first, middle and last row;
  - **trace any written value** to its file, sheet and row, the rule applied, the decision and who made it, model calls
    and approval;
  - **keyboard triage**: j / k to move between causes, a to accept, / to message the worker;
  - **runs report back**: the timeline shows live progress and a notification says when Check is done, so nobody
    watches a progress bar.
  Comparing against the last approved run was considered and dropped: onboarding a fund is mostly a one-time load.
- **The mockup is the source for the actual build, so its flow must be credible.** Fake data is fine, but every number,
  name and statement agrees across screens: sheet row counts equal the sample rows, read = written + left out, the
  issue header and the timeline give the same count, IDs are the same everywhere, sample files are named for the
  sponsor and fund, and the seed is one consistent state (Sponsor A starts empty; Sponsor B's five static jobs for
  B Fund I are approved, with the workflows, ID counters, records and audit they wrote, so GL can be shown there).
- **A coworker:**
  - a message box is always visible (in the side chat);
  - a checklist (Read → Map → Convert → Check → Write) shows progress (the timeline);
  - the worker answers "how far are you?";
  - a change it proposes waits for the user's OK.
- **Built for millions of rows; reviewers must be able to review reliably:**
  - review **distinct values** (accounts, transaction types, names) once each, never row by row;
  - issues are grouped **by cause**, with one decision per cause;
  - a spot-check shows the first, middle and last row written;
  - a tie-out shows rows in = written + left out (for GL: debits = credits, and journals balance).
- **Feedback everywhere.** It is a production system, and output quality that falls short must flow back to improve the
  harness (section 5).
- **Leave the dashboard alone.** Focus on the workflows.
- **Refine the single mockup.** Do not start new styles.

## 3. Architecture (from Anthropic "Building effective agents" and "Agent Skills")

- **A job is a workflow, not an autonomous agent:**
  - fixed stages read → map → convert → check → write, run as code;
  - the model is called only to propose the plan and to decide **unresolved distinct keys**;
  - a re-applied saved workflow makes **zero** model calls;
  - the agent loop is kept for conversation: chat, "why", rule changes, and layouts never seen before.
- **Patterns:**
  - routing: code, then a small model, picks the record type and process path;
  - parallel independent checks;
  - voting: 3 small-model votes on uncertain picks, and if they disagree the pick goes to the human;
  - a code evaluator checks the plan before the human sees it;
  - a step budget, and stop when the requested output reaches its gate;
  - never drift into other inbox files.
- **Tool design (agent-computer interface, poka-yoke):**
  - tools take ids the system issued, never typed names or paths;
  - record type and scope are enumerations;
  - `run_python` returns a staged-table id;
  - samples come back as small markdown tables;
  - every tool description has an example and its edge cases.
- **Skills in the open Agent Skills format.** `skills/<name>/` holds:
  - `SKILL.md`, with frontmatter `name`, `description` and `version`;
  - scenario files, loaded only when that case comes up;
  - `scripts/`, which are executed, never read;
  - `tables/*.yaml`, the decision tables (first match wins), which code evaluates;
  - `checks.yaml` (ERR/WARN);
  - `cases/`, the skill's own test cases.
  Only the frontmatter sits in the prompt until a skill is needed. **Our addition: each skill splits into know-how (which
  the model reads) and rules (which code enforces).** That split makes outputs auditable and lets rule changes replay
  deterministically.
- **The data-quality skill (`check-data-quality`, ticket 25) defines what a problem in the data is, for every flow.**
  Its `checks.yaml` holds the rules by family: completeness (a required field blank on a row, including conditional
  requirements such as DEPT_ID on 1500s/1600s/8050s/9000s), validity (lengths, characters, dates, amounts), uniqueness
  (within the file and against the existing-records store), consistency (cross-file references, COA and mapping) and
  balance (journals, LP offsets). Rows failing the same rule on the same field are one cause. Each cause carries a
  locator computed by code: file, sheet, row ranges, the Excel filter that finds the rows, an optional breakdown, and
  the marked-copy / just-these-rows writers. Business users add a check through the improvement gates, not code.
- **The skill catalogue** is in the mockup (Harness → Skills) and in ticket 21. The first skill is `map-accounts`, with
  the L0–L5 cascade from the GL process flow.
- **Knowledge:**
  - the semantic layer: fields as concepts, with synonyms and where each was learned;
  - the context graph: per-sponsor facts, each with its source;
  - decision models: JDM/DMN-style tables.
- **Performance** (from an external review of a similar system; the parts that apply here):
  - deduplicate before matching, and reuse exact matches;
  - batch 25–50 keys per call, in parallel behind a semaphore;
  - structured output, an output-token cap and minimal reasoning effort;
  - static prompt text first, for prompt caching;
  - send only the top-N candidates;
  - one trace ID per job;
  - p50/p95 latency per record type and per 1,000 rows;
  - client objects created once per process (singletons), async drivers;
  - Polars or DuckDB for the data work, and a 1-million-row load test;
  - no vector database needed yet (a COA has hundreds of rows).
- **A model per role**, for Claude or for free models:
  - Planner (`claude-opus-5-5` or a large free model): plans new layouts and proposes improvements;
  - Executor (`claude-sonnet-5-5` or a mid free model): decides distinct keys, explains, chats;
  - Searcher (`claude-haiku-5-5` or a small free model): reads, retrieves candidates, casts votes;
  - each role has fallbacks, and the trace records which model did each stage.
- **Checkpoints.** Every stage saves its state. A failure or restart resumes from the last checkpoint without repeating
  stages or losing human decisions, and retries are counted.
- **Gates are code** (unchanged project rule): writing the output and writing memory always need a human. The agent
  never approves its own action.

## 4. Business users maintain it, no developers

- **What business users can do in the console:**
  - edit decision-table rows (Skills → open a skill);
  - add column names to the semantic layer;
  - upload a new template, Golden COA or transaction-type mapping;
  - add a new template (the worker maps its columns to known concepts and reuses existing skills).
- **How a change goes live:** each one becomes an Improvement card, is replayed on recorded jobs, and goes live after
  approval.
- **Only a new kind of logic (a new script) needs a developer.**

## 5. Feedback and self-improvement (harness evolves, model stays frozen)

Reference: "Recursive self-improvement without touching the weights" (C. Wijayasundara, LinkedIn). The loop is
signal → diagnose → propose → evaluate → gate → promote. The value is in the gate.

- **Capture.**
  - **Where:** a flag (⚑) on any mapping, rule, suggestion, output row or worker message, with an optional reason;
    plus approve-with-notes and "report an upload problem".
  - **Overrides count as feedback without a comment.**
  - **Each record holds:** skill@version, the decision-table row, the model, and lineage back to the source cell.
- **Routing (done by code):**
  - data rule → a table row;
  - concept → a synonym;
  - skill behaviour → a know-how edit or an example;
  - tool gap → a ticket.
- **Improver:**
  - clusters feedback by cause and proposes the smallest change to one surface;
  - a **write guard** limits what it may edit: table rows, synonyms, skill text and description, examples, bounded
    thresholds;
  - it may never touch scripts, the verifier, answer keys, the severity floor, the never-invent rule, the audit or the
    gates.
- **Gates:**
  - a private holdout;
  - dominance: no regression on any split, and any change to a signed-off output blocks the change;
  - budget parity: no gains bought with more model calls or more questions;
  - a noise band for slow-loop changes;
  - predicted vs observed, tracked for 2 weeks;
  - one-step rollback.
- **Two speeds:**
  - fast nightly loop: deterministic replay of table and synonym changes, which costs no model calls;
  - slow weekly loop: skill-text changes, re-run on the holdout, with human approval.
- **Metrics in business units:**
  - correct cells per 1,000 rows;
  - overrides **and** questions per 1,000 rows (tracking both prevents gaming);
  - rows left out per 1,000 rows;
  - share decided without the model, and first-pass tie-out;
  - upload rejections and time to approve;
  - model calls per 1,000 rows;
  - **success rate**;
  - **cost per successful job = (model spend, retries included, + reviewer time × hourly rate) ÷ successful jobs**;
  - skill misfires: loaded but not needed, needed but not loaded.
- **Pitfalls:**
  - bug fixes that look like learning: tag harness fixes separately;
  - metric artefacts: pin metric definitions;
  - gaming: count rows left out and questions asked.

## 6. Learning the rules now, autonomy later

**Now: the apprenticeship.**
- Every question asks for scope: just this file, always for this sponsor, or every sponsor.
- After each job the worker plays back what it learned. Nothing enters a rulebook without confirmation.
- There is a per-sponsor rulebook recording who taught each rule, from which job, and how often it has fired.
- The worker labels any choice not backed by a rule as "my suggestion · no rule yet".
- **Readiness** = decisions made by approved rules ÷ all decisions, tracked per workflow in Health.

**Later, not built yet.** Autonomy is earned per workflow, within a policy a human approves in advance:

| Level | The worker | The human |
|---|---|---|
| 1 Supervised | Proposes | Approves every job |
| 2 Assisted | Applies the saved workflow and settles what its policy covers | Handles exceptions, approves every job |
| 3 Autonomous with review | Runs jobs that arrive in the inbox | Spot-checks a random sample |
| 4 Autonomous | As level 3; reports outcomes | Reviews weekly samples and Health trends |

- **Moving up** is earned from metrics.
- **Moving down** is automatic on a regression, a new layout, a failed sample or a tie-out break.
- **Always escalated:** an unseen layout, an ERR no rule covers, totals that don't tie, any change to finished work,
  and promotion to organisation scope.
- **Planned UI** (deferred): an autonomy chip, a policy screen, inbox-started jobs, one exceptions queue across all jobs,
  and sampled review.

## 7. Comparison with langchain-ai/paid-media-agent (the inspiration)

- **Kept from it:**
  - approval text built by code;
  - revision-bound, single-use approvals;
  - stale-schema and stale-policy checks;
  - results offloaded to artifacts behind ids;
  - refusing a report whose figures don't reconcile.
- **Improved on its shortcomings:**

  | Area | paid-media-agent | Here |
  |---|---|---|
  | Figures in text | The model writes free-text figures | Every value is copied by code |
  | Audit | Read audit held in memory | Hash-chained audit with a reason per action |
  | Business rules | Markdown prose the model reads | Versioned decision tables that code evaluates |
  | Learning | None | Gated learning: teach-back, rulebook, improver |
  | What is gated | Only external mutations | The output workbook too |
  | Evaluation | Thin | Answer-key scoring, replay and a holdout |

## 8. Decisions on the static-entity spine (from the requirements review)

**Vocabulary.** Sponsor = GP / manager / PE firm that runs funds (the customer). Fund = entity. Record type = investor,
investment, commitment, affiliate, vendor or GL.

**Sponsor and entity attributes.** A sponsor has exactly three: sponsor ID, sponsor name, go-live date. An entity has
the same three (entity ID, entity name, go-live date) plus an optional fund group ID and fund group name. A sponsor holds
standalone funds and fund groups; a group holds several funds. A blank fund group means the fund is standalone. The fund group is only a logical label for now: nothing
depends on it, but one group ID always carries one name. Go-live date is mandatory for both: nothing is saved without it.
Funds of one sponsor need not go live together, so each entity has its own date and entities are loaded as they arrive.
Sponsors and entities are onboarded by typing one row or uploading a list (bulk); a bulk row missing a go-live date is
shown as MISSING and blocks the save until it is set. IDs and names are unique within their level.

1. **Workflow key** = sponsor + record type. The fund is a parameter and a source variant, not part of the key.
2. **A saved workflow is a recipe**: deterministic steps, parameters and the instruction. It holds several source
   variants, for example:
   - a common file;
   - investors extracted from the GL when no investor file is sent;
   - a horizontal layout;
   - a batch without bank accounts.
   For a new file: an exact match applies the recipe; a close match proposes synonyms and asks; a confirmed new layout
   is stored as another variant. A third file is matched against all known variants, and the worker applies the right
   one and asks for review. Missing optional fields stay blank and are reported MISSING.
3. **Mappings transfer across a sponsor's funds.** Differences are reported as a "variation" or as "missing".
4. **A re-applied workflow always needs human approval.** The AI prepares; a human approves.
5. **Templates:**
   - the defaults come from a settings tab (`Source_Samples/mock_data/Upload_templates` is the north star);
   - a template uploaded during a job overrides the default for that job;
   - a sponsor customisation is its own template, chart of accounts and mapping rules, set in the sponsor setup flow.
6. **IDs:** one counter per sponsor and record type, continuing across funds and batches. Fund 2 continues from
   GINV-60. Investors and commitments always get a new ID from the counter, and the old key is kept as the source key.
   Vendors, affiliates and investments keep the client's ID as supplied (or as corrected by the user); a vendor without
   one gets the ID generated from its name, else the counter. The same ID is shown everywhere (sample, every row,
   find, trace) and stored in the existing-records store with the source key it came from.
7. **Static records are unique per sponsor**, loaded per fund, and deduplicated against the sponsor's existing records:
   - same key + same name: left out, with a reason recorded per record;
   - same key + a different name, or the same name + a different key: ERR;
   - the user resolves an ERR by leaving the record out as a duplicate or as incorrect, or by correcting it, and the
     resolution is audited.
8. **No old ID in the source:** the normalised name is the key. Probable merges go to a human, and the crosswalk
   records which kind of key was used.
9. **Missing dependency** (for example, the investor for a commitment):
   - it is an ERR listing the missing items;
   - the user supplies the existing Ledgerline ID (and name) through a quick "existing record, loaded outside the app"
     entry;
   - that record is used as is, with no reconciliation, and stored in the existing-records store.
   **The existing-records store is per sponsor** and fills only from (a) jobs approved in the app, (b) an Ledgerline export
   the user uploads, and (c) that quick entry. Nothing is assumed about records loaded outside the app: before the first
   job of a record type, the worker asks in the chat whether any were loaded into Ledgerline outside the app (yes → upload
   the export, no → proceed, not sure → upload or go ahead marked "not checked"). The answer is stored once per sponsor
   and record type and audited; "go ahead without it" asks again next job. Duplicates are caught only against what the
   store holds, and the worker always says where its count comes from (e.g. "5 from the approved Fund A job · 6 from the
   Ledgerline export").
10. **Severity:** ERR blocks approval and WARN needs an acknowledgement. Correcting data is an easy, audited workaround.
11. **Finished work never changes.** Configuration changes apply only to new jobs and leave a trail.
12. **Scope now:** static entities and the crosswalk, with the GL used as a source for extraction. GL Fund and GL LP
    conversion is the next increment. Keep the spine lean and test it manually with free models.
13. **Every instruction and every action is audited** with its reason, traceable after 10 years.
14. **AI is the preparer and a human is the approver.** There are no separate preparer and approver people.
15. **Required fields are a per-sponsor choice, set with a "Required?" checkbox** on each output column in the mapping
    table. The default comes from the process flows (what the first run needs); the user unticks a field the sponsor does
    not need or ticks one it does. The change applies to every job of that sponsor and is audited. A required field the
    file cannot give makes those rows ERR until it is filled in, mapped to a column, or unticked. Cross-sponsor patterns
    may inform defaults quietly, but the UI never says "learned from other sponsors".
16. **The user can override any source column** with a dropdown under "Comes from" in the same table (the file's
    columns, or "not in this file"). The override is audited, recorded as feedback, and offered as a sponsor rule.
    Constants from sponsor setup (for example GP_ID) stay fixed.

## 9. Open questions found in the process-flow PDFs

See `docs/process-flows.md`, section "Ambiguities".

## 10. Acceptance scenarios (test the build against these)

Agreed on 2026-10-10 while refining mockup v25. Each scenario names the input, the action and what must be true. The
sample data is the mockup's (`docs/mockup/onboarding-console.html`: `CFG`, `GLD`, `REG0`, `EXPORTS`), so the expected
numbers below can be checked exactly. Every scenario must hold in the UI and in the written output; where a number
appears in more than one place (timeline, issues, chat, output), all places must agree.

**A. Starting a job in the chat**
- A1. Sponsor A, "+ Start new job". No dialog opens; the chat box has focus and the worker asks what is needed.
- A2. Type "build the investor upload". The worker replies "Got it: Investors" and asks for the file. The main area
  shows Record type = Investors.
- A3. Use the sample file. It is named `Sponsor_A_Common_Investors.xlsx`. The worker asks which entities it covers,
  with buttons All 3 · Fund A · Fund B · Fund C · Some of them.
- A4. One sentence can carry everything: "build the investor upload for all entities from the sample", then "go",
  starts the job. Start never stays greyed out without saying what it still needs.
- A5. The same can be done by clicking in the main area (record type, entities, file); both paths change the same state.

**B. Records already in Ledgerline**
- B1. First Investors job for Sponsor A: before the plan, the worker asks in the chat whether any investors were loaded
  into Ledgerline outside the app. The plan and "Looks right" stay hidden until it is answered (status "Question for you").
- B2. "yes" + the sample export (`Ledgerline_Investors_Export.xlsx`, 6 investors): the worker reports what it read; rows 3,
  4 and 5 are left out as already in Ledgerline; rows 6 and 7 come to you as clashes (same key, another name; same name,
  another key). Row 12 (Northgate Resources Ltd vs Limited) becomes a clash once "Ltd = Limited" is a rule.
- B3. "no": those rows are written as new; the worker does not ask again for Sponsor A investors.
- B4. "not sure" then "go ahead without it": the run is marked "not checked against Ledgerline" and the question comes
  back on the next Sponsor A investors job.
- B5. The store says where its count comes from, e.g. "5 investors already in Ledgerline: 5 from the approved B Fund I
  investors job" (Sponsor B), and never invents records.

**C. Plan: Required? and the source column**
- C1. Tick Required? on Country (Investors): the plan warns "Required but not in the file: Country"; after conversion
  rows 8, 12 and 14 are ERR until Country is filled in, mapped to a column, or unticked. The tick shows a SPONSOR A tag.
- C2. Pick a different column under "Comes from" (e.g. Country ← Notes): the MISSING flags for that field disappear,
  confidence shows "Set by you", the change is audited and offered as a Sponsor A rule.
- C3. GP_ID stays fixed as the sponsor-setup constant; Investor_ID shows "new ID from Sponsor A's counter · GINV-0001
  onward".

**D. Counts always agree**
- D1. The plan says the investor sheet has 15 rows, and Read says 15 rows. (Sheet row counts equal the rows in the file.)
- D2. After conversion: read = written + left out + need you (Investors with the export: 15 = 7 + 3 + 5). Approval needs
  0 need you, and then read = written + left out.
- D3. The timeline ("Check · N need you"), the issues header ("N need you") and the chat give the same N for every job
  type. With the sample data: Investors 5 (with the export), Commitments 4, Vendors 3, Investments 2 and Affiliates 3
  (answering "no" to records outside the app), GL Fund 11 after resuming.
- D4. GL LP never claims "read = written + left out" with offsets included: it says lines written plus N offsets.
- D5. A journal marked "corrected in the source" is not counted as balanced.

**E. IDs**
- E1. Investors and commitments get new IDs from the sponsor counter (GINV-0001…); the old key is kept as
  Source_Investor_Key. The counter continues across jobs and funds.
- E2. Vendors, affiliates and investments keep the client's ID; a vendor without one gets the ID generated from its name;
  a corrected ID (e.g. "V 1 22!" → V_122) is the one written and stored.
- E3. The two Sheppard vendors get different IDs (SHEPPARDCOMPANY, SHEPPARDCONSULTING), never the same one again.
- E4. The same output ID is shown in the sample, Every row, find and trace, and stored with its source key.

**F. Each issue says where it is in the source file**
- F1. Investors, Alder Co LLC: "Sponsor_A_Common_Investors.xlsx · sheet Investor, row 8 · Investor!A8:L8" with copy,
  and the row's fields "Row Labels: HS-9931 · Investor: Alder Co LLC · Entity: …". Header row = row 1, everywhere.
- F2. A cause with two rows (long source keys) shows "rows 9–10" and a table of those rows with their fields.
- F3. GL issues show sheet Data Source, the row, and Journal, Line, GL Account, Transaction Type, Debit, Credit,
  Description and the referenced investor, vendor, affiliate or investment.

**G. Finding thousands of rows in Excel**
- G1. One value, many rows: a cause of 10,214 GL lines on account 6150 shows "In Excel: Data → Filter, column D “GL
  Account” = 6150 → 10,214 lines" and rows as ranges ("1204–7203, 256000–258999, 909000–910213"), never a list of
  10,214 numbers. The per-row table shows the first 20 and says the rest are in the marked copy.
- G2. Several accounts: deal name blank on 4 accounts (1500, 1600, 8050, 9010), 10,000 lines each, shows "column M
  “Investment” = (Blanks), and column D “GL Account” = 1500, 1600, 8050 and 1 more → 40,000 lines", plus a breakdown
  with one line per account (10,000 each, its row ranges, its own filter to copy, its own marked copy).
- G3. "copy values" copies the filter values; "copy filter" on a breakdown line copies e.g. "Investment = (Blanks);
  GL Account = 8050".
- G4. Marked copy: the user's file untouched, plus a "Worker check" column naming the cause on each affected row, those
  rows highlighted and the filter on. For one account it is named `…_marked_8050.xlsx` and covers only that account.
- G5. Just these rows: only the affected rows, each with its row number in the original file.
- G6. The filter column follows the cause: unmapped account → GL Account; unknown vendor/investor/affiliate → that
  column; unmapped transaction type → Transaction Type; unbalanced journal → Journal; duplicate or long ID → the ID
  column; name clash → the name column; blank deal → Investment blank and GL Account.

**H. Find and focus**
- H1. The find box searches every file of the job (the source and the Ledgerline export): "alder" finds row 8 of the source
  (Needs you) and row 6 of the export (In Ledgerline).
- H2. Saying "show errors in Fund B for harbor" in the chat sets filters status = needs you, entity = Fund B,
  text = harbor, shown in the main area where they can be changed; the issue list narrows too. "show all" clears them.
- H3. Typing in the find box keeps the cursor in the box while results update.

**I. Review tools**
- I1. Funnel on the issues card: rows → distinct records → written by rules · decided by you · to check · left out by
  rules · for you; the parts add up to the rows read.
- I2. Issues are sorted by impact: blocking first, then amount, then rows.
- I3. Heat strip shows where the issues are (by entity, or by journal for GL); a click narrows to it.
- I4. The sample is sized for 95% confidence (±5%); on a 9-row output that is all 9 rows, and it says so.
- I5. Clicking an output ID shows where it came from: file, sheet, row, ID rule, name column, checks, decision, model
  calls, approval.
- I6. Keys: j / k move between causes, a accepts the selected cause, / jumps to the chat box.

**J. Runs, failures and approval**
- J1. "Looks right" starts the run; the timeline shows converting / checking live and a notification says when Check is
  done ("… Check done · 4 need you"), with a link back to the job.
- J2. With the free model profile the first GL run stops at Check and is saved; "Resume from Check" does not repeat
  Read, Map or Convert, and keeps every decision.
- J3. A job cannot be approved before it has converted, while a run has failed, or while anything needs you.
- J4. Approval writes the output, saves the sponsor workflow, advances the ID counter by the IDs generated, adds the
  written records to the store with their source keys, and offers the next record type.

**K. Seed and sponsors**
- K1. Sponsor A starts with nothing approved: no jobs, empty store, counters not started, Investors route "first fund".
- K2. Sponsor B's five static jobs for B Fund I are approved, with their jobs list, workflows, counters (GINV next 6),
  store and audit, so B Fund I has 5 of 5 static types and GL can start there.
- K3. GL sample files and plan text name the job's sponsor, fund and entity ID (e.g. `Sponsor_B_GL_BFundI_2025Q4.xlsx`,
  entity 6100101), never Fund A by default.
- K4. Investors, vendors and affiliates count as done for the whole sponsor; commitments and investments count per fund.

**L. Any required field blank, in any flow (the data-quality skill)**
- L1. Investors row 15 (HS-9970) has a blank investor name: one cause "Investor_Name is blank", recipe "column B
  “Investor” = (Blanks)", row 15, a box to type the name; typing it clears the cause and is audited as supplied by the
  person.
- L2. Commitments row 7 has a blank closing date: "Commitment_Date is blank", recipe "column E “Closing Date” =
  (Blanks)", suggestion "Use 2025-06-30, the closing date of the other rows in this batch".
- L3. Affiliates row 5 has a blank name: "Affiliate_Name is blank", by the same rule (no special case per flow).
- L4. Ticking Required? on a field the file has no column for (Commitments · Currency) says once "This file has no
  “Currency” column" and makes all 8 rows need you until the column is added, another column is picked, or the tick
  is removed.
- L5. A blank generated ID (e.g. an investment with no Deal ID) is not an error: it gets a new ID.
- L6. Several blank rows of one field: one cause, rows as ranges, "Set for all" to supply one value, a breakdown by
  entity when the file covers several, and the marked copy / just-these-rows files.
- L7. GL: a conditional requirement (deal blank on 1500s, 1600s, 8050s, 9000s) works the same way, with the
  two-condition recipe and the per-account breakdown of G2.

## 11. References

- Anthropic, Building effective agents: https://www.anthropic.com/engineering/building-effective-agents
- Anthropic, Agent Skills:
  https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills
- Programmatic tool calling: https://platform.claude.com/docs/en/agents-and-tools/tool-use/programmatic-tool-calling
- LangChain subagents: https://docs.langchain.com/oss/python/langchain/multi-agent/subagents
- Harness self-improvement:
  https://www.linkedin.com/pulse/recursive-self-improvement-without-touching-weights-wijayasundara-hx4ie/
  - Reference repos: github.com/cwijayasundara (`self_improving_tax_ai_ex_v1`, `dream_rsi_v1.0`, and others).
- Paid-media agent: https://github.com/langchain-ai/paid-media-agent
- **Market patterns** (summary of web research on Flatfile, OneSchema, Dromo, Osmos, Copilot Finance reconciliation,
  DataSnipper, BlackLine, Rillet and Basis; most of it is vendor material):
  - plan first, then approve;
  - mappings as a reviewable table with confidence;
  - an exceptions queue with a suggested fix per item;
  - reason codes;
  - a diff before commit;
  - a standing tie-out panel;
  - per-cell provenance;
  - corrections become proposed rules;
  - few approvals, to avoid approval fatigue.
  **No product documents corrections turning into gated, versioned rules in detail. That is our differentiator.**
