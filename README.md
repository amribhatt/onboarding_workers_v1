# Onboarding Worker

An agent that works like an onboarding knowledge worker. A person drops in a sponsor's source file and says what
to build. The worker opens the file the way a colleague would, works out what it is, proposes a plan, carries on by
itself, says loudly what is missing, shows what ties out, and **asks a human to approve** before anything is
written. The model proposes; code decides; humans approve.

- **Code decides.** Copying cells, totals, balancing, duplicate checks, ID generation and writing upload files are
  deterministic and tested. The model never retypes a number or an ID.
- **Never invents.** A field the source cannot provide stays blank and is reported as `MISSING`.
- **Gated.** Writing an upload and every memory write pause for a named person. The agent cannot approve its own
  action.
- **Audited.** Every action is recorded with its reason in an append-only, hash-chained log.
- **Learns per sponsor.** Corrections become rules only after a person approves them.
- **Model agnostic.** Local models, any OpenAI-style service, or a cloud project: one setting.

## Quick start

```bash
uv sync
uv run onboarding-worker demo        # offline sample, no key, no sign-in, opens the console
```

With a real model:

```bash
cp .env.example .env                 # choose a provider below and fill its few settings
uv run onboarding-worker doctor      # checks the install and the configuration
uv run onboarding-worker serve       # http://127.0.0.1:8765  (asks you to register and sign in)
```

On Windows, `start.ps1` (or `start.bat`) serves in the background and opens the browser; `stop.ps1` stops it.

## Sign in

`serve` asks you to register once (full name, email, password) and then to sign in with the email and password. The signed-in name is the
reviewer on every decision and in the audit, so nobody types a name. This is a basic account store, kept as JSON in
the state directory with salted `scrypt` hashes; it is not single sign-on. The offline `demo` runs without it.

## Model providers

Set `WORKER_MODEL_PROVIDER` and `WORKER_MODEL`. A new provider is one entry in the table in `src/worker/agent.py`.

<!-- AUTO:providers -->
| `WORKER_MODEL_PROVIDER` | Use it for |
|---|---|
| `openai` | OpenAI, with `OPENAI_API_KEY` |
| `openrouter` | OpenRouter (free tool-capable models work), with `OPENROUTER_API_KEY` |
| `ollama` | A local model through Ollama: no key, no provider rate limit (`OLLAMA_BASE_URL`) |
| `compatible` | Any OpenAI-style service: `WORKER_BASE_URL`, optional `WORKER_API_KEY` |
| `vertex` | Google Cloud (Vertex AI): `GOOGLE_APPLICATION_CREDENTIALS` is the path of a service-account JSON |
| `demo` | Offline scripted model for the bundled sample; no key |
<!-- /AUTO:providers -->

Examples:

```bash
# a local model through Ollama
WORKER_MODEL_PROVIDER=ollama
WORKER_MODEL=<a tool-calling model you have pulled>

# Google Cloud with a service-account file (the path only; keep the file outside the repository)
WORKER_MODEL_PROVIDER=vertex
WORKER_MODEL=<a Gemini model>
GOOGLE_APPLICATION_CREDENTIALS=/secure/place/service-account.json
```

## How a job runs

Every job moves through five stages: **Read → Map → Convert → Validate → Download**.

1. **Read.** The file is opened and its sheets and headers are found.
2. **Map.** Each template field gets a source column, a fixed value, or a generated ID. You can change any column
   in the plan. Questions the worker cannot settle alone appear on the plan with standard buttons; its explanations
   are in the chat.
3. **Convert.** Values are copied by code, never retyped.
4. **Validate.** Totals tie out, duplicates are found, and each problem is grouped by cause.
5. **Download.** After a named person approves, the upload file is written for download.

### IDs

Each sponsor decides once how new IDs are made: use the ID that is in the file (you point at the column), or
generate new ones with a prefix and running number (the worker continues the pattern of IDs already in the system),
or a custom format. A column of names is refused as an ID unless a person says why.

## Configuration

Settings come from `.env` or the Settings page. `.env.example` lists them all with comments; the main ones:

| Variable | Meaning |
|---|---|
| `WORKER_MODEL_PROVIDER`, `WORKER_MODEL` | Which provider and model (table above) |
| `WORKER_MODEL_FALLBACKS` | OpenRouter only: models to try if the first fails or is rate limited |
| `WORKER_MODEL_EXEC`, `_SMALL`, `_PLAN`, `_SEARCH` | A model per role; unset falls back to `WORKER_MODEL` |
| `WORKER_APPROVER_TOKEN` | If set, state-changing calls need the header `X-Approver-Token` |
| `WORKER_REQUIRE_LOGIN` | `1` asks for sign-in (default for `serve`; off for `demo`) |
| `WORKER_STATE_DIR` | State directory (default `.worker_state`) |
| `WORKER_BUDGET_TOKENS`, `WORKER_BUDGET_CALLS` | Per-job spend caps on model use |

## Commands

<!-- AUTO:commands -->
| Command | What it does |
|---|---|
| `run` | see --help |
| `chat` | see --help |
| `serve` | web console |
| `demo` | web console with an offline sample |
| `verify` | re-check the hash chain of an audit file and report the first break |
| `metrics` | latency p50/p95 from the audit files of finished jobs |
| `models` | list free OpenRouter models that can call tools |
| `db` | database: migrate, verify the audit chains, anchor their heads |
| `graph` | Neo4j projection (WORKER_GRAPH=on): project, rebuild, check; lineage queries |
| `doctor` | check the installation and configuration |
<!-- /AUTO:commands -->

## Sample data

`docs/sample_files/` holds synthetic source workbooks and upload templates used for testing: ten numbered sets, a
40-fund generated ledger, and the upload templates. Nothing in it is real client data.

## Development

```bash
uv sync
uv run pytest -q --tb=short > .logs/test.log 2>&1; tail -15 .logs/test.log
uv run ruff check src tests
sh scripts/install_hooks.sh          # once per clone: a merge on main refreshes the README
```

The README's provider table, command table and ticket list are generated by `scripts/update_readme.py`; a
post-merge hook runs it whenever something is merged to `main`. Work is tracked as small tickets, the current list
is below and the running log is in `STATUS.md`.

<!-- AUTO:tickets -->
<!-- /AUTO:tickets -->

## Production-shaped stack

`docker compose up` runs the app on PostgreSQL with an immutable, verified audit log. With `DATABASE_URL` unset
everything stays on files. Setup, backup and restore are in [docs/operations.md](docs/operations.md).

## Known limits and security posture

- This is a demo build; sign-in is basic and registration is open to anyone who can reach the server.
- Model-written code runs only in the network-less sandbox container; without Docker those features are off.
- Cell and file text is treated as data, never as instructions. The hard guarantee is the gates in code.
- Every job has a spend cap; `unlimited` lifts it on purpose and by name.
- Hosted models are rate limited by their provider; a local model is limited by the machine it runs on.

Direction and rules: [docs/design-onboarding-assistant.md](docs/design-onboarding-assistant.md),
[docs/process-flows.md](docs/process-flows.md).
