# How agentic EDM and reconciliation products handle large exception volumes

Question: how do products that are agentic but deal with enterprise data management (EDM) or reconciliation at a large firm solve it, and what does that mean for onboarding_worker?

Method: vendor product docs and vendor pages only. Where a page was unreachable or silent, the item is marked UNVERIFIED. Vendor marketing numbers are listed as claims, never as evidence of accuracy. Fetched 2026-10-11; several vendor sites were reachable only in part (BlackLine, Duco, Informatica docs returned a PDF dump, a wrong site, or 403).

## 1. What was found per product class

| Class (examples) | Deterministic core | AI role | Scale approach | Exception handling | Human in the loop | Learning | Audit | Evidence quality |
|---|---|---|---|---|---|---|---|---|
| Reconciliation, rules engines (BlackLine Transaction Matching, Duco, SmartStream) | Matching rules: 1:1, many:1, many:many, tolerances; BlackLine says matches are "automatic or suggested" [1] | SmartStream: unsupervised fuzzy matching and "Smart Agents" that "investigate and progress exceptions autonomously" (26.10) [2]; Duco: agentic rule builder that turns plain English into reviewable rules (third-party description only, UNVERIFIED at primary) | Rules run in the engine; SmartStream "millions of transactions in minutes" [2] | Auto-match, then residual breaks to queues with workflow; BlackLine auto-certification by thresholds (global or per account) (UNVERIFIED at primary, marketing blog only) | Suggested matches and queues; SmartStream page does not describe approval of agent actions [2] | SmartStream Air/Affinity learns from user matches and exception handling (press release, ~2021) [3] | General "auditable governance" wording only [2] | Weak: no benchmarks, no method for any speed or error figure [2] |
| MDM / entity resolution (Reltio, Semarchy, Profisee) | Match rules; perfect matches merge automatically, similar ones become Potential Matches for a steward [4] | Match IQ and LLM-based matching are optional extra models (community posts, UNVERIFIED at primary docs) | Not stated in the pages fetched | Potential Match Review page: rules-met attributes highlighted, up to five profiles side by side [4] | Steward decides each potential match [4] | Steward decisions train the model (community post only, UNVERIFIED) | Unmerge exists as an API (community post, UNVERIFIED) | Docs are qualitative; no scores, thresholds or error rates on the fetched pages [4] |
| Data quality and governance (Ataccama, Informatica CLAIRE, Collibra, Databricks) | Rules execute as ordinary rules; Collibra pushes rules down to the warehouse in native SQL [5]; Databricks builds a per-table statistical model of commits and row counts [6] | Agents suggest rules from profiling and write SQL or descriptions; Ataccama agent "suggests, you review" [7]; Informatica agent profiles, recommends and cleanses (page 403, from search snippet only) [8] | Pushdown to the warehouse (Collibra) [5]; scan and table-level models (Databricks) [6] | Review changes lists "everything the AI Agent did" and lets you revert [7]; dismiss as "Not an issue" [6] | Some actions pause for approval; the page does not say which [7]; reference data edits staged as draft [7] | Databricks: none beyond history windows [6] | Ataccama review list [7]; Databricks audit log system table [9] | Docs are candid about limits (views unsupported, training period up to two weeks) [6]; no accuracy claims anywhere |
| Agentic migration and ingestion (Datafold, Matillion Maia, Flatfile) | Row-level data diff is the parity check [10]; Matillion pipelines are versioned code in Git (blog, UNVERIFIED) | LLM translates code and loops on compile errors and diffs [11]; Flatfile ML proposes field and enum mappings [12] | Datafold diff over "billions of rows" is asserted with no benchmark [10] | Diff report at dataset, column and row level [11] | Maia asks permission before running, committing and applying fixes; plan mode shows a step plan first (docs page via search snippet) [13] | Flatfile remembers past mappings and suggests them again; ML suggests only 1:1 "assign" rules [12] | Datafold "audit log of validation runs", contents unspecified [10] | Medium: Datafold "over 6x faster" and "guaranteed" have no baseline or method [11] |
| Platform-level agent frameworks (Palantir AIP, Anthropic finance agents) | The Ontology carries typed objects and actions; every action is permissioned and logged [14] | Agent creates proposals; they are staged for review before the Action runs [15] | Not described | Proposals tab with an Action preview and an agent decision log [15] | Accept executes the Action; open proposals visible 24 h, only to the automation's creator [15] | Docs suggest writing back the human decision for later learning [15] | "Every action taken by human users or AI agents" logged [14]; Anthropic Managed Agents: full audit log of tool calls [16] | Anthropic page does not say which close steps are code and which are model; 64.37% is a model-level benchmark, not an agent measure [16] |

## 2. Recurring patterns

1. **Rules first, model second.** Matching, rule execution, diffing and totals run as code. The model writes rules, maps, suggests and explains. [5][10][11]
2. **Confidence tiers.** Auto-merge or auto-match only on a perfect rule hit; near misses are "potential" or "suggested" and go to a human. [1][4]
3. **Exceptions are grouped before a person sees them.** Residual breaks are routed to queues, with workflow, labels and assignment, instead of shown row by row. (Duco and BlackLine detail is third-party or marketing, UNVERIFIED.)
4. **Propose, stage, approve, apply.** Agent output is a draft, a proposal, a plan or a staged edit; a separate step publishes or executes it. [7][13][15]
5. **Review list with revert.** One screen lists everything the agent did and allows undo. [7]
6. **Pushdown and set-based compute.** Checks run where the data lives, in SQL or the warehouse, not in the agent. [5][6]
7. **Independent parity check on the output.** Row-level diff between source and target, not model self-assessment. Caveat: if the same agent loops until its own diff passes, the check is circular. [10]
8. **Learn from decisions, with memory.** Reuse past mappings and stewards' choices as suggestions; unreviewed learning is rarely documented. [12][3]
9. **Statistical baselines for volume anomalies.** Per-table history models with a training period and a "not an issue" dismissal. [6]
10. **Every action logged and permissioned; evaluation harness.** Platform logging of human and agent actions; AIP Evals compares LLMs and variance across runs. [14]

Not found at any primary source: published precision/recall of auto-match, false-merge rates, independent throughput benchmarks, or tamper-evident (hash-chained) audit. Treat immutability as an unmet industry norm, not a standard.

## 3. Mapping to onboarding_worker

| # | Pattern | Status | Evidence in repo |
|---|---|---|---|
| 1 | Rules first, model second | Have | `worker.resolve` (look up first, model only for leftover distinct keys, disagreement goes to the human), `worker.setops`, `worker.accuracy`, `worker.recon`; never-invent rule |
| 2 | Confidence tiers | Partly | `worker.accounts` L0/L1 apply, L3/L4 suggest, L5 top five; `Stage.match_existing` probable merge is an open conflict. Gap: no tolerance or one-to-many matching between upload and client side in `worker.recon` (group-and-sum checks only) |
| 3 | Group exceptions by cause | Have | `worker.quality`: rows failing the same rule on the same field are one cause, ranked by impact, with locator and Excel filter (ticket 28) |
| 4 | Propose, stage, approve, apply | Have | `write_upload` and memory writes gated by `interrupt_on`; `agent.check_approval` refuses ERR and unexplained WARN in code; hash-bound approvals in `worker.web` |
| 5 | Review list with revert | Partly | `ImprovementStore.rollback` and trail CSV exist; no one-screen "everything the agent did, undo item" for a single job (UNVERIFIED that console has it) |
| 6 | Pushdown, set-based | Have | `worker.columnar` (Parquet cache), `worker.setops` (DuckDB), 1M-row GL load test |
| 7 | Independent parity check | Partly | `accuracy.conservation`, `control_totals`, `paired_values` are code and independent of the model. Gap: the checks compare staged against written, both produced by the same pipeline; there is no second independent derivation (for example a differently coded re-sum from the raw source) |
| 8 | Learn from decisions | Have | `worker.feedback`, `worker.learnindex` (exact key, value-free steps only, no sponsor names), `worker.recipes`, `worker.improver` with replay gates and named promotion |
| 9 | Statistical baselines | Gap | No cross-job volume or distribution baseline per sponsor and record type ("this file has 40 percent fewer rows than the last"). `worker.metrics` measures latency and overrides, not data drift |
| 10 | Log all, evaluate | Have | `worker.audit` hash chain with `verify`, `trail`, `replay`, `slowloop` noise gate, `metrics`; scripted model for tests |

Where onboarding_worker is stricter than every product found: hash-chained append-only audit that blocks the action if it cannot be written, gates that the agent cannot approve, answer-key isolation, and replay with a held-out set before a rule change is promoted. No vendor page documents any of these.

## 4. Prioritised gap list (fits the rules)

Each item is code-decided, model-optional, gate-compatible, and adds no code to skills.

1. **Independent second computation for control totals.** A separate path (DuckDB sum straight over the cached Parquet of the source, Decimal-exact) compared against the pipeline's staged and written totals. It closes the "circular validation" risk in pattern 7. Small, goes in `worker.accuracy`.
2. **Tolerance-aware matching engine for GL reconciliation.** One-to-one, many-to-one and many-to-many with amount and date tolerances, in SQL, with tiers: exact (auto), within tolerance (suggest, gated), residual (cause group). Extends `worker.recon`. Largest gap against the reconciliation class, and the most relevant to 100,000+ record issues.
3. **Cross-job drift baseline.** Row count, null share, distinct-key count and total per sponsor and record type compared with the last approved job; report as a WARN finding with a reason box. Uses `worker.metrics` and `worker.review` as is.
4. **Per-job "what the agent did" list with item revert.** Render the audit records of one run as a list with a revert-to-previous-value action that is itself audited and gated.
5. **Approve-by-exception thresholds.** Let an admin set a per-sponsor limit (for example, auto-acknowledge a WARN cause below N rows or under a value) as a versioned control, never touched by the improver. The `improver` write guard already refuses controls; this adds a human-set control that the gate reads.
6. **Confidence calibration record.** For each tier (L0 to L5, match tiers), count suggestions accepted versus overridden in `worker.feedback` and show the rate per tier. This replaces vendor-style "over 90%" claims with the firm's own measured number; no vendor in this survey published one.
7. **External anchor of the audit chain head.** Periodic export of the chain head hash to a location the app cannot rewrite. Optional, since the hash chain already detects edits but not truncation plus re-chaining. UNVERIFIED need; confirm with the security owner.

Not recommended: LLM-based match or merge decisions (Reltio FERN, SmartStream Smart Agents style). They conflict with "code decides"; the evidence base for their accuracy is not public.

## Sources

[1] BlackLine Transaction Matching Module Overview, https://pages.blackline.com/rs/blacklinesystems/images/BlackLine-Transaction-Matching-Module-Overview.pdf (PDF unreadable here; claims taken from the search snippet of the vendor page, UNVERIFIED)
[2] SmartStream, Smart Reconciliations: Agentic Matching and Control, https://smart.stream/resources/ai-in-data-validation-and-reconciliations-part-2
[3] SmartStream, AI-enabled exceptions management press release, https://smartstream-stp.com/es/press/smartstream-launches-ai-enabled-technology-for-exceptions-management
[4] Reltio, Review potential matches, https://docs.reltio.com/en/objectives/resolve-potential-matches/potential-matching-at-a-glance/potential-matching-operation/review-potential-matches-overview
[5] Collibra, rule workbench and pushdown, https://productresources.collibra.com/docs/collibra/dqc/latest/Content/DataQuality/DQCoreComponents/Rules/ta_rule-workbench.htm
[6] Databricks, Anomaly detection, https://docs.databricks.com/aws/en/data-governance/unity-catalog/data-quality-monitoring/anomaly-detection/
[7] Ataccama, AI Agent Handbook, https://docs.ataccama.com/ataccama-one-agentic/latest/gen-ai/ai-agent-handbook.html
[8] Informatica, Data quality agent, https://docs.informatica.com/claire/data-management-agents/current-version/data-quality-agent/data-quality-agent.html (403 on fetch; search snippet only)
[9] Databricks, What is Unity Catalog, https://docs.databricks.com/aws/en/data-governance/unity-catalog/ (audit and lineage mentioned; retention and immutability not covered)
[10] Datafold, validation guide, https://www.datafold.com/data-migration-guide/validate-prove-parity/
[11] Datafold Migration Agent, https://docs.datafold.com/data-migration-automation/datafold-migration-agent
[12] Flatfile, mapping concepts (legacy docs), https://flatfile.com/docs/legacy-docs/concepts/mapping
[13] Matillion, Maia docs, https://docs.matillion.com/metl/docs/maia-features/ (approval behaviour from the search summary, not re-fetched)
[14] Palantir, AIP architecture, https://www.palantir.com/docs/foundry/architecture-center/aip-architecture
[15] Palantir, AIP Logic automate, https://www.palantir.com/docs/foundry/logic/aip-logic-integration-automate (staged proposals; search snippet)
[16] Anthropic, Agents for financial services, https://www.anthropic.com/news/finance-agents
