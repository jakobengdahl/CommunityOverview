# Durable Agent Execution & Recovery Contract

**Contract version:** v1
**Status:** Accepted (design) with a reference implementation. This document is
the source of truth for the *semantics* of durable agent execution in the open
core. The seam it describes lives in `backend/agents/execution/`; the executable
form of this contract is `backend/agents/execution/contract.py`
(`ExecutionStoreContractTests`), which every adapter must pass.
**Scope:** Open-source core only. Multi-tenant / workspace isolation, per-tenant
concurrency and retention policy, and the choice of a durable storage technology
are **out of scope** here — see [§8 Public/private boundary](#8-publicprivate-boundary).

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
| `cancel(job_id, now)` | → `CANCELLED` if non-terminal; `True`/`False`. |
| `recover_stale(now)` | Reset expired-lease `RUNNING` jobs to `PENDING`; return them. |
| `get(job_id)` | Fetch by id, or `None`. |
| `list_jobs(states, agent_id, kind, limit)` | Inspection surface, newest-first; the basis for durable run history. |

All `now` parameters default to the current UTC time; passing an explicit value
keeps adapter behaviour deterministic under test.

### Reference adapter

`InMemoryExecutionStore` (`backend/agents/execution/memory_store.py`) is the
reference, thread-safe implementation — the *null / volatile* adapter, analogous
to `FileGraphPersistenceBackend` for graph persistence. It proves the contract
is implementable and serves tests and standalone runs. **It is not durable** —
its state does not survive a restart. Durable local and hosted adapters implement
the same methods with real persistence and are held to the same
`ExecutionStoreContractTests`.

## 9. Public/private boundary

This contract covers only generic core reliability. It deliberately excludes:

- **Tenant / workspace isolation** and per-tenant concurrency, quotas and
  retention policy.
- **The durable storage technology** (local file/SQL, hosted database) and its
  migration posture.
- **Run-history APIs and UI**, and **approval / governance** semantics.
- Any business-specific scheduling, prioritisation or fairness policy.

Those are hosted-layer or later-slice concerns. The seam defined here is what
they build on: a hosted adapter binds durable, workspace-scoped persistence
behind the same `ExecutionStore` interface without changing the producers,
workers, or this contract.
