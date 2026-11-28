# User functions and triggers threat model

This model describes user functions on main: packaged code that the host runs
in a separate, confined process and that reaches the graph only through a tool
broker. It covers manifest admission, the hardened-subprocess runner and its
sandbox, the broker, manual, webhook, schedule and graph-change triggers,
webhook token storage ([ADR 0014](../../adr/0014-webhook-token-storage.md)) and
session-staged writes
([ADR 0005](../../adr/0005-sessions-may-stage-graph-changes.md)). Function code,
webhook requests and their files, and graph-change event content are treated as
untrusted. The normative behaviour is in
[USER_FUNCTION_CONTRACT.md](../../USER_FUNCTION_CONTRACT.md); the runner design
is [ADR 0012](../../adr/0012-user-function-isolation.md).

Out of scope, because it is not on main: the `node` runtime (the host accepts
only `python`), the reserved `secrets`, `egress` and `graph:delete`
capabilities, third-party function dependencies, HMAC webhook authentication,
and trigger delivery across instances. The authorization hook a deployment
installs, and the authentication in front of the API, are inputs to this model,
not part of it.

## Assets

- Graph content, its visibility rules and per-graph scope, including the
  `origin_graph_id` and federation keys that decide which graph a node belongs
  to.
- Staged session layers and the review and merge step that guards the main
  graph from staged changes.
- The host process, its files and its credentials, which function code must not
  reach.
- Webhook bearer tokens, their hashes in `webhook_tokens.db`, and the per-function
  enable and write switches stored beside them.
- Function run records in the run-history store, and the schedule and
  graph-change delivery ledger.
- Files uploaded to a webhook run, held only for that run.
- Host capacity: CPU, memory, disk, run slots and the scheduler loop.

## Actors

An operator installs function packages under the functions root, enables
functions and grants through `COMMUNITYOVERVIEW_FUNCTIONS_ENABLED`, binds
functions to graphs for webhooks and schedules, and sets the isolation minimum.
A graph administrator, as decided by the authorization hook, mints, rotates and
revokes webhook tokens and sets the webhook switches. An authenticated user or
client invokes functions manually through the REST route or the CLI, and may act
in a staged session. An external system holds a webhook token. The scheduler
acts as the graph-bound `function_schedule` system actor for schedule and
graph-change runs; webhook runs act as the `function_webhook` actor. Adversaries
include a function author whose code is malicious or compromised, a holder of a
leaked token, an unauthenticated caller of the webhook route, a user who edits
graph content to trigger or steer a function, and a user who tries to replay or
claim another actor's run.

## Trust boundaries

1. **Package into host.** The manifest and source files cross from the
   functions root into admission. The manifest decides grants, triggers, limits
   and the write target, but only within what the operator enabled.
2. **Host into function process.** The run's envelope and package cross into a
   confined child process. From here on the function's code, output, log and
   exit are untrusted.
3. **Function into broker.** Every tool call crosses back over standard input
   and output as JSON-RPC. The broker decides grant, scope and access per call.
4. **External caller into webhook route.** An HTTP request with a bearer token,
   a JSON body or multipart files becomes a run acting as a system actor.
5. **Graph events into triggers.** A write by any actor becomes a projected
   event snapshot that is persisted in the ledger and delivered as a run's
   `entity`.
6. **Staged session into main graph.** A session-targeted run writes only to
   the session's layer; content reaches the graph only through merge.

## Entry points

- `POST /functions/{id}/invoke` in
  [routes.py](../../../backend/functions/routes.py) (`invoke_function`), and the
  [CLI](../../../backend/functions/cli.py) that calls it: `input`,
  `correlation_id`, the `Idempotency-Key` header and the request's session.
- `POST /functions/{id}/webhook-token`, `.../webhook-token/rotate`,
  `DELETE .../webhook-token` and `PUT .../webhook-settings`: token management and
  the `enabled` and `allow_write` switches.
- `POST /functions/{id}/webhook` in
  [webhook.py](../../../backend/functions/webhook.py): the `Authorization`
  header, `Idempotency-Key`, and a JSON body or a multipart body with one `input`
  part and file parts.
- The scheduler loop and the graph system listener in
  [scheduler.py](../../../backend/functions/scheduler.py) (`tick`,
  `on_graph_event`).
- The function process's standard output (protocol messages), standard error
  (the log) and exit status, read by
  [protocol.py](../../../backend/functions/protocol.py) and
  [runner.py](../../../backend/functions/runner.py).
- Environment configuration read in
  [server.py](../../../backend/api_host/server.py):
  `COMMUNITYOVERVIEW_FUNCTIONS_ROOT`, `..._ENABLED`, `..._ISOLATION_MINIMUM`,
  `..._WEBHOOK_GRAPHS`, `..._SCHEDULE_GRAPHS`, and `FUNCTION_WEBHOOKS_ENABLED`.

## Threats (STRIDE-style)

- **Spoofing:** a caller presents a guessed, leaked or revoked webhook token; a
  token for one function is used against another; a caller replays an
  idempotency key to obtain another actor's run record; a function claims
  another origin, correlation id, graph or session in its tool arguments.
- **Tampering:** a package uses symlinks or `..` paths to load files from
  outside its directory; a manifest declares grants, triggers or limits beyond
  what is enabled or allowed; a function writes outside its graph, sets or
  clears federation and provenance keys, or rewrites metadata wholesale; a
  session-targeted run writes directly to the graph; a crafted event or a
  changed trigger makes a queued delivery match where the original would not.
- **Repudiation:** a run's writes cannot be traced to the function, version,
  run and invoker; several webhook callers share one token and actor.
- **Information disclosure:** function code reads host files, credentials or
  other processes; a broker result returns nodes the actor may not see; a
  graph-change envelope or the ledger carries sensitive fields; error text
  leaks internal detail to the function; the token database or plaintext
  tokens leak; the webhook route reveals which functions are reachable.
- **Denial of service:** a function loops, allocates, fills its work directory,
  starts processes or floods the broker; webhook callers send large or slow
  bodies or many requests; two functions trigger each other without end;
  frequent schedules or a burst of graph events exhaust run capacity.
- **Elevation of privilege:** a function escapes the sandbox into the host; a
  run gets more access than the actor that started it; a webhook run writes
  without the write switch; a run continues after its grant is revoked.

## Mitigations with code references

- **Admission and enablement.** `FunctionInvocationService` in
  [invocation.py](../../../backend/functions/invocation.py) is the one admission
  path for every trigger. `_refusal` rejects a function that is not enabled or
  whose declared grants are not all enabled, a trigger the manifest does not
  declare, an actor binding without `actor_id`, and failed input; it fails
  closed (`invalid_manifest`) on any exception. `load_manifest` accepts only
  contract versions 1 to 7, the `python` runtime, valid triggers per version,
  known grants, limits at or below `_LIMITS`, and refuses `secrets` and
  `egress`. `_schema` refuses external `$ref` and `$id` values, so validation
  fetches nothing.
- **Package containment.** `_read_manifest` and `load_manifest` check that the
  directory, manifest, entry point and every file resolve inside the function's
  directory, refuse package symlinks, and read the files into memory.
  `HardenedSubprocessRunner.start` in
  [runner.py](../../../backend/functions/runner.py) writes them into a fresh
  `fnrun-*` directory with paths checked by `safe_relative_path`, so the
  function never runs from the installed package.
- **Process confinement.** The runner starts
  [sandbox.py](../../../backend/functions/sandbox.py) with `python -I -S`, an
  empty environment, no inherited descriptors beyond the standard streams and a
  status pipe, and a new session. `confine` applies `apply_rlimits` (address
  space, CPU, file size, 64 open files, no core files), `no_new_privs`, the
  Landlock ruleset of `apply_landlock` (read the interpreter, standard library
  without `site-packages`, helper library and the copied package; write only
  the work directory) and the `seccomp_program` filter (no socket creation, no
  process creation or namespace flags on `clone`, a deny list of kernel
  interfaces). A control the spec asks for that cannot be applied aborts the
  start. `_child` sets `PR_SET_PDEATHSIG` so the function dies with its
  supervisor.
- **Reported isolation level.** `HardenedSubprocessRunner._run_probe` applies
  each offer in a probe child and reports `full` only when `_controls_for` holds
  all four controls (Landlock ABI 3 for `filesystem`, seccomp for `network` and
  `processes`, ABI 6 for `isolation`). `FunctionInvocationService._isolation`
  rejects runs below `COMMUNITYOVERVIEW_FUNCTIONS_ISOLATION_MINIMUM`, admission
  logs a warning at `degraded`, and every run record stores the level and the
  missing controls.
- **Runtime limits.** `supervise` in `sandbox.py` acts as child subreaper, polls
  the work-directory quota with `_check_work_dir_quota` and kills every
  descendant it sees before reporting. `run_function` enforces the wall-clock
  deadline; [protocol.py](../../../backend/functions/protocol.py) `run_session`
  bounds each message and the result size (`MAX_OUTPUT_BYTES`) without
  buffering past them, and `_LogReader` in
  [runner.py](../../../backend/functions/runner.py) keeps at most
  `MAX_LOG_BYTES` of standard error.
- **Broker scope and grants.** `FunctionBroker._dispatch` in
  [broker.py](../../../backend/functions/broker.py) accepts only tools in
  `TOOLS`, checks the declared grant and the live `grant_enabled` callback on
  every call (so revocation applies mid-run), and `_validate_keys` refuses
  `SCOPE_ARGUMENTS` and unknown arguments. `_refuse_graph_scope` and
  `_refuse_reserved_id` refuse federation and provenance keys and `federated::`
  ids; `update_node` metadata needs `metadata_merge: true`; `_ARGUMENT_CAPS`
  bound `limit` and `depth`; the call budget and `MAX_MESSAGE_BYTES` apply.
- **Actor access per call.** The broker re-binds the invoking actor's
  authorization inputs with `use_request_authorization` and evaluates the hook
  for every call. For the graph-bound actors in `_GRAPH_BOUND_ACTORS` it wraps
  the call in `restrict_function_graph` from
  [access.py](../../../backend/service/access.py), requires the decision to
  match the bound graph, and stamps `origin_graph_id` on added nodes.
  `_narrow_similar_nodes` filters similarity results the service does not
  narrow. Service exceptions become a fixed `tool_failed` message; a denied
  decision's reason and a service result's `error` or `message` text are
  forwarded through `_sanitize`, which only trims and truncates them.
- **Attribution.** Broker writes other than `graph.attach_run_file` carry
  `function_origin` and the run's correlation id, and run inside
  `function_mutation_context` with the run's chain depth;
  `graph.attach_run_file` runs under `bind_execution_context` with the run's
  correlation id only. Run records keep the actor, scope, origin, trigger, tool
  names, outcomes and durations; the per-call `ToolCallRecord` holds no tool
  arguments or results, but the record also stores the function's output and
  error text.
- **Idempotency and replay.** `admit` stores keys as `fn:<id>:<key>` (or
  `schedule-fn:` for scheduler runs) and `_replay` returns an existing run only
  for the same write target and session (`_binding_digest`), to a caller with
  an actor id, after a fresh read decision; for a record that is not redacted
  it also requires the same actor and scope. One run per function at a
  time (`_busy`), serialized by `_admission`.
- **Webhook tokens.** `WebhookTokenStore` in
  [token_store.py](../../../backend/functions/token_store.py) creates the
  database `0600` in a `0700` directory, mints `secrets.token_urlsafe(32)`,
  stores only its SHA-256 hash, allows one token per function, and rotates and
  revokes by function, graph and token id. `register_token_routes` in
  `routes.py` requires an `admin` decision matching the bound graph for every
  management call, mints only when `COMMUNITYOVERVIEW_FUNCTIONS_WEBHOOK_GRAPHS`
  binds the function to that graph, and returns tokens with
  `Cache-Control: no-store`. `server.py` refuses a token database inside the
  graph directory or any configuration directory.
- **Webhook ingress.** The `webhook` route returns 404 unless
  `FUNCTION_WEBHOOKS_ENABLED`, the function's `WebhookPolicy` switch and its
  enablement are all on; it accepts exactly one `Bearer` header whose token
  resolves to this function's row. `WebhookPolicy.get` joins the switches to
  the current token row, so revoking the token disables them.
  `WebhookPolicy.admit` limits each function to 60 admitted requests per rolling minute. JSON bodies stop at
  `MAX_INPUT_BYTES`; `_read_multipart` caps total bytes, part headers and part
  count, and `READ_DEADLINE_SECONDS` bounds the upload. `graph:write` and the
  `mutate` check apply to a webhook run only when `allow_write` is set.
- **Run files.** `RunFiles` in
  [run_files.py](../../../backend/functions/run_files.py) keeps uploads under
  private state, bounded by `MAX_RUN_FILES` and `MAX_RUN_FILE_BYTES`, addressed
  by run-scoped ids, and removed when the run ends; `remove_leftovers` clears
  them at startup. `graph.attach_run_file` is refused in a staged session.
- **Schedules.** `FunctionScheduler.tick` generates only the latest
  epoch-aligned slot for functions that are enabled and bound in
  `COMMUNITYOVERVIEW_FUNCTIONS_SCHEDULE_GRAPHS`. `ScheduleSlots` in
  [schedule_slots.py](../../../backend/functions/schedule_slots.py) admits at
  most 60 attempts per function per rolling minute, retries only confirmed
  failures, and never reclaims an attempt with an uncertain outcome.
  `server.py` refuses a ledger that shares a file with the execution databases.
- **Graph-change triggers.** `on_graph_event` ignores `external-change` events
  and the function's own origin, and delivers only when every present snapshot
  is tagged with the bound graph. `valid_trigger` in
  [graph_change.py](../../../backend/functions/graph_change.py) refuses trigger
  paths that overlap a schema `sensitive_fields` path, checked against the
  current schema at enqueue and delivery; `filtered_entity` persists only
  declared paths and omits sensitive subtrees. `_filter_graph_event` skips a
  delivery whose trigger changed unless `projection_covers` holds.
  `enqueue_event` raises chain depth for function- and agent-origin events, and
  `admit` rejects depth above `chain_depth_limit` (3 by default).
- **Session-staged writes.** `admit` takes the session only from the request
  context, requires `named_mode` to be `staged` and a `mutate` decision with
  `GRAPH_OPERATION_STAGE`, and refuses a graph-targeted invocation that carries
  a session. `load_manifest` refuses a session target for schedule and
  graph-change triggers, and `replay_webhook` refuses session-targeted
  functions. The broker binds the session with `bind_session`, re-checks the
  staged mode per call, and `StagedWrite._commit` in
  [staging.py](../../../backend/service/staging.py) labels an entry with
  `function_origin` only when every contributing write has that origin. The
  graph changes only through `merge_staged_changes` in
  [service.py](../../../backend/service/service.py), authorized as
  `GRAPH_OPERATION_MERGE`.
- Behaviour is exercised by the tests in
  [backend/functions/tests/](../../../backend/functions/tests/).

## Residual risk

- At isolation level `degraded`, which is the default minimum, a function can
  read, and without the `filesystem` control also write, what the
  application's user can, including configuration and credentials, and without the `isolation` control it can signal the
  supervisor; termination of its descendants is then best effort. Seccomp is
  available only on x86-64 and arm64. Operators who do not fully trust function
  authors must set the minimum to `full`.
- Function code is trusted to the extent of its enabled grants and the invoking
  actor's access. A `graph:write` function may make any write the actor could,
  and writes made before a timeout or kill stay in the graph.
- Run records include the function's own captured log, its output and its
  error message and `function_code`, which `function_run_to_dict` in
  [run_history.py](../../../backend/agents/run_history.py) returns as stored.
  The run-history routes in
  [agent_routes.py](../../../backend/api_host/agent_routes.py) do not apply the
  authorization hook per record, so graph content or tool results a function
  puts in any of these are readable by whoever can read run history.
- Error text that the authorization hook or a service result returns reaches
  the function after `_sanitize` trims and truncates it; it is not replaced by
  a fixed message, so whatever detail that text carries is visible to function
  code.
- A redacted run record keeps no attribution, so `_replay` cannot compare actor
  and scope; any caller with an actor id whose current read decision allows it
  gets the redacted record back for a reused idempotency key.
- A webhook token is a bearer credential: any holder acts as the
  `function_webhook` actor, and callers sharing a token cannot be told apart.
  The token database is not encrypted, is excluded from graph export, and must
  be protected and backed up by the operator. Token management records only
  creation and rotation times in the token row.
- The rate limit counts only authenticated webhook requests; unauthenticated
  requests still cost a hash lookup, and the 404 versus 401 answer reveals
  which function ids have webhooks enabled. A token holder can keep a function
  busy for up to `READ_DEADLINE_SECONDS` with a slow multipart upload.
- Webhook input and run files are attacker-controlled. Input schema validation
  is the only check before the function sees them; the function must treat
  them as untrusted.
- Graph-change projection relies on schema `sensitive_fields`. A secret stored
  in a path not marked sensitive, or declared by a trigger, enters the envelope
  and the ledger. Finished deliveries remain in the ledger for 30 days.
- Chain-depth tracking covers function- and agent-origin events only. Merged
  staged writes fire events with origin `session-merge`, so a chain restarts at
  depth zero after a merge; the merge step is the control.
- Delivery, the in-process upload hold and run serialization assume one host
  process. Events from other writers are not delivered, and a second instance
  sharing the stores is not covered by this model.
- Disk-quota scanning is polling and can be overshot or raced between scans, as
  described in the contract §8; operators should also cap the backing storage.
