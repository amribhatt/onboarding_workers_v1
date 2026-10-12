# 21 Skills, starting with map-accounts

Why: the worker's abilities should be named, versioned skills a new analyst would recognise ("map accounts", "build
reference numbers"), each with its rules as data that code evaluates. Source: the GL process flows in
`Source_Samples/Process_Flows` (GL_Entity_Pf, LP_entity), summarised in the mockup's decision tables.

Depends on: 20.

Do:
- Skills follow the open Agent Skills format (Anthropic, "Equipping agents for the real world with Agent Skills"),
  which Deep Agents loads from a skills folder. Layout `skills/<name>/`:
  - `SKILL.md` with YAML frontmatter `name` and `description` (with a `version`). Only these fields sit in the
    system prompt (level 1). The description is the trigger, so write it carefully.
  - Body (level 2, read only when the skill is used): know-how for the model and what to ask the human. Keep it short.
  - Scenario files (level 3, read only when that case comes up), e.g. `bank-accounts.md`, `lp-offsets.md`,
    `closing-entries.md`. Keep cases that rarely occur together in separate files.
  - `scripts/` holds the code tools. `SKILL.md` says explicitly "execute, do not read". Scripts run on the data
    without loading it into context.
  - `tables/*.yaml` holds the decision tables (inputs, outputs, hit policy `first`, rows) and `checks.yaml` holds
    ERR/WARN. Code evaluates them; the model reads a table only to explain a decision.
  - Skills are versioned; every job records the skill versions it used.
- Start from evaluation: build each skill against the failing cases in the replay set, and keep its fixture cases in
  `skills/<name>/cases/` so its tests run on their own.
- Security: skills come only from this repository and are reviewed like code. A skill must never tell the model to
  reach the network or read files outside the job. Script dependencies are pinned in `pyproject.toml`.
- A small decision-table evaluator (`worker.decide`): first match wins, typed inputs, and returns the row that fired so
  the UI can show "why". Sponsor overrides live in `templates/sponsors/<sponsor>/tables/` and replace whole rows by id.
- Skill `map-accounts`:
  - Distinct keys `normalize(Account Type) | normalize(GL Account) | normalize(Transaction Type)`; bank and cash
    accounts narrow on the first 4 digits.
  - Cascade, first hit wins: L0 Historic (Golden or sponsor store, 0.97, apply) → L1 transaction type pins one allowed
    account (0.85, apply) → L3 hybrid (FS-grouping prefilter, BM25 + small embeddings, ≥ 0.50, suggest) → L4
    account-primary (suggest) → L5 no match (top 5 candidates, needs the human).
  - The model only ranks and explains L3–L5 candidates. Code checks every pick against the COA and the transaction-type
    mapping.
  - ERR: account + type combination not allowed by the mapping; `ACCT_NO` blank. WARN: transaction type not in the
    tagging reference.
  - Output: one row per distinct key (tier, score, evidence, account). Code applies it to every line.
  - An approved L3–L5 pick is written to the sponsor store through the gated memory write. Promotion to the Golden COA is
    a separate admin-gated action.
- Plan lines and suggestions carry `skill@version` and the table row that fired. The UI shows it only behind "why".

Tests: only frontmatter is in the system prompt at start; a scenario file is read only when its case occurs; each
tier fires on a fixture; a disallowed combination is ERR; a sponsor override beats the organisation row; an
approved pick becomes L0 next run; no account number is produced that is not in the COA.

Acceptance: green tests (`> .logs/t21.log`), ruff clean, STATUS.md updated, one commit.
