# User Function Contract

**Contract version:** 1
**Status:** Draft contract, adopted by
[ADR 0011](adr/0011-sandboxed-user-functions.md). It is normative for the first
implementation slices. Implemented so far, in `backend/functions/`: the broker
and its protocol session (§6, §7, §9 attribution, the per-message and tool-call
limits of §8), the default hardened-subprocess runner of ADR 0012 with the
Python helper library (§3.1, the time, memory and log limits of §8, §10), and
manifests, enablement, manual invocation and run records (§4, §5, §11) with
the `/functions` API. Not yet implemented: the Node.js runtime.
**Scope:** Open-source core. This version covers manually invoked functions that
read and write the graph through a broker. Every other trigger, tool and binding
is out of scope (see [§13](#13-reserved-for-later-versions)); the fields they
will need are reserved here so that adding them is not a breaking change.

**Related:**
[ADR 0012](adr/0012-user-function-isolation.md) (the default runner and the
isolation levels in §3.1),
[`DURABLE_EXECUTION_CONTRACT.md`](DURABLE_EXECUTION_CONTRACT.md) (run state,
idempotency and run history this contract reuses),
[`EVENT_SUBSCRIPTIONS.md`](EVENT_SUBSCRIPTIONS.md) (the event envelope and the
origin-based loop prevention reused here),
[`SECRET_PROVIDER_CONTRACT.md`](SECRET_PROVIDER_CONTRACT.md) (the seam a later
version binds secrets through),
[`SESSION_OVERLAY_CONTRACT.md`](SESSION_OVERLAY_CONTRACT.md) (the staged-write
model a later `write_target: session` follows),
[`EXTERNAL_ADMIN_AND_AUTOMATION_SEAMS.md`](EXTERNAL_ADMIN_AND_AUTOMATION_SEAMS.md)
(actor classes and correlation forwarding).

---

## 1. Purpose

A **user function** is a small, versioned program, written by whoever operates
an instance, that reacts to an invocation by reading and changing the graph. A
typical example: "when a threshold changes, recompute the derived values on the
nodes that depend on it".

User functions are deliberately narrow:

- **Deterministic code, not an AI agent.** A function runs no model loop. It is
  invoked, does its work through tools and returns a result. Agents remain the
  mechanism for open-ended, model-driven work.
- **Short-lived, not a process engine.** A run is bounded by a hard wall-clock
  limit (§8). There is no multi-step orchestration, no waiting on external
  events and no state carried between runs other than what the function writes
  to the graph.
- **Isolated, not a plugin.** Function code runs outside the application process
  and never imports, calls or depends on application modules. Everything it
  does crosses the broker described in §6.

## 2. Terms

| Term | Meaning |
|---|---|
| **Function** | A versioned package: a manifest plus code. Identified by `id` and `version`. |
| **Manifest** | `function.json`, the declarative description of a function (§4). |
| **Host** | The application side: validates manifests, starts runs, runs the broker, records results. |
| **Runner** | The isolation adapter that starts the function's process and enforces limits (§8). The isolation technology is chosen outside this contract. |
| **Broker** | The host component that answers the function's tool calls, checking grants and scope on every call (§6). |
| **Run** | One execution of one function version for one invocation. |
| **Invocation** | The request that starts a run, together with the actor and scope it carries (§5). |
| **Grant** | A permission the manifest declares and the operator enables (§7). |

## 3. Invariants

These hold for every run and every runner, with one qualification: invariant 2,
invariants 4, 6 and 7 insofar as they rest on it, and the part of invariant 5
that covers processes a run starts, hold in full only at isolation level
`full`. Below it, a function may reach files and process information directly
instead of through the broker, and, without the `processes` and `isolation`
controls, a process it starts may escape termination; a runner reports which
controls it could not enforce (§3.1). An implementation slice is tested against
them.

1. **No in-process code.** Function code never runs inside the application
   process and never receives a reference to an application object, module,
   database handle or service credential.
2. **Every effect goes through the broker.** A function's only channel to the
   host is the protocol in §6. A runner provides no network access. The only
   writable filesystem is a private, temporary working directory; read access
   is limited to the runtime itself, the helper library (§10) and the
   function's own directory.
3. **Grants are a ceiling, not a request.** A tool call outside the function's
   enabled grants fails, whatever the call claims about itself.
4. **Scope is fixed by the host.** The graph a run may touch and the actor it
   acts as are set when the run starts. The function cannot name another graph,
   another actor or another session.
5. **The time limit is absolute.** A run that has not returned by its deadline is
   terminated (§8), together with every process it started: nothing a run
   starts outlives it. No configuration raises the platform maximum.
6. **Writes are attributed.** Every write a run makes carries the function's
   origin and the run's correlation id (§9).
7. **No secret material in v1.** No secret value is reachable from a function in
   this contract version; see §13 for how a later version adds them.

### 3.1 Isolation levels

How much of invariant 2 a runner can enforce depends on the host: the kernel
features available, and whether the application itself runs unprivileged
inside a container. A runner therefore probes the host when it starts and
reports the controls it enforces:

| Control | Enforced means |
|---|---|
| `filesystem` | The process can read only the runtime, the helper library and the function's directory, and write only its working directory. |
| `network` | The process can create no socket other than a connected pair (`socketpair`), so it can reach neither the network nor any socket outside the run. |
| `processes` | The process can start no other process and cannot leave its session, so termination reaches everything it runs. |
| `isolation` | The process cannot signal, trace or read the memory or `/proc` entries of any process outside the run, the application included. |

The **isolation level** of a runner is `full` when it enforces all four
controls, and `degraded` otherwise. Independent of the level, every runner:

- runs the function in a separate process with an empty environment and no
  inherited file descriptors besides its standard streams;
- enforces the wall-clock limit and terminates everything the run started
  (invariant 5); where it cannot prevent new processes (`processes` missing), a
  per-run supervisor process that adopts orphaned descendants terminates every
  descendant it can see before the run is recorded as finished. Without the
  `isolation` control the function can kill that supervisor, so this
  termination is best-effort, and a runner that loses its supervisor records
  the run as `failed` with `runner_lost`;
- applies the memory limit (§8) and reads the process's output with the size
  limits of §8, terminating the run when they are exceeded rather than
  buffering further.

An instance setting, `isolation_minimum`, is `degraded` (the default) or
`full`. A run is `rejected` with `isolation_below_minimum` when the runner's
level is below it. When a runner starts at level `degraded`, the host logs a
warning naming the missing controls, and every run record carries the level and
the missing controls (§11).

At level `degraded` the function may be able to read files and process
information that the application's own user can read, which can include
configuration and credentials. Operators who run functions from authors they do
not fully trust set `isolation_minimum: full`.

## 4. Package and manifest

### 4.1 Layout

Functions are loaded from configuration, without a database or a hosted control
plane. Each function lives in its own directory under the configured functions
root (default `config/functions/`):

```
config/functions/
  recompute-derived-values/
    function.json        # the manifest
    main.py              # entry module named by the manifest
    ...                  # further source files, if any
```

The directory name equals the manifest `id`. The host reads a function's
directory to list it with its validation state (§4.4) and runs only a function
that is enabled.

### 4.2 Manifest fields

`function.json` is a JSON object:

```json
{
  "contract_version": "1",
  "id": "recompute-derived-values",
  "version": "1.2.0",
  "name": "Recompute derived values",
  "description": "Recomputes values on nodes that depend on a changed threshold.",
  "runtime": "python",
  "entry_point": "main.py",
  "input_schema": {
    "type": "object",
    "properties": {"node_id": {"type": "string"}},
    "required": ["node_id"]
  },
  "output_schema": {"type": "object"},
  "triggers": [{"kind": "manual"}],
  "grants": ["graph:read", "graph:write"],
  "write_target": "graph",
  "limits": {"timeout_seconds": 60}
}
```

| Field | Required | Meaning |
|---|---|---|
| `contract_version` | yes | The version of this contract the manifest targets. `"1"` for this document. |
| `id` | yes | Stable identifier: lowercase letters, digits and `-`, 3–64 characters. Equals the directory name. |
| `version` | yes | Semantic version of the function. Recorded on every run. |
| `name` | yes | Human-readable name. |
| `description` | no | What the function does. |
| `runtime` | yes | `python` or `node` (§10). |
| `entry_point` | yes | Path, relative to the function directory, of the file the runner starts. Must not leave the directory. |
| `input_schema` | no | JSON Schema the invocation `input` is validated against before the run starts. Absent means `input` must be an empty object. |
| `output_schema` | no | JSON Schema the function's `result.output` is validated against. A mismatch fails the run. |
| `triggers` | yes | List of trigger objects. Version 1 accepts exactly `[{"kind": "manual"}]` (§13). |
| `grants` | yes | List of grants the function needs (§7). May be empty. |
| `write_target` | no | Where writes land. Version 1 accepts only `"graph"`, the default (§13). |
| `limits` | no | Per-function limits, each at most the platform maximum (§8). |
| `retry` | reserved | Retry policy for non-manual triggers (§13). Must be absent in version 1. |
| `secrets` | reserved | Secret references (§13). Must be absent in version 1. |
| `egress` | reserved | Outbound network allowlist (§13). Must be absent in version 1. |

### 4.3 Validation

The host validates a manifest when it loads it and again before every run.
A manifest is **rejected**, and the function cannot run, when:

- `contract_version` is not one the host supports;
- a required field is missing, or a field has the wrong type;
- a field holds a value this contract version does not allow, such as a trigger
  kind other than `manual`, a `write_target` other than `graph`, an unknown
  grant, a limit above the contract maximum in §8, or any reserved field;
- `input_schema` or `output_schema` uses `pattern` or `patternProperties`: the
  host checks schemas in its own process, outside any run's limits, where a
  backtracking regular expression could stall it;
- `entry_point` does not resolve to a file inside the function directory.

Top-level fields this contract does not name are ignored, so that tooling may
add its own annotations. A field §4.2 lists as reserved is not unknown: it is
rejected, because accepting it silently would suggest a behaviour version 1
does not provide.

### 4.4 Enablement

Loading a manifest does not make it runnable. An operator enables a function,
and each grant it declares, explicitly in instance configuration. A function
that is not enabled, or that declares a grant the operator has not enabled, is
listed with its validation state and every invocation of it is `rejected`
(§11). Enabling a function is an operator action; a function cannot enable
itself or another function.

## 5. Invocation

### 5.1 Manual trigger

Version 1 has one trigger: an explicit, manual invocation through the host's
API (and any CLI built on it). The invocation names the function `id`, carries
an `input` object and, optionally, an `idempotency_key` and a `correlation_id`.

The invocation is authorized as the calling actor: the host evaluates the
caller's graph access through the existing authorization hook before the run
starts. A caller without read access cannot start any function, and a caller
without mutate access cannot start a function whose manifest declares
`graph:write`; such an invocation is `rejected`.

### 5.2 Envelope

The function receives one envelope, built by the host. It reuses the event
envelope from `EVENT_SUBSCRIPTIONS.md` (`schema_version`, `event_id`,
`event_type`, `occurred_at`, `origin`, `entity`) and adds an `invocation`
block:

```json
{
  "schema_version": "1.0",
  "event_id": "evt-…",
  "event_type": "function.invoke",
  "occurred_at": "2026-10-14T09:00:00Z",
  "origin": {
    "event_origin": "web-ui",
    "event_session_id": null,
    "event_correlation_id": "corr-…",
    "attribution": {"actor": {"…": "…"}, "scope": {"…": "…"}}
  },
  "entity": null,
  "invocation": {
    "contract_version": "1",
    "run_id": "job-…",
    "function_id": "recompute-derived-values",
    "function_version": "1.2.0",
    "trigger": "manual",
    "input": {"node_id": "…"},
    "idempotency_key": "…",
    "chain_depth": 0,
    "deadline": "2026-10-14T09:01:00Z"
  }
}
```

- `entity` is `null` for a manual invocation. Graph-change triggers (§13) fill
  it with the triggering entity's `before`, `after` and `patch`, unchanged from
  the event payload.
- `origin` describes **who started the run**. The origin the run's own writes
  carry is defined in §9.
- `deadline` is informative: the function may use it to stop early. The runner
  enforces the limit regardless (§8).
- The envelope never contains a secret, a credential or a token.

### 5.3 Idempotency

A run is a durable job in the `ExecutionStore`
([`DURABLE_EXECUTION_CONTRACT.md`](DURABLE_EXECUTION_CONTRACT.md) §5). Its
idempotency key is `fn:<function_id>:<key>`, where `<key>` is the invocation's
`idempotency_key`, or a fresh id when none is given. An invocation that repeats
a key returns the existing run instead of starting a new one, in any state,
until retention purges that run. Callers that retry a manual invocation after a
network failure therefore pass the same key. A `rejected` run also holds its
key: after fixing the cause (for example, enabling the function), invoke with a
new key.

## 6. Broker protocol (ABI)

### 6.1 Transport

The function and the host speak **JSON-RPC 2.0 over the function process's
standard input and output**, one JSON object per line (newline-delimited, UTF-8).
Standard error is captured as the run's log (§11) and is never parsed as
protocol. This transport needs no network inside the isolation boundary and is
the same for every runtime and every runner.

### 6.2 Sequence

1. The runner starts the process. The host writes one notification:
   `{"jsonrpc": "2.0", "method": "invoke", "params": <envelope>}`.
2. The function sends zero or more **tool calls**, each a request:
   `{"jsonrpc": "2.0", "id": 1, "method": "tool", "params": {"name": "graph.search", "arguments": {…}}}`.
   The host answers each with a response carrying `result` or `error`, in the
   order of the requests. A function makes one call at a time; a request sent
   while another is unanswered is answered with the error `concurrent_call`.
3. The function ends with one notification:
   `{"jsonrpc": "2.0", "method": "result", "params": {"status": "ok", "output": {…}}}`
   or `{"jsonrpc": "2.0", "method": "result", "params": {"status": "error", "error": {"code": "…", "message": "…"}}}`,
   and then exits.

A process that exits without sending `result` fails the run with
`no_result`. Output on standard output that is not valid JSON-RPC, or a
message the sequence above does not allow, fails the run with
`protocol_error`. Anything after `result` is ignored. Run-level error codes are
listed in §11.

### 6.3 Tools in version 1

| Tool | Grant | Maps to |
|---|---|---|
| `graph.search` | `graph:read` | `search_graph` |
| `graph.get_node` | `graph:read` | `get_node_details` |
| `graph.get_related` | `graph:read` | `get_related_nodes` |
| `graph.find_similar` | `graph:read` | `find_similar_nodes` |
| `graph.add` | `graph:write` | `add_nodes` (nodes and edges) |
| `graph.update_node` | `graph:write` | `update_node` |

Arguments and results follow the mapped MCP tool, with these restrictions; a
call that breaks one is refused with `invalid_arguments`:

- Each tool accepts only its documented arguments. Arguments that would change
  scope are rejected: a graph or session id, an event origin or correlation id
  (those come from the run, §3, §9), federation depth, and visualization
  actions.
- A function cannot set, change or clear a node's or edge's graph scope or
  provenance. The federation and provenance keys (`origin_graph_id`,
  `origin_node_id`, `federation_distance`, `is_federated`, `read_only`,
  `adopted_from` and the related keys) are refused at the top level of a node
  or edge and inside its `metadata`, and `metadata` must be an object; node ids
  in the reserved `federated::` namespace are refused, whether created, updated
  or used as an edge endpoint, and so is an edge endpoint given by a name that a
  federated reference node carries, unless a node created in the same call
  carries that name (the endpoint then resolves to the new node); and `graph.update_node` changes `metadata` only
  with `metadata_merge: true`, so keys the function does not name are kept.
- Work per call is bounded: `limit` is an integer from 0 to 500 and `depth`
  from 0 to 3.

Deletion is not available in version 1 (§13).

### 6.4 Tool errors

A tool call the broker refuses returns a JSON-RPC error with the numeric
`code` `-32000` and a `data.code` that is one of:

| `data.code` | Meaning |
|---|---|
| `unknown_tool` | The tool does not exist in this contract version. |
| `grant_denied` | The function's enabled grants do not cover the tool. |
| `access_denied` | The run's actor may not perform this action on this graph. |
| `invalid_arguments` | The arguments failed validation, including a scope-changing argument. |
| `limit_exceeded` | The run has used its tool-call budget (§8). |
| `concurrent_call` | A request arrived while an earlier one was unanswered (§6.2). |
| `tool_failed` | The underlying operation failed; `message` carries a sanitized reason. |

A refused tool call does not end the run; the function decides how to react.

## 7. Grants and scope

### 7.1 Grants

| Grant | Allows |
|---|---|
| `graph:read` | The read tools in §6.3. |
| `graph:write` | The write tools in §6.3. Implies nothing about read; a function that writes usually declares both. |

A function's grants are the grants its manifest declares. Every declared grant
must be enabled by the operator before the function can run (§4.4); a tool call
covered by no declared grant returns `grant_denied`. Revoking a grant while a
run is in progress takes effect at that run's next tool call, which returns
`grant_denied`; later invocations are `rejected` until the grant is enabled
again.

### 7.2 Scope

A run acts on exactly one graph, the one the invocation targets, and as exactly
one actor, the one that invoked it. Every tool call is evaluated against both:
the broker checks the grant (§7.1) and then the actor's access through the
authorization hook, read or mutate, exactly as a direct request from that actor
would be checked. A function therefore never has more access than the person or
system that started it.

## 8. Limits

| Limit | Platform maximum | Behaviour when reached |
|---|---|---|
| Wall-clock time | **120 seconds** | The runner terminates the process. The run ends `timed_out`. |
| Memory | 256 MiB | Allocation beyond the limit fails, or the runner terminates the process. The run ends `failed` with `memory_exceeded`. |
| Tool calls per run | 200 | Further calls return `limit_exceeded`. |
| Size of one tool call or response | 1 MiB | The call is refused with `invalid_arguments`, or the response is replaced by a `tool_failed` error. |
| Size of `result.output` | 256 KiB | The run ends `failed` with `output_too_large`. |
| Captured log | 256 KiB | Further log output is dropped and the truncation is recorded. |
| Concurrent runs per function | 1 | A further invocation is `rejected` with `busy`. |

- The maximums in this table are contract maximums. An operator may lower any
  of them for an instance; no setting raises one. These limits keep functions
  short, so that long-running work does not move into them.
- A manifest may set a lower value through `limits` (`timeout_seconds`,
  `memory_mib`, `max_tool_calls`). A value above the contract maximum rejects
  the manifest (§4.3). The limit a run gets is the smaller of the manifest
  value and the instance value.
- The memory limit may be enforced as a limit on the process's address space,
  or, for a runtime that reserves address space it does not use, as the
  runtime's own heap limit under a larger address-space ceiling.
  The helper library (§10) reports an allocation failure as a `result` with
  `status: error` and code `memory_exceeded`, which the host records as
  `failed` with `memory_exceeded`. A process that exits without a result after
  reaching the limit is recorded the same way wherever the runner can tell.
- Termination is forced. The runner does not wait for the function to clean up,
  and writes the function made before it was terminated stay in the graph.
  Functions that make several writes should make them idempotent, so that a
  second run repairs what a terminated run left behind.

## 9. Attribution and loop prevention

Every write a run makes carries an event context set by the host:

- `event_origin` = `function:<function_id>@<function_version>`;
- `event_correlation_id` = the run's correlation id: the invocation's, when it
  carried one, otherwise a new id;
- `event_session_id` = `null` in version 1;
- `attribution` = the invoking actor and scope.

This gives a later graph-change trigger what it needs to avoid loops, and fixes
the rules it must follow:

- **Self-exclusion is mandatory.** A function is never triggered by an event
  whose origin is its own `function:<id>@…`. Unlike event subscriptions, where
  self-exclusion is opt-in, this is not configurable.
- **Chain depth is bounded.** A run started by an event that another function
  run caused gets `chain_depth` = the causing run's depth + 1. The host refuses
  to start a run whose depth would exceed the instance limit (default 3), and
  records the refusal.
- **Correlation is forwarded, not regenerated**, so a chain of runs can be
  traced through one id (as in `EXTERNAL_ADMIN_AND_AUTOMATION_SEAMS.md`).

In version 1, with manual triggers only, `chain_depth` is always 0. The rules are
part of this version so that the trigger slice implements them, not designs them.

## 10. Runtimes

The protocol in §6 is language-neutral. Version 1 names two runtimes:

| `runtime` | Entry point | Notes |
|---|---|---|
| `python` | A `.py` file run with the runner's Python interpreter. | First runtime implemented. |
| `node` | A `.js` or `.mjs` file run with the runner's Node.js. | Contracted here; implemented in a later slice. A host without it rejects `node` manifests as unsupported. |

A thin helper library per runtime hides the protocol. The function only writes
a handler:

```python
from communityoverview_function import run

def handle(envelope, tools):
    node = tools.graph.get_node(node_id=envelope["invocation"]["input"]["node_id"])
    # … compute …
    tools.graph.update_node(node_id=node["id"], updates={"metadata": {"derived": 42}})
    return {"updated": [node["id"]]}

run(handle)
```

The helper library is part of the open core. Third-party dependencies of a
function are outside this contract: version 1 runs a function with only its
own source files and the helper library on its module path. How a later
version installs dependencies is an isolation decision (§13).

## 11. Run records

A run is an `ExecutionJob` with `kind = function`, recorded in the run-history
store that agent runs use, with the same retention. Like an agent run record,
it is history, not queued work: it is enqueued already `RUNNING` when the run
starts and is never claimed, leased or reclaimed by a worker. That store's
retry policy allows one attempt, so a run is executed at most once: there is
no automatic retry in version 1, and a host crash never re-executes a run whose
writes may already have landed. Runs execute in the host process that started
them: when the host starts, it closes every record a previous process left
`RUNNING` as `failed` with `runner_lost`. The run-history retention sweep
closes any record still left behind, which then also reads `runner_lost`.

The run record exposes:

| Field | Meaning |
|---|---|
| `id` | Run id (`job-…`). |
| `function_id`, `function_version` | What ran. |
| `trigger` | `manual` in version 1. |
| `status` | `running`, `succeeded`, `failed`, `timed_out`, `cancelled` or `rejected`. |
| `error` | For `failed`, `timed_out` and `rejected`: a code and a sanitized message. |
| `output` | The validated `result.output`, for `succeeded`. |
| `correlation_id`, `origin`, `actor` | Who started it and the id its writes carry. |
| `tool_calls` | The count, and per call the tool name, outcome and duration, without arguments or results. |
| `usage` | Wall-clock time and peak memory, where the runner can measure it. |
| `isolation` | The runner's isolation level and, at `degraded`, the missing controls (§3.1). |
| `log` | The captured standard error, truncated per §8. |
| `created_at`, `started_at`, `finished_at` | Timestamps. |

Status meanings:

- `rejected`: the run never started, because the manifest failed validation, the
  function or one of its declared grants is not enabled, the invoking actor
  lacks the required access, the input failed `input_schema`, another run of
  the function is in progress, the runner's isolation level is below the
  instance minimum, or the chain-depth limit was reached.
- `failed`: the function returned `status: error`, exited without a result,
  broke the protocol, exceeded memory or produced output that is too large or
  fails `output_schema`.
- `timed_out`: the wall-clock limit was reached.
- `cancelled`: an operator cancelled the run. Cancelling a running run
  terminates its process as in §8; writes already made stay.

An invocation from a caller without read access is refused before any record is
created, so that a caller who may not read the graph cannot fill its run
history; a caller who may read but lacks the access a function needs gets a
`rejected` record with `access_denied`.

Records never contain secret values, credentials or tokens. Tool arguments and
results are not recorded, because they may hold graph content the run record's
readers should not see. For the same reason, a reader whose graph visibility the
authorization hook narrows sees `output`, `log` and `actor` only for runs they
started themselves.

Run-level error codes are a closed set:

| Status | Code | Cause |
|---|---|---|
| `rejected` | `invalid_manifest` | The manifest failed validation (§4.3). |
| `rejected` | `not_enabled` | The function or one of its declared grants is not enabled (§4.4). |
| `rejected` | `access_denied` | The invoking actor lacks the access §5.1 requires. |
| `rejected` | `invalid_input` | `input` failed `input_schema`. |
| `rejected` | `chain_depth_exceeded` | The chain-depth limit was reached (§9). |
| `rejected` | `busy` | Another run of the same function is in progress (§8). |
| `rejected` | `isolation_below_minimum` | The runner's isolation level is below the instance's `isolation_minimum` (§3.1). |
| `failed` | `function_error` | The function returned `status: error`; its code and message are kept, sanitized. A result with code `memory_exceeded` is recorded as `memory_exceeded` instead (§8); the code is reported by the function's own process. |
| `failed` | `runner_lost` | The runner lost control of the run, for example because its supervisor process died (§3.1). |
| `failed` | `no_result` | The process exited without sending `result`. |
| `failed` | `protocol_error` | The function broke the protocol (§6.2). |
| `failed` | `memory_exceeded` | The memory limit was reached (§8). |
| `failed` | `output_too_large` | `result.output` exceeded its limit (§8). |
| `failed` | `invalid_output` | `result.output` failed `output_schema`. |
| `timed_out` | `timeout` | The wall-clock limit was reached (§8). |

The store's states do not change, and its `fail` method takes an optional
`result` so that a failed run keeps the same fields as any other. In the
run-history store:

- `succeeded` is `SUCCEEDED`, recorded with `complete`;
- `failed` and `timed_out` are `DEAD_LETTER`, recorded with `fail`, whose
  `error` (stored as `last_error`) starts with the code (`timeout: …`) and
  whose `result` keeps the run's other fields. The run-history view reads the
  code from that prefix to tell `timed_out` from `failed`;
- `cancelled` is `CANCELLED`, recorded with `cancel`, whose `result` keeps the
  outcome; `rejected` is stored already `CANCELLED`, since the run never
  started, with the outcome and code in its `result`.

## 12. Compatibility

- `contract_version` is a single integer string. A host lists the versions it
  supports and rejects others. The text of a contract version does not change
  once a host implements it, so two hosts that support the same version accept
  and run the same manifests the same way. Until a host implements a version,
  its text may be amended in place.
- Once a version is implemented, any change to what a manifest may contain or
  what a run may do, including
  enabling a capability reserved in §13, is a new contract version.
- A new version is **additive** when every manifest valid under the previous
  version is valid under it and behaves the same: adding a tool, a grant, a
  trigger kind, a `write_target` value or an optional field. A host that
  supports an additive version also accepts manifests that declare the
  versions before it.
- Removing or changing the meaning of a field, a tool, a grant or a status is
  **breaking**. A host may support more than one breaking line at a time.
- The envelope's `schema_version` follows `EVENT_SUBSCRIPTIONS.md`; the
  `invocation` block follows `contract_version`.

## 13. Reserved for later versions

These are deliberately outside version 1. Each is listed with the field or rule
already reserved for it, so that adding it is an additive contract version
(§12) instead of a redesign.

| Capability | Reserved now | Constraint already fixed |
|---|---|---|
| Scheduled triggers | `triggers[].kind = "schedule"`, `retry` | Idempotency per fire time, as in `DURABLE_EXECUTION_CONTRACT.md` §5. |
| Graph-change triggers | `triggers[].kind = "graph_change"`, the `entity` envelope field, `chain_depth` | §9: mandatory self-exclusion, bounded chain depth, forwarded correlation. |
| Inbound webhook triggers | `triggers[].kind = "webhook"` | Authenticated by a bearer token the application generates per function. The token is shown once, stored only as a hash, can be rotated and revoked, and is bound to one function. |
| Session-staged writes | `write_target = "session"` | Writes land in a session overlay and reach the graph only through that contract's review and merge (`SESSION_OVERLAY_CONTRACT.md`). |
| Deletion | grant `graph:delete` | A separate grant, never implied by `graph:write`. |
| Outbound network | `egress`, a broker HTTP tool | Through the broker only, with a per-function allowlist and the existing SSRF guard; never raw sockets. |
| Secrets | `secrets` | References resolved by the broker at the point of use through the `SecretProvider` seam; never placed in the envelope, the environment of the function process, logs or run records. |
| Third-party dependencies | — | Decided with the isolation runtime. |
| Hosted management | — | Workspace-scoped storage, quotas and administration belong to a hosted layer, outside this repository. |

The isolation runtime itself (hardened subprocess, container, microVM or
WebAssembly) is not chosen by this contract; ADR 0012 picks the default runner.
Every runner must satisfy §3, report its level per §3.1, and enforce §8.
