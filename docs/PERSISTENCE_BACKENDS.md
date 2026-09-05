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
not a rewrite of every node — and never asks the backend's type.

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
- `save_graph_data(data)` — persist that dict, whole. **Must be atomic**: a
  reader must see the previous graph or the new one, never a partial write.
  The file backend does this with a temp file and a rename. Runs on
  `GraphStorage`'s single background writer thread.
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
| `change_notification` | the backend can report changes made by another instance | declared only; the notification seam is a later change |

Everything defaults to `False`. `SNAPSHOT_ONLY` is that default, and what the
file backend declares.

The declaration is checked once, when `GraphStorage` is constructed
(`capabilities_of`). A backend that declares `incremental_writes` without
implementing all five entity methods is refused there with a `TypeError`
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
single-worker background thread, so they land in the order they were issued
and `flush()` drains both kinds. A write that raises puts the exception on its
`Future`, as a failing snapshot always has; the in-memory graph is not rolled
back.

What still goes through the snapshot path on every backend: the bootstrap
write of an empty graph, `save()` called explicitly (the maintenance scripts,
`reload()`), and a backend paired with a vector sidecar.

## Vectors and history

The file backend keeps node vectors in a binary sidecar and mutation history
in an NDJSON sidecar next to `graph.json`. Both exist only for the file
backend. Any other backend:

- receives each node's vector inline, as an `embedding` key on the node
  payload (`None` when the node has none), in snapshots and upserts alike.
  Store it, and return it from `load_graph_data`, or semantic search starts
  empty after every restart;
- has no mutation history; `get_recent_history` returns nothing. A backend
  that wants an audit trail keeps its own.

## Writing a backend

1. Implement the snapshot contract. Run the test suite with your backend in
   place of the file backend — every existing behaviour must hold, since
   `GraphStorage` will drive you exactly as it drives the file backend.
2. Declare `SNAPSHOT_ONLY` and ship. This is a complete, correct backend.
3. To stop rewriting the whole graph per mutation, implement the five entity
   methods, declare `incremental_writes`, and `transactions` if your batch is
   atomic. `backend/core/tests/test_persistence_seam.py` shows a recording
   in-memory backend that exercises every routing case; it doubles as a
   reference for what each method receives.

The protocols are `runtime_checkable`, so `isinstance(backend,
IncrementalGraphPersistenceBackend)` works, but `GraphStorage` never uses it:
what it does is decided by what you declare.

## Current state

`FileGraphPersistenceBackend` is snapshot-only. It remains the default, needs
no configuration, and behaves exactly as before the incremental contract
existed. No shipped backend declares `incremental_writes` yet; the contract is
what the shared-store and file-coalescing backends are built against.
