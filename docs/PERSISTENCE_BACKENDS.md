# Persistence Backends

How `GraphStorage` talks to wherever the graph is stored, and what a backend
has to implement to sit behind it. The seam is `backend/core/storage_backends.py`;
this document is the contract a third party implements against.

## Two contracts, one seam

`GraphStorage` keeps the whole graph in memory and treats the backend as the
durable copy. There are two levels a backend can implement:

| Contract | Protocol | Required | Shape of a write |
|---|---|---|---|
| Snapshot | `GraphPersistenceBackend` | always | the whole graph |
| Incremental | `IncrementalGraphPersistenceBackend` | when declared | one entity, or one atomic batch |

Every backend implements the snapshot contract. It is the right shape for
startup, for export, and for the bootstrap write of an empty graph, and it is
the only shape a backend that cannot write one entity at a time needs.

A backend that *can* write one entity at a time implements the incremental
contract as well and **declares** it. `GraphStorage` then delivers a mutation as
the entity operations that describe it — a renamed node is one `upsert_node`,
not a rewrite of every node. Which contract drives a backend is decided by
that declaration, not by an `isinstance` check against the protocols. (The two
things `GraphStorage` does decide by type are the vector and history sidecars,
which exist for the file backend alone — see *Vectors and history*.)

## The snapshot contract

```python
class GraphPersistenceBackend(Protocol):
    def exists(self) -> bool: ...
    def load_graph_data(self) -> dict: ...
    def save_graph_data(self, data: dict) -> None: ...
    def default_graph_name(self) -> str: ...
    def capabilities(self) -> BackendCapabilities: ...
```

- `exists()` — whether there is a stored graph to load. When it returns `False`
  at startup, `GraphStorage` writes an empty graph through `save_graph_data`
  and waits for it, so a second instance opened right after can load it.
- `load_graph_data()` — the graph as a dict with `nodes`, `edges` and
  `metadata` lists/dicts, in the shape of `graph.json` (see
  [DATA_MANAGEMENT.md](DATA_MANAGEMENT.md#graph-json-format)). Node and edge
  dicts are what `Node.to_dict()` / `Edge.to_dict()` produce, plus an
  `embedding` key on nodes for backends without a vector sidecar (below).
  **The dict is handed over**: `GraphStorage` rewrites it in place
  (timestamps become `datetime`s), so return a copy, never your own store.
- `save_graph_data(data)` — persist that dict, whole. **Must be atomic**: a
  reader must see the previous graph or the new one, never a partial write.
  The file backend does this with a temp file and a rename. The dict is
  handed over the other way: a backend may keep it, and `GraphStorage`
  never touches it again. Runs on `GraphStorage`'s single background
  writer thread.
- `default_graph_name()` — the name to fall back on when the stored metadata
  has none.
- `capabilities()` — see below. A backend written before this method existed
  is accepted and treated as declaring nothing.

## Declaring capabilities

```python
@dataclass(frozen=True)
class BackendCapabilities:
    incremental_writes: bool = False
    transactions: bool = False
    change_notification: bool = False
```

| Flag | Means | Consequence in `GraphStorage` |
|---|---|---|
| `incremental_writes` | the entity operations are implemented | mutations arrive as entity operations, not snapshots |
| `transactions` | `apply_batch` lands all of its operations or none | a multi-entity mutation arrives as one batch; without it, as a snapshot |
| `change_notification` | the backend can report changes made by another instance | `GraphStorage` subscribes after its first load and refreshes what each reported entity touches; see *Reporting external changes* |

Everything defaults to `False`; `SNAPSHOT_ONLY` is that default.

The declaration is checked once, when `GraphStorage` is constructed
(`capabilities_of`). A backend that declares `incremental_writes` without
implementing all six methods of the incremental contract is refused there
with a `TypeError`
naming the missing ones — better than failing on the first mutation, after
the in-memory graph has already changed.

## The incremental contract

```python
class IncrementalGraphPersistenceBackend(GraphPersistenceBackend, Protocol):
    def upsert_node(self, node: dict) -> None: ...
    def delete_node(self, node_id: str) -> None: ...
    def upsert_edge(self, edge: dict) -> None: ...
    def delete_edge(self, edge_id: str) -> None: ...
    def apply_batch(self, operations: Sequence[EntityOperation]) -> None: ...
    def checkpoint(self) -> None: ...
```

- An **upsert** receives the entity's serialized form — exactly the dict that
  entity would occupy in a snapshot's `nodes` or `edges` list — and replaces
  the stored entity whole. It is not a patch: fields absent from the payload
  are absent from the entity.
- A **delete** of an entity that is not stored is not an error.
- Each method is complete on return; there is no separate commit.
- `apply_batch(operations)` applies `EntityOperation`s in the order given. A
  backend that declares `transactions` must make the batch atomic. One that
  does not is never handed a batch: it gets single operations, and a snapshot
  for anything larger.
- `checkpoint()` folds anything the backend has deferred into its canonical
  snapshot, so that what is on disk afterwards is the whole graph. A backend
  that defers nothing implements it as a no-op. `GraphStorage` calls it from
  `flush()` and at shutdown.

```python
@dataclass(frozen=True)
class EntityOperation:
    kind: "node" | "edge"
    action: "upsert" | "delete"
    entity_id: str
    payload: dict | None      # the serialized entity for an upsert, None for a delete
```

Operations within a batch are ordered so that an edge never outlives an
endpoint in the store: deleting a node sends `delete_edge` for each incident
edge before `delete_node`. Adding nodes and edges together sends the nodes
first, in their own write, then the edges — as `add_nodes` has always saved.

## How a mutation is routed

`GraphStorage._persist` decides the shape of every write from the declared
capabilities:

| Backend declares | Mutation touches | What the backend receives |
|---|---|---|
| nothing | anything | `save_graph_data` (whole graph) |
| `incremental_writes` | one entity | that entity's method |
| `incremental_writes` + `transactions` | several entities | one `apply_batch` |
| `incremental_writes` only | several entities | `save_graph_data` — the snapshot is atomic, a loop of single writes is not |

All writes — snapshots and entity operations alike — go through the same
single-worker background thread, so they land in the order they were issued;
`flush()` drains both kinds and then asks the backend to `checkpoint()`. A
write that raises puts the exception on its `Future`, as a failing snapshot
always has; the in-memory graph is not rolled back. After a failed *entity*
write the next write — the next mutation, `flush()` or shutdown, whichever
comes first — is a whole-graph `save_graph_data` of the in-memory graph, so a
transient failure heals the way it always did: the next successful write
carries everything, rather than leaving the mutation only in memory.

What still goes through the snapshot path on every backend: the bootstrap
write of an empty graph (also what `reload()` of a missing store does),
`save()` called explicitly (the maintenance scripts), and — on the file
backend — a write while a pre-split `graph.json` still holds vectors the
sidecar does not, because only the snapshot path completes that migration.

## Vectors and history

The file backend keeps node vectors in a binary sidecar and mutation history
in an NDJSON sidecar next to `graph.json`. Both exist only for the file
backend. A write that moved a vector carries it to the sidecar first, on the
snapshot path and the entity path alike. Any other backend:

- receives each node's vector inline, as an `embedding` key on the node
  payload (`None` when the node has none), in snapshots and upserts alike.
  Store it, and return it from `load_graph_data`, or semantic search starts
  empty after every restart;
- has no mutation history; `get_recent_history` returns nothing. A backend
  that wants an audit trail keeps its own.

## Writing a backend

The contract is executable: `backend/core/tests/persistence_contract.py`
holds `PersistenceBackendContract`, the test class every backend is held to,
and `InMemoryGraphPersistenceBackend`, the reference implementation of the
incremental contract. A backend is done when a subclass of the contract class
passes against it.

1. Implement the snapshot contract. Subclass `PersistenceBackendContract` in
   a test module and provide a `factory` fixture — a zero-argument callable
   returning a backend bound to one fresh store, such that calling it again
   opens the *same* store. The snapshot clauses now run against you: a
   snapshot round-trips through a reopened backend, a later one replaces
   the graph whole, the loaded dict is the caller's to mutate, an
   interrupted snapshot leaves the previous graph readable.
2. Declare `SNAPSHOT_ONLY` and ship. This is a complete, correct backend;
   the entity clauses skip themselves.
3. To stop rewriting the whole graph per mutation, implement the incremental
   contract (the four entity methods, `apply_batch` and `checkpoint`),
   declare `incremental_writes`, and `transactions` if your batch is
   atomic. The entity clauses — upsert replaces whole, deletes are
   idempotent, a batch applies in order and lands entirely or not at all,
   writes survive a checkpoint and a reopen, a later snapshot wins — now
   run too.
4. Give the contract a way to hurt you: override
   `interrupt_next_snapshot` and `interrupt_next_append` so the crash-shape
   clauses run (an interrupted snapshot leaves the previous graph readable;
   an interrupted atomic batch lands nothing), and `previous_version_store`
   if there is a store your previous release wrote. Without an override
   those clauses skip, and a skipped clause is an unverified one.
5. If your store can have more than one writer, implement the notification
   protocol and declare `change_notification`. The clauses that then run are
   the ones a shared store exists for: a write made through a second backend
   on the same store reaches a running `GraphStorage` — its node dictionary,
   its edges, lexical search, and the vector index behind semantic search —
   without a restart, an external delete takes the incident edges with it,
   and the subscription ends when the storage shuts down.

`backend/core/tests/test_persistence_contract_file.py` is the file backend
against the contract, with every hook implemented;
`test_persistence_contract_memory.py` runs the reference backend both as
declared and as snapshot-only. `test_persistence_seam.py` covers the other
half — which shape `GraphStorage` hands a backend for each mutation.

The protocols are `runtime_checkable`, so `isinstance(backend,
IncrementalGraphPersistenceBackend)` works, but `GraphStorage` never uses it:
which contract drives you is decided by what you declare. The one type check
it does make is for the file backend's sidecars (above). The contract does
check it: a backend declaring `incremental_writes` must satisfy the
`IncrementalGraphPersistenceBackend` protocol, and one declaring
`change_notification` the `ChangeNotifyingBackend` protocol.

## Reporting external changes

Every read path serves state built at load: the node and edge dictionaries,
the NetworkX graph, the searchable-text cache behind lexical search, and the
vector index behind semantic search. Nothing tells them another writer moved
the store underneath. That is why one instance is the limit today, and it is
what `change_notification` is for — it is a property of the store, not of the
storage engine: a file lock is kernel-local and coordinates nothing between
machines.

A backend that declares it implements two methods:

```python
def start_change_notification(self, listener) -> None: ...
def stop_change_notification(self) -> None: ...
```

`GraphStorage` subscribes once, after its first load — a change reported
against a model that does not exist yet has nothing to refresh — and
unsubscribes in `shutdown_events()`. The listener is called only with
changes the store has already applied, and from a thread of the backend's own
— which thread, and why it matters, is *Which thread reports* below. The
refresh never writes: not the change it was told about, and not the graph it
holds.

The payload a report carries is **read, not taken**. `GraphStorage` parses a
copy, so the dict you hand over comes back exactly as you passed it and stays
yours to cache, log or retry with. (The obligation runs the other way too: a
backend must copy a payload the application hands it on a write — see
`test_the_stored_payload_is_a_copy` in the contract.)

What the backend passes is an `ExternalChange`:

- `ExternalChange.entities(operations)` — the same `EntityOperation`s a
  mutation is delivered as, read the other way round, in the order the store
  applied them. Each upsert carries the entity's new content, so the refresh
  needs no read-back. An upsert whose payload carries an `embedding` hands
  the vector over with it.

  **Report what the store applied together as one change, not one change per
  entity.** The vector index is rebuilt whole whenever it changes, so
  `GraphStorage` settles it once per reported change rather than once per
  operation, and every read is blocked while it does. Splitting a batch of a
  hundred into a hundred reports asks for a hundred rebuilds instead of one,
  each linear in the index — the whole cost of the refresh is decided here,
  by the backend, not by the size of the batch.
- `ExternalChange.unknown()` — the backend knows only that something
  changed. `GraphStorage` drains its own write queue and reloads the whole
  graph. Two things it will not do: bootstrap, so a store that reports it is
  not there (mid-restore, say) is never overwritten with this instance's
  graph — the graph in memory is served on, with a warning — and emit
  per-entity events, since a reload has no before-states. A backend that
  wants subscribers to see individual changes has to report them as
  operations.

A payload this build cannot read stops the batch, and `GraphStorage` logs it
and reloads rather than throwing into the backend's thread — where the
instance that made the write would read it as its own write having failed.
The operations before the unreadable one have already been applied, which is
not a problem in itself: they are real store state, so what is in memory is
incomplete rather than wrong, and the reload completes it. If the reload
cannot run either — the store is being replaced as we read it — that is
logged too and the graph in memory stays behind the store until the next
change is reported. Nothing is raised at the backend on any of these paths.

### Which thread reports

Report from a thread of your own — whatever a notification channel, a poller
or a watcher runs on. One kind of thread is forbidden: **never a thread that
is executing a write**, whether it is the write of the application being
refreshed or of another application sharing the store. In practice: never
synchronously from inside a call an application made into the backend.

A refresh may have to wait for the refreshed application's write queue.
Delivered from inside that application's own write, it would be waiting for
the call it is inside; `GraphStorage` sees that one — the report arrives on
the thread it runs its writes on — and raises `ExternalChangeRefused` back at
you rather than wait. Delivered from inside a *second* application's write it
is worse and quieter: each instance's refresh waits on its own queue while
that queue waits for the other's refresh to return, and both stop for good.
`GraphStorage` cannot see that one. It is yours to get right, and every real
transport already does: a listener connection, a poller and a watcher each
have a thread of their own.

`ExternalChangeRefused` is deliberately not an I/O error. A write that fails
is healed by re-issuing the whole graph, and answering a reporting bug that
way would overwrite whatever the other writer had just committed.

### When both instances wrote the same thing

A local mutation is in memory before it is in the store: it is applied on the
calling thread and written in the background. So when two instances write the
same node, the store settles on whichever write reached it last — and the
instance whose write *won* is the one at risk, because it is never told about
its own write. Applying a report that predates it would leave that instance
serving a value the store does not hold, and nothing would put it right: there
is no later change to report.

`GraphStorage` resolves it as **last writer wins, by `updated_at`**. A reported
node upsert is ignored when the node held in memory carries a later
`updated_at` than the payload; a warning names the node. Consequences worth
knowing before you build on it:

- It is a wall clock. The instances share no other ordering, so their clocks
  have to be roughly in step for this to mean anything.
- A tie defers to the report. Equal stamps are unresolvable, and taking the
  store's side is what converges the two instances.
- A stamp that cannot be compared — one naive against one aware, which a
  backend handing over `datetime` objects of its own can produce — is not an
  answer, so the report is applied.
- **A payload with no `updated_at` is not an unstamped payload.** The model
  fills the field in at parse time, stamped *now*, so such a report is
  normally the newer one and applies — but against a held stamp dated in the
  future, which is what a clock-skewed peer produces, it loses. Send the
  stamp. A payload whose `updated_at` is explicitly `null` does not reach the
  comparison at all: it fails validation, and an unreadable payload is a
  whole-graph reload.
- **Edges carry no `updated_at`**, so an edge upsert is applied as reported.
- **Deletes carry no payload**, so an external delete is applied whatever this
  instance last did to the entity.

A backend that can order writes itself — a log sequence number, a stream id, a
commit timestamp the store assigns — has a better answer than a wall clock, and
should carry it in the payload's `updated_at` rather than leaving it to the
writing instance's clock.

### A local write that failed

A failed entity write leaves a mutation in memory and nowhere else, and
`GraphStorage` heals it by re-issuing the whole graph on the next write,
`flush()` or shutdown. On a shared store that is a last-resort recovery, not
a routine: it re-asserts one instance's whole image over a store someone else
is writing. A refresh arriving while such a write is outstanding therefore
does neither thing — it does not reload (that would drop the mutation) and it
does not heal first (that would overwrite the change being reported). It logs
and leaves both sides intact; the instance stays behind the store until the
write has been re-issued. A backend declaring `change_notification` should
keep that path rare: retry a failing entity write internally rather than
raising, where it can.

Refreshed entities emit the ordinary `node.*` / `edge.*` events, with
`event_origin` set to `external-change`, so subscriptions, agents and the
history see them and an agent that reacts by writing can tell them from its
own instance's work. An external edge whose endpoint this instance does not
have is reported and skipped rather than inventing the missing node.

The reference backend in `persistence_contract.py` implements the protocol:
instances built on the same store notify each other, from one thread the
store owns rather than from inside the write, which is what lets the contract
prove the refresh path end to end — including two `GraphStorage` instances
writing one store at the same time. Because it dispatches rather than
delivers inline, the contract has a `settle_notifications` hook; any backend
whose reports are not delivered before the write returns has to override it.

## Current state

`FileGraphPersistenceBackend` is the default, needs no configuration, and
declares `incremental_writes` and `transactions` — **not**
`change_notification`. Not an oversight: two instances writing one graph file
would fight over the checkpoint that folds the journal back in, and the
`journal_id` binding a journal to its graph assumes a single writer lineage.
Declaring notification would advertise a shared store the file backend cannot
safely be. A shared store is what the seam is there for.

The file backend keeps `graph.json` as the graph — written whole and
atomically — and lands each mutation as one appended
line in `graph.journal.ndjson` beside it, folding the journal back into
`graph.json` every 100 mutations, on `checkpoint()`, and on every whole-graph
save; loading replays the journal — and refuses one written against a
different `graph.json`, which it tells by a `journal_id` the backend keeps in
the file's metadata and stamps on every journal line. That id is the backend's
own: it is not in the dict `load_graph_data` hands out, so `GraphStorage` and
exports never see it. See
[DATA_MANAGEMENT.md](DATA_MANAGEMENT.md#graph-journal) for what that means
for backups and for replacing a graph file. The constructor's
`checkpoint_interval` and `journal_path` are the only knobs, and nothing sets
either in the app.
