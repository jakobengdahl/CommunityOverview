# ADR 0011 — Sandboxed user-defined functions: first contract

- **Status:** Proposed
- **Date:** 2026-10-14
- **Scope:** Open-source core
- **Related:** [`USER_FUNCTION_CONTRACT.md`](../USER_FUNCTION_CONTRACT.md) (the
  contract this decision adopts);
  [`DURABLE_EXECUTION_CONTRACT.md`](../DURABLE_EXECUTION_CONTRACT.md) and
  [ADR 0001](0001-local-durable-execution-store.md) (run state it reuses);
  [`EVENT_SUBSCRIPTIONS.md`](../EVENT_SUBSCRIPTIONS.md) (envelope and origins);
  [ADR 0005](0005-sessions-may-stage-graph-changes.md) (staged writes a later
  version targets)

## Context

Operators want a graph change to cause a precise, repeatable effect: when a
threshold changes, recompute what depends on it; on demand, normalise a set of
nodes. Today the only automation is AI agents. Agents suit open-ended work, but
they are slow, costly and non-deterministic for a fixed rule, and their tools run
inside the application process.

Running operator-written code is the obvious answer and the main risk. Arbitrary
code could reach application internals, another graph, credentials or internal
networks, run indefinitely, trigger itself through its own writes, or repeat an
external side effect. A design that tries to settle every trigger, tool and
binding at once would also have to settle secrets, egress and multi-tenant
isolation before any of it can be tested.

Facts from the current code that shape the decision:

- `ExecutionStore` already provides durable jobs, idempotency on a key, terminal
  states and run history; agent runs are recorded through it.
- The event envelope and its `event_origin` / `event_correlation_id` context
  already exist, but self-exclusion for event consumers is opt-in, there is no
  bound on chained triggers, and agent writes do not forward correlation.
- Graph access is checked per actor through an authorization hook (read or
  mutate), so a broker can check a function's call exactly as it would check a
  direct request from the invoking actor.
- Process isolation exists only in narrow forms: attachment extraction runs in
  an environment-stripped, time-limited worker process that is killed on
  timeout, and stdio MCP integrations run as subprocesses. No general isolation
  runtime for operator code has been chosen.

## Decision

### 1. Adopt a narrow first contract

The open core adopts [`USER_FUNCTION_CONTRACT.md`](../USER_FUNCTION_CONTRACT.md),
contract version 1. It covers **manually invoked functions that read and write
the graph through a broker**, with no outbound network and no secrets. Scheduled,
graph-change and webhook triggers, session-staged writes, deletion, egress and
secrets are reserved: their manifest fields and the rules they must follow are
named now, and a manifest that uses them is rejected until a later version
implements them.

### 2. Functions run outside the process and talk only to a broker

Function code runs in a separate process started by a runner. It speaks
JSON-RPC 2.0 over its standard input and output with a host-side broker, and has
no other channel. The same protocol serves Python and Node.js; Python is
implemented first. The isolation technology is left to a runner adapter, which
must meet the contract's invariants and limits.

### 3. A hard wall-clock limit of 120 seconds

Every run is terminated at its deadline; the platform maximum is 120 seconds, a
manifest may only lower it, and no setting raises it. Functions are for short,
bounded reactions. Long-running work stays out of them, which keeps them distinct
from a process engine.

### 4. Direct writes as the invoking actor

A function holding the `graph:write` grant writes directly to the graph, with
the invoking actor's access checked on every call. Each write carries the origin
`function:<id>@<version>` and the run's correlation id. Writes staged in a
session and merged later (ADR 0005) are a reserved `write_target` for a later
version.

### 5. Loop prevention is part of the contract, not the trigger

Self-exclusion from a function's own origin is mandatory, chained runs carry a
bounded `chain_depth`, and correlation is forwarded. Version 1 only has manual
triggers, so these rules cannot yet fire; fixing them now means the trigger slice
implements them rather than designs them.

### 6. Reuse the durable execution seam

A run is an `ExecutionJob` with a new `function` kind, idempotent on
`fn:<function_id>:<key>`, recorded with the same retention as agent runs.
Manual runs are attempted once.

## Consequences

- An operator can write and run a deterministic graph function as soon as the
  runner slice lands, without secrets or network policy being designed first.
- The contract states what a function cannot do, so the isolation evaluation has
  a concrete target: any runner that meets §3 and §8 of the contract qualifies.
- A function's writes are attributable and traceable from the first version,
  which later graph-change triggers depend on.
- Version 1 is less useful for integrations: a function cannot call an external
  system until egress and secrets are added in a later version.
- Writes a terminated run made stay in the graph. Functions that make several
  writes need to be idempotent; the contract says so, but cannot enforce it.
- `ExecutionKind` gains a value, and the run-history view gains the
  `timed_out` and `rejected` statuses.

## Options considered

- **Full contract in one step** (all triggers, egress, secrets, hosted
  management). Rejected for now: it couples the first usable slice to the
  isolation choice and the secret and egress design, which carry most of the
  risk, and leaves nothing testable until all of it is settled.
- **HTTP broker on loopback.** Rejected: it opens a network path into the
  isolation boundary, which conflicts with deny-by-default networking.
- **Pure functions returning a change list.** Simpler and safer, but a function
  could not read the graph dynamically while it runs, which most realistic
  functions need.
- **Every write as a governance proposal.** Rejected as the default: functions
  would only ever produce suggestions. Staged writes in a session cover the
  reviewed case better and are reserved for a later version.

## What this ADR does not decide

- The isolation runtime (hardened subprocess, container, microVM or
  WebAssembly) and how third-party dependencies are installed.
- The API and CLI surface used to invoke functions and read run records.
- Hosted, workspace-scoped management of functions, quotas and entitlements,
  which belongs outside this repository.
