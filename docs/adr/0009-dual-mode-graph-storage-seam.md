# ADR 0009 — Dual-mode graph storage: ratify the seam, keep one graph per runtime

- **Status:** Proposed
- **Date:** 2026-10-08
- **Scope:** Open-source core only
- **Related:** [`PERSISTENCE_BACKENDS.md`](../PERSISTENCE_BACKENDS.md) (the seam
  this ADR is about), [ADR 0008](0008-node-attachment-storage.md) (the adjacent
  byte-storage seam, and the prior art for how a storage seam is scoped to a
  graph), [`CORE_RUNTIME_AND_EXTENSION_ENABLEMENT.md`](../CORE_RUNTIME_AND_EXTENSION_ENABLEMENT.md)
  (standalone-versus-hosted framing, and the sentence this ADR finds in
  tension with it), [`CORE_ENABLEMENT_IMPLEMENTATION_PLAN.md`](../CORE_ENABLEMENT_IMPLEMENTATION_PLAN.md)
  (Priority 4 asks for exactly this identification step),
  [`CAPACITY.md`](../CAPACITY.md) (the measured per-graph cost),
  `backend/core/storage_backends.py`, `backend/core/postgres_backend.py`,
  `backend/api_host/persistence.py`, `backend/runtime/request_context.py`,
  `backend/runtime/authorization.py`, `backend/service/access.py`

## Context

The brief is: *introduce the core storage seam needed for hosted shared storage
while preserving file-only persistence as a supported standalone mode.* The
hosted layer sits on whatever that seam turns out to be, so the shape is
expensive to change later.

Reading the code first changes the question. **A dual-mode graph persistence
seam already exists, is documented as a third-party contract, and has a second
backend behind it.** What does *not* exist is anything that lets one running
process serve more than one graph — and that is the only part of "hosted shared
storage" the existing seam does not reach. This ADR is therefore about the
boundary, not about inventing a persistence abstraction that is already there.

This ADR decides nothing on its own: it lands as a proposal so the owner can
settle one question that the repository's own documents currently answer two
different ways.

### What the code does today that constrains the answer

- **The seam exists, with two levels.** `GraphPersistenceBackend` and
  `IncrementalGraphPersistenceBackend` (`backend/core/storage_backends.py`)
  are `Protocol`s: snapshot writes (`load_graph_data`, `save_graph_data`,
  `exists`, `default_graph_name`, `capabilities`) and, when a backend
  **declares** them through `BackendCapabilities`, entity writes
  (`upsert_node`, `delete_node`, `upsert_edge`, `delete_edge`, `apply_batch`,
  `checkpoint`). Routing is decided by that declaration, not by an
  `isinstance` check. [`PERSISTENCE_BACKENDS.md`](../PERSISTENCE_BACKENDS.md)
  is already written as the contract a third party implements against.
- **File-only is the default and needs no configuration.**
  `FileGraphPersistenceBackend` keeps `graph.json` plus a
  `graph.journal.ndjson` journal, and declares `incremental_writes` and
  `transactions` — deliberately not `change_notification`, because two
  instances writing one file would fight over the checkpoint.
- **Shared storage is already implemented, and is already optional.**
  `PostgresGraphPersistenceBackend` (`backend/core/postgres_backend.py`) is
  selected by `GRAPH_BACKEND=postgres` through `build_persistence_backend`
  (`backend/api_host/persistence.py`), with `psycopg` in
  `backend/requirements-postgres.txt` rather than the base requirements, and
  nothing on the always-imported path touching it. It carries
  `GRAPH_POSTGRES_SCHEMA` (one database, several graphs, one per schema), an
  optional opaque `GRAPH_POSTGRES_SCOPE` row scope with a row-level security
  policy bound on it under `FORCE ROW LEVEL SECURITY`, cross-instance change
  notification over `LISTEN`/`NOTIFY`, and traversal answered by the store.
- **`GRAPH_BACKEND=file` returns `None`.** `build_persistence_backend` hands
  back no backend for the default, so `GraphStorage` constructs its own from
  `json_path` exactly as it did before that module existed. The standalone
  path has no second construction site to drift from the first.
- **The in-memory model is one graph per process.** `GraphStorage` holds the
  whole graph in memory and treats the backend as the durable copy. One
  instance is constructed in `create_app` (`backend/api_host/server.py`) and
  then bound into everything downstream in that same function: the
  `FederationManager` event callbacks, `setup_events`, the `GraphService`, the
  `AgentRegistry`, and the agent delivery callback. Nothing in that wiring is
  parameterised by a graph.
- **That residency has a measured price, and it is not only memory.**
  [`CAPACITY.md`](../CAPACITY.md) puts the marginal cost at ~10,069 B per node
  and its 1.7 edges for the file backend (~9,180 B for PostgreSQL), over a
  process floor of ~50 MB (~61 MB with psycopg imported). A second graph in
  the same process costs a second graph's resident memory **and a second set
  of connections**: `PostgresGraphPersistenceBackend.__init__` builds its own
  pool and opens its own listening connection on every construction, and
  nothing shares either across two of them, so a process holding N graphs
  holds N of both. [`PERSISTENCE_BACKENDS.md`](../PERSISTENCE_BACKENDS.md)
  ("Sizing it: what an instance costs") gives the multiplier as
  `instance_count × (pool_size + 1) × graph_count`, which at the default
  `pool_size` of 4 is 5 server connections per graph held.
- **Two sidecars exist for the file backend alone.** `GraphStorage` builds the
  embedding sidecar and the history sidecar only when the backend is a
  `FileGraphPersistenceBackend` (`_init_embedding_sidecar`,
  `_init_history_store`). Under `postgres`, vectors travel inline in the node
  payload and mutation history stops: `/api/history` returns nothing and
  `HISTORY_MAX_EVENTS` / `HISTORY_MAX_AGE_DAYS` go inert. **The asymmetry runs
  the opposite way to the one this task was written to guard against** — today
  it is the shared mode that is the reduced one, not standalone.
- **Sessions do not follow the graph.** They stay file-backed under
  `SESSIONS_DIR`, or a directory derived from the graph path when it is unset
  (`AppConfig.resolve_sessions_dir`), so two instances can share a graph
  without sharing sessions.
- **A PostgreSQL schema is already claimed by one graph.**
  `_claim_or_check_graph_identity` writes the graph name into the
  `graph_metadata` row on first use and raises `GraphIdentityCollision` if a
  second graph name meets it. The schema is treated as one graph's home, in
  code, today.
- **Several scopes behind one set of tables is ruled out on the record.**
  [`PERSISTENCE_BACKENDS.md`](../PERSISTENCE_BACKENDS.md) ("Keeping scopes
  apart") measures why: `graph_metadata` holds exactly one row for the whole
  store, so graph metadata crosses scopes and either scope's whole-graph save
  replaces it; `exists()` answers for the store rather than the scope, so a
  scope with no rows loads another scope's metadata; `id` stays the primary
  key of the whole table, so two scopes cannot both hold `n0` and the backend
  refuses such a write; and change notification is per schema, so scopes hear
  each other's entity ids. Its conclusion is explicit: *a schema per scope has
  none of this*.

### What the request seams left, and what they did not

The dependency for this work is the request/workspace/graph-scope seam. What
it actually left in the code:

- `backend/runtime/request_context.py` resolves an **actor** (id, type, auth
  source) and a **scope** (`workspace_id`, `workspace_kind`, `graph_id`) from
  request headers, explicit overrides or environment, with a public summary
  that omits the identifiers. Standalone resolves to `source: "default"` and
  an anonymous, unauthenticated actor.
- `backend/runtime/authorization.py` adds a `GraphAuthorizationHook` protocol,
  a standalone-safe default hook with `permissive` / `read-only` / `deny-all`
  modes, and `GraphAccessNarrowing` (`enabled`, `allow_local_graph`,
  `include_graph_ids`).
- `backend/service/access.py` applies that narrowing by **filtering nodes
  already in memory**: `node_graph_id` reads `metadata.origin_graph_id`, an
  edge is visible only when both endpoints are, and stats and federation
  display names are recomputed from the visible set.

Two consequences matter here, and both are easy to misread:

1. **Graph-scope narrowing is a visibility filter over one store, not a store
   selector — and in this repository it is inert.** When enabled it decides
   which nodes of the single resident graph a request may see. No code path
   uses `graph_id` to choose where a read or a write goes. And nothing in the
   core switches it on: `DefaultGraphAuthorizationHook` returns the default
   `GraphAccessNarrowing`, whose `enabled` is `False`, and `matches()`
   short-circuits to `True` while it is; no production code constructs one
   with `enabled=True`, and `GraphService` defaults to that hook. So
   `include_graph_ids` can only arrive from an injected hook, which lives
   outside this repository. What the core does with a request's declared
   `graph_id` today is put it in the authorization context for a hook to
   consider, and record it in attribution — nothing else.
2. **`graph_id` already names something else.** `metadata.origin_graph_id` is
   stamped by `FederationManager` from `federation_config`'s
   `graphs[].graph_id` onto *cached federated* nodes; a local node carries
   none, which is what `allow_local_graph` admits. Reusing the same request
   field as a storage selector would put two namespaces — federation peers and
   local stores — behind one identifier.

Separately, `EventScopeAttribution` (`backend/core/events/models.py`) carries
`workspace_id`, `workspace_kind` and `graph_id` on mutation events, taken from
the request. **Nothing reconciles that declared `graph_id` against the store
the write actually landed in**, because with one resident graph there is
nothing to reconcile. Any design that makes a process serve several graphs
turns that from a tautology into an unverified claim in an audit record.

### What the repository already commits to — and the tension

[`CORE_RUNTIME_AND_EXTENSION_ENABLEMENT.md`](../CORE_RUNTIME_AND_EXTENSION_ENABLEMENT.md)
says both of these, a few lines apart:

- for standalone open-core operation, *"one deployment normally serves one
  graph"*;
- for the target hosted model, *"tenant-aware operation should not imply one
  dedicated application stack per graph"*, with *"multiple graphs or customer
  workspaces served by the same runtime instances"* as the default target
  architecture.

Those two are compatible only if "runtime instance" means the hosted service
environment rather than a core process. If it means a core process, the second
sentence requires the core to grow multi-graph serving, which the first does
not anticipate and no code supports.

[`CORE_ENABLEMENT_IMPLEMENTATION_PLAN.md`](../CORE_ENABLEMENT_IMPLEMENTATION_PLAN.md)
Priority 4 asks to *"identify the storage abstraction seams needed before a
shared RBAC-aware storage backend is introduced"*. This ADR is that
identification step; the shared backend itself arrived first.

[ADR 0008](0008-node-attachment-storage.md) answered the same question for
attachment bytes and is the prior art: a seam deliberately beside the graph
persistence seam, with a backend chosen at **boot** from
`ATTACHMENT_BACKEND`, and a `namespace` that is **one per graph**, defaulting
to the file backend's graph stem or `pg:<GRAPH_POSTGRES_SCHEMA>`. It also
explains why the row scope is deliberately *not* part of that namespace. Any
proposal here that made storage per-request rather than per-boot would leave
the two seams disagreeing about what a graph is.

## Forces

- **The standalone requirement is a stated requirement, not a preference.**
  File-only must stay genuinely supported: the default, needing no
  configuration, with no feature of its own removed to make the shared mode
  work. Any option that makes it a test fixture fails the brief. Today the
  file backend is the *richer* mode (history and the vector sidecar), so the
  live risk is not neglect but a change that strips it for symmetry.
- **One consumer is blocked on this, and it sits outside this repository.** A
  hosted shared-storage migration depends on this task; its own description is
  to move the hosted service off file-bound persistence *without breaking the
  core standalone mode*. What it needs from the core is a seam it can put a
  shared store behind — which exists — plus a defensible answer to who
  multiplexes graphs.
- **A sibling consumer already shipped, over the request seams rather than
  this one.** A hosted audit-event ingestion pipeline was completed against
  the core event and attribution seams and depends on the request seams, not
  on this task. It is evidence rather than a pending requirement: it fixes
  `EventScopeAttribution`'s shape as something already consumed, so the
  `graph_id` reconciliation gap above is a live concern for it and the field
  cannot be quietly repurposed.
- **One-graph-per-instance is the owner's standing direction.** It is recorded
  in the project's planning records and stated in this repository's prose, and
  it is a commercial boundary as much as a technical one. An ADR cannot move
  it; it can only say what follows either way.
- **Two ceilings bound "many graphs, one process", and the backend is not the
  one that was removed.** Shared storage removed the "many instances, one
  graph" limit. What bounds the other direction is resident memory per graph
  *and* connections per graph — and the connection ceiling is the one that
  fails a boot rather than slowing a process, because an instance that cannot
  get a connection does not start. Either way it would be a read-path and
  lifecycle change, not a backend swap.
- **Reversibility is asymmetric.** Adding a per-request store selector later
  is a change to boot wiring. Removing one after the hosted layer depends on
  it is not.

## Options considered

### Option A — Ratify the seam as it stands: one graph per core runtime (recommended)

Treat the existing persistence seam as *the* dual-mode storage abstraction,
and state the property it already has: a core runtime binds exactly one graph
at boot, for every backend. A hosted layer serving many graphs does the
multiplexing above the core, not inside it. The file-only default is
unchanged.

- **For:**
  - No new seam, no new code on the request path, nothing for the standalone
    mode to lose.
  - Matches what both storage seams already do: the graph is fixed at boot
    (`ATTACHMENT_NAMESPACE` per graph, a PostgreSQL schema claimed by one
    graph name, `GraphStorage` constructed once in `create_app`).
  - Matches the owner's standing direction, and keeps the shared-store work
    that already landed as the answer to the problem it was built for — many
    instances, one graph.
  - Every boot-bound singleton (federation manager, agent registry, event
    subscriptions, sessions directory, history and vector sidecars) stays
    correct by construction.
  - The audit `graph_id` stays trivially true: one process, one graph.
- **Against:**
  - It reads against the hosted sentence in
    `CORE_RUNTIME_AND_EXTENSION_ENABLEMENT.md`, so that document has to be
    amended or the hosted layer has to accept a core runtime per graph.
  - The hosted layer pays per-graph process overhead (~50–61 MB floor each)
    and per-graph operational surface. For many small graphs that is the
    expensive shape. (Connections are not a differentiator: at
    `pool_size + 1` per graph held they cost the same under either option.)
  - It leaves the property enforced nowhere: two prose sentences and no check.

### Option B — A graph-resolver seam above the persistence seam

Introduce a resolver that maps a request's graph scope to a `GraphStorage`
instance, with a registry of resident graphs; `create_app` takes the resolver
instead of a single store. Standalone binds a single-entry resolver that
always returns the same store.

- **For:**
  - Delivers literally what the hosted target sentence describes: several
    graphs on the same runtime instances.
  - Amortises the process floor (~50–61 MB) across graphs, and a lazy,
    evictable registry bounds memory by resident graphs rather than by owned
    graphs — subject to the connection ceiling below, which it does not
    amortise.
  - The resolver is the natural place to make the declared `graph_id` and the
    serving store agree.
- **Against:**
  - It reverses the owner's standing direction on what open core is for, which
    is not an architectural call.
  - The storage seam is the small part. Every singleton bound in `create_app`
    becomes per-graph: federation (which has its own `graph_id` namespace),
    the agent registry and its workers, event subscriptions and webhook
    delivery, the sessions directory, the history and vector sidecars. Several
    of those are stateful and keyed by nothing today.
  - Resident memory is one ceiling (~10 kB per node), so a registry needs
    eviction, and eviction interacts with the single background writer thread
    and the journal/checkpoint lineage each `GraphStorage` owns.
  - **It amortises the process floor but not the connections, and the
    connection ceiling is the one that refuses rather than degrades.** Each
    graph held costs `pool_size + 1` server connections — 5 at the default
    pool size — so the budget is
    `instance_count × (pool_size + 1) × graph_count`
    ([`PERSISTENCE_BACKENDS.md`](../PERSISTENCE_BACKENDS.md), "Sizing it:
    what an instance costs"), and against a stock server's 97 available
    connections five instances holding four graphs each is already 100. In
    fairness to this option that cost is per graph *held*, so Option A pays
    the same total for the same graphs; what Option B saves is the process
    floor, not the connections. The consequence specific to a registry is
    that holding graphs lazily only helps if eviction also closes the pool,
    and an instance that cannot get a connection fails to boot rather than
    running slowly — so the saving is in the resource that degrades and the
    risk is in the resource that refuses.
  - It overloads the request `graph_id`, which currently means a federation
    peer, with "which store", or introduces a second identifier and a mapping
    between them.
  - The standalone path gains a resolver it never needed — survivable
    (single-entry, byte-identical behaviour) but it is new code on every
    request in the mode that is supposed to stay simplest.
  - It is a read-path and lifecycle change of a size that wants its own ADR
    once the boundary question is settled, not a paragraph in this one.

### Option C — One store, many graphs by row scope only

Keep one resident graph per process but let several graphs share one set of
PostgreSQL tables, separated by the opaque row scope and its policy.

- **Against:** ruled out on measured evidence already in the repository, not
  on taste: one `graph_metadata` row for the whole store, `exists()` answering
  for the store rather than the scope, `id` as the primary key across scopes,
  and per-schema change notification. `PERSISTENCE_BACKENDS.md` states the
  conclusion directly. The row scope is the layer *underneath* schema-per-graph
  separation, not a replacement for it.

### Option D — The hosted layer owns graph storage

Move node and edge storage behind the hosted boundary; the core reads and
writes through it.

- **Against:** it fails the stated requirement outright — the core would no
  longer run standalone on a file — and it moves graph schema evolution
  somewhere no contributor to this repository can see it. Recorded here only
  so the option is visibly rejected rather than unconsidered.

## Proposal

**Option A**, with one gap closed and the boundary question referred to the
owner.

1. **Ratify the existing persistence seam as the dual-mode storage
   abstraction.** `GraphPersistenceBackend` + `IncrementalGraphPersistenceBackend`
   with capability declaration is the contract; `GRAPH_BACKEND` selects a
   backend at boot; `file` is the default and constructs through
   `GraphStorage` itself. No new abstraction layer is introduced for shared
   storage, because the shared backend already sits behind this one.

2. **Promote the unit of storage identity into the backend contract: one
   graph per core runtime, every backend.** This is descriptive of today's
   code (`create_app`, `_claim_or_check_graph_identity`,
   `ATTACHMENT_NAMESPACE`), and it is not unstated —
   [`PERSISTENCE_BACKENDS.md`](../PERSISTENCE_BACKENDS.md) already says every
   deployment this repository ships holds one graph per process, and
   `CORE_RUNTIME_AND_EXTENSION_ENABLEMENT.md` says a deployment normally
   serves one graph. But it sits there as an aside inside a capacity-sizing
   section, next to the arithmetic for a process that holds several. What is
   proposed is elevating it to a stated property of the seam, where a
   third-party backend author reads the contract rather than the sizing notes.

3. **Protect file-only explicitly rather than by accident.** Two properties
   are the test of "still first-class", and both are checkable: a clone with
   no configuration runs the file backend with no extra dependency, and no
   capability is removed from the file backend to make another backend
   symmetric. Where the shared mode lacks something the file mode has (history
   today), that is the shared mode's gap to close, never the file mode's
   feature to drop.

4. **Close the one real gap: bind the runtime's graph identity at boot and
   refuse a request that contradicts it.** The runtime knows which graph it
   serves; a request may declare a `graph_id`; nothing compares them, so an
   unverified identifier reaches the audit record as if the store had supplied
   it. The proposal is that a declared graph scope be checked against what
   this runtime can legitimately be asked about, and refused through the
   existing authorization seam otherwise.

   **The predicate is the hard part, and it is not "equals this runtime's
   graph".** As the Context section establishes, this field's values come from
   the *federation* namespace — `federation_config`'s `graphs[].graph_id` —
   and a local node carries no graph id at all, which is why
   `allow_local_graph` exists. A federation peer id therefore never equals
   this runtime's own graph name by construction, so a rule written that way
   would refuse the field's entire legitimate use rather than only
   contradictory values. The check has to be "this runtime's own graph
   identity, or a graph id this deployment's federation config declares", and
   refuse anything else. If that pairing is judged too loose to be worth
   having, the honest conclusion is that closing this gap needs the second,
   store-identity identifier this ADR argues for above, and that it should
   wait for one rather than overload the field further.

   This is wanted under either option — it is a correctness property of
   attribution, not a multi-graph feature — and it is the one piece of work
   this ADR would put in this repository.

5. **Do not build a graph resolver until the boundary question is answered.**
   If the owner answers it the other way, Option B becomes the decision, this
   ADR is superseded, and the per-graph lifecycle work listed under Option B
   gets its own ADR. The costs are written down above so that conversation
   starts from evidence.

## Consequences

If this proposal is accepted:

- The blocked hosted consumer can proceed against a seam that is already
  present and documented: a shared store behind `GRAPH_BACKEND=postgres`, with
  schema-per-graph separation and an opaque row scope whose value the hosted
  layer supplies. The part that stays outside this repository is routing a
  request to the runtime that serves its graph.
- A hosted deployment serving many graphs runs a core runtime per graph behind
  its own router, and pays that process floor per graph. That is a real cost
  and the reason the boundary question is worth asking rather than assuming.
- `CORE_RUNTIME_AND_EXTENSION_ENABLEMENT.md`'s hosted paragraph needs
  amending to say that the shared runtime environment, not a single core
  process, is what serves several graphs — or this ADR is the wrong answer.
  That edit is deliberately not made here: it is the decision, and the
  decision is the owner's.
- Audit attribution gains a property it does not have: an event's `graph_id`
  either names this runtime's graph or the request was refused.
- Nothing changes for an existing standalone deployment. No migration, no new
  dependency, no new required configuration.

### Existing persisted data

- **No change to any on-disk or in-database format.** `graph.json`, the
  journal, the embedding and history sidecars, and the PostgreSQL entity
  tables are all untouched by this proposal. Item 4 adds a refusal on a
  request path, not a field.
- **No migration.** Nothing needs rewriting in either direction, and a graph
  moved between backends with `scripts/graph_file_to_postgres.py` is
  unaffected.
- **One behaviour change to be aware of, and only if item 4 is implemented:** a
  deployment that sets `COMMUNITYOVERVIEW_GRAPH_SCOPE_ID`, or sends
  `x-communityoverview-graph-id`, to a value the predicate does not admit
  would start being refused where it is currently served. The current
  permissiveness is total: the core does not narrow on the value at all
  (narrowing is inert without an injected hook), so it reaches the
  authorization context and the audit record and is otherwise ignored. A
  deployment relying on that should be identified before any refusal lands —
  which is the main reason item 4 is worth reviewing on its own rather than
  folding into a larger change, and why its predicate is written out above
  rather than left to the implementation.

### `config/default/schema_config.json`

**No change.** Nothing here adds or renames a node type, a relationship type
or a required field, so the migration surface that document guards is not
touched. Nor does anything here change a public REST or MCP contract: item 4
adds a refusal using the existing authorization-denial shape rather than a new
endpoint, request field or response field. If a later Option B were chosen,
that assessment would have to be made again for it.

## What this ADR does not decide

Deliberately left open, because each is the owner's:

1. **Does the hosted target mean many graphs per core process, or many graphs
   per hosted service environment?** Everything above follows from this one
   answer, and the repository currently says both. Answering it "per process"
   makes Option B the decision and supersedes this ADR.
2. **Should one-graph-per-runtime be enforced in code, stated publicly as the
   open-core boundary, or left as a documented convention?** It is currently
   prose in two documents and enforced nowhere. This matters beyond
   architecture — if it is the line between the open core and a hosted
   offering, it has neither a check nor an authoritative public statement
   today.
3. **Should mutation history be made available under the shared backend?** It
   stops there today. Item 3 above says the gap belongs to the shared mode
   rather than being closed by removing the sidecar, but whether to close it,
   and at what cost, is not this ADR's call. It is the one place where the two
   modes are genuinely unequal in capability.
4. **If item 4 is implemented, is a contradicting graph scope a refusal or a
   logged warning?** A refusal is proposed because an unverified identifier in
   an audit record is worse than a failed request, but a deployment already
   sending such a value would see behaviour change, so the choice is the
   owner's to take with that in view.
5. **Where does per-graph *provisioning* live** — creating, suspending and
   removing a graph and its store? `EXTERNAL_ADMIN_AND_AUTOMATION_SEAMS.md`
   already places tenant lifecycle management outside the core. Nothing here
   changes that, and nothing here asks the core to grow it.
