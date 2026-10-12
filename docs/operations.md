# Operations: running, backing up and restoring the worker

This is the runbook for the production-shaped stack in `docker-compose.yml`: the app and PostgreSQL (the system of
record), a migrate job, an audit verifier, an anchor writer, and optionally Neo4j. Without `DATABASE_URL` the
product runs on files under `WORKER_STATE_DIR` exactly as before (tests, the demo, a laptop).

## What lives where

| State | Where, with `DATABASE_URL` set | Where, without |
|---|---|---|
| Audit logs (jobs, engine, harness, feedback, recipe history, replay, models) | PostgreSQL `audit_log`, one chained stream per former file (the path under the state folder) | `*.jsonl` files |
| Existing records, ID counters and reservations, account mappings, Golden versions | PostgreSQL | files |
| Sponsor memory, recipes, improvement cards, decision-table versions | PostgreSQL | files |
| Console document (`console.json`) | PostgreSQL `console_state`, compare-and-set on a version | file, replaced atomically |
| LangGraph checkpoints (a paused gate) | PostgreSQL (`PostgresSaver`) | `checkpoints.sqlite` |
| Engine store and spool (`db.backend.Backend.store`) | PostgreSQL `store_chunks` | `<state>/engine` |
| Source workbooks (inbox), upload files, replay blobs, staged tables, the agent path's reservation ledger and `job_state.json` | the `worker-data` volume (`/data`) | the state folder |
| Skills and templates | the image (from git) | the repository |
| Neo4j (only with `WORKER_GRAPH=on`) | derived from PostgreSQL; never authoritative | not used |

## First start

```bash
python scripts/init_secrets.py        # writes ./secrets/*.txt (git-ignored): passwords and the two database URLs
cp .env.example .env                  # set WORKER_MODEL_PROVIDER, the model key, and WORKER_APPROVER_TOKEN
docker compose up -d --build
curl -s localhost:8765/readyz         # 200 {"ok": true, ...} when the stack is ready
```

Order is enforced: `postgres` healthy, then `migrate` runs to completion (schema, triggers, grants, the application
role's password, the checkpoint tables), then `worker`, `verifier` and `anchor` start. The worker refuses to start
without `WORKER_APPROVER_TOKEN`. The console is published on `127.0.0.1:8765` only; put a reverse proxy with TLS in
front of it and block `/metrics` there. Authentication is still the shared token plus a self-declared reviewer name:
real single sign-on is not built (ticket 07).

### Roles

| Role | Used by | Can |
|---|---|---|
| `ow_owner` (superuser in the bundled compose file) | `migrate` job only | create and change the schema |
| `ow_app` | worker, verifier, anchor | SELECT and INSERT on `audit_log`; SELECT, INSERT, UPDATE, DELETE on state tables; nothing else |

For stricter separation, give the verifier and the anchor their own read-only roles and credentials; the compose file
uses the application role for all three to stay simple.

## How audit immutability works, and where it stops

Layers, in the order they stop an attempt:

1. **Privileges.** `ow_app` has only INSERT and SELECT on `audit_log`; UPDATE, DELETE and TRUNCATE are revoked.
2. **Triggers that raise.** UPDATE, DELETE and TRUNCATE are refused for every role, the owner included. A BEFORE
   INSERT trigger re-hashes the stored text and checks the link to the previous row, so a broken chain cannot be
   inserted. Event triggers refuse `DROP TABLE audit_log` and any `ALTER TABLE` that leaves an audit trigger disabled.
3. **Audit before effect.** Every state table a decision changes refuses a write unless an audit row exists in the
   same transaction. Effects outside the database (an upload file) are written only after their audit record is.
4. **Verification.** `verifier` walks every stream from its stored text (incrementally often, in full now and then)
   and appends its result to its own chained stream. A broken chain makes `/readyz` answer 503.
5. **Anchors.** `anchor` writes the head of every stream to a create-only sink every 15 minutes
   (`WORKER_ANCHOR_INTERVAL_SECONDS`). Anchors chain to each other. The verifier compares them with the database:
   a different hash or a missing row inside a stream is `history_rewritten`; a stream that ends before an anchored
   entry is `behind_anchor`.
6. **No deletes.** Nothing in the product deletes an audit row or an anchor.

Outside the guarantee, stated plainly:

* A **database superuser, or root on the database host,** can switch triggers off. A rewrite of anchored records is
  detected at the next verifier run (and `/readyz` goes 503); changes made after the last anchor, within the anchor
  interval, are not detected.
* The **anchor directory** in the bundled compose file is a local volume on the same host: that is a convenience,
  not a guarantee. Mount an append-only location the database host cannot write (another host's `chattr +a`
  directory, a bucket with Object Lock in COMPLIANCE mode behind a mounted path). An object-store sink needs only
  the three methods of `AnchorSink` (`put`, `names`, `read`); none is shipped.
* An application that writes a **false but well-formed record** is not caught by immutability; the gates and a
  person's approval are the controls for that. The `X-Reviewer` name is self-declared.
* The `files` backend (no `DATABASE_URL`) is tamper-evident only.

Commands (all use `DATABASE_URL` or `DATABASE_URL_FILE`):

```bash
onboarding-worker db migrate                                     # apply migrations (owner credentials)
onboarding-worker db verify --anchor-dir /anchors                # exit 1 and name the stream and seq on a problem
onboarding-worker db verify --follow --anchor-dir /anchors       # what the verifier service runs
onboarding-worker db anchor --anchor-dir /anchors [--follow]     # write an anchor now / every interval
```

## Health, logs and metrics

* `/livez`: the process answers. Used by the container health check; no dependency is checked.
* `/readyz`: 200 only when the service is not draining, the state folder is writable, the skills load valid, no
  accepted decision has been waiting more than two minutes, and with a database: reachable, migrated to this build's
  revision, and the last verification did not fail. Each failing check is named. Neo4j is not part of readiness.
* `/metrics`: Prometheus text (requests and latency by route, runs by status, gates waiting, accepted decisions not
  yet executed, draining). Not for the public: block it at the proxy.
* Logs are one JSON object per line on stdout (`WORKER_LOG_FORMAT=json`), with `request_id` (the `X-Request-ID`
  header is echoed). Fields named like a secret are replaced. Cell values, files and prompts are never logged.

## Shutdown and accepted approvals

An approval is written to `runs/<run>/approvals/NNNN.json` on the volume **before** the person is told it was
accepted. On SIGTERM the service stops taking work (`503`, `/readyz` fails), waits up to `WORKER_DRAIN_SECONDS`
(45; the compose `stop_grace_period` is 60s) for what was accepted, then stops. At the next start any accepted
decision that never executed runs once, exactly as decided; if its gate is no longer pending the person is told in
the run and nothing is guessed. A `kill -9` between the write and the commit of an upload is the one case left to
the audit trail: `write_upload` is on record, so check it and `world.committed` before deciding again.

## The graph toggle

`WORKER_GRAPH=off` (default): lineage and account questions are answered from the SQL views `id_lineage_v` and
`account_current_v` (`onboarding-worker graph lineage --sponsor <slug>`). `on`: from Neo4j, a projection of the same
rows. To use it: set `WORKER_GRAPH=on` and `NEO4J_PASSWORD` in `.env`, then `docker compose --profile graph up -d`.

```bash
onboarding-worker graph rebuild        # wipe and project again from PostgreSQL
onboarding-worker graph fingerprint    # graph vs the SQL views; exit 1 when they differ
```

Neo4j is disposable: rebuild it, never restore it. It is not backed up.

## Backup and restore

What to protect: the PostgreSQL database (everything decided), the `worker-data` volume (source workbooks, upload
files, replay blobs), and the anchors.

```bash
scripts/backup.sh [folder]             # dump + a fresh anchor + files and anchors archives + SHA256SUMS
```

Order matters: the dump first, the files archive after it, so a row never refers to a file that is missing.
Blobs are content-addressed and append-only, so a blob newer than the dump does no harm. Copy the three files off
the host. Keep 14 daily, 8 weekly and 12 monthly. The recovery point is the time of the last dump (24 hours with a
nightly cron); WAL archiving for point-in-time recovery is designed, not built.

### Restore drill (monthly, and before relying on a backup)

```bash
scripts/restore_drill.sh backups/onboarding-<stamp>.dump
```

It restores into a scratch database, prints the schema revision and the counts, re-verifies every chain and compares
them with the anchors, then drops the scratch database. A restored dump that was altered fails as
`history_rewritten`. A dump older than the latest anchor is reported as "ends before the latest anchor" (the work
since the backup) and passes. The live database is never touched.

### Real restore

1. `docker compose stop worker verifier anchor`.
2. Restore: `docker compose exec -T postgres pg_restore -U ow_owner -d onboarding --clean --if-exists --no-owner < dump`
   (or restore into a fresh volume).
3. `docker compose run --rm migrate` (idempotent; nothing pending when the dump is at this build's revision).
4. Restore the files archive into the `worker-data` volume.
5. `docker compose run --rm --no-deps verifier onboarding-worker db verify --anchor-dir /anchors --restore-drill`:
   the chains must verify; the output says how many entries the backup lacks against the latest anchor.
6. `docker compose up -d`, then check `/readyz`.

Do not hand-edit the audit tables to "repair" a finding: the verifier and the anchors exist so that a break is
reported, not hidden. A real finding is an incident: keep the database and the anchors as they are and investigate.

## Going back to files

`DATABASE_URL` unset means files. There is no automatic export of the database to the file layout (not built): a
switch back loses what only the database holds. Treat the move to the database as one-way until `db export` exists.

## Upgrades

Migrations are forward only and applied by the `migrate` job before the new `worker` starts (`depends_on`). Take a
backup first. A build refuses readiness when the database is not at its revision.

## Not built (stated, so nobody assumes)

* Moving an existing file-based state folder into the database (`db import`) and the reverse (`db export`).
* Yearly partitioning and archival of `audit_log`, an object-store anchor sink, point-in-time recovery.
* PgBouncer, replicas, high availability; one app replica only.
* Real authentication (single sign-on, verified principals, session expiry).
* Row-level security; per-service database roles beyond owner and application.
* The agent path's reservation ledger and `job_state.json`, replay records and blobs, and staged tables still live
  on the volume even with a database.
