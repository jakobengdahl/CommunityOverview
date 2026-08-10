# ADR 0001 — Default local durable execution store

- **Status:** Accepted
- **Date:** 2026-08-10
- **Scope:** Open-source core only
- **Related:** [`DURABLE_EXECUTION_CONTRACT.md`](../DURABLE_EXECUTION_CONTRACT.md)
  (the seam this decision picks a default adapter for),
  [`EVENT_SUBSCRIPTIONS.md`](../EVENT_SUBSCRIPTIONS.md),
  [`AGENT_SCHEDULING.md`](../AGENT_SCHEDULING.md)

## Context

The durable execution contract defines a replaceable persistence seam,
`ExecutionStore` (`backend/agents/execution/store.py`), and ships one reference
adapter, `InMemoryExecutionStore`. The reference adapter proves the contract is
implementable but is explicitly **not durable** — its jobs do not survive a
restart, which is the whole problem the contract exists to solve.

The contract deliberately left two things open (see its §9 boundary):

1. **the durable storage technology** for the default local adapter, and
2. **its migration posture** — how that store's on-disk shape evolves, and what
   "replaceable by a hosted adapter" means in practice.

This ADR closes both. It does **not** implement the adapter, wire it into the
scheduler/worker, or add run-history APIs — those are later, separately
sequenced slices that build on this decision. What it fixes is the *technology
and the posture* those slices target, so they are not re-litigated per slice.

### Requirements the default local store must meet

Drawn from the contract:

- **Durability across restart.** Queued (`PENDING`) and in-flight (`RUNNING`)
  jobs, their attempt counts, leases, backoff `run_at`, terminal outcomes and
  correlation fields must all survive a process restart.
- **Atomic claim.** `claim_next` must transition exactly one runnable job to
  `RUNNING` under concurrency — the single hardest operation to get right — and
  must reclaim jobs whose lease has expired.
- **Idempotent enqueue.** `enqueue` must be idempotent on `idempotency_key`,
  which requires a uniqueness guarantee on that key.
- **Inspection / history surface.** `list_jobs` (filter by state / agent / kind,
  newest-first) is the basis for durable AgentRun history, so the store needs
  ordered, filterable reads.
- **Replaceable, not central.** It is one adapter behind a `Protocol`; a hosted
  adapter must be able to replace it without touching producers, workers, or the
  contract.

### Constraints from the open core

- The base install is intentionally light and ML-free (`requirements.txt`), and
  the project convention is to prefer an existing utility over a new dependency.
- The existing default persistence backend (`FileGraphPersistenceBackend`) is a
  single-file, embeddable, zero-service artifact. The local execution store
  should match that operational profile: a developer or a self-hoster gets
  durability by running the process, with nothing extra to stand up.

## Decision

**The default local durable `ExecutionStore` adapter is backed by SQLite, using
the Python standard-library `sqlite3` module.**

Concretely, when the adapter is implemented (the next slice):

- It persists jobs in a **single SQLite database file dedicated to execution
  state** (e.g. `execution.db`), owned by the execution store and kept separate
  from the graph snapshot (`graph.json`). Execution state is operational runtime
  state, not user content, and the two have different lifecycles.
- It opens the database in **WAL journal mode** with a bounded `busy_timeout`,
  and performs `claim_next` inside a write transaction (`BEGIN IMMEDIATE`) so the
  select-and-transition is atomic under concurrent workers.
- `idempotency_key` carries a **`UNIQUE` constraint**; `enqueue` upserts against
  it and returns the existing row on conflict, satisfying the idempotency rule at
  the storage layer instead of in application code.
- Row shape is anchored to `ExecutionJob.to_dict()` / `from_dict()`; the
  `payload` and `result` blobs are stored as JSON text. Indexes on
  `(state, run_at)` and `created_at` back `claim_next` and `list_jobs`.
- It is registered as one implementation of the `ExecutionStore` `Protocol` and
  must pass `ExecutionStoreContractTests` unchanged — the same suite the
  in-memory reference adapter passes.

### Why SQLite

- **Zero new runtime dependency.** `sqlite3` is in the CPython standard library,
  so the default durable store adds nothing to `requirements.txt` and keeps the
  base install light — consistent with the project's dependency rule and with
  the file-backed graph default.
- **Real transactions for the hard part.** `claim_next` needs an atomic
  select-and-update under concurrency. SQLite gives that with a genuine ACID
  transaction, rather than a hand-rolled scheme over flat files or advisory file
  locks. Terminal-state stickiness and lease reclaim likewise reduce to
  conditional `UPDATE ... WHERE state = ...` statements.
- **Durable by design.** Committed rows survive a restart; WAL mode gives durable
  commits with good read/write concurrency for a single-host process.
- **Embeddable, single artifact.** One file, no service to run — the same
  operational profile as the existing default persistence backend, which is what
  a self-hoster and the standalone/dev experience need.
- **Adequate query surface.** Indexed, ordered, filterable reads serve
  `list_jobs` and the run-history that builds on it without a second store.

### Migration posture

"Migration" here has two distinct meanings; the decision treats them separately.

1. **Schema evolution within the local store (owned, forward-only).**
   - The store owns its own schema. Schema version is tracked with
     `PRAGMA user_version`.
   - Startup runs **idempotent, forward-only** migrations: `CREATE TABLE IF NOT
     EXISTS`, then additive, version-guarded `ALTER TABLE` steps. A newer adapter
     opening an older database upgrades it in place; opening an already-current
     database is a no-op.
   - New job fields are introduced as **additive and default-safe**, mirroring
     the tolerant `ExecutionJob.from_dict` (unknown/missing keys fall back to
     defaults), so a schema lag never makes an existing database unreadable.
   - A point release does not perform destructive or lossy migrations. A change
     that cannot be expressed additively is a new ADR, not a silent break.

2. **Swapping the whole adapter (behavioural contract, not data migration).**
   - Replacing the local SQLite adapter with a hosted adapter is a **deployment
     swap behind the `ExecutionStore` Protocol**, not a row-level data migration.
     What transfers is the *behaviour* — every adapter passes the same
     `ExecutionStoreContractTests` — not the bytes on disk.
   - In-flight jobs are **ephemeral operational state, not durable user data**.
     The core makes **no promise of automatic cross-technology data migration**
     of the queue. On a store swap, a deployment drains or lets in-flight jobs
     settle first; because `enqueue` is idempotent on `idempotency_key`,
     producers can safely re-enqueue any intended occurrence against the new
     store. Terminal history, if it must be retained across a swap, is exported
     through the `list_jobs` inspection surface rather than by copying the
     database file.
   - This keeps the local store genuinely replaceable: hosted layers bind their
     own durable, higher-concurrency store behind the same seam without the core
     having to guarantee binary or schema compatibility with SQLite.

### Concurrency posture

The default target is **one host, one process, worker threads** — the model the
contract's leasing already assumes. SQLite's own locking plus `busy_timeout`
tolerates occasional multi-process access on a shared file, but horizontal
scale-out and cross-node concurrency are explicitly a **hosted-adapter concern**,
not a promise of the local default. Multi-tenant isolation, per-tenant
concurrency and retention policy remain out of core entirely (contract §9).

## Consequences

**Positive**

- The default durable store adds no dependency and no service; durability comes
  from running the process, matching the existing self-host story.
- The hardest contract guarantees (atomic claim, idempotent enqueue, sticky
  terminal transitions) are enforced by the storage engine, which lowers the risk
  in the implementation slice that follows.
- `list_jobs`/run-history has a real query engine underneath from day one.

**Negative / accepted trade-offs**

- SQLite is not the right store for horizontal scale-out; that is deliberately
  delegated to hosted adapters behind the same seam.
- Under heavy multi-process contention on one file, writers serialise. Acceptable
  for the local/self-host default; the scaling answer is a different adapter, not
  a different local technology.
- No automatic migration of an in-flight queue when swapping adapters. Accepted:
  the queue is operational state, and idempotent re-enqueue plus history export
  cover the real needs without coupling the core to SQLite's on-disk format.

## Alternatives considered

- **JSON files / append-only log (extend the file-backed style).** Matches the
  existing graph persistence aesthetically, but provides no transactional
  select-and-update, so `claim_next` and idempotent `enqueue` would need a
  hand-rolled locking scheme that duplicates, less safely, what SQLite already
  guarantees. Rejected.
- **Embedded key-value store (`dbm`, LMDB, etc.).** `dbm` lacks transactions and
  secondary indexes; LMDB is a new native dependency. Neither improves on SQLite
  for this workload. Rejected.
- **Local PostgreSQL / an external broker (Redis, RabbitMQ).** Real queue
  engines, but each is a service the self-hoster must stand up and operate —
  wrong profile for the open-core *default* and a new heavy dependency. These are
  exactly the kind of store a **hosted** adapter may choose behind the same seam;
  they are not the local default. Rejected as the default.
- **Keep only the in-memory reference adapter.** Fails the core requirement:
  jobs must survive a restart. Rejected.

## What this ADR does not decide

- The concrete adapter implementation, its module location, and how the
  scheduler/worker select it at runtime (next slice).
- Run-history API/UI shape, and approval/governance semantics (later slices).
- Any hosted or workspace-scoped store technology, isolation model, or retention
  policy — those live outside the open core.
