# Runtime extension hooks

`backend/runtime/extensions.py` defines four optional contracts. Standalone
defaults allow mutations and execution, discard diagnostics, and enqueue external
events through the existing HTTP worker. Graph authorization, schema validation,
revision checks, agent causation limits and agent rate limits still apply.

## Registration and context

An embedding application can select its collaborators directly at startup:

```python
from backend.api_host import AppConfig, create_app
from backend.runtime.extensions import RuntimeExtensions

extensions = RuntimeExtensions(
    mutation_policy=my_policy,
    execution_admission=my_admission,
    event_sink=my_observer,
    event_delivery=my_delivery_adapter,
)
app = create_app(
    AppConfig(graph_file="graph.json"),
    authorization_hook=my_authorization_hook,
    runtime_extensions=extensions,
)
```

Both new arguments are keyword-only and optional. `authorization_hook` implements
`GraphAuthorizationHook` and is passed to `GraphService` for reads and writes.
`create_app()` and the existing
`uvicorn backend.api_host.server:get_app --factory` command keep their defaults.

Construction order is explicit: select and bind extensions, construct storage,
construct federation and event delivery, construct the graph service, then start
federation and the agent registry. The binding covers all application construction
without changing the process default. Storage receives the selected instance in
its constructor; dispatchers and agent workers inherit it from storage, including
workers created later in response to graph events. ASGI execution also binds the
selected instance for each request through response streaming, so HTTP model
invocations use the application's admission policy. The binding is reset when
execution exits, including on errors.

Optional pre-built storage remains supported as the second positional argument or
`graph_storage=storage`. Construct it with
`GraphStorage(..., runtime_extensions=extensions)` first. If both storage and
extensions are supplied, they must refer to the same instance; a mismatch raises
`ValueError` before any new collaborators are constructed. If extensions are
omitted, the storage's instance is retained. Composition is fixed at construction:
there is no reconfiguration method, and the `AppRuntime` fields are frozen.
Replacing storage or worker extension attributes directly is unsupported because
other collaborators may already have captured them.

The app exposes these collaborators as `app.state.runtime`, an `AppRuntime`.
Use `app.state.runtime.run_function(runner, spec, broker, envelope, attempt=1)`
to execute a user function with the same extensions. It delegates to the existing
function runner with an explicit instance, so admission happens before subprocess
startup. Model invocations within agent and user-function execution bind that
instance for the duration of the work.

A worker-only process uses exactly the same construction path, without HTTP
routes or static files:

```python
from backend.api_host import create_worker_runtime

runtime = create_worker_runtime(
    AppConfig(graph_file="graph.json"),
    authorization_hook=my_authorization_hook,
    runtime_extensions=extensions,
)
try:
    wait_for_shutdown()  # The embedding process owns its lifetime.
finally:
    runtime.stop()
```

`create_worker_runtime` accepts the same arguments as `create_app` and starts
event delivery, federation and configured agents immediately. `runtime.stop()`
stops them and flushes storage, including supplied storage. The HTTP application
calls it from its shutdown handler. Worker-only processes can also use
`runtime.run_function(...)`; each process must receive its own composition at boot.

For independently constructed clients, the existing process default remains
available through `configure_runtime_extensions(extensions)`. Set it before
constructing any clients; it does not reconfigure an existing application.
`GraphStorage`, `GraphService`, `AgentWorker` and `run_function` also retain their
lower-level constructor/keyword injection APIs. Independently invoked model
clients use the currently bound instance or the process default.

`RuntimeContext` carries immutable actor, scope, session, attribution,
correlation/request IDs, agent/run/event IDs, attempt and causation depth.
Actor and scope come from the existing request authorization context, so the
same identity trust boundary applies. Event payload fields cannot supply execution
scope or attempt. Queued work snapshots both the payload and request context;
retries retain run/event IDs, correlation and causation. Approved proposals
continue to restore their stored correlation, session and causation depth.
Hooks must not mutate the graph or reenter a write while deciding it.

## G1: mutation policy

`MutationPolicyHook.evaluate(context, change_set)` returns
`MutationPolicyDecision(allowed=True, code="allowed")` or a denial with a stable
code. `change_set` is a tuple of `ResourceChange` records: kind, resource ID and
immutable before/after documents, including types, revisions, metadata and edge
links. An absent side is `None`; vectors are omitted. Candidate revisions are
the pre-commit revisions (zero for a new resource), not a promise of the revision
the store will assign.

The policy runs at the storage commit boundary for direct CRUD, archival and
type changes, at whole-graph replacement for imports/archive restore, at merge
import commits, before staged writes persist, and again before final session
merges. A linked Agent, subscription and edge are decided together. A denial
restores tentative in-memory changes, persists no graph writes and emits no
activation events. Session merge denials retain the layer and write no merge
record. Ordinary writes return the policy code in `error`; import/restore
returns it in `error_code`. Invalid decisions and hook exceptions deny with
`mutation_policy_error`. Existing authorization and validation run independently.
Changes arriving from another storage writer are already committed observations;
each writer must enforce policy at its own commit boundary.

## G2: execution admission

`ExecutionAdmissionHook.admit(context)` returns an `AdmissionDecision` with
`outcome="allow"`, `"defer"` or `"deny"`, and a stable `code`. Deferral requires
`retry_at`, a finite UTC Unix timestamp; the core bounds each retry to between
10 milliseconds and 60 seconds from the decision.

Agent work checks the core depth and rate guards before admission. A permissive
hook cannot raise either limit. Deferred queued work waits interruptibly and
rechecks the core rate guard and admission with an incremented server-owned
attempt, retaining the original context. Denied work never starts the agent's
model/tool loop.

User-function admission precedes `runner.start` and uses the broker's fixed
invocation context. Denial returns a failed `FunctionRunResult`; deferral returns
`status="deferred"`, `code` and `retry_at`, with no subprocess started. The caller
owns rescheduling and passes the next attempt; the runner never retries itself.

Both model provider implementations check admission immediately before each
network invocation, including calls outside agent work and every tool-loop turn.
A refusal raises `AdmissionRefused` carrying `code`, `decision.outcome` and bounded
`retry_at`. Inside agent work, model deferrals wait interruptibly and retry
admission for that invocation without replaying prior tools. Other callers
handling deferral must retain their current messages and execution context when
retrying that invocation.
Invalid decisions and admission exceptions deny with `execution_admission_error`.

## G3: runtime diagnostics

`RuntimeEventSink.emit(event)` observes `RuntimeEvent` version 1. Events contain
a UTC timestamp, stable reason, resource IDs and the full runtime context:

- `mutation_policy.allowed` / `mutation_policy.denied` use the decision code.
- `loop_guard.rejected` uses `causation_depth_limit`.
- `loop_guard.throttled` uses `event_rate_limit`.
- `loop_guard.recovered` uses `event_rate_recovered`.

Rate events describe transitions, not every poll. Each configured observer has
one daemon consumer and a bounded queue of 128 events. Publication never waits
for the observer: exceptions are logged, and excess diagnostics are dropped
when the queue is full. These are best-effort diagnostics, not an audit ledger.
They never enter graph history, the graph event dispatcher or agent subscriptions.

## G4: external event delivery

`EventDeliveryAdapter.deliver(subscription, event, context)` replaces only the
external transport. It receives a detached subscription and event, with a stable
`context.delivery_id` formed from subscription ID and event ID, plus correlation.
Return `DeliveryOutcome(outcome, delivery_id, correlation_id, code)` where outcome
is `delivered`, `deferred` or `terminal_failure`. `deferred` transfers responsibility
for retry to the adapter, including a durable enqueue. The core never also sends
directly or falls back to HTTP after adapter failure; exceptions produce a
terminal `delivery_adapter_error`. Adapters own idempotency using the delivery ID.

Internal agent routing and the exclusion of `internal://` URLs run before this
seam. Without a replacement, `DefaultEventDeliveryAdapter` enqueues once into
the existing HTTP worker; its SSRF checks, pinned transport and retry policy
remain authoritative. Queue acceptance reports `deferred` / `http_queued`.
`GraphStorage.setup_events(on_delivery_outcome=callback)` reports adapter outcomes
and subsequent default HTTP delivered/retry/terminal outcomes with the same IDs.
`EventDispatcher` accepts the same callback directly. Existing
`DeliveryWorker.on_result` reports also carry `delivery_id` and `correlation_id`.
