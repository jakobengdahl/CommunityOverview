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
declared and as snapshot-only; `test_persistence_contract_postgres.py` is the
worked example of a backend built up one step at a time: it declares all
three capabilities, having landed first as `SNAPSHOT_ONLY` with the entity
clauses skipping, then with the entity contract, then with notification. One
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
`IncrementalGraphPersistenceBackend` protocol, and one declaring
`change_notification` the `ChangeNotifyingBackend` protocol.

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
touches it, `psycopg` lives in `backend/requirements-postgres.txt` rather than
the base requirements, and the app selects it nowhere yet. It exists for the
one deployment the file backend cannot serve — several instances sharing one
graph — because a file is rewritten whole by whichever instance saved last,
and on a FUSE-mounted object store its locking gives no protection at all.

The property that makes a database the answer here is that it is
*client/server*: ten autoscaled instances are ten clients of one server named
by the connection string, not ten copies of a store. That is also why an
embedded database is not an alternative, however good its write path —
SQLite and DuckDB would give each instance its own writer on a shared file,
which is the problem rather than the fix.

Nodes, edges and metadata are JSONB rows, the same payloads the file backend
writes: the graph's own schema is configuration, not something these tables
should have an opinion about. Four things about it are worth knowing before
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
  failure rather than proceed. Neither level is left to the environment.
- **Whole-graph saves are serialised per store**, by a second advisory lock
  keyed on the schema. Without it two concurrent saves do not merely race for
  last place: the second writer's `DELETE` takes its snapshot when the
  statement starts, so after waiting for the first writer's commit it skips
  the rows that writer deleted and cannot see the rows it inserted. The store
  then ends holding the union of two saves — a graph neither instance wrote —
  or the second save dies on a duplicate key for any id they share, which is
  what two instances of the *same* graph mostly have.

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

To provision it that way, create these three tables and grant the app role
`USAGE` on the schema plus `SELECT, INSERT, UPDATE, DELETE` on them. The
primary key on `graph_metadata.only_row` is not decoration: the save upserts
that row `ON CONFLICT (only_row)`, so a table without it boots cleanly and
then fails on **every** save.

```sql
CREATE TABLE <schema>.graph_nodes (
  id text PRIMARY KEY,
  doc jsonb NOT NULL
);
CREATE TABLE <schema>.graph_edges (
  id text PRIMARY KEY,
  doc jsonb NOT NULL
);
CREATE TABLE <schema>.graph_metadata (
  only_row boolean PRIMARY KEY DEFAULT true CHECK (only_row),
  doc jsonb NOT NULL
);
```

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

One window stays open, and it is the seam's rather than this backend's.
`GraphStorage` loads and *then* starts notification — deliberately, so that
no change is reported against a model that does not exist yet — so a write
committed between the load and the `LISTEN` is announced to a connection that
is not listening, and the server does not replay it.

**That window is narrow in time and unbounded in consequence, and it is the
one case with no recovery at all.** A reconnect reports `unknown()` precisely
because the announcements it missed are gone; the first connect deliberately
does not, because the caller has just loaded. So an entity written in the gap
and never written again is never reported, and that instance serves the wrong
value for as long as it runs. Later announcements do not help: each names only
its own entities. Closing it means either an `unknown()` at start — one
redundant whole-graph read per boot — or listening before the load, which is
the order the seam specifies and not this backend's to change.

The floor on `psycopg` is 3.2 for `Connection.notifies(timeout=...)`, which
is how the listening thread reads its channel while still noticing a stop.
