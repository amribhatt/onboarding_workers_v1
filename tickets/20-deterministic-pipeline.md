# 20 Deterministic pipeline, measured model calls

Why: the Deep Agent picks every tool through the model. That is slow, costs tokens, and caused the drift and loops in
STATUS.md "Live-run history". Code should run the steps; the model should only propose the plan and decide what code
cannot. Model calls must grow with unresolved distinct keys, never with rows.

Do:
- A fixed pipeline per record type: `read → map → convert → check → write`. Each stage is a plain function that
  writes `audit.record`. The model is called only to (a) propose the plan (field mapping, path, ID scheme) and
  (b) decide unresolved distinct keys. A re-applied saved workflow makes zero model calls.
- Distinct-key resolution: collect the distinct values a stage needs decided (account + transaction type, names,
  classes), resolve them by lookup first, and send only the leftovers to the model in batches of 25–50. Run batches in
  parallel behind an `asyncio.Semaphore` (`WORKER_MAX_CONCURRENCY`). Use structured output, an output-token cap, and
  minimal reasoning effort; check the effort setting was honoured from the response. Put static prompt text first so
  prompt caching works, and send only the top-N candidates with the fields the decision needs.
- A model per role, set by env and in Settings, for Claude or free models alike:
  - `WORKER_MODEL_PLAN` (Planner: new layouts, improver proposals);
  - `WORKER_MODEL_EXEC` (Executor: distinct-key decisions, explanations, chat);
  - `WORKER_MODEL_SEARCH` (Searcher: reading, candidate search, the 3 votes);
  - each role has its own fallback list;
  - `WORKER_MODEL` stays as the default for any role left unset;
  - the trace records which model did each stage.
- Checkpoints: every stage writes a checkpoint (staged tables, plan, decisions, run state). A failure such as a timeout,
  a 429 or a crash resumes from the last checkpoint without repeating earlier stages or losing human decisions. This
  also covers a restart of the server (STATUS: "persist staged tables so a paused gate survives a restart").
  Retries are counted per job.
- Model, HTTP and DB clients are created once per process. Use async drivers.
- Tracing: one trace ID per job, shared by LangSmith and the audit. Per job, record model calls, input/output tokens,
  and time per stage (read, map, convert, check, write) and per model call.
- `worker.metrics latency` reports p50/p95 per record type, per sponsor file and per 1,000 rows.
- Columnar engine (Polars or DuckDB) for copy, join and dedup. Stream the workbook; never hold cell objects per row.
- Load test with a synthetic GL of 1,000,000 lines and 300 distinct account/type pairs (generated, no real names).
  Record wall time, peak memory and stage times in `.logs/load_1m.log`.
- Patterns from Anthropic, "Building effective agents" (a job is a workflow; the agent is only for conversation):
  - **Routing:** a cheap classifier (code first, then a small model) picks the record type and process path, for
    example investor paths 1–4 or the GL posting model. `WORKER_MODEL_SMALL` decides distinct keys;
    `WORKER_MODEL` proposes plans for layouts it has never seen.
  - **Parallel sections:** independent checks run in parallel (dedup, lengths and characters, crosswalk, balance).
  - **Voting:** low-confidence picks (L3–L5) are decided by 3 small-model votes. If they disagree, the pick goes to the
    human. Agreement becomes part of the confidence shown.
  - **Evaluator:** before the plan reaches the human, code checks it — every required field is covered, constants come
    only from setup, no source column is mapped twice by accident. A failed check is fixed by the model once, then
    shown to the human.
  - **Stopping conditions:** a step budget per job; stop as soon as the requested output reaches its gate. Never move
    on to other files in the inbox (STATUS live-run history: drift and timeouts).
- Tool design pass (agent-computer interface, "poka-yoke"):
  - Tools take ids the system issued (staged table id, column id from `column_profile`), never free-typed names or
    paths.
  - Enumerations for record type and scope.
  - `run_python` writes only to its scratch folder, and the result comes back as a staged table id.
  - Samples are returned as small markdown tables. The model is never asked to count rows or escape JSON.
  - Each tool description has an example, its edge cases, and how it differs from similar tools.
  - Test tools on fixture inputs and fix the descriptions where the scripted model and live runs go wrong.
- Keep the Deep Agent for open conversation (chat, "why", rule changes). It may use programmatic tool calling: the
  model writes one short script that calls the audited tools.

Tests (scripted model): a re-applied workflow makes 0 model calls; 1,000 rows with 12 unresolved keys make at most
1 batch call; the semaphore caps concurrency; trace and stage timings appear in the audit; output is byte-identical
across two runs.

Targets to agree and record in STATUS.md: re-applied workflow on 1M rows under 2 minutes; p95 per model call under
10 s.

Acceptance: green tests (`> .logs/t20.log`), ruff clean, load-test log saved, STATUS.md updated, one commit.
