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
    store_traversal: bool = False
```

| Flag | Means | Consequence in `GraphStorage` |
|---|---|---|
| `incremental_writes` | the entity operations are implemented | mutations arrive as entity operations, not snapshots |
| `transactions` | `apply_batch` lands all of its operations or none | a multi-entity mutation arrives as one batch; without it, as a snapshot |
| `change_notification` | the backend can report changes made by another instance | `GraphStorage` subscribes before its first load, holds what arrives until the load returns, then refreshes what each reported entity touches; see *Reporting external changes* |
| `store_traversal` | `traverse` is implemented | `get_related_nodes` asks the store for the reachable ids instead of walking the in-memory graph — but only while the store is current; see *Answering a traversal from the store* |

Everything defaults to `False`; `SNAPSHOT_ONLY` is that default.

The declaration is checked once, when `GraphStorage` is constructed
(`capabilities_of`). A backend that declares `incremental_writes` without
implementing all six methods of the incremental contract is refused there
with a `TypeError`
naming the missing ones — better than failing on the first mutation, after
the in-memory graph has already changed. `change_notification` and
`store_traversal` are checked the same way, and for the same reason stated
twice over: a missing `start_change_notification` would fail at first use, and
a missing `traverse` would not fail at all — every traversal would warn and fall back
to the walk, quietly, for the life of the process.

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
`test_persistence_contract_memory.py` runs the reference backend as declared,
as snapshot-only, and a third time reporting every entity write via
`entities_read_on_demand` instead of `entities` — so the deferred form below
is held to every notification clause by a backend that needs no server to
run, not only by PostgreSQL's own transport for it.
`test_persistence_contract_postgres.py` is the
worked example of a backend built up one step at a time: it declares all
four capabilities, having landed first as `SNAPSHOT_ONLY` with the entity
clauses skipping, then with the entity contract, then with notification, then
with store traversal. One
clause still skips for it — the backwards-compatibility one, because a store
written by a previous release of this backend does not exist yet. Count it
the way step 4 says to: a skipped clause is an unverified one whatever the
reason.
`test_persistence_seam.py` covers the other half — which shape `GraphStorage`
hands a backend for each mutation.

The protocols are `runtime_checkable`, so `isinstance(backend,
IncrementalGraphPersistenceBackend)` works, but `GraphStorage` never uses it:
which contract drives you is decided by what you declare. The one type check
it does make is for the file backend's sidecars (above). The contract does
check it: a backend declaring `incremental_writes` must satisfy the
`IncrementalGraphPersistenceBackend` protocol, one declaring
`change_notification` the `ChangeNotifyingBackend` protocol, and one
declaring `store_traversal` the `TraversingBackend` protocol.

## Reporting external changes

Every read path serves state built at load: the node and edge dictionaries,
the NetworkX graph, the searchable-text cache behind lexical search, and the
vector index behind semantic search. Nothing tells them another writer moved
the store underneath. That is why a backend that cannot report is a backend
one instance at a time, which is where the default file backend still stands,
and it is what `change_notification` is for — it is a property of the store,
not of the storage engine: a file lock is kernel-local and coordinates
nothing between machines. `PostgresGraphPersistenceBackend` declares it; see
*Cross-instance notification, over LISTEN/NOTIFY* below for how.

A backend that declares it implements two methods:

```python
def start_change_notification(self, listener) -> None: ...
def stop_change_notification(self) -> None: ...
```

`GraphStorage` subscribes once, *before* its first load, and unsubscribes in
`shutdown_events()`. It still never applies a change against a model that does
not exist yet: reports arriving before the load has returned are held and
replayed once it has (`_BootGate`), which closes the window a write could
otherwise fall into. See *The boot window is closed* below. The listener is called only with
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
  applied them. Each upsert carries the entity's new content, gathered when
  the report was dispatched. An upsert whose payload carries an `embedding`
  hands the vector over with it.
- `ExternalChange.entities_read_on_demand(read_content)` — the same, except
  that `GraphStorage` calls `read_content()` to get the operations, **after it
  has drained its write queue**. What comes back is applied as the store's own
  answer, with no conflict rule consulted, which is sound precisely because
  this instance's queued writes are already in it. **Prefer this wherever the
  backend can re-read its store**; *When both instances wrote the same thing*
  below is what the alternative costs.

  Three things about *when* it is called decide whether an implementation is
  correct. `GraphStorage`'s write queue is the one thread a report may **not**
  arrive on, and it applies a report inline — with one exception, the boot
  replay, which is called out in each bullet it changes:

  - It is called **on the thread the report was delivered on**, further down
    that call stack. A backend that dispatches from a poller it needs to keep
    polling must hand the report to another thread itself. **The exception is
    a report held across the first load** (see *The boot window is closed*):
    it is replayed on the thread that finished the load, and the dispatching
    thread is gone by then. So `read_content` must not close over anything
    bound to the thread that created it — a thread-local, a session, a cursor.
    A pooled connection, which is what `_resolve` takes, is thread-agnostic
    and survives the replay.
  - The application's lock is held for its whole duration, so it must not call
    back into the storage, and **its latency is that instance's write stall** —
    every mutation waits for it. Bound the read: a pool with no timeout, or one
    long enough to wait out a hung server, stalls the instance for exactly that
    long. Under `entities` the same read happened off-lock, so this is a real
    trade for the correctness it buys. For a replayed report it is the **boot**
    that stalls rather than a write: no mutation can be queued yet, and
    construction does not finish until the replay does.
  - It is called **at most once per report**; a second ask returns the first
    ask's answer rather than a fresher read.

  Raising from `read_content` is not an error to swallow: `GraphStorage` logs
  it and reloads the whole graph, and the report is not lost.

  **Report what the store applied together as one change, not one change per
  entity.** The vector index is rebuilt whole whenever it changes, so
  `GraphStorage` settles it a small fixed number of times per reported change
  — one pass to evict what the change touched, one to adopt the vectors it
  carried, one to generate for the nodes it did not, and a fourth only when
  that generation ran at a width the batch had not adopted, which empties the
  index and strands the adopted ones — rather than per operation. Splitting a
  batch of a hundred into a hundred reports asks for hundreds of rebuilds
  instead of those few, each linear in the index. The cost of a refresh is
  decided here, by the backend, not by the size of the batch.
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
its own write.

**The store settles it, where the backend can re-read.** A backend reporting
`entities_read_on_demand` is asked for the content after `GraphStorage` has
drained its own write queue, under the lock that every mutation needs in order
to be queued. What it reads is therefore the store's answer with this
instance's own work already in it, and there is nothing left for the
application to arbitrate: it takes what it is given. Every instance ends on
whatever the store committed last, which is the only ordering the instances
actually share.

One consequence worth expecting: the answer is often this instance's own
write, and applying that would emit an event whose before and after are the
same. For a **node** upsert `GraphStorage` compares the answer against what it
holds and applies nothing when they agree — including the vector, which it
compares against the index rather than against the node, since an adopted
embedding lives in the index and not on the node it describes. An edge upsert is
never offered the comparison at all - it is applied as reported, exactly as it
was before - so two instances that make the same edit to an edge they both
hold each emit an `edge.update` whose before and after are the same, on top of
the real one each emits for its own write.

**A backend that reports `entities` instead falls back to a wall clock.** Its
content was gathered when the report was dispatched, which can predate the
receiving instance's queued writes, so `GraphStorage` protects itself with
**last writer wins, by `updated_at`**: a reported node upsert is ignored when
the node held in memory carries a later `updated_at` than the payload, and a
warning names the node. That rule is weaker than it sounds, and this is the
reason to prefer the other constructor:

- It is a wall clock. The instances share no other ordering, so their clocks
  have to be roughly in step for this to mean anything.
- **It does not order the commits.** The stamp is taken in memory under the
  lock and the write commits asynchronously afterwards, so the write that
  commits *last* can carry the *earlier* stamp. The instance holding the later
  stamp then refuses the store's value — and goes on refusing it, because
  there is no further change to report. Measured on PostgreSQL 16 before the
  read was deferred, five racing renames from each of two instances: 4 of 8
  runs ended with the two instances on different values, in both directions,
  with no failed write on either side.
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

A backend stuck on this path but able to order writes itself — a log sequence
number, a stream id, a commit timestamp the store assigns — has a better answer
than its writer's wall clock, and should carry that in the payload's
`updated_at`.

Two things hold on both paths, because neither has anything to arbitrate:

- **Edges carry no `updated_at`**, so an edge upsert is applied as reported.
- **Deletes carry no payload**, so an external delete is applied whatever this
  instance last did to the entity.

### A local write that failed

A failed entity write - or a failed whole-graph write, the same way - leaves
the backend's on-disk image behind what memory holds, and `GraphStorage`
heals the gap by re-issuing the whole graph on the next write, `flush()` or
shutdown. On a shared store that is a last-resort recovery, not
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

## Answering a traversal from the store

`get_related_nodes` walks the in-memory graph: it is a BFS over the NetworkX
copy every instance holds. That copy is the reason the whole topology has to
be resident, which is the ceiling a shared store exists to remove. A backend
declaring `store_traversal` offers to answer the same question itself.

```python
class TraversingBackend(Protocol):
    def traverse(
        self,
        anchor_id: str,
        depth: int,
        relationship_types: list[str] | None = None,
        include_archived: bool = False,
    ) -> dict: ...
```

It returns **ids only** — `{"node_ids": [...], "edge_ids": [...]}` — and
deliberately: the payloads are already in memory, and shipping them back
would make the store pay for what the caller already has.

The answer has to match the walk's exactly, which means matching it on the
cases nobody states out loud:

- an edge between two nodes that are both exactly `depth` away is **not**
  returned, because neither endpoint was ever expanded;
- an edge whose far endpoint is absent from the graph **is** returned, and the
  missing id is not — a traversal continues *through* such an id, so a path
  can be longer than there are nodes;
- an archived node blocks the path through it, and the edge that would have
  reached it is dropped too;
- an archived anchor is still returned;
- an anchor that is not in the graph yields nothing at all, not a lone anchor.

Order is not part of the contract. The walk collects into sets, so its order
is an artefact.

`GraphStorage` asks the store only while the store is **current** — every
write it has issued has landed, whichever path that write took, and no resync
is owed after a failed one. Writes are asynchronous, so between a mutation and
its write landing the in-memory graph is ahead of the store, and a traversal
answered there would not see the caller's own write.

Membership is decided by the store and the payloads resolved from memory
afterwards, so the graph can change in between. When anything the store
decided on has since vanished — a node archived or deleted, an edge archived
or deleted — the result is discarded and the walk answers instead. Patching
the store's answer is not an option: dropping an archived node leaves behind
whatever was reachable only through it, and recomputing that is the walk.

The walk is also the fallback when the store raises. A store that cannot
answer is not a failed request.

The store walks a level at a time rather than answering in one
depth-limited recursive query. Both return the same set; only the first
stops when a level reaches nothing new. A recursive CTE cannot: its working
table is keyed on (id, depth), and a recursive term may not consult its own
accumulated result to prune ids already seen, so it runs every level the
caller asked for. On 500 nodes and 5000 edges, complete at depth 5, asking
for 1000 levels cost 16.3 s that way and 0.08 s this way - and
`mcp_tools.get_related_nodes` accepts a depth with no cap.

`backend/core/tests/test_traversal_equivalence.py` holds the two
implementations to the same answer by fuzzing randomised graphs against the
walk, rather than by two people reading two pieces of code.

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

`PostgresGraphPersistenceBackend` (`backend/core/postgres_backend.py`) is the
second backend, and it is **optional**: nothing in the always-imported path
touches it and `psycopg` lives in `backend/requirements-postgres.txt` rather
than the base requirements. It exists for the
one deployment the file backend cannot serve — several instances sharing one
graph — because a file is rewritten whole by whichever instance saved last,
and on a FUSE-mounted object store its locking gives no protection at all.

The property that makes a database the answer here is that it is
*client/server*: ten autoscaled instances are ten clients of one server named
by the connection string, not ten copies of a store. That is also why an
embedded database is not an alternative, however good its write path —
SQLite and DuckDB would give each instance its own writer on a shared file,
which is the problem rather than the fix.

### Selecting a backend

`GRAPH_BACKEND` picks one. It defaults to `file`, so a deployment that sets
nothing runs exactly what it ran before this setting existed — `GraphStorage`
builds its own `FileGraphPersistenceBackend`, and there is no second
construction site to drift from that one.

| Variable | Default | Meaning |
|---|---|---|
| `GRAPH_BACKEND` | `file` | `file` or `postgres` |
| `GRAPH_POSTGRES_DSN` | *(unset)* | libpq connection string; required for `postgres` |
| `GRAPH_POSTGRES_SCHEMA` | `public` | One database can hold several graphs, one per schema |
| `GRAPH_POSTGRES_POOL_SIZE` | *(backend default)* | Connections this instance may hold; at least 1 |
| `GRAPH_POSTGRES_SCOPE` | *(unset)* | An opaque identifier tagging this instance's rows; see *Keeping scopes apart*. Set-but-empty is refused, not read as unset |

Six ways to get it wrong fail at boot rather than later, in
`backend/api_host/persistence.py`: an unrecognised `GRAPH_BACKEND`, `postgres`
with no DSN (unset, empty or whitespace), `postgres` without the psycopg extra
installed, a `GRAPH_POSTGRES_POOL_SIZE` below 1, a `GRAPH_POSTGRES_SCOPE` set
to an empty value, and a `GRAPH_POSTGRES_SCOPE` set while `GRAPH_BACKEND` is
not `postgres`. The last two are the ones where silence would be worst — a
scope that is dropped leaves the instance with no isolation while the
configuration says it has some — so neither is normalised away the way an
empty `GRAPH_BACKEND` or pool size is, and the value is passed through
unstripped because an opaque identifier is not ours to tidy. The first
matters most — a value of `postgresql` falling back to `file` would boot
happily and look correct until a second instance started writing the same
graph.

**Selecting `postgres` moves the graph and the node vectors, leaves one thing
where it is, and stops one more.** The vectors travel inline, as an
`embedding` key on each node,
rather than in the binary sidecar the file backend keeps beside `graph.json`.

Sessions do not move. They stay file-backed in the directory `SESSIONS_DIR`
names, or one derived from the graph path when it is unset, so two instances
sharing a database but not that directory share a graph and not their
sessions — see [CAPACITY.md](CAPACITY.md) for what that condition costs in
practice.

Mutation history stops. `GraphStorage` builds its history sidecar only for a
file-backed store, so under `postgres` the `/api/history` endpoints return
nothing and `HISTORY_MAX_EVENTS` and `HISTORY_MAX_AGE_DAYS` become inert. That
is stated under *Vectors and history* above and is repeated here because this
is the section an operator reads before flipping the switch.

### Importing an existing graph file

Use `scripts/graph_file_to_postgres.py` to copy an existing `graph.json` into
`PostgresGraphPersistenceBackend`:

```bash
python scripts/graph_file_to_postgres.py data/active/graph.json \
  --dsn "$GRAPH_POSTGRES_DSN" \
  --schema "${GRAPH_POSTGRES_SCHEMA:-public}"
```

The tool refuses to replace a target that already contains graph data. Pass
`--allow-non-empty-target` only when replacing that target is intentional. After
the write, it reloads the target and verifies the node count, edge count, and
that every edge endpoint refers to a stored node.

**Into a store that keeps scopes apart, pass `--scope`.** A row that carries no
scope is admitted to every session (*Keeping scopes apart*, below), so a graph
converted without one would be readable by every scope in the store. The tool
therefore refuses an unscoped conversion when the target shows any sign of
keeping scopes apart: row-level security enabled, or a policy present, on
`graph_nodes` or `graph_edges`, or rows there already carrying a scope. It
refuses before writing anything, and `--allow-non-empty-target` does not
override it: that flag decides whether to replace a graph, not whether to
publish one to every scope.

```bash
python scripts/graph_file_to_postgres.py data/active/graph.json \
  --dsn "$GRAPH_POSTGRES_DSN" \
  --schema "${GRAPH_POSTGRES_SCHEMA:-public}" \
  --scope "$GRAPH_POSTGRES_SCOPE"
```

With `--scope`, verification also counts the nodes and edges carrying that
scope, rather than trusting the reload alone: a scoped session is shown every
row that carries no scope as well as its own, so a graph written unscoped
would pass the reload's count check with the right numbers.

**A scope's graph wants a schema of its own.** Rows that carry no scope, and the
metadata table's one row per schema, are shared by every scope in the schema
(*Keeping scopes apart*, below). So when a scoped conversion finds its target
not empty, `--allow-non-empty-target` replaces those for every scope there, not
only for this one. The refusal says so: pass the flag only if the schema holds
no graph but this scope's, neither another scope's nor one written without a
scope. The tool leaves that call to the operator. A scoped conversion reads the
target through the backend, which returns only rows carrying no scope and this
scope's own whatever role runs it (a policy is a second layer on top), and rows
carrying no scope look alike whoever wrote them. An application started
against an empty schema saves an empty graph there, metadata included, so a
first conversion after that start needs the flag.

The import migrates only the graph payload read from `graph.json`. Embedding
sidecars, history sidecars, and session files are not migrated. Regenerate or
move those artifacts separately if the deployment needs them.

`GRAPH_POSTGRES_POOL_SIZE` is the one setting here with a ceiling to fit
under rather than a value to pick freely. *Sizing it: what an instance costs*,
below, has the arithmetic and the table; it is not repeated here, so there is
one copy to keep true.

### Inside the PostgreSQL backend

Nodes, edges and metadata are JSONB rows, the same payloads the file backend
writes: the graph's own schema is configuration, not something these tables
should have an opinion about. Six things about it are worth knowing before
writing a backend of your own against a shared server:

- **Migration takes an advisory lock.** Every instance runs the same
  `CREATE TABLE IF NOT EXISTS` on boot, and autoscaling means they run it at
  the same moment. `IF NOT EXISTS` does not make that safe on its own: two
  concurrent creates of the same name can still collide in the catalog, and
  an instance that fails here fails to *start*. The lock is transaction-scoped
  (`pg_advisory_xact_lock`), so it is released by the commit rather than by an
  unlock that an exception could skip.
- **Connections are the resource that scales with instance count**, and the
  server's ceiling is shared by every instance at once — 100 on a stock
  server, three of them reserved. The per-instance pool is therefore small by
  default (`DEFAULT_POOL_SIZE`), and a deployment that raises it should check
  that the server's `max_connections` covers
  `instance_count × (pool_size + 1)`. The `+ 1` is notification: a listener
  cannot return its connection to a pool and still be listening, so it holds
  one further connection open per instance for as long as it runs.

- **A load is one moment, under `REPEATABLE READ`.** Nodes, edges and metadata
  read as three queries are three moments: PostgreSQL takes its snapshot per
  *statement* under the default isolation, so another instance saving in
  between hands the reader edges whose endpoints are not among the nodes it
  got — a graph that never existed. On a shared store that interleaving is
  the normal case, not a race to engineer. `REPEATABLE READ` takes the
  snapshot once, at the transaction's first statement, and the three reads
  share it; being transaction-scoped, it leaves nothing on the connection
  when it returns to the pool. The tempting alternative — one statement with
  the three as subqueries — is a trap worth naming, because it reads as
  cheaper: `jsonb_agg` builds a single `jsonb` value, one `jsonb` value
  cannot exceed 256 MB, and a save has no such limit because it writes a row
  per entity. A store would grow past that line and become permanently
  unloadable by the instance that wrote it, and with vectors carried inline
  the ceiling arrives in the tens of thousands of nodes. The count depends
  on how the vector serialises rather than on its dimension alone: a float32
  widened to Python `float` prints ~17 significant digits and costs about
  20 bytes per element in `jsonb`, while a rounded one costs about 12 — so a
  node with a 384-element vector measured between roughly 4.6 kB and 8.0 kB
  of aggregate, putting the limit somewhere between about 34 000 and 58 000
  nodes. Take the low end: real embeddings arrive widened. The limit applies
  to the uncompressed value while the table is TOAST-compressed on disk, but
  the gap is not alarming — a vector is high-entropy, the measured ratio was
  1.4×, and 256 MB of aggregate showed up as about 180 MB on disk. Disk size
  does warn you here; it just warns late.
- **The save states its own isolation level**, `READ COMMITTED`, as the load
  states `REPEATABLE READ`. The lock below only works because the `DELETE`
  after it takes a fresh snapshot at statement start; under a server or role
  default of `REPEATABLE READ` the snapshot would be taken at the lock,
  before it blocks, and the writer that waited would die on a serialization
  failure rather than proceed. Neither level is left to the environment for
  the save or the load — each states its own. Migration (`_ensure_schema()`)
  and `exists()` are not part of that guarantee: they run under whatever the
  connection's environment defaults to, which is fine for what they do —
  neither reads graph data, so neither is exposed to the statement-snapshot
  anomaly the save and the load guard against. What they actually send
  depends on whether the store has been migrated before. Cold (nothing
  provisioned yet), `_ensure_schema()` issues one advisory-lock statement,
  then one catalog lookup for the schema (`pg_namespace`) plus a
  `CREATE SCHEMA IF NOT EXISTS` if it is missing, then for each of the three
  tables one catalog lookup via `_create_missing()` (a `pg_class`/
  `pg_namespace` join) plus a `CREATE TABLE IF NOT EXISTS` if it is missing:
  up to four catalog lookups and four creates behind the one lock, not a
  single `SELECT`. Once a process has migrated once, `self._migrated`
  short-circuits every later call on that backend object: no advisory lock,
  no catalog lookup, nothing sent to the server. `exists()` adds exactly one
  further read on top of whichever of those two paths `_ensure_schema()`
  took — the single-row `SELECT` against `graph_metadata` — so a warm
  `exists()` call is one `SELECT` and zero advisory locks, and a cold one is
  that same `SELECT` plus everything above. The traversal's two indexes are
  not in those counts: they are created after the lock is released, one pooled
  connection each, and each costs a `pg_class`/`pg_index` lookup plus a
  `CREATE INDEX IF NOT EXISTS` only when the lookup says it is missing — see
  the index bullet below for why they sit outside the transaction.
- **Whole-graph saves are serialised per store**, by a second advisory lock
  keyed on the schema. Without it two concurrent saves do not merely race for
  last place: the second writer's `DELETE` takes its snapshot when the
  statement starts, so after waiting for the first writer's commit it skips
  the rows that writer deleted and cannot see the rows it inserted. The store
  then ends holding the union of two saves — a graph neither instance wrote —
  or the second save dies on a duplicate key for any id they share, which is
  what two instances of the *same* graph mostly have.
- **Two expression indexes carry the traversal**, on `doc->>'source'` and
  `doc->>'target'`. Nothing indexes those by default, so without them the
  traversal scans every edge at every level. Measured at depth 3 on 20 000
  nodes: 3.7 ms against 61 ms unindexed at 60 000 edges, and 89 ms against
  258 ms at 300 000. Migration
  creates them best-effort, each in its own connection *outside* the
  migrating transaction — a failed statement inside that transaction aborts
  the whole thing, so catching the error there would recover nothing — and a
  failure is logged rather than raised, because a role with DML and no DDL
  should still boot and still answer, slowly. That role is asked about the
  catalog first: `CREATE INDEX IF NOT EXISTS` checks ownership before
  existence, so a store provisioned exactly as below — indexes included —
  would otherwise warn on every boot that its traversals scan, while they
  seek. That check is on validity, not just presence: a `CREATE INDEX
  CONCURRENTLY` that fails leaves the name behind with `indisvalid = false`,
  which the planner will not use and `IF NOT EXISTS` will not replace, so
  migration reports it rather than treating it as done. It reports without
  naming a cause, because the catalog cannot: a build still in progress reads
  `indisvalid = false` too, and telling an operator their own running build
  had failed is how they abort it. The repair is `REINDEX INDEX CONCURRENTLY`,
  which fixes it in place without blocking writes — not a `DROP`, which takes
  a stronger lock than the one this bullet is about. Migration does neither:
  `REINDEX … CONCURRENTLY` cannot run inside a transaction block, and these
  connections are not autocommit.

  `CREATE INDEX` without `CONCURRENTLY` holds a `ShareLock` on `graph_edges`
  while it builds, so on an existing large store the first boot after this
  upgrade blocks writes to that table for the duration and concurrent
  instances queue behind it. Provisioning by hand does not avoid that lock —
  the statements below take exactly the same one — but it lets the operator
  choose when it is taken, and on a live store the statement to use is
  `CREATE INDEX CONCURRENTLY`, outside a transaction, before the upgrade.

- **The level query is never prepared**, and it is the only statement here
  that opts out. Its selectivity *is* a parameter: `= ANY(%(frontier)s)` holds
  one id on the first level and thousands by the third. psycopg prepares a
  statement after a few executions, and PostgreSQL may then plan a prepared
  statement generically — without the array in hand — so it plans for the
  small frontier and then meets the large one. Measured at 50 000 nodes,
  depth 3 from the most connected node, the same call repeated: 329, 209,
  201, then **4 358 ms and never fast again**, because the plan is cached for
  the connection's life. A traversal issues one level execution per level, so
  a handful of requests is enough. The plans differ in kind rather than
  degree: with a 2 000-id frontier, a custom plan takes a hash left join over
  a sequential scan (45 ms) and a generic one a nested loop (830 ms).

  Nothing else here needs it, and that was measured rather than assumed.
  `_resolve`'s `WHERE id = ANY(%s)` is subject to the same generic-vs-custom
  split as the level query — the primary key's unique btree is just as
  sensitive to the array's estimated length, not immune to it. Measured with a
  real prepared statement crossing psycopg's `prepare_threshold` (`PREPARE
  r(text[]) AS SELECT id, doc FROM graph_nodes WHERE id = ANY($1)`, simple
  protocol, execution 1 = custom plan vs. execution 10 = generic plan, on the
  repo's 50 000-node fixture): at 1 000 ids the custom plan is a bitmap heap
  scan (1 000 rows, 3.6 ms) and the generic plan an index scan on the primary
  key (its fixed `rows=10` estimate, 2.2 ms); at 20 000 ids the custom plan
  switches to a sequential scan (20 000 rows, 15.3 ms) while the generic plan
  stays an index scan on the same fixed `rows=10` estimate and costs more,
  45.3 ms. The plans are not identical at any size, and the generic plan's
  *fixed* mis-estimate — never updated for the array actually bound — is the
  same array-length sensitivity the paragraph above describes for the level
  query, not its absence. (A separate measurement using
  `SET plan_cache_mode = force_generic_plan / force_custom_plan` instead of a
  real prepared statement reported identical plans and 51/55 ms; that method
  does not reach `prepare_threshold` the way psycopg does in production and
  does not reproduce here — treat the numbers above, from an actual prepared
  statement, as authoritative.)

  `_resolve` still does not need `prepare=False`, because unlike the level
  query it never reaches the regime where the mis-estimate costs more than the
  correct one: `_resolve` filters on the primary key directly, so even the
  generic plan's index scan stays cheap relative to a full table read, and a
  real `_resolve` call in production carries a change notification's worth of
  ids, not 20 000. A separate prepared-statement run at
  `prepare_threshold=5` with 20 000 ids gives a flat 216–280 ms per call with
  no cliff, dominated by transferring 20 000 jsonb documents rather than by
  planning. The level query is different because it filters on *expressions*
  (`doc->>'source'`, `doc->>'target'`) and then joins, which is exactly where
  the frontier's size decides the join strategy and where the mis-estimate can
  turn into orders of magnitude rather than tens of milliseconds. Every other
  statement is single-row key access, or a read whose only parameter is the
  scope — a value that changes no selectivity worth a plan of its own.

`exists()` answers for the *graph*, not for the tables. Migration creates the
tables on every boot, so table presence would report a store that was never
written as existing, and `GraphStorage` would load an empty graph instead of
bootstrapping one. What it reads is the single metadata row, which only a save
writes.

Both `CREATE SCHEMA IF NOT EXISTS` and `CREATE TABLE IF NOT EXISTS` check the
caller's `CREATE` privilege *before* the existence short-circuit, so each is
asked for only when a catalog lookup says it is missing (an exact match on
`pg_namespace` / `pg_class` — `to_regclass` would not do, because it parses
its argument as a name and so case-folds an unquoted schema). Without that, an
app role that owns nothing but DML on tables an operator provisioned — the
ordinary least-privilege setup on managed PostgreSQL — dies at boot against a
store it has every permission it actually needs on.

To provision it that way, create these three tables and their two indexes,
and grant the app role `USAGE` on the schema plus
`SELECT, INSERT, UPDATE, DELETE` on the tables. The grants cover the app's
reads and writes; they do not cover maintenance. A role that owns nothing
cannot `ANALYZE`, and PostgreSQL answers that with a warning rather than an
error - the backend now prints the warning, but keeping the statistics
current is the operator's or autovacuum's job on a store provisioned this
way, and stale statistics leave the two indexes below unused. The
primary key on `graph_metadata.only_row` is not decoration: the save upserts
that row `ON CONFLICT (only_row)`, so a table without it boots cleanly and
then fails on **every** save.

```sql
CREATE TABLE <schema>.graph_nodes (
  id text PRIMARY KEY,
  doc jsonb NOT NULL,
  -- Optional, and nullable for a reason: a row that carries no scope is
  -- every row of every store written before this column existed, and the
  -- policy below admits it to every session. See "Keeping scopes apart": a
  -- store that sets no GRAPH_POSTGRES_SCOPE leaves the column NULL on every
  -- row it writes, and still names it in its reads.
  scope_id text
);
CREATE TABLE <schema>.graph_edges (
  id text PRIMARY KEY,
  doc jsonb NOT NULL,
  scope_id text
);
CREATE TABLE <schema>.graph_metadata (
  only_row boolean PRIMARY KEY DEFAULT true CHECK (only_row),
  doc jsonb NOT NULL
);

-- The traversal needs these. Note the double parentheses: an expression
-- index takes its own, and a single pair is a syntax error. As written, the
-- two statements below take a ShareLock on graph_edges for the whole build
-- and block writes until it finishes - fine on an empty table, an outage on
-- a large one that is already serving. There, add CONCURRENTLY to each and
-- run them outside a transaction, and check `indisvalid` afterwards: a
-- concurrent build that fails leaves the index behind, unusable, and
-- REINDEX INDEX CONCURRENTLY is what repairs it.
CREATE INDEX graph_edges_source_idx ON <schema>.graph_edges ((doc->>'source'));
CREATE INDEX graph_edges_target_idx ON <schema>.graph_edges ((doc->>'target'));
```

#### Keeping scopes apart

Each entity row can carry an **opaque scope identifier**, and a store that
supplies one also gets a row-level security policy binding on it. It is off
until a host asks: `GRAPH_POSTGRES_SCOPE` unset is a store whose `scope_id`
is NULL on every row and whose tables carry no policy, which is exactly the
store this backend wrote before the column existed.

What the value *means* is the host's business. The backend stores it,
compares it and passes it through; nothing here interprets it, and the name is
generic because the seam is.

Leaving it unset is not quite "the column is never read". Once the column is
there an unscoped instance still names it, and its predicate reduces to
`scope_id IS NULL` — the same set the policy would show a session that named
no scope. On a store only that instance writes, every row carries NULL and
nothing changes. On a store it *shares* with a scoped one, it sees the rows
carrying no scope and not the scoped ones, and its whole-graph save deletes
only what it saw. That is deliberate: an instance that read every row would
delete every row.

Set it and three things change.

- **Every entity row this instance writes carries the value.** Both write
  paths — the whole-graph save and the entity upsert — stamp it, including a
  row that carried none before, so a store does not drift into two kinds of
  row. The `graph_metadata` row is the exception, and the first boundary below
  says what follows from that.
- **Every statement this instance issues carries the predicate**
  `scope_id IS NULL OR scope_id = <the value>`. That is the application layer,
  and it holds whether or not the server enforces anything — which matters for
  the least-privilege provisioning above, where the app role owns nothing and
  no policy can be created at all.
- **The tables take a policy carrying the same expression**, with
  `ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY`. FORCE is not
  decoration: a policy does not apply to a table's *owner* without it, and on
  a self-provisioned store the owner is this application. That is the second
  layer — what still refuses when the application's own query is wrong.

The predicate admits a row that carries **no** scope to every session, which is
what makes the seam optional: an already-populated table keeps working when
the column lands, and a store written before a scope was configured stays
readable after one is. It admits a row that carries **a** scope only to a
session set to that same value — and `= NULL` is never true, so a session that
named no scope is refused a scoped row rather than shown every one of them.
That is the direction that has to fail closed, because an unset setting is what
a mistake arrives as.

Two boundaries and one cost, none of them accidents:

- **`graph_metadata` is not covered, and for two scopes behind one set of
  tables that is a hazard rather than a gap.** Its primary key is a column that
  can only hold true, so it holds one row for the whole store and has nowhere
  to put a second scope's; a column there would suggest an isolation the shape
  cannot deliver. What follows, measured with the policy enabled *and* forced:
  the single metadata row is readable by every scope in both directions, and
  the next whole-graph save from either replaces it — so whatever a host puts
  in graph metadata crosses scopes and can be destroyed by another. And because
  `exists()` answers for the *store* rather than for the scope, a scope with no
  rows of its own finds the store non-empty, skips the bootstrap
  `GraphStorage` would otherwise do, and loads an empty graph carrying another
  scope's metadata — `graph_name` included. A schema per scope has none of
  this; one set of tables for several scopes is the shape this bullet exists to
  rule out.
- **`id` remains the primary key of each entity table**, so two scopes sharing
  one table cannot both hold `n0`. An upsert therefore conflicts with *the* row
  of that id whatever scope it carries, and the backend **refuses** such a
  write rather than applying it: the conflicting row carries the same predicate
  every read does, and a conflict that predicate excludes raises rather than
  silently affecting no rows. Before it was there, the statement replaced the
  other scope's row and restamped its scope, and that scope's next load
  returned nothing. Where the policy is in force the server refuses the same
  write, so the two layers agree; a whole-graph save carrying such an id fails
  on the primary key, which is the same answer by a louder road. Louder, and
  worth knowing before it happens: `GraphStorage` answers a failed entity write
  by re-issuing the whole graph, so once such an id is in an instance's memory
  every subsequent write becomes that failing save and the instance persists
  nothing until the id leaves it. Change
  notification is per schema too (the channel is derived from it), so two
  scopes behind one set of tables would hear each other's entity ids announced.
  The seam is the row-level layer *underneath* the separation this backend
  already offers — a schema per graph — not a replacement for it.
- **A policy costs the traversal its expression indexes.** A policy reaches a
  query as a security qual, and PostgreSQL will not evaluate a qual that is not
  leakproof before one. The traversal filters on `doc->>'source'`, and
  `jsonb_object_field_text` is not marked leakproof (`texteq`, which the
  primary key uses, is) — so for a session the policy applies to, the two
  indexes above go unused and each level of a walk reads the edge table.
  Measured on PostgreSQL 16: a one-id frontier over 1000 edges plans a bitmap
  scan on both indexes without a policy and a sequential scan with one. The
  indexes exist for a 3.7 ms against 61 ms difference at 60k edges, so this is
  the price of the second layer, and it is the reason the policy is created
  only for a store that configured a scope. A deployment that wants the
  separation without the cost separates by schema and leaves the scope unset.

**A scope the store cannot hold is a refusal, not a warning.** If
`GRAPH_POSTGRES_SCOPE` is set and the tables have no `scope_id` column — an
older store whose app role cannot `ALTER TABLE` — the backend raises at boot
rather than writing rows that carry no scope, because such rows are readable
by every other session on that store. Provision the column and the policy, or
run the instance without a scope:

```sql
ALTER TABLE <schema>.graph_nodes ADD COLUMN IF NOT EXISTS scope_id text;
ALTER TABLE <schema>.graph_edges ADD COLUMN IF NOT EXISTS scope_id text;

-- Create the policy BEFORE enabling row-level security. A table with it
-- enabled and no policy admits nothing at all, so the other order leaves a
-- window in which the store looks empty.
CREATE POLICY graph_nodes_scope_policy ON <schema>.graph_nodes
  FOR ALL USING (scope_id IS NULL
                 OR scope_id = current_setting('app.graph_scope', true));
CREATE POLICY graph_edges_scope_policy ON <schema>.graph_edges
  FOR ALL USING (scope_id IS NULL
                 OR scope_id = current_setting('app.graph_scope', true));

ALTER TABLE <schema>.graph_nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE <schema>.graph_nodes FORCE ROW LEVEL SECURITY;
ALTER TABLE <schema>.graph_edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE <schema>.graph_edges FORCE ROW LEVEL SECURITY;
```

**What the upgrade itself costs.** The first boot after this change runs
`ALTER TABLE … ADD COLUMN` on both entity tables of **every** PostgreSQL store,
scope configured or not. The column has no default, so it is a catalog-only
change and rewrites nothing — but it takes `ACCESS EXCLUSIVE` for the moment it
runs, a stronger lock than the `ShareLock` the index note above warns about: it
queues readers as well as writers, and it waits behind any transaction already
holding the table, such as a `REPEATABLE READ` traversal or a whole-graph save.
Brief on any store, but on a busy one it is worth taking that first boot when a
long transaction is unlikely — or adding the column by hand beforehand with the
statements above, after which the migration finds it there and asks for
nothing.

The backend runs those statements itself where it owns the tables, one
transaction per table so a failure part-way cannot leave a table with
row-level security enabled and no policy. It binds `app.graph_scope` with
`set_config(..., is_local => true)` at the start of each transaction, so the
value is gone when the connection returns to the pool.

**Two payload restrictions** are worth knowing before pointing an existing
graph at this backend, because neither is shared with the file backend. A
whole-graph save carrying one offending value fails entirely, so a graph
holding one cannot be migrated here at all. An entity write fails only the
write that carries it — one operation, or the whole batch it is in, since a
batch is one transaction — but `GraphStorage` answers a failed entity write
by re-issuing the whole graph, which then fails the same way, so the value
has to go either way.

- **Non-finite floats.** `NaN` and `Infinity` are not JSON, but Python's
  `json` module writes them bare and reads them back, so `graph.json` holds
  them happily; `jsonb` rejects them. This is the likelier of the two,
  because a backend with no vector sidecar receives every node's embedding
  inline (see *Vectors and history*) — one degenerate vector is enough.
- **NUL in a string.** `\u0000` is valid JSON and round-trips through
  `graph.json`; PostgreSQL cannot store it in a `jsonb` column. This one
  needs a hostile or corrupted string rather than an arithmetic accident.

The backend declares `incremental_writes` and `transactions`. A mutation
therefore reaches it as the entity operations that describe it — a renamed
node is one row, not a rewrite of every node — and that is what makes
several writers safe rather than merely orderly: two instances editing
different parts of the graph touch different rows and do not contend at all,
where two whole-graph saves had the later one discard the earlier one's work
whatever it was. Concurrent writes to the *same* entity resolve
last-writer-wins on the row, which is why the entity path states
`READ COMMITTED` as the save does: under `REPEATABLE READ` the second writer
would abort rather than wait.

An entity operation takes the save's lock too, but in **SHARE** mode where
the save takes it exclusively. Row locks alone are not enough, and the case
that proves it is a delete: a save *replaces* a row rather than updating it,
so a `DELETE ... WHERE id` that waited on the save's row lock unblocks to
find its target tuple dead and the replacement outside its own statement
snapshot. It removes nothing, raises nothing, and the caller is told the
entity is gone while it is still there. An upsert survives the same
interleaving because `ON CONFLICT` sees the new row — which is why the
anomaly is easy to miss, and why an earlier version of this backend shipped
with the delete broken. The same window has a second face: an entity write
inserting a *new* id into it makes the save itself abort on a duplicate key.

SHARE keeps the economy the entity path exists for. Two instances editing
different entities do not wait for each other — except behind a whole-graph
save already queued for the exclusive lock, since PostgreSQL makes a new
request queue behind a conflicting waiter. That exception is not a
regression to work around: it is what stops a stream of entity writes
starving the save indefinitely.

A batch holds one row lock per operation until it commits, so two batches
touching the same entities in opposite orders deadlock. The order is the
caller's and cannot be sorted away — a delete followed by an upsert of one
id is not the same batch reordered — so a deadlocked batch is retried
(`DEADLOCK_RETRIES`, three times), which is safe precisely because the batch
is atomic: the aborted transaction left nothing behind. Retries are bounded,
and under heavy contention from many instances a batch can still exhaust
them; the error then propagates and `GraphStorage` heals by re-issuing the
whole graph, which is the pre-entity behaviour rather than a new hazard.

`checkpoint()` is a no-op here. The file backend needs it because it appends
to a journal and rewrites the graph only periodically; a database has no
deferred state, so what is already committed is the canonical form.

### Cross-instance notification, over LISTEN/NOTIFY

The other direction is the server's own `LISTEN`/`NOTIFY`. Each instance
holds one connection listening on a channel derived from the schema, and each
write announces itself on that channel.

- **The announcement is issued inside the writing transaction.** The server
  holds it until commit and discards it on rollback, so a listener is never
  told about a change that did not happen and a retried batch announces once —
  from the attempt that committed. That is why there is no bookkeeping here:
  announcing after the commit would leave a window in which the write is
  visible and unannounced, and a process dying in it would leave every other
  instance permanently behind with nothing to notice.
- **The announcement carries identifiers, never content.** A node payload
  carries its embedding inline when there is no vector sidecar, so one node
  can exceed the server's entire payload allowance. The listener reads the
  content back from the store — which is also the stronger design: between the
  commit and the read a third instance may have written the same entity again,
  and the store's answer is then newer than the announcement rather than
  contradicting it. The store decides what happened to a named identifier;
  one that is no longer there is reported as a delete.
- **The payload ceiling is enforced here, not by the server.** `NOTIFY` caps a
  payload at 8000 bytes and *raises* past it — inside the writing transaction,
  which would abort the batch being announced. An announcement that will not
  fit therefore degrades to the whole-graph form (`ExternalChange.unknown()`)
  before the statement is issued. A large batch costs the other instances a
  reload; it must never cost the writer its mutation.
- **Order is preserved.** `GraphStorage` drops an external edge whose endpoint
  is not present yet, so a report that grouped by kind would silently lose
  every edge created alongside its endpoints — the ordinary shape of a create.
- **An instance is not told about its own writes.** The server delivers a
  notification to the connection that sent it as readily as to any other, so
  the announcement carries the writer's origin and a listener skips its own.
  Without it every mutation would emit a second event to every subscriber.
- **An announcement this build cannot read is a reload, not noise.** During a
  rolling deploy the store is shared with instances running a different
  version. Ignoring an unreadable announcement would leave this instance stale
  for as long as the other keeps writing.
- **The listening connection is outside the pool, and is reconnected.**
  `LISTEN` registers on the session, so a pooled connection would stop
  listening the moment it was returned; and the thread reading it blocks for
  the instance's lifetime. One instance therefore costs `pool_size + 1`
  connections. A connection that drops is re-established with backoff, and the
  instance then reports `unknown()`: the announcements sent while it was not
  listening are gone, and the server does not replay them. Without that, a
  single failover would put an instance silently and permanently out of step —
  it would go on answering reads and pass its health check.
- **Failing to establish the *first* connection fails the start.** An instance
  that boots without listening looks healthy and is silently wrong, which is
  the same failure reached at boot instead of at runtime.

`start_change_notification` returns only once the connection is listening.
Returning earlier would lose every write made in the gap — and the caller's
next act is typically to serve traffic, so that gap is exactly when the first
cross-instance write arrives. `stop_change_notification` joins the listening
thread rather than merely signalling it, because a refresh already inside the
listener is running against a model the caller is about to tear down.

**The boot window is closed**, by the seam rather than by this backend.
`GraphStorage` now starts notification *before* its first load and holds what
arrives until the load returns, then replays it in arrival order — see
`_BootGate` in `storage.py`. Loading first and listening second, as it did
until then, left a write committed in between announced to a connection that
was not listening; narrow in time and unbounded in consequence, because a
reconnect reports `unknown()` precisely because its missed announcements are
gone, while the first connect deliberately did not, so an entity written in
the gap and never written again was never reported and that instance served
the wrong value for as long as it ran.

Replaying late is sound rather than merely tolerable: a report carries
identifiers and `_resolve` reads the content when the application asks, so a
replayed report reads what the store holds at replay time — at least as new
as what the announcement described. The same property makes a duplicate
harmless, which matters because a write committed during the load is both in
the load and in the buffer. What the gate costs is a bounded buffer: past
`_BOOT_BUFFER_LIMIT` held reports it drops them and reports `unknown()`
instead, which one whole-graph read subsumes.

The two alternatives, recorded because the choice is not obvious: an
`unknown()` at start would be correct but imposes one redundant whole-graph
read on every instance on every boot, and listening before the load *without*
the gate would report against a model that does not exist yet, which is what
the seam's original ordering existed to prevent. The gate keeps that promise
while removing the gap.

The floor on `psycopg` is 3.2 for `Connection.notifies(timeout=...)`, which
is how the listening thread reads its channel while still noticing a stop.

### Sizing it: what an instance costs

One instance costs **`pool_size + 1`** server connections while notification is
running — the pool, plus the listening connection that cannot go back to a pool
and still be listening. So a deployment needs
`instance_count × (pool_size + 1)`, and it needs it at the instance count it
*scales to*, not the one it was tested at. Getting this wrong is not a slow
deployment: the instance that cannot get a connection fails to boot.

Against a stock server — `max_connections` 100, three reserved for superusers,
so 97 available:

| Instances | `pool_size` | Connections | Fits in 97 |
|---:|---:|---:|:--|
| 1 | 4 (default) | 5 | yes |
| 10 | 4 (default) | 50 | yes, with room for psql and a migration |
| 10 | 8 | 90 | yes, with 7 spare — a psql session and a migration, and no more |
| 20 | 4 (default) | 100 | **no** |
| 20 | 2 | 60 | yes |

Two things worth reading off that table. Raising `pool_size` costs
`instance_count` connections per step, not one — it is the multiplied number,
which is why the default is deliberately small. And scaling out is cheaper per
instance at a small pool than at a large one, so an autoscaling deployment
should lower `pool_size` before it raises `max_connections`.

`backend/core/tests/test_multi_instance_postgres.py` asserts the per-instance
half of this against a running server, so the number above is measured rather
than argued.

### The acceptance test

`backend/core/tests/test_multi_instance_postgres.py` is the whole stack run
against itself: two `GraphStorage` instances on one store, writing at the same
time, on the **entity** path and with content asserted.

It asserts what the earlier layers cannot: that neither instance's writes are
lost when they touch **distinct** entities, that both converge on what the
other wrote and on what the store holds, that a rename arriving by report is
searchable there under its new name and no longer under its old one, and that
a delete takes the node's edges and its vector with it. Nothing in it calls
`save()`: that is the whole-graph path, where two writers overwrite each other
by design, which is why the contract's own two-writer clause — which does drive
two instances against a real store — asserts liveness and explicitly not
content.

One thing it checks before believing any of that: `_resync_pending` on both
instances. A failed entity write is invisible to the caller — `add_nodes`
discards the future and swallows the exception with a print — and it makes the
instance drop every external report it is sent (with a warning per report)
until the next write, flush or shutdown heals it with a whole-graph write.
Without that check a convergence assertion can pass vacuously, or a lost write
can be laundered into an overwrite.

#### A contested entity, and the defect that found this section

Writes to **distinct** entities lose nothing; that is the property above, and
it is the one this work exists to provide. Writes to the **same** entity now
converge too — every party ends on whatever the store committed last — but
they did not always, and the module keeps the cases that pin it because the
way they failed is not a way a race would reliably show.

The report used to carry content gathered when the announcement was
dispatched, and the application defended itself against that with `updated_at`.
Since commit order is not stamp order, the write that committed last could
carry the earlier stamp, and the instance holding the later stamp then refused
the store's value permanently. Measured on PostgreSQL 16, five racing renames
from each of two instances: **4 of 8 runs ended divergent** on an idle
machine — and **none at all under load**, which is the part worth keeping.
A race-based regression test would have passed on a CI runner for the wrong
reason.

So the two cases that guard the fix are constructed rather than raced: one puts
a value stamped *earlier* into the store after an instance already holds a
later stamp, and asserts the instance takes it; the other pins the ordering
that makes that sound, by watching when the content is read relative to the
instance's own queued write. The raced case is kept as well, asserting that
the store and both instances settle on one value — it guards no particular
interleaving, which is exactly why it is worth having alongside two that each
guard one.
