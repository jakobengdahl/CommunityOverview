# Session Overlay Contract

Staged editing in a session: changes made in the session stay out of the graph
until someone explicitly merges them.

- **Status:** Draft contract, adopted by
  [ADR 0005](adr/0005-sessions-may-stage-graph-changes.md). It is normative for
  implementation. A question marked *open* is decided in review before the slice
  that needs it starts (§20).
- **Scope:** Open-source core. It covers the model, the storage seam, the read
  and write semantics, the review and merge API, and the invariants every
  implementation slice is tested against.
- **Related:** [`MULTI_USER_SESSIONS_DESIGN.md`](MULTI_USER_SESSIONS_DESIGN.md)
  (the session model this extends; D4 is revised by ADR 0005),
  [`PERSISTENCE_BACKENDS.md`](PERSISTENCE_BACKENDS.md) (the storage seam this
  adds capabilities to),
  [`ANNOTATION_CONTRACT.md`](ANNOTATION_CONTRACT.md) (the field-versioning
  pattern §11 follows).

---

## 1. Purpose

Today every write lands in the graph at once, whoever or whatever makes it. This
contract adds a second way to work. A session can be put in **staged** mode, and
then:

- the session's writes are held in a thin **layer** of its own;
- reads made in the session see the graph *with* that layer applied;
- nobody else sees the changes;
- nothing reaches the graph until someone reviews the changes and **merges**
  them, completely or in part, or **discards** them.

The layer holds only what changed. It never copies the graph.

## 2. Terms

| Term | Meaning |
|---|---|
| **Graph** | The persisted nodes and edges every direct write lands in. Called the *main graph* where the contrast matters. |
| **Write mode** | `direct` (today's behaviour) or `staged`. It is set when a session is created, and it can only ever change from `direct` to `staged` (§5). |
| **Layer** | A session's staged changes: at most one **entry** per entity. |
| **Entry** | The net staged change to one node or edge: create, update or delete (§6). |
| **Tombstone** | A delete entry. It hides the entity in the composed view. |
| **Composed view** | The graph as seen from a staged session: main graph plus layer (§8). |
| **Revision** | A per-entity counter only the storage assigns (§4). |
| **Base** | The main-graph entity as it was when an entry was first staged: its revision and its full content. |
| **Field** | A top-level attribute of a node or edge. Each top-level key of `metadata` counts as its own field, the granularity of the existing metadata merge-patch. |
| **Merge** | Applying selected entries to the main graph as one unit (§11). |
| **Shared store** | A persistence backend several application instances write to at once. It declares `change_notification`; PostgreSQL is one. |
| **Single-writer store** | A backend written by one instance only. The file backend is one. |

## 3. Invariants

Every implementation slice is reviewed and tested against these. A change that
breaks one is a defect, whatever else it achieves.

- **I1 — Isolation.** A write made in a staged session changes no node, edge or
  graph metadata in the main graph, and emits no graph event, until a merge
  applies it.
- **I2 — Fail closed on the session.** A request that names a session is either
  handled in that session's write mode or refused. It is never silently handled
  as a direct write. That covers:
  - a session id that does not resolve;
  - an entry point that does not understand sessions;
  - two different sessions named in one request.

  A staged session never becomes a direct one (§5.2). A caller who believes it
  is staging therefore never writes to the main graph by mistake.
- **I3 — Thin layer.** A layer holds entries only for entities the session
  changed. Creating, reading, merging or discarding a layer never copies
  unchanged entities.
- **I4 — Composition is consistent.** Every read listed in §8.2, made in a staged
  session, returns the composed view. No composing read returns an entity the
  layer tombstoned, or an edge with an endpoint the composed view lacks.
- **I5 — Direct mode is unchanged.** With no session, or in a direct-mode
  session, reads and writes behave as before this contract. The exceptions are
  those §18 lists.
- **I6 — Revisions are storage-assigned and never go backwards.** A client
  cannot set a revision. Every write the storage applies to an entity leaves it
  at a higher revision than the store held for that entity before.
  - Only an explicit end of lineage restarts a revision: a delete, or a
    whole-graph replacement an operator asks for (§4.2).
  - A downgrade to a build without revisions is outside this guarantee (§4.6).
- **I7 — No silent overwrite.** A merge applies an entry only when, for every
  field it changes, one of these holds:
  - the main graph still holds the base value;
  - the main graph already holds the staged value;
  - the caller resolved that field explicitly.

  For a node delete, this also covers every main-graph edge incident to the
  node. The store checks these conditions inside the merge unit, not just the
  instance before it (§11.3).
- **I8 — Atomic merge.** A merge either applies every selected entry, and removes
  each from the layer, or applies none and leaves the layer as it was. This holds
  across a crash (§11.6).
- **I9 — Idempotent merge.** Repeating a merge with the same merge id writes
  nothing further and returns the first result, also after a crash.
- **I10 — Discard touches only the layer.** Discarding changes nothing in the
  main graph.
- **I11 — No orphaned work.** A layer is never deleted as a side effect. Deleting
  a session with a non-empty layer needs an explicit discard.
- **I12 — System consumers read the graph.** Event subscriptions, agent
  scheduling, federation peers, history and diagnostics never see a layer.

## 4. Entity revisions

Revisions come first and stand on their own. They give direct writes optimistic
concurrency too, and a merge cannot be safe without them.

### 4.1 The field

- Nodes and edges gain **`revision`**, an integer of at least 0.
- The name is deliberately not `version`, which already has three meanings in
  the code: the graph metadata's `version`, the event envelope's
  `schema_version`, and the annotation `version`.
- `revision` is **server-owned**. That is new in kind:
  - today callers may supply `created_at` and `updated_at` on create;
  - unknown keys in a node payload are folded into `metadata`.

  `revision` must be neither. The service layer drops a caller-supplied
  `revision`, and never folds it into `metadata`.

### 4.2 How it advances

- A newly created entity has revision **1**.
- Every write the storage applies to an existing entity advances its revision.
  That includes node and edge updates, archiving and unarchiving, and a merge's
  writes.
- A direct write that would leave the entity's content unchanged need not be
  applied. If it is not applied, the revision does not move.
- A merge applies every operation it lists, even one that changes no content.
  §11.6 relies on that.
- Two things end an entity's lineage, and a later entity with the same id then
  starts again at 1:
  - deleting the entity;
  - an explicit whole-graph replacement that an operator asks for, such as a
    conversion into a non-empty target.

  §11.1 decides conflicts on field values, not on revision equality, so a
  restarted lineage cannot make a stale change look current.
- An entity loaded without the field reads as revision **0**. Its first applied
  write gives it a revision. No migration pass is needed. `0` therefore means
  "present, never revised under this contract", and never "absent" (§4.3).

### 4.3 Expectations on direct writes

Writes may carry an expectation about the entity they touch:

- **`expected_revision`** (an integer) on an update, an archive change or a
  delete of a node or edge. It holds when the stored revision equals it.
- **`expect_absent`** on a create. It holds when no entity with that id exists.

A write whose expectation does not hold is refused with
**`entity_revision_conflict`**, carrying `current_revision` (§16.3). The
existing `expected_updated_at` on node updates keeps working unchanged. When
both it and `expected_revision` are given, both must hold.

### 4.4 Where revisions are assigned and expectations enforced

Today, a write changes the in-memory model, fires its event and returns. It
reaches the store afterwards, through a single-worker I/O queue. A shared
store cannot work that way once revisions exist:

- The store, not an instance, must choose the next revision.
- A refusal from the store must reach the caller.

So the two kinds of store behave differently.

**In a shared store, every write is synchronous.** That covers direct writes,
with or without an expectation, and merges.

- The write goes through the same single-worker queue as today, and the caller
  waits for it. So it is ordered after every write the instance queued before
  it, and nothing queued later can overtake it.
- The store applies it in one statement, or one transaction:
  - the revision is the stored revision plus one;
  - the expectation is checked there, as a condition of the write: an update or
    delete applies only when the stored revision matches, and a create with
    `expect_absent` only when no row with that id exists.
- Only after the store commits does the instance update its in-memory model,
  with the revision the store returned. Then the event fires, carrying that
  revision, and the call returns.
- A write the store refuses changes nothing in the instance and fires no event.
  An expectation that does not hold is returned as `entity_revision_conflict`;
  a store failure is returned as an error. Neither marks the storage for the
  whole-graph resync that heals a failed fire-and-forget write. With no
  fire-and-forget writes, a shared store has no such write to heal.
- The backend declares **`revision_enforcement`**, next to `incremental_writes`
  and `transactions`. `EntityOperation` gains `expected_revision` and
  `expect_absent`, and returns the stored revision.
- A backend that declares `change_notification` without `revision_enforcement`
  cannot support staged sessions. Declaring `staged_sessions` on such a
  backend is refused when the backend is constructed (§7).

**A single-writer store keeps today's path for writes without an expectation.**

- The instance is the only writer, so the revision it computes under its lock
  is exact.
- Its fire-and-forget writes, and the whole-graph resync that heals a failed
  one, stay as they are.
- A write with an expectation, and every merge, is submitted to the same queue.
  The instance checks the expectation under the storage lock and waits for the
  write to land before it returns.

### 4.5 Where it shows

- Every read that returns an entity returns its `revision`.
- Every graph event carries it in `before` and `after`.
- Edge update events gain a `patch`, as node updates already have.

### 4.6 Downgrade

An older build ignores `revision`. It drops the field from every entity it
rewrites. Under the file backend a whole-graph save rewrites every entity;
under an incremental backend, only the entities written.

Those entities read as revision 0 afterwards, which is why a downgrade is
outside I6.

It does not make a merge unsafe, because conflicts are decided on field values,
and I7 still holds. What a downgrade loses is only the ordering of the
rewritten entities.

## 5. Write modes

### 5.1 Set at creation

- **Graph default:** each graph has a default write mode, `direct` unless
  configured otherwise. A deployment sets it with `SESSION_WRITE_MODE_DEFAULT`
  (`direct` | `staged`), read the way other settings are.
- **A session gets its write mode when it is created.** It is the mode the
  create request names, or else the graph default at that moment. The session
  also stores `write_mode_source`: `explicit` or `inherited`.
- **A staged session is always created explicitly on the server.** It is
  created by a create request that names `staged`, or that inherits it. This
  matters because today a browser session can exist only locally until its
  first non-empty save.
  - A client sends the session header (§16.1) only for a session that exists on
    the server.
  - A session that exists only in the browser is therefore always direct.
- **Stability:** changing the graph default affects only sessions created
  afterwards. A session's mode changes only through an explicit request (§5.2).
- **Visibility:** every response that describes a session reports `write_mode`
  and `write_mode_source`.

### 5.2 Changing it

- **`direct` → `staged`:** allowed. The change fans out as a session op (§15),
  so every participant sees it.
- **`staged` → `direct`:** never allowed. It is refused with
  `write_mode_irreversible`.
  - This is what makes I2 hold between participants. Once one participant has
    merged, another could otherwise switch the session to `direct`, and the
    first participant's next write would land in the main graph.
  - Direct editing resumes in a new session.
- **Unsupported backend:** a backend that does not declare `staged_sessions`
  (§7) cannot hold a layer.
  - Creating a session in, or switching one to, `staged` there is refused with
    `staged_unsupported`.
  - A graph default of `staged` fails at boot, naming the setting.

### 5.3 Where it is stored

`write_mode` and `write_mode_source` are session metadata. They are stored in
the session document next to `name`. They are neither graph content nor layout,
so D4's rule about the document still holds.

## 6. The layer

### 6.1 Entry shape

```json
{
  "session_id": "…",
  "kind": "node | edge",
  "entity_id": "…",
  "action": "create | update | delete",
  "base_revision": 7,
  "base": { "…": "the main-graph entity at first staging; null for create" },
  "staged": { "…": "create: the full entity; update: changed fields only; delete: null" },
  "staged_by": "actor, as resolved for the request",
  "staged_at": "ISO-8601",
  "session_seq": 42
}
```

- **`base` is the whole entity**, not only the changed fields. §11 needs the
  whole entity for a delete, and so that later edits to further fields of the
  same entity keep the first base.
- **In an update, `staged` uses merge-patch semantics for `metadata`:** a key set
  to `null` removes that key. Every other field in `staged` replaces the base
  value outright.
- **`session_seq`** is the session sequence number of the op that last changed
  the entry (§15).

### 6.2 Collapsing repeated edits

A layer holds the net change per entity:

| Existing entry | New staged action | Result |
|---|---|---|
| none | create / update / delete | a new entry, with `base` read from the main graph now |
| create | update | the create, with the new values merged into `staged` |
| create | delete | the entry is removed; nothing was ever in the graph |
| update | update | one update; `base` kept from the first; `staged` holds the latest value of every field changed so far |
| update | delete | a delete, keeping the first `base` |
| delete | create (same id) | an update against the original `base`. `staged` holds every top-level field of the new entity, and sets to `null` every `metadata` key of `base` that the new entity lacks, so the composed view shows exactly the new entity |

A field staged back to exactly its base value may be dropped from `staged`. An
update left with no staged fields may be removed.

### 6.3 Validation and cascades

Staged writes are validated exactly like direct writes: schema, field limits,
relationship applicability and the rest. They are validated against the
**composed** view, not the main graph.

- **Staging an edge create:** both endpoints must exist in the composed view.
- **Staging a node delete:** in the same op, it also tombstones every edge
  incident to that node in the composed view, mirroring the direct cascade. The
  merge re-checks the incident edges (§11.1, §11.3).
- **Edge updates:** they stage only the fields a direct edge update accepts
  (`type`, `label`, `metadata`) and archive changes. Endpoints cannot change;
  moving an edge is a delete plus a create.

### 6.4 Limits

- A layer holds at most `SESSION_LAYER_MAX_ENTRIES` entries. The default is
  1000; see *open* §20.
- A staging op that would exceed the cap is refused whole, with `layer_full`.
- The cap bounds the per-query cost of composition (§8.4).

## 7. Storage seam

Layers are persisted by the **graph persistence backend**, not by the session
store. That is how one unit can cover a merge's graph writes and its layer
removal (I8), and how layers are shared wherever the graph is.

A backend that can hold layers declares **`staged_sessions`**. A shared store
must also declare `revision_enforcement` (§4.4). It implements:

| Operation | Contract |
|---|---|
| `load_layer(session_id)` | Every entry of that layer. An unknown session yields an empty list. |
| `stage(session_id, entries, removals)` | Upsert the given entries and remove the given entity keys, atomically. |
| `merge(session_id, merge_id, operations, checks, removals, record)` | See below. |
| `discard(session_id, removals)` | Remove the given entries, or all of them. |
| `load_merge(merge_id)` | The stored merge record, or none. |
| `drop_layer(session_id)` | Remove the whole layer; used only by an explicit discard-and-delete (§13). |
| `layers_in_use()` | The ids of sessions whose layers are not empty. |

`merge` does the following **as one unit**:

1. Verify the `checks` of §11.3.
2. Apply the graph `operations`, each with its expectation.
3. Remove the entries.
4. Store the merge `record`.
5. Announce the graph changes through change notification, exactly as a direct
   write does.

A failed check or expectation fails the whole unit and changes nothing. A
`merge_id` already recorded returns that record and does nothing else.

Backend obligations:

- **PostgreSQL.** Two tables live in the graph's schema: one for layer entries,
  keyed on `(session_id, kind, entity_id)`, and one for merge records keyed on
  `merge_id`. Where the store keeps scopes apart, both tables carry the scope
  column and the same policy as the graph tables, so a layer is as isolated as
  the rows it would change. `merge` is one transaction, and it notifies inside
  it as other writes do.
- **File backend.** Layers and merge records live in sidecar files beside the
  graph file. `merge` follows the write-ahead sequence of §11.6.
- **In-memory reference backend.** It implements the capability, so the
  backend contract suite proves the semantics without a server.

The operations are listed by name and meaning. The Python signatures are fixed
in the first slice that needs them (§21) and documented in
`PERSISTENCE_BACKENDS.md`, as the existing seam is.

## 8. Composed reads

### 8.1 The rule

For an entity id `e`, read in a staged session:

- **Entry is a create:** the staged entity.
- **Entry is an update:**
  - the current main-graph entity with the staged fields applied;
  - if the main-graph entity no longer exists, `base` with the staged fields
    applied.
- **Entry is a delete:** absent.
- **No entry:** the main-graph entity, unchanged.

An edge is absent from the composed view when either endpoint is absent from it.

Every entity a composing read returns carries **`staged_state`**:

| Value | Meaning |
|---|---|
| `created` | Staged create. |
| `updated` | Staged update of an entity the main graph still holds. |
| `orphaned` | Staged update of an entity the main graph no longer holds. |
| `none` | No entry. |

`revision` is the main graph's, or `0` for `created` and `orphaned`. Staging
never advances it.

### 8.2 Which reads compose

**Compose, when the request acts in a staged session:**

- `search_graph`, lexical and semantic
- `get_node_details`
- `get_related_nodes`
- `find_similar_nodes`, `find_similar_nodes_batch`
- `list_typed_nodes`, `list_typed_edges`
- `get_graph_stats`, `get_subtypes`
- `audit_relationship_applicability`
- node resolution for the session's own canvas:
  - `GET /sessions/{id}?resolve=true`;
  - `add_nodes_to_session`, whose id check must accept a staged create;
  - `get_visualization_layout`;
  - the other session tools that resolve node ids.
- saved-view resolution
- `export_graph` when a session is given
- the chat assistant's graph tools, when the chat runs in the session

**Never compose (I12):**

- mutation history (`/history*`)
- the event-subscription dispatcher
- agent scheduling and registry scans
- the graph export federation peers pull
- storage diagnostics
- `scripts/graph_file_to_postgres.py`

These read the main graph by definition.

### 8.3 How each kind of read composes

| Read | Approach |
|---|---|
| Lookup by id, listing, stats | Overlay the entries on the in-memory maps. The cost is linear in the layer. |
| Incident edges, edges between nodes | Main-graph adjacency, minus tombstoned edges and edges to absent nodes, plus staged edges. |
| Traversal | Walk a composed adjacency whenever the layer holds anything traversal filters on: any edge entry, any node create or delete, or a node update that changes `type` or `archived`. For that session, the backend's own traversal (`store_traversal`) is bypassed. Only when every entry is a node update that leaves both of those fields alone can the main-graph traversal be used as it stands, with payloads composed afterwards. |
| Lexical search | Search the main graph and drop every id that has an entry. Score the layer's creates and updates, as composed, with the same scorer. Merge both ranked lists. |
| Semantic search | Main-graph results with ids that have entries dropped, merged by score with vectors for the layer's creates and updates. |

**Layer vectors** are derived data. They are not persisted. Each instance embeds
a layer's entries when it first needs them, if embeddings are enabled. It keeps
them apart from the main index, and replaces them when an entry changes.

**Layer caching.** An instance may cache a layer it has loaded. The cache is
invalidated by the session op that changes the layer (§15). An instance that
cannot observe that op, because it is not on the session's event bus, reloads
the layer from the backend on each composing request.

### 8.4 Cost

A composing read costs its main-graph cost plus work linear in the layer size.
It may also need an in-process walk instead of the store's traversal. §6.4's cap
bounds the extra work.

## 9. Staged writes

### 9.1 What stages

In a staged session, every graph write the session makes goes to the layer
instead of the graph. That includes `add_nodes`, `update_node`,
`delete_nodes`, `add_edge`, `update_edge`, `delete_edge(s)`, the archive
operations and federation adoption. It does not matter whether the write comes
through REST, MCP, the chat assistant or an approved agent proposal:

- The response has the shape the direct write returns, plus `"staged": true` and
  the resulting entry.
- **No graph event is emitted** (I1). Subscriptions, agents and history hear
  about the change when it is merged, not before.
- The session emits a session op instead (§15), so every participant's composed
  view follows.
- An agent tool call held by the governance gate records the session it was
  made in. When it is approved, it stages in that session. It never skips the
  layer.

### 9.2 The two expectations a staged write may carry

These are different things with different names and different error codes:

- **`expected_revision`** on a graph write always means the **entity** revision
  (§4.3). On a staged write, it is checked against the composed entity's
  `revision`: the main graph's value, since staging never advances it. A
  mismatch is `entity_revision_conflict`.
- **`expected_session_seq`** on a graph write is the session sequence number. It
  detects a race between two participants staging in the same session. A
  mismatch is `session_seq_conflict`.
- **The existing session tools are unchanged.** Their session-sequence check
  keeps its parameter name, `expected_revision`, and its error code,
  `revision_conflict`. On a session tool, those have only that meaning.

Concurrent staging within one session otherwise follows D2: server-ordered.

## 10. Review: the diff

Reading a layer's diff returns one item per entry:

```json
{
  "kind": "node",
  "entity_id": "…",
  "action": "update",
  "base_revision": 7,
  "main_revision": 9,
  "fields": {
    "description": { "base": "…", "staged": "…", "main": "…", "conflict": true },
    "tags":        { "base": [], "staged": ["a"], "main": [], "conflict": false }
  },
  "status": "clean | conflict | orphaned | id_taken | dangling",
  "depends_on": [ { "kind": "node", "entity_id": "…" } ]
}
```

| Status | When |
|---|---|
| `clean` | It can be merged as is. |
| `conflict` | One or more fields conflict, or a node delete finds main-graph edges it did not tombstone (§11.1). |
| `orphaned` | An update whose entity no longer exists in the main graph. A delete of an entity that no longer exists is `clean`: it has nothing left to do. |
| `id_taken` | A create whose id now exists in the main graph. |
| `dangling` | An edge create or update whose endpoint would be absent from the main graph after this merge. That is judged against the main graph plus the entries merged with it, not against the composed view. |

`depends_on` lists the entries this one cannot be merged without (§11.4). The
`main` values are read when the diff is produced, and a merge re-checks them
(§11.3).

## 11. Merge

### 11.1 Conflicts

**For an update entry**, and each field `f` in `staged`:

- `f` **conflicts** when `main[f] != base[f]` and `main[f] != staged[f]`.
- In words: the main graph changed the field since it was staged, to something
  other than what the session wants.
- A field the main graph changed to the staged value does not conflict.
- A field the main graph did not change does not conflict, however far its
  revision has moved on for other reasons.

Values are compared as JSON values. A list conflicts as a whole: tags are one
field. A field absent from the entity counts as `null`.

**For a delete entry**, it conflicts in either of these cases:

- the main-graph entity differs from `base` in any field, other than `revision`
  and `updated_at`;
- for a node, the main graph holds an incident edge that the layer does not
  tombstone. That edge was added since, or was missed.

A delete is never applied to a changed entity, or through an unseen edge,
without an explicit choice.

**Why values, not revisions:**

- it needs no history, which PostgreSQL mode does not keep;
- it is immune to a restarted lineage (§4.2);
- it lets independent edits to one entity merge cleanly, the way annotation
  field versions already do.

### 11.2 Resolutions

A merge request may carry a resolution per entry:

| Choice | Meaning |
|---|---|
| `take_session` | The session's version wins. |
| `keep_main` | The main graph's version wins; the entry is dropped from the merge and removed from the layer. |
| `as_new` | For a node only: the session's version becomes a new node with a new id, and the main-graph node is left alone. |
| `manual` | For an update only: the caller supplies the final value of every conflicting field. |

What each choice does, by status:

| Status | `take_session` | `keep_main` | `as_new` | `manual` |
|---|---|---|---|---|
| `conflict` (update) | staged values win on the conflicting fields | drop | new node from the composed version | caller's values |
| `conflict` (delete) | delete anyway, including the unstaged incident edges, each with its own expectation | drop | — | — |
| `orphaned` | re-create under the same id, from `base` with the staged fields applied | drop | new node from `base` with the staged fields applied | — |
| `id_taken` | the staged entity replaces the main-graph entity, as an update of every field | drop | create under a new id | — |
| `dangling` | only for a dependant of a dropped create (below): keep it, attached to the main-graph entity that holds the id | drop | — | — |

**Dependants of a dropped create.** When `keep_main` drops an `id_taken` create,
every staged edge that names that id becomes `dangling` for this merge. It must
then be resolved:

- `keep_main` drops it;
- `take_session` keeps it, attached to the main-graph entity that holds the id.

Nothing attaches it silently.

**`as_new` and edges.** A staged edge create in the same merge that names the
old id as an endpoint is created against the new id instead. Staged edge
updates and tombstones keep the edge they name: an edge's endpoints never
change (§6.3).

**What blocks the merge.** Any selected entry that is not `clean`, and has no
resolution the table allows for its status, **stops the whole merge**. The
response is `merge_blocked`, carrying the current diff of the blocking entries.
Nothing is written.

These choices are the conflict outcomes the design has carried from the start:
replace the main-graph object, keep it, create a separate object, or merge the
parts by hand.

### 11.3 Races between the diff and the merge

The instance decides a merge on what it has read. The store makes sure that is
still true when the merge applies.

- Every graph write carries an expectation (§4.3):
  - `expected_revision` set to the revision the instance just read, for an
    update or delete;
  - `expect_absent` for every create.
- The unit also carries **`checks`**: every other condition the decision
  depended on. The store verifies each one inside the unit:
  - **Endpoints exist.** For every edge the merge creates or keeps, both
    endpoints exist, or are created by the same unit.
  - **Incident edges as listed.** For every node the merge deletes, the node's
    incident edges in the store are exactly those the merge deletes with it.
- In a shared store, the unit locks the rows those checks read, so they cannot
  change before it commits. The same rule applies to direct writes: an edge
  create locks its endpoint rows for share, and a node delete locks its node
  row. So an edge create and a node delete on the same node are ordered against
  each other, whichever instance issues them.
- If any expectation or check fails, the whole merge fails with
  `merge_raced`, and nothing is written. The caller re-reads the diff and tries
  again.

### 11.4 Selection and dependencies

A merge request names:

- the entries, a list of `(kind, entity_id)` or `all`;
- optional resolutions;
- a caller-chosen **`merge_id`**, which gives I9.

The selection must be **closed under dependencies**. An entry depends on:

- for an edge create: the create entries of its endpoints, where they have them;
- for a node delete: the tombstones of its incident edges;
- for an `as_new` node: the edge creates in the layer that name the old id.

A selection that leaves out a dependency is refused with `merge_incomplete`,
naming the missing entries. `all` is always closed.

### 11.5 Effect

On success:

- The selected entries are applied to the main graph and removed from the layer
  as one unit (§7). The applying order never leaves an edge without both
  endpoints.
- Revisions advance as for any direct write.
- Graph events fire after the unit commits, as for direct writes:
  - `event_origin` is `session-merge`;
  - `event_correlation_id` is the merge id;
  - attribution goes to the merging actor, and to each entry's `staged_by`.
- A **merge record** is stored with the layer (§7). It holds:
  - the merge id, session id, actor and time;
  - per entry: action, entity, base revision, resulting revision and resolution.

  It is the audit trail of the merge, and it does not depend on mutation
  history, which PostgreSQL mode does not keep. How long merge records are kept
  is *open* (§20).

A partial merge leaves the unselected entries in the layer untouched.

### 11.6 Crash safety

**PostgreSQL.** The unit is one transaction, so nothing needs recovering.

**File backend.** It is single-writer. It holds the storage lock from step 1 to
step 4, so no other write can touch the graph in between:

1. Write the merge record, marked `pending`, to the merge log and sync it. It
   holds everything recovery needs: the selected entries, and every graph
   operation with the content and revision it will produce.
2. Apply the graph operations as one journal batch. The file backend already
   makes a batch atomic, and a merge applies every operation it lists (§4.2).
3. Rewrite the layer file without the merged entries.
4. Mark the record `applied`.

On load, a `pending` record is resolved against the graph itself. It does not
rely on the journal, whose lines a checkpoint or whole-graph save removes:

- **Every operation's content and revision are present in the graph:** the batch
  landed. Steps 3 and 4 are completed.
- **None is present:** it did not land. The record is removed, and the layer is
  left as it was.

The lock rules out interleaved writes, and the batch is atomic, so no mixed
state can occur.

A retry with the same `merge_id` finds the record. A `pending` one is resolved
first; an `applied` one is returned (I9).

## 12. Discard

- **Scope:** discard all entries, or a list of them.
- **Effect:** the entries are removed from the layer, and nothing else changes
  (I10).
- **Visibility:**
  - it emits a session op, so every participant's composed view follows;
  - it records a session activity entry;
  - it emits no graph event.

## 13. Session lifecycle

- **Delete:** deleting a session whose layer is not empty is refused with
  `layer_not_empty`, unless the request says `discard_staged: true`. Then the
  layer is dropped first, and the delete proceeds as today (I11).
- **D11 still applies:** every connected client moves to its own new, empty
  session, generated in the browser as today.
  - The delete notification carries the deleted session's write mode.
  - A client leaving a `staged` session creates its replacement explicitly on
    the server, as `staged` (§5.1), before it sends that session's header.

  A client that was staging therefore keeps staging (I2).
- **Retention:** D13 still applies. A layer lives as long as its session does.
  Whether staged work should expire is *open*.
- **Rename:** no effect on the layer.
- **Write-mode change:** as in §5.2.

## 14. Permissions

The authorization hook is asked about staging, merging and discarding as
**`mutate`** actions. A hook written today, including the read-only mode of the
default hook, therefore refuses them exactly as it refuses a direct write.

The authorization context gains two optional fields:

- **`graph_operation`:** `stage`, `merge` or `discard`;
- **`session_id`:** the session concerned.

A hook that wants to treat these differently from a direct write reads them.
`target` keeps its existing meaning, the tool or route name.

**Session routes.**

- The session routes in `service/rest_api.py` call no hook today.
- The ones that change a layer, set a write mode or merge must call it.
- Two session routes the host serves, trigger-token and auto-add agents, already
  do.

The core still has no accounts (D7). The actor recorded on entries and merge
records is whatever the request resolved: an actor header, or failing that the
client id. Deciding *who may merge* in a deployment with identities is the
hook's job, not this contract's.

## 15. Realtime

Staging, discarding, merging and changing the write mode are session ops. They
advance the session sequence and fan out on the session event bus, like every
other session op. The new op kinds are:

- **`graph_change_staged`:** carries the resulting entry, or its removal after
  collapsing;
- **`graph_changes_discarded`:** carries the removed keys;
- **`graph_changes_merged`:** carries the merge id and the merged keys;
- **`write_mode_changed`:** carries the new mode.

A client re-reads composed entities, or applies the carried entries, to keep its
canvas in step. The catch-up snapshot stays as it is: it carries session state,
and a client that needs the layer reads the diff.

## 16. API surface

The shapes below are illustrative. Field names and error codes are normative,
routes are indicative, and the exact request and response models are fixed in
the slice that implements them.

### 16.1 Acting in a session

**Over HTTP**, a request acts in a session through the header
**`x-communityoverview-session-id`**. The binding has two rules:

- **Bound once, for every request.** The header is bound into the request
  context by middleware, not route by route. That includes the routes that do
  not bind the workspace and graph headers today: session routes, agent routes,
  proposal approval and the host's own routes. No route can miss it.
- **No silent ignoring.** A route that neither composes (§8.2) nor stages (§9),
  and can be reached with the header, refuses such a request with
  `session_not_supported`. The header must never be ignored (I2).

**Over MCP**, graph read and write tools gain an optional
**`visualization_session_id`** argument. The same rules apply:

- a tool that neither composes nor stages refuses the argument with
  `session_not_supported`;
- a session tool, which already names its session, refuses a header naming a
  different session with `session_mismatch`;
- where the header and the argument name the same session, there is nothing to
  decide. Where they differ, the request is refused with `session_mismatch`.

The name `session_id` is avoided for this argument. It already names the
per-tab event id and the MCP transport session.

**Session resolution:**

- In a direct-mode session, a session id changes nothing about graph reads and
  writes.
- An id that does not resolve is refused with `session_not_found` (I2).
  Clients send it only for sessions that exist on the server (§5.1), so this
  does not block today's local-first sessions.

### 16.2 New operations

| Operation | REST (indicative) | MCP tool |
|---|---|---|
| Read the diff | `GET /api/sessions/{id}/staged` | `get_staged_changes` |
| Merge | `POST /api/sessions/{id}/staged/merge` | `merge_staged_changes` |
| Discard | `POST /api/sessions/{id}/staged/discard` | `discard_staged_changes` |
| Switch to staged | `PATCH /api/sessions/{id}` with `write_mode` | `set_session_write_mode` |

A session is created as staged through the existing create route and tool,
with `write_mode` in the request.

### 16.3 Error codes

Every refusal carries an `error` code, a human-readable `message` and the data
needed to act on it. Over MCP, it also carries `success: false`. The existing
session-tool code `revision_conflict` is unchanged (§9.2).

| Code | HTTP | Raised by |
|---|---|---|
| `entity_revision_conflict` | 409 | an entity expectation on a direct or staged write (§4.3, §9.2) |
| `session_seq_conflict` | 409 | `expected_session_seq` on a graph write (§9.2) |
| `merge_blocked` | 409 | an unresolved entry (§11.2) |
| `merge_raced` | 409 | an expectation or check failing inside the merge unit (§11.3) |
| `merge_incomplete` | 422 | a selection not closed under dependencies (§11.4) |
| `write_mode_irreversible` | 409 | switching a staged session to direct (§5.2) |
| `layer_not_empty` | 409 | deleting a session with a non-empty layer (§13) |
| `layer_full` | 422 | exceeding the layer cap (§6.4) |
| `staged_unsupported` | 422 | staged mode on a backend without `staged_sessions` (§5.2) |
| `session_not_found` | 404 | a session id that does not resolve (§16.1) |
| `session_not_supported` | 400 | a session on a route or tool that neither composes nor stages (§16.1) |
| `session_mismatch` | 400 | two different sessions named in one request (§16.1) |

## 17. Frontend obligations

Frontend work is specified in its own implementation slice. This contract fixes
only what the user must be able to see:

- the session's write mode, where they are editing;
- which entities on the canvas are staged, and how (`staged_state`);
- how many changes are pending;
- a review surface showing the diff of §10;
- merge and discard with the choices of §11.2.

A staged session must not look like a direct one. Assuming edits have reached
the graph when they have not is the failure this feature invites.

## 18. Compatibility

Direct mode is the default, and a deployment that never enables staged mode
sees no behaviour change beyond these:

- **`revision` appears on entities.** It is additive in the graph file, in
  PostgreSQL's JSONB rows and in API responses. Older clients ignore it.
- **Expectations are checked.** A write that carries one (§4.3) is checked, and
  waits for the store.
- **In a shared store, every write is synchronous** (§4.4).
  - A write returns after the store commits, not before, so writes are slower
    by one round trip.
  - A store failure is reported to the caller, instead of being healed later
    by a whole-graph resync.
- **In a shared store, edge creates and node deletes on the same node are
  ordered against each other** (§11.3).

Otherwise:

- The session document gains only `write_mode` and `write_mode_source`. Its
  layout, references and annotations are unchanged. D4 holds for the document,
  revised by ADR 0005.
- The existing `expected_updated_at` optimistic check on node updates, and the
  session tools' `revision_conflict`, are kept.

## 19. Out of scope

- merging one session's layer into another session, or branching a session;
- merging into a different graph or a federation peer;
- live co-editing of the same field with CRDT or OT (D2 stands);
- time travel or history browsing of layers;
- per-field access control;
- read-only sharing of a session. When sharing exists, a shared session shows
  its composed view, and a shared graph shows the main graph.

## 20. Open questions

Each is decided in review before the slice that needs it starts. The
recommendation comes first.

1. **Graph default write mode.** `direct`. That keeps every existing deployment
   unchanged, and staging is opted into per session or per graph.
2. **Layer cap.** 1000 entries per layer. That is ample for a workshop, and it
   keeps composition cheap. It is configurable.
3. **Merge-record retention.** Keep records as long as the session exists, plus
   90 days after a session is deleted. They are the only audit a merge leaves
   in PostgreSQL mode.
4. **Staged-work expiry.** None in the core, consistent with D13. A deployment
   that wants expiry adds it on top.
5. **Embedding layer entries.** Embed lazily, per instance, when a composing
   semantic read first needs them (§8.3). This avoids paying for embeddings of
   work that may be discarded.

## 21. Implementation order

These are dependencies between parts of the design, not a schedule.

| Slice | Content | Depends on |
|---|---|---|
| **S1** | Entity revisions (§4): the field; its advancement; expectations on direct writes; synchronous writes and store-assigned revisions in a shared store; endpoint and node-row locking; `revision_enforcement`; the edge `patch`. Useful on its own. | — |
| **S2** | The `staged_sessions` capability and the layer store (§7) in the in-memory, file and PostgreSQL backends, with contract tests, including the merge unit's checks and the file backend's write-ahead merge (§11.3, §11.6). | S1 |
| **S3** | Write modes (§5); session binding in middleware with no silent ignoring (§16.1); staged writes (§9); permissions (§14). | S2 |
| **S4** | Composed reads (§8) across every read in §8.2. | S3 |
| **S5** | Diff, merge, discard and dependencies (§10–§12), over REST and MCP. | S4 |
| **S6** | Frontend (§17), including creating the replacement for a deleted staged session (§13). | S5 |
| **S7** | End-to-end workshop acceptance: isolation, composed search and traversal, concurrent main-graph edits, selective merge, discard, and unchanged direct mode. | S6 |
