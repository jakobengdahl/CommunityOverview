# External Admin and Automation Seams

## Purpose

This document defines the generic seams the CommunityOverview core should expose so
that external admin/control layers and external automation layers can integrate cleanly
without embedding their policies, identities, or business logic inside the core itself.

The core's job is to provide well-shaped surfaces. What external layers build on those
surfaces — their RBAC models, lifecycle policies, orchestration workflows, or business
rules — is explicitly out of scope here and belongs outside the public core repository.

**Relationship to other enablement docs:**
- [CORE_RUNTIME_AND_EXTENSION_ENABLEMENT.md](./CORE_RUNTIME_AND_EXTENSION_ENABLEMENT.md) — broader runtime and extension enablement context, including the auth/identity seam foundation (section E) and the API/event surface goals (section H) that this document elaborates
- [CORE_ENABLEMENT_IMPLEMENTATION_PLAN.md](./CORE_ENABLEMENT_IMPLEMENTATION_PLAN.md) — the prioritised implementation sequence for these seams

---

## Design principles

### 1. Surface shape, not policy

The core exposes seam shapes — actor context slots, mutation endpoints with action
context fields, read-only status paths, event publication points. It does not enforce
any particular lifecycle policy, approval workflow, or access control rule on those
seams beyond the standalone-safe defaults.

### 2. Privileged boundaries live outside the core

An external admin layer may enforce its own authentication boundary, role model, and
mutation policy on top of the core's generic seams. None of that policy should flow
back into the core. The core must remain usable without any external privileged layer
attached.

### 3. Read paths and mutation paths are distinct API families

Status and export surfaces must be stable, narrowly scoped, and safe to expose to
read-only consumers. Mutation surfaces carry action context and must be treated as a
separate family with separate authorization expectations. Mixing them creates surfaces
that are hard to scope safely for external consumers.

### 4. Automation layers are first-class actors, not humans

Automated processes — scheduled jobs, event-driven workers, integration webhooks —
have different safety requirements from human admin sessions. The core's action context
should be able to represent and distinguish these. Replay safety (idempotency) and
trigger traceability matter for automated paths in ways that do not apply to interactive
human sessions.

### 5. Standalone defaults remain permissive and safe

Every seam described here must default to no-op or passthrough behavior in standalone
mode. An external admin layer is optional, not a prerequisite.

---

## Request actor classes

The core's action context should be able to carry a generic representation of who or
what is making a request. Three actor classes cover the range of real-world callers:

### Human admin actor

A real person with elevated access credentials acting through an admin interface or
direct API session.

Relevant context fields:
- `actor_class`: `human_admin`
- `actor_id`: a stable identifier for the person's account or session identity
- `session_id`: the authenticated session that issued the request
- optional: a reason or annotation field for audit purposes

Human admin sessions are issued by the external auth boundary. The core should accept
the resolved identity as a claim but must not enforce a specific identity provider.

### System actor

An automated process operating programmatically against the core's admin mutation
surface: integration workers, event-driven callbacks, external orchestration agents.

Relevant context fields:
- `actor_class`: `system_actor`
- `actor_id`: a stable service identity name or service account label
- `trigger_ref`: a reference to the event, webhook, or job run that triggered this
  invocation — for traceability back to the initiating cause
- `idempotency_key`: a caller-supplied key that uniquely identifies this invocation
  attempt, enabling safe replay without double-execution

System actors must not carry human session fields. The core should be able to treat
system actor requests differently from interactive sessions (e.g., for rate limiting,
audit record shaping, or replay-safety enforcement).

### Scheduled actor

A sub-class of system actor for time-triggered automation: maintenance sweeps,
retention jobs, health verification passes.

Additional relevant fields:
- `schedule_id`: a reference to the cron entry or scheduler job that fired this run
- `scheduled_at`: the nominal scheduled fire time, distinct from actual execution time

Scheduled actors share system actor replay-safety requirements. The distinction matters
for dead-man monitoring: an external monitoring layer can verify that expected scheduled
invocations were received by matching `schedule_id` and `scheduled_at` values in audit
records.

### Standalone default

When no actor context is provided — the default standalone case — the core should treat
requests as if from an anonymous or single-owner local session. No external actor layer
should be required for standalone operation.

---

## Correlation keys and idempotency

External admin and automation layers often involve chains of related actions across
multiple systems. The core's mutation surfaces and event outputs should carry correlation
keys so that external layers can trace a chain of related actions without requiring the
core to understand the chain's semantics.

### Idempotency keys for mutation paths

For any mutation that an automation layer may retry (due to network failure, timeout, or
restart), the core should accept a caller-supplied `idempotency_key` and return a stable
result for repeated calls with the same key and parameters. The key is opaque to the
core; its uniqueness and scope are the caller's responsibility.

Design expectations:
- idempotency behavior should be explicit and documented per mutation endpoint
- the core should store or cache idempotency outcomes for a defined window
- a repeated call within the idempotency window with the same key must not produce a
  second side-effect

### Correlation keys for cross-layer tracing

When a mutation originates from a cross-boundary request (a handoff, an external event,
a queued job), the initiating layer typically holds a correlation identifier. The core
should propagate that identifier through its own audit output and event payloads so that
an external observer can join the mutation record back to its origin without parsing
opaque log text.

Correlation keys are caller-defined and opaque to the core. The core should carry them
in a well-known field, not buried in freeform metadata.

Key design rule: correlation keys must be forwarded, not regenerated. When the core
receives a `trigger_ref` or similar correlation field in a system actor request, it must
propagate that value into the resulting audit context rather than replacing it with an
internally generated identifier.

---

## Read-only status and export surfaces

External automation and monitoring layers frequently need to observe the state of the
core's managed objects (graphs, workspaces, configuration contexts) without needing or
being granted mutation authority. Providing a well-defined read-only surface is safer
and easier to scope than sharing a generic admin credential.

### Principles for read-only surfaces

- Read-only API paths must be cleanly separable from mutation paths so that a
  narrowly-scoped read credential cannot accidentally reach a mutation endpoint.
- Read-only responses should support field projection or minimal-field modes so that
  callers can receive only the data they need.
- Read-only paths must be safe for polling at reasonable intervals without requiring
  push notification infrastructure.
- Sensitive internal state (internal job retry counts, storage paths, session tokens,
  internal system identifiers) must not appear in read-only export payloads unless
  explicitly required and justified.

### Typical read-only surfaces the core should support

**Object status reads:** current lifecycle state and basic metadata for graphs,
workspaces, configuration contexts, or other managed objects. Stable and safe for
regular polling.

**Capability and configuration inspection:** which capabilities are enabled, what
runtime mode is active, what schema version is in use. Safe for automation that
needs to verify readiness before triggering dependent actions.

**Mutation outcome records:** a stable read surface over the outcomes of recent
mutations — what changed, when, by which actor class — suitable for change tracking
and reconciliation loops in external systems.

**Health and readiness state:** explicit signals for whether the core is ready to
serve requests, distinct from whether it is merely running.

### What read-only surfaces must not expose

- mutation endpoints, even under a read-scoped credential
- internal queue internals, storage paths, retry state, or dead-letter contents
- session tokens, credentials, or security-sensitive internal identifiers
- cross-object data that would aggregate information a caller should not have access
  to as a side effect of observing a single object

---

## Mutation API design for external admin layers

When an external admin layer needs to change state through the core — provisioning
a workspace, adjusting configuration, executing a lifecycle transition — the mutation
surface should be shaped to support safe external invocation.

### Action context on mutations

Every mutation endpoint that an external admin layer may call should accept an action
context block alongside the mutation payload. The action context captures:

- the actor class and actor identity (see actor classes above)
- an optional reason or annotation string, required for human admin actors performing
  privileged actions
- an idempotency key for system actor invocations
- any correlation keys from the triggering event or external request

The action context is recorded in the mutation's audit output. The core must not allow
a privileged mutation to commit without a valid action context when one is required for
that endpoint.

### Pre-commit audit obligation

For mutations that carry audit significance, the core should write an audit record
before committing the change. A mutation whose audit write fails should not proceed
with the change. This invariant is important for external admin layers that need a
reliable audit trail: they can trust that any committed mutation has a corresponding
audit record.

In standalone mode, audit recording may be minimal (local structured log). The seam
must be defined so that an external audit store can be attached later without changing
the mutation logic.

### Distinguishing admin mutation from application mutation

Not every write operation through the core needs to be treated as an admin mutation.
The core should distinguish:

- **application mutations:** user-initiated graph edits, node additions, relationship
  changes — the normal product workflow
- **admin mutations:** configuration changes, lifecycle transitions, bulk operations,
  or other actions that an external admin layer may need to invoke with elevated context

Admin mutations should carry action context. Application mutations may carry actor
context for attribution but do not need the full admin action context model.

---

## Handoff and event hooks

When an external admin layer needs to delegate an action to another system, or when the
core produces an event that external automation should react to, a handoff or event hook
pattern avoids tight coupling between the core and the consuming layer.

### Outbound event hooks

The core should support publishing lifecycle events (graph created, workspace changed,
configuration updated, health state changed) to a configurable outbound event hook.
The hook itself is configured externally; the core's responsibility is to publish a
well-structured event payload with consistent fields.

Event payloads must include:
- event type identifier
- the affected object type and identifier
- actor context from the triggering action (if available)
- timestamp
- any correlation keys present in the triggering request

The core must not require a specific event broker or consumer. A no-op default
(logging only) must be safe in standalone mode.

### Inbound handoff endpoints

When an external layer needs to request that the core perform a lifecycle action on its
behalf — and the core needs to record who requested it and why — the mutation endpoint
should support a handoff origin field alongside the action context.

The handoff origin carries:
- an external `request_id` that the initiating system assigned
- the type of request (opaque to the core; passed through to audit output)
- the timestamp when the initiating system created the request

The core uses the `request_id` as a correlation key in the resulting mutation record.
This allows the external system to join its own request log to the core's audit output
by matching on `request_id`, without the core needing to understand the external system's
workflow semantics.

### Asynchronous action patterns

For long-running operations, the core should support returning an action reference
identifier that the caller can use to poll for completion status. This avoids requiring
long-lived HTTP connections for administrative operations and allows external automation
to use an observe-and-check pattern rather than synchronous wait.

---

## Audit-friendly action context

Across all the seams described above, the core should support a consistent audit context
model that makes mutation records navigable without requiring an external observer to
understand internal implementation details.

### Audit record minimum fields

Every mutation record (log entry, event payload, or audit store record) that may be
consumed by an external layer should include:

| Field | Description |
|---|---|
| `event_id` | Stable unique identifier for this audit record |
| `action_type` | What kind of mutation occurred (generic enumerated label) |
| `target_type` | The type of object affected |
| `target_id` | The identifier of the affected object |
| `actor_class` | `human_admin`, `system_actor`, `scheduled_actor`, or `anonymous` |
| `actor_id` | Stable identity of the actor (opaque to the core; supplied by caller) |
| `timestamp` | ISO 8601 timestamp of the event |
| `result` | `committed`, `denied`, or `failed` |
| `idempotency_key` | Present when the mutation was a system actor call |
| `trigger_ref` | Present when the mutation was triggered by an external event |
| `correlation_keys` | Map of caller-supplied correlation identifiers to propagate |

### Append-only semantics

Audit records must be append-only. The core must not mutate or delete audit records
after they are written. A failed or denied action still produces an audit record with
`result=denied` or `result=failed`; the absence of an audit record must not be a valid
signal that an action was not attempted.

### Actor distinguishability

Human admin actors, system actors, and scheduled actors must be distinguishable in
audit output. The core must not collapse all actor types into a single generic `user`
class, as this would prevent external audit consumers from applying different review
rules to automated versus human-initiated actions.

---

## Supporting external privileged boundaries without embedding them

The core should make it possible for an external layer to enforce a strong privileged
boundary on top of its generic seams — without the core itself implementing or knowing
about that boundary.

### What the core should do

- accept actor context supplied by a trusted gateway or external auth layer, rather
  than resolving actor identity itself
- distinguish actor classes in its own action context model
- enforce that admin mutation endpoints require an action context when configured to
  do so, failing closed when the context is absent or malformed
- propagate correlation keys and actor context through to audit output
- expose a read-only surface that can be scoped to a narrower credential without
  sharing the mutation credential

### What the core must not do

- implement an internal operator role hierarchy or approval workflow
- enforce lifecycle policy (what states an object may transition between) as core
  business logic — that belongs to the external admin layer's policy model
- assume a specific identity provider or authentication system
- require an external privileged layer to exist for standalone operation
- expose internal implementation details (storage paths, retry counts, job queue
  internals) through the read-only export surface

### The credential separation pattern

The recommended pattern for an external layer integrating with the core's admin seams:

1. The external layer holds a **mutation credential** that it uses to call admin mutation
   endpoints, passing actor context in the request.
2. A separate **read-only credential** is used for status reads and audit log queries.
   This credential cannot reach mutation endpoints.
3. The core's auth seam (see section E of
   [CORE_RUNTIME_AND_EXTENSION_ENABLEMENT.md](./CORE_RUNTIME_AND_EXTENSION_ENABLEMENT.md))
   is configured to accept the claims carried by each credential type.

The core does not issue these credentials. The external layer manages them. The core
only validates the claim format and scope it was configured to require.

---

## Non-goals

This document explicitly excludes:

- **Lifecycle policy definitions.** Whether a workspace may be suspended, what
  states an object may transition through, and what approvals are needed — these are
  external admin layer concerns, not core concerns.
- **Role definitions or RBAC models.** The core may accept a role claim in actor
  context, but the role hierarchy and permission matrix belong outside the core.
- **Business workflow logic.** Approval queues, handoff queue semantics, escalation
  rules, billing event handling, grace period enforcement — none of these should be
  implemented in the core.
- **Specific identity provider integration.** The core may accept an identity claim
  from a trusted gateway; it must not hard-code a specific identity provider.
- **External automation agent logic.** What automation agents do with the read-only
  export surface or the event hooks — their scheduling, their decision logic, their
  graph updates — belongs outside the core.
- **Multi-tenant lifecycle management.** Provisioning, suspending, or deprovisioning
  tenants or user accounts is not a core function. The core may expose the generic
  seams that enable an external layer to do those things safely, but the policies and
  execution belong elsewhere.

The goal of this document is to ensure the core's seam surfaces are shaped in a way
that makes external admin and automation layers implementable without requiring forks,
patches, or policy leakage into the core.
