# How premium organisations build production agentic systems, compared with this repo's design debate

Researched 2026-10-10. Every claim carries the URL of the page that owns it. Items I could not read at the primary source
are marked UNVERIFIED and listed at the end. Repo facts come from `skills/`, `src/worker/{skills,decide,improver,slowloop,recon,jobs,tools,agent}.py`.

## 1. The answer

The primary sources agree on one shape: keep the model on a short leash of reviewed, versioned artefacts; do exact work in
code the model calls; put a human gate on consequential actions; treat every change to those artefacts like a software
release (review by someone other than the author, evals in isolation and alongside the rest, pinned versions, a
checksum, a known-good version to roll back to); and log enough to rerun a past job. This repo already has most of the
skeleton. The gaps are not in the loop's mechanics but in what is versioned and who is accountable: a hand-typed skill
version, rules still living in Python (`recon.py` specs, `jobs.eval_static`), a segregation-of-duties check that compares
name strings, and no recorded "rule set in force" on a finished job.

## 2. What premium organisations actually do

| Practice | Source | What it means for this repo |
|---|---|---|
| Skills load in three levels: metadata always, body when triggered, files and scripts as needed; scripts run and only their output enters context | https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview | `skills.py` already mirrors this. Scripts being "executed, not read" is the platform's own norm, so `map-accounts/scripts/map_keys.py` is conventional, but see the next row. |
| Scripts in a skill are the highest risk tier; review every bundled file; skill authors must not be their own reviewers | https://platform.claude.com/docs/en/agents-and-tools/agent-skills/enterprise | Supports prompt engineer (c): code inside a skill needs a code-review path, not the know-how path. Improver already forbids `scripts/` (`FORBIDDEN`). |
| Pin production to a specific skill version (omitting it means "latest" silently changes production); keep the previous version; checksum reviewed skills and verify at deploy | same enterprise page | Version string + content hash together, plus a registry row with owner, version, evaluation status. |
| Require an eval suite before promotion: triggering accuracy, isolation, coexistence with other skills, instruction following, output quality; 3-5 queries minimum; test across every model you use | same enterprise page; https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices | Replay on mappings covers output quality only. Triggering and coexistence evals are absent, and `WORKER_MODEL` is configurable, so skills must be evaluated per model. |
| Build evals before writing the skill; write minimal instructions to close observed gaps; revise from observed agent behaviour | https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices | Matches "feedback -> proposed diff". Argues for a baseline run without the change. |
| Match freedom to fragility: exact script for fragile steps, prose for heuristics; plan -> validate -> execute for batch and high-stakes work | best-practices page above | Exactly the know-how vs engine split; the plan-validate-execute pattern is the repo's stage -> review -> gated `write_upload`. |
| Use the simplest solution; workflows (code paths) before agents; human checkpoints; sandboxed testing | https://www.anthropic.com/engineering/building-effective-agents | Supports "engine is code, model proposes". |
| Grade the outcome not the path; regression evals stay near 100%, capability evals start low and graduate; isolate trials; read transcripts; watch saturation | https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents | Two suites: a regression set that must not drop, and a capability set that is allowed to fail. Re-isolate each replay from state. |
| Deterministic hooks guarantee an action happens; the model cannot skip them | https://code.claude.com/docs/en/hooks-guide | `audit.record` as a hard precondition is the same idea. |
| Approvals pause the run; serialized state is not authenticated, so store server-side, authenticate reviewers, validate decision IDs, block replayed submissions | https://openai.github.io/openai-agents-python/human_in_the_loop/ | Hash-bound approvals (ticket 11) match. Check replay protection explicitly. |
| Guardrails have scope limits: input guardrails only on the first agent, output only on the last, and not on hosted or built-in tools | https://openai.github.io/openai-agents-python/guardrails/ | A gate must sit at the action (`write_upload`), not around the conversation. The repo does this. |
| `interrupt()` resumes by re-running the node from its start; side effects before it must be idempotent; checkpointer required, durable one in production | https://docs.langchain.com/oss/python/langgraph/interrupts | Audit and counter commits (`id.counter_committed`) must not run before the gate or must be idempotent. Worth a test. |
| Offline eval on curated datasets for regression; online eval for monitoring and harvesting failures; pin CI to a dataset version; splits | https://docs.langchain.com/langsmith/evaluation-concepts | Freeze the replay set per promotion and record its version. |
| ADK: schema-backed eval files, default threshold of exact tool trajectory, efficiency metrics reported but never failing, conformance tests against recorded baselines | https://adk.dev/evaluate/index.md | Repo's "no more questions or model calls" gate is stricter than ADK's; keep it. Recorded-baseline conformance is the generic `replay()` of (b). |
| Validate a new model against the production model offline, check segments not only the average, then canary or A/B before full traffic; registry with pointer to the previous version; metadata store of component versions per run | https://docs.cloud.google.com/architecture/mlops-continuous-delivery-and-automation-pipelines-in-machine-learning | Per-sponsor segments before promotion (supports b). Shadow or canary on live jobs is a step the repo does not have. |
| Automatic event logging over the system's lifetime, including who verified the result | https://artificialintelligenceact.eu/article/12/ (applies to high-risk systems from 2027-12-02; whether this system is in scope is not established) | Record the verifier's identity as an authenticated principal, not a string. |
| Independent validation, effective challenge, ongoing monitoring, change control, inventory, governance | SR 11-7 https://www.federalreserve.gov/supervisionreg/srletters/sr1107.htm and PRA SS1/23 https://www.bankofengland.co.uk/prudential-regulation/publication/2023/may/model-risk-management-principles-for-banks-ss (page lists five principles incl. "Independent model validation") | UNVERIFIED in detail: I could not fetch the SR 11-7 text. See section 5. |

## 3. Practice against the debate

### Where practice agrees
- Skill = reviewed files, engine = code, model proposes (a, b, c): building-effective-agents, skills best practices (links above).
- Pin to a version, keep a rollback, promote only after evals pass (a): enterprise page; Google MLOps page.
- Typed, schema-backed eval and rule artefacts (a: "freeze a schema per kind"): ADK states a formal schema behind eval files (https://adk.dev/evaluate/index.md via its search summary; the schema file names are UNVERIFIED).
- A content hash or checksum (b): the enterprise page asks for checksums of reviewed skills. See the refinement below.
- Per-segment checks before promotion, so a per-sponsor held-out set (b): Google MLOps page.
- Generic replay over recorded jobs (b): ADK conformance and LangSmith experiments on pinned datasets.
- Skill scripts blur the line (c): the enterprise page rates scripts "High" risk and says the author is not their own reviewer.

### Where practice disagrees or refines
- Hash instead of a typed version (b). Sources want both: a human-readable version for the registry (owner, version, status) and a checksum for integrity (enterprise page). Make the hash the identity, keep the label.
- "Skill = know-how only" (a, c). The platform's skill includes scripts, reference data and templates by design (overview page). The repo's stricter rule is a local choice; justify it as a risk-tier decision, not as how skills "are".
- "Prefer example/table_row over prose" (c). Platform guidance does favour examples over rules ("examples" pattern, best-practices page), but also warns against an untested constant (voodoo constants); a table row still needs a justification field.
- Holdout "counts only" (a). I found no primary source that prescribes it; sources prescribe a held-out split and segment checks. It is a reasonable local control, not an industry norm (UNVERIFIED as practice).
- Noise-band over 3 runs (slow loop). Anthropic frames this as pass@k / pass^k and says to choose by use-case (demystifying-evals). Pick pass^k (all runs) for gated actions.

### What the debate missed
1. **Skill-level evals beyond outcome replay**: triggering accuracy, coexistence, and per-model results (enterprise page). A new skill can steal triggers from `map-accounts`.
2. **Model change is a change**: swapping `WORKER_MODEL` alters skill behaviour; sources require testing across the models in use (best-practices, enterprise).
3. **Rule set in force per job**: Google's metadata store records component versions per run so a run can be traced and rerun. Repo audits `skill.stamp` on some tool results but not a manifest (skill hashes + table hashes + engine version + model id) on every finished job.
4. **Segregation of duties by identity**: enterprise page says authors must not self-review; repo checks `approver != proposed_by` as lowercase strings (`improver.promote`). An authenticated principal and role are needed.
5. **Resume idempotency**: LangGraph re-runs the node on resume; the repo commits counters and audit lines around the gate.
6. **Replay-protection and snapshot trust**: OpenAI notes states are not authenticated.
7. **Online side**: shadow or canary on real jobs, drift monitoring, failure harvesting into the replay set (LangSmith, Google).
8. **Eval saturation**: a replay set near 100% stops detecting change; add harder cases deliberately (demystifying-evals).

## 4. Recommended target architecture and migration

```
 human drop -> console/agent adapters (thin, no rules)
                      |
              evaluate(job, ruleset)  <- ONE engine, frozen code, no domain constants
                      ^
        ruleset = {skill@hash, tables@hash, checks@hash, engine version, model id}   (manifest, written on every job)
                      ^
 proposal -> replay(ruleset', recorded jobs + cases + per-sponsor holdout) -> ready card
          -> named human (authenticated, not the proposer) promotes -> new manifest pinned to NEW jobs; old kept
 gates: write_upload / memory write / promote  -> interrupt + audit record before effect
```

Migration order (each step keeps the 554 tests green; add tests first, change after):
1. Manifest only: compute content hashes of skills/tables/checks and write them into the job's audit record. No behaviour change.
2. Parity test: console path (`jobs.eval_static`/`eval_gl`) vs agent path (`tools.py`) on the same recorded job.
3. Lint: fail CI if the engine holds literal rule values outside tables (start as a report, then gate).
4. Skill-level evals: trigger/non-trigger cases and coexistence for each skill, per configured model.
5. Generic `replay(manifest, recorded_job)`; route `improver` and `slowloop` through it.
6. Identity: promoter is an authenticated principal with a role; reject proposer = approver by principal id.
7. Move recon specs and static rules into typed tables one kind at a time, each with a golden test that the old and new produce equal output.
8. Shadow runs of a candidate ruleset on live jobs (read-only) before promotion.

## 5. Questions the pending positions (architect reviewer, fintech engineer) should be tested against
- Can you reproduce a past job's result byte for byte under the rule version in force at that time, including the model id and prompt text?
- Who may author, who may review, who may promote a rule, and is that enforced by identity rather than a name field?
- Who validates independently of the person or system that built the change, and what is the evidence they challenged it?
- If the log write fails, does the action stop (this repo: yes) and is the log tamper-evident and retained?
- What happens when the model changes, a sponsor override changes, or an upstream chart changes: does that re-trigger validation?
- What is the rollback, and does it leave finished work untouched?
- Is a human approving something they can see (hash-bound), and can an approval be replayed?

## 6. Not verified
- SR 11-7 and OCC 2011-12 text: the Fed and OCC PDF/HTML URLs returned 404 or only redirects through my fetch tool; section-level claims (effective challenge, outcomes analysis, change control) are from search summaries only. Treat as UNVERIFIED and read the letter directly.
- PRA SS1/23 content on model change and independent validation: the page only lists principle titles; scope there is firms with internal model approval, so applicability to a fund administrator is by analogy. UNVERIFIED.
- OpenAI "A practical guide to building agents": the PDF fetched as binary; I could not read the guardrail and risk-rating passages. UNVERIFIED.
- Anthropic Agent SDK permission and hook audit specifics, ADK evalset schema file names, and Google agent whitepapers: not read in full.
- EU AI Act applicability to this system is not established; Article 12 is cited only as an example of log content.
- No source found for "holdout counts only", "slow loop >= 3 runs", or a content hash as skill identity; these are repo decisions.
