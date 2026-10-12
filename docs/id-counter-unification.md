# One ID counter per (sponsor, record type)

Before: the console kept `S["ctr"]` in its job state; the agent path kept a file or `id_counters` row through
`worker.idgen`. A sponsor worked in both paths had two counters and could be issued the same ID twice.

After: `worker.idgen` owns the one counter (`IdScheme` in files, `SqlIdScheme` in PostgreSQL/SQLite). The console reads
and moves it through `Console.id_store(...)`, the same `persist.id_scheme` the agent's `assign_ids` and
`set_id_scheme` use. `S["ctr"]` and the console's own counter arithmetic are deleted.

## Rules (all in `idgen`)

- Next number = stored counter, never below the scheme's `start` (`idgen.next_number`). In SQL the counter also
  respects `high_water` (`held`), so a number held by a live agent reservation is not issued to the console.
- The counter moves only at `commit(reservation)`, after approval and after the upload file and its audit record
  exist (console: `Console.write_output`; agent: `_commit_approved`; engine worlds: `World.commit`).
- `commit` is a compare-and-set: it succeeds only when the counter is still where the reservation started.
  Otherwise `CounterMoved` is raised and nothing moves, so a number is never issued twice. File mode serialises the
  read-modify-write with one process-wide lock; SQL holds the row lock in one transaction. The console shows the
  refusal ("... counter moved ... run the job again") and the job stays unapproved; approving again re-reads the
  counter and issues new IDs.
- The ID-decision gate is unchanged: `idgen.decision_refusal`, a named approver, no default approver.
- One numbering loop: `Reservation.take` is used by the agent path, by the console's `issue_ids` and by the
  decision preview (their three copies of "skip numbers already in use" are gone).

## Old console state

A console state file (or database document) that still has `ctr` is opened once: each `"Sponsor|Type": n` is lifted
into the one counter with `IdScheme.lift_to(n)` (raises to `n`, never lowers), then `ctr` is dropped from the state.
Opening it again changes nothing. The demo seed (`COUNTERS_SEED`) goes in the same way.

## Behaviour you may notice

- `set_id_scheme(start=N)` now takes effect for the agent path too (it used to apply only with `reset_counter_to`);
  the console always did this. One rule: next = max(counter, start).
- Counter panels list a type once its counter is above 1 (before: once any job had been approved for it).
- Both paths on one sponsor continue each other. `test_parity` snapshots the sponsor's ID store before the console
  approves, so the agent starts from the counter the console started from.
- Two file-mode processes on one state folder are not covered by the lock (the console state file has no
  compare-and-set either); PostgreSQL is.

## Golden lock

No golden file moved (`tests/test_golden_lock.py` passes unchanged; no `CHANGES.md` row needed): every golden job
sees the same counter value as before, because the shared counter holds what `ctr` held.

## Tests

`tests/test_one_counter.py`, in file mode, SQLite and PostgreSQL (`integration`, `WORKER_TEST_PG_URL`): console and
agent issue disjoint continuing IDs; restart; rejected runs move nothing; old `ctr` taken over once and never
lowering; two reservations cannot both commit; racing threads, exactly one wins; a console job whose numbers were
taken meanwhile is refused and then gets new ones. Changed: `test_parity` (snapshot order above) and one assertion
in `test_acceptance` (`counter_of` instead of `S["ctr"]`).

## Not verified

- `test_adapter_parity_agent::test_run_job_digest_is_within_6kb_on_a_million_line_gl` and
  `test_gl_agent::test_F4_a_million_line_dirty_gl_file...` fail on the memory bound (about 650-720 MB against 600)
  on this machine. The first fails the same way on the untouched HEAD; the GL path is not touched here.
