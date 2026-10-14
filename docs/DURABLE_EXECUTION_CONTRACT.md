# Durable Agent Execution & Recovery Contract

**Contract version:** v1
**Status:** Accepted (design) with a reference implementation. This document is
the source of truth for the *semantics* of durable agent execution in the open
core. The seam it describes lives in `backend/agents/execution/`; the executable
form of this contract is `backend/agents/execution/contract.py`
(`ExecutionStoreContractTests`), which every adapter must pass.
**Scope:** Open-source core only. Multi-tenant / workspace isolation, per-tenant
concurrency and retention policy, and the choice of a durable storage technology
are **out of scope** here — see [§9 Public/private boundary](#9-publicprivate-boundary).

This contract complements [`EVENT_SUBSCRIPTIONS.md`](EVENT_SUBSCRIPTIONS.md)
(which owns event matching and webhook delivery) and
[`AGENT_SCHEDULING.md`](AGENT_SCHEDULING.md) (which owns the time-based trigger
model). Those documents describe *what produces* work; this one describes *how
that work is queued, executed exactly-enough-once, and recovered after a
restart*.

---

## 1. Motivation

Today the agent runtime is volatile. `AgentScheduler` deduplicates fired
schedules in an in-memory map, and each `AgentWorker` consumes an in-memory
`queue.Queue`. A process restart therefore:

- loses every queued and in-flight event,
- forgets which schedules already fired this minute (risking a double-fire or a
  silent miss), and
- keeps no durable record of what ran, whether it succeeded, or why it failed.

There is no retry, no backoff, no dead-letter path, and no idempotency guard.
`EVENT_SUBSCRIPTIONS.md` names these limitations explicitly ("in-memory queue;
events are not persisted; lost on restart").

Before scheduled and graph-triggered agents can be depended on, the core needs a
**replaceable durable execution seam** so that queued events, agent jobs,
retries, dead-letter state, idempotency, recovery and correlation survive a
restart. Reliability of this kind belongs in the reusable core; hosted layers
add tenant-aware adapters on top of the same seam.

This document defines that seam. It does **not** wire it into the scheduler and
worker, choose a durable storage technology, or add run-history APIs/UI — those
are separate, sequenced slices that build on this contract.

## 2. Vocabulary

| Term | Meaning |
|---|---|
| **Job** | One durable unit of agent work (`ExecutionJob`). Created by a producer, claimed by a worker, driven to a terminal state. |
| **Producer** | The component that enqueues jobs: the scheduler (time trigger) or the event router (graph mutation / subscription). |
| **Worker** | The component that claims a job, executes the agent turn, and reports the outcome. |
| **ExecutionStore** | The replaceable persistence seam that holds jobs and enforces the state machine. |
| **Adapter** | A concrete `ExecutionStore` implementation (reference in-memory, durable local, hosted). |
| **Lease** | A time-bounded claim a worker holds on a RUNNING job; the basis for restart recovery. |
| **Idempotency key** | A producer-chosen key that makes `enqueue` safe to call more than once for the same intended occurrence. |

## 3. The job resource

`ExecutionJob` (`backend/agents/execution/models.py`) carries the following
fields. All of them MUST survive a restart when persisted by a durable adapter;
`to_dict` / `from_dict` define the serialization a durable adapter round-trips.

| Field | Purpose |
|---|---|
| `id` | Stable unique job id (`job-<uuid>`). |
| `agent_id` | The agent the job runs. |
| `kind` | `scheduled` or `event` (`ExecutionKind`). |
| `idempotency_key` | Deduplication key for `enqueue` (see §5). |
| `payload` | The trigger payload handed to the worker (e.g. the event envelope). |
| `state` | Lifecycle state (`ExecutionState`, see §4). |
| `attempts` | Number of times the job has been claimed for execution. |
| `run_at` | Earliest time the job is eligible to run; used for scheduled delay and retry backoff. |
| `lease_owner`, `lease_expiry` | The worker holding the job and until when (see §6). |
| `correlation_id`, `session_id`, `origin` | Provenance, mirroring the event-context vocabulary in `EVENT_SUBSCRIPTIONS.md`. |
| `last_error`, `dead_letter_reason` | Failure diagnostics. |
| `result` | Small terminal result summary. |
| `created_at`, `updated_at`, `started_at`, `finished_at` | Timestamps. |

## 4. Lifecycle state machine

```
                       claim_next
        enqueue        (attempt++)
   ───────────────▶ PENDING ─────────────▶ RUNNING
                      ▲  │                   │  │  │
        fail (budget  │  │ fail (budget      │  │  │ complete
        remains,      │  │ exhausted)        │  │  ▼
        backoff)      │  │                   │  │ SUCCEEDED  (terminal)
                      │  ▼                   │  │
                      │ DEAD_LETTER ◀────────┘  │ fail (budget exhausted)
                      │ (terminal)              │   / claim past budget
   recover_stale /    │                         │
   claim reclaim ─────┘                         │ cancel
   (expired lease)                              ▼
                                            CANCELLED  (terminal)
```

- **Non-terminal:** `PENDING` (queued, eligible once `run_at` has passed),
  `RUNNING` (claimed, holding a lease).
- **Terminal:** `SUCCEEDED`, `DEAD_LETTER`, `CANCELLED`.
- **Terminal states are sticky.** No operation moves a job out of a terminal
  state. A `complete`/`fail` call that arrives after `cancel` is a no-op —
  cancellation wins over a late completion.

## 5. Idempotency

`enqueue(job)` is idempotent on `idempotency_key`: if a job with that key already
exists (in **any** state, terminal or not), the store returns the existing job
and creates nothing new.

Producers therefore scope the key to the *intended occurrence*:

- **Scheduled** jobs key on agent + fire minute (e.g.
  `sched:<agent_id>:<YYYY-MM-DDTHH:MM>`), so a restart mid-minute cannot fire the
  same schedule twice.
- **Event** jobs key on the source event id, so re-delivery of the same graph
  mutation does not enqueue duplicate work.

This moves the scheduler's current in-memory minute-dedup into durable state,
where it survives a restart.

## 6. Execution, leasing and at-least-once delivery

- `claim_next(worker_id, now, lease_seconds)` atomically selects the oldest
  runnable job, transitions it to `RUNNING`, increments `attempts`, and grants a
  lease until `now + lease_seconds`. Only one worker holds a **valid** lease on a
  job at a time — `claim_next` will not hand out a job whose lease is still live.
- A job is **runnable** when it is `PENDING` with `run_at <= now`, **or**
  `RUNNING` with an expired lease (reclaimed after a crash — see §7).
- A worker that runs longer than its lease calls `renew_lease` to keep it.
- On success the worker calls `complete`; on failure, `fail`.
- `complete`/`fail` act by **job id and are not lease-guarded**: the first
  terminal write wins, and the sticky-terminal rule then makes every later write
  a no-op. If a worker's lease expired and another reclaimed the job, whichever
  worker reaches a terminal state first decides the outcome; the other's result
  is dropped. This is consistent with at-least-once delivery (a reclaimed job may
  run more than once) and keeps the core seam simple. An adapter that needs the
  reclaiming worker's result to win — rejecting a stale worker's completion —
  may add a lease-ownership check without changing this contract.

Delivery is **at-least-once**: a worker that crashes after claiming but before
completing will have its job reclaimed and retried. Agent actions that write to
the graph should remain idempotent (the event-context / `ignore_origins`
mechanism in `EVENT_SUBSCRIPTIONS.md` already supports this).

## 7. Retry, backoff, dead-letter and recovery

- **Retry budget.** A job is attempted at most `RetryPolicy.max_attempts` times.
- **Backoff.** After `n` failed attempts, `fail` reschedules the job as `PENDING`
  with `run_at = now + RetryPolicy.backoff_seconds(n)`, a deterministic capped
  exponential (`min(base * factor^(n-1), max)`, no jitter — jitter, if ever
  needed, belongs in a hosted adapter, not the core contract).
- **Dead-letter.** When the budget is exhausted, `fail` moves the job to
  `DEAD_LETTER` with a `dead_letter_reason`. Dead-lettered jobs are retained for
  inspection, never retried automatically.
- **Restart recovery.** A job left `RUNNING` by a crashed worker is reclaimed
  once its lease expires: `recover_stale` resets expired-lease jobs to `PENDING`
  as an explicit startup sweep, and `claim_next` also reclaims them on demand.
  Each reclaim counts as an attempt, so a job that crashes the process every time
  is dead-lettered once the budget is spent rather than looping forever.
- **Cancellation.** `cancel` moves any non-terminal job to `CANCELLED`.

## 8. The `ExecutionStore` seam

`ExecutionStore` (`backend/agents/execution/store.py`) is a
`@runtime_checkable Protocol`, matching the existing persistence-seam style
(`GraphPersistenceBackend`). Adapters implement it structurally; no inheritance
is required.

| Method | Contract |
|---|---|
| `enqueue(job)` | Idempotent on `idempotency_key`; returns the stored job. |
| `claim_next(worker_id, now, lease_seconds)` | Claim oldest runnable job → `RUNNING`; reclaim expired leases; dead-letter a claim past budget; else `None`. |
| `renew_lease(job_id, worker_id, now, lease_seconds)` | Extend a lease held by `worker_id`; `False` otherwise. |
| `complete(job_id, result, now)` | → `SUCCEEDED`; no-op if already terminal. |
| `fail(job_id, error, now)` | Retry with backoff or dead-letter; no-op if already terminal. |
| `cancel(job_id, result, now)` | → `CANCELLED` if non-terminal; `True`/`False`. `result` is stored like `complete`'s, for a caller that can explain the cancellation. |
| `recover_stale(now)` | Reset expired-lease `RUNNING` jobs to `PENDING`; return them. |
| `get(job_id)` | Fetch by id, or `None`. |
| `list_jobs(states, agent_id, kind, limit, created_after)` | Inspection surface, newest-first; the basis for durable run history. `created_after` keeps jobs created strictly after it. |
| `list_terminal_older_than(cutoff, redacted)` | Terminal jobs whose `finished_at` (else `updated_at`) is before `cutoff`, oldest first; optionally only redacted / unredacted ones. |
| `redact(job_ids, now)` | Strip each terminal, unredacted job's content (§8a) and stamp `redacted_at`; returns the count. |
| `purge(job_ids)` | Delete each terminal job; returns the count. |

All `now` parameters default to the current UTC time; passing an explicit value
keeps adapter behaviour deterministic under test.

### Reference adapter

`InMemoryExecutionStore` (`backend/agents/execution/memory_store.py`) is the
reference, thread-safe implementation — the *null / volatile* adapter, analogous
to `FileGraphPersistenceBackend` for graph persistence. It proves the contract
is implementable and serves tests and standalone runs. **It is not durable** —
its state does not survive a restart.

### Durable local adapter

`SqliteExecutionStore` (`backend/agents/execution/sqlite_store.py`) is the
default **durable** local adapter, backed by SQLite via the stdlib `sqlite3`
module per [ADR 0001](adr/0001-local-durable-execution-store.md). Reopening the
same database file after a restart recovers every job and its state. It holds
`idempotency_key` under a `UNIQUE` constraint, runs `claim_next` inside a
`BEGIN IMMEDIATE` transaction, and tracks its schema with `PRAGMA user_version`.
It implements the same methods as every other adapter and passes the same
`ExecutionStoreContractTests`, plus restart-durability tests the volatile
adapter cannot satisfy.

Hosted adapters implement the same methods with their own persistence and are
held to the same contract. Wiring the scheduler/worker to run live agent work
through a store (rather than the current in-memory queue) is a separate,
later slice.

## 8a. Retention, redaction and purge

Terminal jobs can be aged out. The store operations are `list_terminal_older_than`,
`redact` and `purge` (§8); every adapter implements them and the shared contract
tests exercise them.

- **Only terminal jobs.** `redact` and `purge` skip `PENDING` and `RUNNING` jobs
  (and unknown ids) without error. A job's age is measured from `finished_at`,
  falling back to `updated_at`. A run history record left `RUNNING` by a crashed
  process therefore keeps its content until something moves it to a terminal
  state; the read window still hides it once it is old enough.
- **Redaction** (`models.redact_job`, shared by all adapters) keeps `state`,
  every timestamp, `id`, `agent_id`, `kind`, `idempotency_key`,
  `correlation_id`, `attempts` and `dead_letter_reason`. It reduces
  `payload.event` to `{event_type, entity: {kind, id, type}}` (dropping the
  entity data and the event's actor attribution), drops every other payload key
  except `agent_name`, and keeps those identifiers only as short strings or
  integers. In `result` it nulls `summary` and every non-boolean, non-numeric
  value. It nulls `last_error`, `session_id` and `origin`, and stamps
  `redacted_at`. Redacting an already redacted job is a no-op.
- **Purge drops idempotency memory.** A purged job's `idempotency_key` is
  forgotten: a later `enqueue` with that key creates a new job (§5 holds only
  while the job exists). Retention therefore must not be shorter than the window
  in which a producer may re-deliver the same occurrence; the floor below keeps
  it to at least a week.

The durable SQLite adapter adds `redacted_at` in schema version 2, an additive,
forward-only migration of a version-1 file, run once inside a write transaction
even when several processes open the file together. It enables
`secure_delete` and checkpoints the WAL after a redact or purge that changed
rows, so the removed content does not linger in free pages or the `-wal` file.
Copies taken before a sweep (backups, disk snapshots) are outside its reach.

### Run-history retention policy

Durable AgentRun history applies these operations through
`backend/agents/run_history_retention.py`, configured by environment:

| Variable | Effect |
|---|---|
| `AGENTS_RUN_HISTORY_RETENTION_DAYS` | Purge terminal runs older than this. Minimum 7 days; a smaller value is raised to 7. |
| `AGENTS_RUN_HISTORY_REDACT_AFTER_DAYS` | Redact terminal runs older than this. Clamped to the retention, so redaction precedes or coincides with purge. |
| `AGENTS_RUN_HISTORY_EXPORT_MAX_DAYS` | How far back `GET /agents/runs` reads (by `created_at`). Clamped to, and defaulting to, the retention. |
| `AGENTS_RUN_HISTORY_SWEEP_INTERVAL_HOURS` | Hours between sweeps (default 24). |

With none set, history is kept forever and nothing is redacted or purged. A
value that is not an integer from 1 to 36500 (1 to 87600 for the interval)
fails startup; leave a window unset rather than very large to keep history
forever. The sweep runs when the
agent registry starts (also when agents are disabled, since it governs data
already at rest) and then every interval; a failing sweep is logged and never
stops the runtime. `python -m backend.agents.run_history_retention [--dry-run]`
runs one sweep against `AGENTS_RUN_HISTORY_DB` by hand. Each sweep logs one
audit record (logger `backend.agents.run_history_retention.audit`) holding only
the policy, the cutoffs and the redacted/purged counts — no run ids or content.
`GET /agents/runs/retention-policy` returns the effective policy and the last
sweep's record.

## 9. Public/private boundary

This contract covers only generic core reliability. It deliberately excludes:

- **Tenant / workspace isolation** and per-tenant concurrency, quotas and
  retention policy.
- **The durable storage technology** (local file/SQL, hosted database) and its
  migration posture. The *default local* choice is recorded in
  [ADR 0001](adr/0001-local-durable-execution-store.md) (SQLite via stdlib
  `sqlite3`); hosted storage technologies remain a hosted-layer concern.
- **Run-history APIs and UI**, and **approval / governance** semantics.
- Any business-specific scheduling, prioritisation or fairness policy.

Those are hosted-layer or later-slice concerns. The seam defined here is what
they build on: a hosted adapter binds durable, workspace-scoped persistence
behind the same `ExecutionStore` interface without changing the producers,
workers, or this contract.
