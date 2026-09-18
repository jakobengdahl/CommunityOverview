# Session Overlay Contract

Staged editing in a session: changes made in the session stay out of the graph
until someone explicitly merges them.

- **Status:** Draft contract, adopted by
  [ADR 0005](adr/0005-sessions-may-stage-graph-changes.md). It is normative for
  implementation. A section marked *open* is decided in review before the slice
  that needs it starts (§20).
- **Scope:** Open-source core. It covers the model, the storage seam, the read
  and write semantics, the review and merge API, and the invariants every
  implementation slice is tested against.
- **Related:** [`MULTI_USER_SESSIONS_DESIGN.md`](MULTI_USER_SESSIONS_DESIGN.md)
  (the session model this extends; D4 is revised by ADR 0005),
  [`PERSISTENCE_BACKENDS.md`](PERSISTENCE_BACKENDS.md) (the storage seam this
  adds a capability to),
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
| **Write mode** | `direct` (today's behaviour) or `staged`, resolved per session (§5). |
| **Layer** | A session's staged changes: at most one **entry** per entity. |
| **Entry** | The net staged change to one node or edge: create, update or delete (§6). |
| **Tombstone** | A delete entry. It hides the entity in the composed view. |
| **Composed view** | The graph as seen from a staged session: main graph plus layer (§8). |
| **Revision** | A per-entity counter only the storage assigns (§4). |
| **Base** | The main-graph entity as it was when an entry was first staged: its revision and its full content. |
| **Field** | A top-level attribute of a node or edge. Each top-level key of `metadata` counts as its own field, the same granularity as the existing metadata merge-patch. |
| **Merge** | Applying selected entries to the main graph as one unit (§11). |

## 3. Invariants

Every implementation slice is reviewed and tested against these. A change that
breaks one is a defect, whatever else it achieves.

- **I1 — Isolation.** A write made in a staged session changes no node, edge or
  graph metadata in the main graph, and emits no graph event, until a merge
  applies it.
- **I2 — Fail closed on the session.** A request that names a session the server
  cannot resolve is refused. It is never treated as a direct write. A caller who
  believes it is staging must never write to the main graph by mistake.
- **I3 — Thin layer.** A layer holds entries only for entities the session
  changed. Creating, reading, merging or discarding a layer never copies
  unchanged entities.
- **I4 — Composition is consistent.** Every read listed in §8.2, made in a staged
  session, returns the composed view. No composing read returns an entity the
  layer tombstoned, or an edge with an endpoint the composed view lacks.
- **I5 — Direct mode is unchanged.** With no session, or in a direct-mode
  session, every read and write behaves exactly as before this contract, apart
  from `revision` now being present (§4).
- **I6 — Revisions are storage-assigned and monotonic.** A client cannot set a
  revision. Every write the storage applies to an entity advances its revision
  by exactly one. A backend shared by several writers refuses a write whose
  expected revision is not the stored one.
- **I7 — No silent overwrite.** A merge applies an entry only when every field it
  changes is still at its base value in the main graph, or when the caller has
  resolved that field explicitly. For a delete, this covers every field.
- **I8 — Atomic merge.** A merge either applies every selected entry, and removes
  each from the layer, or applies none and leaves the layer as it was. This holds
  across a crash (§11.5).
- **I9 — Idempotent merge.** Repeating a merge with the same merge id writes
  nothing further and returns the first result.
- **I10 — Discard touches only the layer.** Discarding changes nothing in the
  main graph.
- **I11 — No orphaned work.** A layer is never deleted as a side effect. Deleting
  a session with a non-empty layer needs an explicit discard.
- **I12 — System consumers read the graph.** Event subscriptions, agent
  scheduling, federation peers, history and diagnostics never see a layer.

## 4. Entity revisions

Revisions come first and stand on their own. They give every direct write
optimistic concurrency too, and a merge cannot be safe without them.

### 4.1 The field

- Nodes and edges gain **`revision`**, an integer of at least 0.
- The name is deliberately not `version`, which already has three meanings in
  the code: the graph metadata's `version`, the event envelope's
  `schema_version`, and the annotation `version`.
- `revision` is **server-owned**. A caller-supplied value is ignored, stripped at
  the service layer like other fields the storage owns, and never folded into
  `metadata`.

### 4.2 How it advances

- A newly created entity has revision **1**.
- Every write the storage applies to an existing entity advances its revision by
  exactly **1**. That includes node and edge updates, archiving and unarchiving,
  and a merge's writes.
- A write that would leave the entity's content unchanged need not be applied.
  If it is not applied, the revision does not move.
- Deleting an entity ends its lineage. A later entity with the same id starts
  again at 1. §11.1 is written so that this reuse cannot make a stale change
  look current: conflicts are decided on field values, not on revision equality
  alone.
- An entity loaded without the field reads as revision **0**. Its first applied
  write makes it 1. No migration pass is needed.
- The cascade that deletes a node's incident edges applies no revision check to
  those edges, as today. A staged node delete is different: it tombstones the
  incident edges explicitly (§6.3), so the merge checks each of them.

### 4.3 Expected revision on direct writes

- Updates, archive changes and deletes of nodes and edges accept an optional
  **`expected_revision`**. When it is given and does not equal the stored
  revision, the write is refused with a conflict carrying the current revision.
- The existing `expected_updated_at` on node updates keeps working unchanged.
  When both are given, both must hold.

### 4.4 Who enforces it

- In a single-writer backend (the file backend), the storage's own lock orders
  writes, so the in-process check is the enforcement.
- In a backend shared by several writers (PostgreSQL), the **store** must enforce
  it. The operation carries the expected revision, and the store applies it
  only if the stored revision matches: a conditional update, not an upsert. An
  instance's in-memory check alone cannot stop two instances from both assigning
  N+1.
- To carry the expectation, `EntityOperation` gains an optional
  `expected_revision`. A backend that enforces it declares so in its
  capabilities, next to `incremental_writes` and `transactions`.

### 4.5 Where it shows

- Every read that returns an entity returns its `revision`.
- Graph events carry it in `before` and `after`.
- Edge update events gain a `patch`, as node updates already have.

### 4.6 Downgrade

An older build ignores `revision` and drops it from every entity it rewrites.
Under the file backend a whole-graph save rewrites every entity, and under an
incremental backend only the entities written. Those entities then read as
revision 0. A staged change based on a later
revision then fails the field check or the expected-revision check (§11) and
surfaces as a conflict. It fails safe, not silently.

## 5. Write modes

### 5.1 Resolution

- **Graph default:** each graph has a default write mode, `direct` unless
  configured otherwise. A deployment sets it with the configuration setting
  `SESSION_WRITE_MODE_DEFAULT` (`direct` | `staged`), read the way the other
  settings are.
- **Per session:** the session carries `write_mode`: `inherit` (the default),
  `direct` or `staged`. The **resolved** mode is the session's own value unless
  that is `inherit`, in which case it is the graph default at the time of
  resolution.
- Every response that describes a session reports both values, the stored
  `write_mode` and `resolved_write_mode`, so nobody has to guess where their
  edits go.

### 5.2 Transitions

- **`direct` → `staged`:** always allowed.
- **`staged` → `direct`**, or any change that would resolve to `direct`: refused
  while the layer is not empty. Merge or discard first. This includes a change of
  the graph default: a session inheriting `staged` with a non-empty layer keeps
  resolving to `staged` until its layer is empty.
- **Unsupported backend:** a backend that does not declare `staged_sessions`
  (§7) cannot hold a layer. Setting a session to `staged` there is refused, and
  a graph default of `staged` fails at boot, naming the setting.

### 5.3 Where the mode is stored

`write_mode` is session metadata, stored in the session document next to `name`.
It is neither graph content nor layout, so D4's rule about the document still
holds.

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
  "staged_by": "actor id, as resolved for the request",
  "staged_at": "ISO-8601",
  "session_seq": 42
}
```

- **`base` is the whole entity**, not only the changed fields. §11 needs the
  whole entity for a delete, and so that later edits to further fields of the
  same entity keep the first base.
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
| delete | create (same id) | an update whose `staged` holds every field of the new entity, against the original `base` |

A field staged back to exactly its base value may be dropped from `staged`. An
update left with no staged fields may be removed.

### 6.3 Validation and cascades

Staged writes are validated exactly like direct writes: schema, field limits,
relationship applicability and the rest. They are validated against the
**composed** view, not the main graph.

- **Staging an edge create:** both endpoints must exist in the composed view.
- **Staging a node delete:** in the same op, it also tombstones every edge
  incident to that node in the composed view, mirroring the direct cascade.
- **Edge updates:** they stage only the fields a direct edge update accepts
  (`type`, `label`, `metadata`) and archive changes. Endpoints cannot change;
  moving an edge is a delete plus a create.

### 6.4 Limits

- A layer holds at most `SESSION_LAYER_MAX_ENTRIES` entries. The default is
  1000; see *open* §20.
- A staging op that would exceed the cap is refused whole, naming the setting.
- The cap bounds the per-query cost of composition (§8.4).

## 7. Storage seam

Layers are persisted by the **graph persistence backend**, not by the session
store. That is how one transaction can cover a merge's graph writes and its
layer removal (I8), and how layers are shared wherever the graph is. A backend
that can hold layers declares a new capability, **`staged_sessions`**, and
implements:

| Operation | Contract |
|---|---|
| `load_layer(session_id)` | Every entry of that layer, in no particular order. An unknown session yields an empty list. |
| `stage(session_id, entries, removals)` | Upsert the given entries and remove the given entity keys, atomically. |
| `merge(session_id, merge_id, operations, removals, record)` | Apply the graph `operations` (each may carry `expected_revision`), remove the entries, and store the merge `record`, **as one unit**. An expected revision that does not hold fails the whole unit. A `merge_id` already recorded returns that record and does nothing else. |
| `discard(session_id, removals)` | Remove the given entries, or all of them. |
| `load_merge(merge_id)` | The stored merge record, or none. |
| `drop_layer(session_id)` | Remove the whole layer; used only by an explicit discard-and-delete (§13). |
| `layers_in_use()` | The ids of sessions whose layers are not empty. |

Backend obligations:

- **PostgreSQL.** Two tables live in the graph's schema: one for layer entries,
  keyed on `(session_id, kind, entity_id)`, and one for merge records keyed on
  `merge_id`. Where the store keeps scopes apart, both tables carry the scope
  column and the same policy as the graph tables, so a layer is as isolated as
  the rows it would change. `merge` is one transaction.
- **File backend.** Layers are sidecar files beside the graph file, one per
  session. `merge` appends one journal batch tagged with the merge id, then
  rewrites the layer file and the merge log. A crash between the two is
  recovered on load (§11.5).
- **In-memory reference backend.** It implements the capability, so the
  backend contract suite proves the semantics without a server.

The operations are listed by name and meaning. The Python signatures are fixed
in the first implementation slice (§21) and documented in
`PERSISTENCE_BACKENDS.md`, as the existing seam is.

## 8. Composed reads

### 8.1 The rule

For an entity id `e`, read in a staged session:

- **Entry is a create:** the staged entity.
- **Entry is an update:** the current main-graph entity with the staged fields
  applied. If the main-graph entity no longer exists, the read returns `base`
  with the staged fields applied, flagged orphaned (§10).
- **Entry is a delete:** absent.
- **No entry:** the main-graph entity, unchanged.

An edge is absent from the composed view when either endpoint is absent from it.

Every entity a composing read returns carries **`staged_state`**: `created`,
`updated` or `none`. `revision` is the main graph's; staging never advances it.

### 8.2 Which reads compose

**Compose, when the request acts in a staged session:**

- `search_graph`, lexical and semantic
- `get_node_details`
- `get_related_nodes`
- `find_similar_nodes`, `find_similar_nodes_batch`
- `list_typed_nodes`, `list_typed_edges`
- `get_graph_stats`, `get_subtypes`
- `audit_relationship_applicability`
- session node resolution (`GET /sessions/{id}?resolve=true` and its MCP
  equivalents)
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
| Semantic search | Main-graph results with ids that have entries dropped, merged by score with the layer's own vectors. A staged create or update is embedded when it is staged, if embeddings are enabled, and those vectors are kept apart from the main index. |

### 8.4 Cost

A composing read costs its main-graph cost plus work linear in the layer size.
It may also need an in-process walk instead of the store's traversal. §6.4's cap
bounds the extra work.

## 9. Staged writes

In a staged session, every graph write the session makes goes to the layer
instead of the graph. That includes `add_nodes`, `update_node`,
`delete_nodes`, `add_edge`, `update_edge`, `delete_edge(s)`, the archive
operations and federation adoption. It does not matter whether the write comes
through REST, MCP, the chat assistant or an agent:

- The response has the shape the direct write returns, plus `"staged": true` and
  the resulting entry.
- **No graph event is emitted** (I1). Subscriptions, agents and history hear
  about the change when it is merged, not before.
- The session emits a session op instead (§15), so every participant's composed
  view follows.
- Concurrent staging within one session follows D2: server-ordered, with the
  existing optional `expected_revision` on the **session** sequence for callers
  who want to detect a race.
- An agent tool call held by the governance gate stages when it is approved, if
  the call was made in a staged session. It never skips the layer.

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
  "status": "clean | conflict | orphaned | id_taken | dangling"
}
```

| Status | When |
|---|---|
| `clean` | It can be merged as is. |
| `conflict` | One or more fields conflict (§11.1). |
| `orphaned` | An update or delete whose entity no longer exists in the main graph. |
| `id_taken` | A create whose id now exists in the main graph. |
| `dangling` | An edge create or update whose endpoint no longer exists in the composed view, after changes to the main graph. |

`main_revision` and the `main` values are read when the diff is produced. A
merge re-checks them (§11.3).

## 11. Merge

### 11.1 Conflicts, per field

For an **update** entry and each field `f` in `staged`:

- `f` **conflicts** when `main[f] != base[f]` and `main[f] != staged[f]`.
- In words: the main graph changed the field since it was staged, to something
  other than what the session wants.
- A field the main graph changed to the staged value does not conflict.
- A field the main graph did not change does not conflict, however far its
  revision has moved on for other reasons.

For a **delete** entry, it conflicts when the main-graph entity differs from
`base` in any field, other than `revision` and `updated_at`. A delete is never
applied to a changed entity without an explicit choice.

Comparing values rather than revisions is deliberate:

- It needs no history, which PostgreSQL mode does not keep.
- It is immune to an id being deleted and re-created (§4.2).
- It lets independent edits to one entity merge cleanly, the way annotation
  field versions already do.

### 11.2 Resolutions

A merge request may carry a resolution per entity:

| Choice | Effect | Applies to |
|---|---|---|
| `take_session` | Staged values win on the conflicting fields. For a delete: delete anyway. | all |
| `keep_main` | The entry is dropped from the merge and removed from the layer. | all |
| `as_new` | The staged node is created as a new node with a new id. The main-graph node is left alone. Staged edges that pointed at the old id are carried over to the new id within the same merge. | node update, `id_taken` create |
| `manual` | The caller supplies the final value of every conflicting field. | update |

An entry with status `conflict`, `orphaned`, `id_taken` or `dangling`, and no
resolution that clears it, **stops the whole merge**. The response is a conflict
carrying the current diff of the blocking entries. Nothing is written.

These four choices are the conflict outcomes the design has carried from the
start: replace the main-graph object, keep it, create a separate object, or
merge the parts by hand.

### 11.3 Races between the diff and the merge

- The merge re-reads every selected entity's main revision while it validates.
- It sends every graph write with that revision as `expected_revision` (§4.4).
- If any of them has moved by the time the unit applies, the whole merge fails
  with `merge_raced` and nothing is written.
- The caller re-reads the diff and tries again.

### 11.4 Request and effect

A merge request names:

- the entries, a list of `(kind, entity_id)` or `all`;
- optional resolutions;
- a caller-chosen **`merge_id`**, which gives I9.

On success:

- The selected entries are applied to the main graph and removed from the layer
  as one unit. The applying order never leaves an edge without both endpoints.
- Revisions advance as for any direct write.
- Graph events fire as for direct writes:
  - with `origin` `session-merge`;
  - with `correlation_id` set to the merge id;
  - with attribution to the merging actor, and to each entry's `staged_by`.
- A **merge record** is stored with the layer (§7). It holds:
  - the merge id, session id, actor and time;
  - per entry: action, entity, base revision, resulting revision and resolution.

  It is the audit trail of the merge, and it does not depend on mutation
  history, which PostgreSQL mode does not keep. How long merge records are kept
  is *open* (§20).

A partial merge leaves the unselected entries in the layer untouched.

### 11.5 Crash recovery

- **PostgreSQL:** the unit is one transaction, so there is nothing to recover.
- **File backend:** the journal batch carries the merge id.
  - On load, a layer file that still holds entries of a merge whose tagged batch
    is in the journal or the checkpoint is completed: the entries are removed and
    the record is written.
  - A merge whose batch never reached the journal left the graph untouched. Its
    entries stay in the layer.

In both cases, I8 holds after the crash.

## 12. Discard

- **Scope:** discard all entries, or a list of them.
- **Effect:** the entries are removed from the layer, and nothing else changes
  (I10).
- **Visibility:**
  - it emits a session op, so every participant's composed view follows;
  - it records a session activity entry;
  - it emits no graph event.

## 13. Session lifecycle

- **Delete:** deleting a session whose layer is not empty is refused, unless the
  request says `discard_staged: true`. Then the layer is dropped first, and the
  delete proceeds as today (I11).
  - D11 still applies: every connected client gets its own new, empty session.
  - That new session inherits the graph's write mode, not the deleted
    session's layer.
- **Retention:** D13 still applies. A layer lives as long as its session does.
  Whether staged work should expire is *open*.
- **Rename:** no effect on the layer.
- **Write-mode change:** as in §5.2.

## 14. Permissions

The authorization hook gains three actions:

- **`stage`:** a write made in a staged session;
- **`merge`;**
- **`discard`.**

Their `target` names the session. A hook that does not decide these actions
explicitly treats them as `mutate`.

- Read-only mode therefore refuses staging and merging, as it refuses direct
  writes.
- The permissive default allows them.

The session REST endpoints that change a layer, set a write mode or merge must
call the hook. That is new: today's session REST endpoints do not.

The core still has no accounts (D7). The actor recorded on entries and merge
records is whatever the request resolved: an actor header, or failing that the
client id. Deciding *who may merge* in a deployment with identities is the
hook's job, not this contract's.

## 15. Realtime

Staging, discarding and merging are session ops. They advance the session
sequence and fan out on the session event bus, like every other session op. The
new op kinds are:

- **`graph_change_staged`:** carries the resulting entry, or its removal after
  collapsing;
- **`graph_changes_discarded`:** carries the removed keys;
- **`graph_changes_merged`:** carries the merge id and the merged keys.

A client re-reads composed entities, or applies the carried entries, to keep its
canvas in step. The catch-up snapshot stays as it is: it carries session state,
and a client that needs the layer reads the diff.

## 16. API surface

The shapes below are illustrative. Field names are normative, routes are
indicative, and the exact request and response models are fixed in the slice
that implements them.

### 16.1 Acting in a session

**Over HTTP:**

- a request acts in a session through the header
  **`x-communityoverview-session-id`**;
- the header is bound into the request context the same way as the existing
  `x-communityoverview-workspace-id` and `-graph-id` headers.

**Over MCP:**

- graph read and write tools gain an optional **`visualization_session_id`**
  argument;
- where both the header and the argument are present, the argument wins.

The name `session_id` is avoided for this argument. It already names the
per-tab event id and the MCP transport session.

In a direct-mode session, a session id changes nothing about graph reads and
writes. An id that does not resolve is refused (I2).

### 16.2 New operations

| Operation | REST (indicative) | MCP tool |
|---|---|---|
| Read the diff | `GET /api/sessions/{id}/staged` | `get_staged_changes` |
| Merge | `POST /api/sessions/{id}/staged/merge` | `merge_staged_changes` |
| Discard | `POST /api/sessions/{id}/staged/discard` | `discard_staged_changes` |
| Set write mode | `PATCH /api/sessions/{id}` with `write_mode` | `set_session_write_mode` |

Conflict responses follow the pattern annotation field conflicts established:

- HTTP 409;
- over MCP, `success: false`;
- an `error` code of `field_conflict`, `merge_blocked`, `merge_raced`,
  `layer_full` or `session_not_found`;
- a human-readable `message`;
- the data needed to act on it.

## 17. Frontend obligations

Frontend work is specified in its own implementation slice. This contract fixes
only what the user must be able to see:

- the session's resolved write mode, where they are editing;
- which entities on the canvas are staged, and how;
- how many changes are pending;
- a review surface showing the diff of §10;
- merge and discard with the choices of §11.2.

A staged session must not look like a direct one. Assuming edits have reached
the graph when they have not is the failure this feature invites.

## 18. Compatibility

- Direct mode is the default. A deployment that never enables staged mode sees
  no behaviour change, apart from `revision` appearing on entities (I5).
- `revision` is additive in the graph file, in PostgreSQL's JSONB rows and in
  API responses. Older clients ignore it.
- The session document gains only `write_mode`. Its layout, references and
  annotations are unchanged. D4 holds for the document, revised by ADR 0005.
- The existing `expected_updated_at` optimistic check on node updates is kept.

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
5. **Embedding staged nodes.** Embed at staging time when embeddings are
   enabled, so semantic search sees staged content. The cost is one embedding
   call per staged create or text change, the same as a direct write.

## 21. Implementation order

These are dependencies between parts of the design, not a schedule.

| Slice | Content | Depends on |
|---|---|---|
| **S1** | Entity revisions (§4): the field, its advancement, `expected_revision` on direct writes, store-enforced checks in PostgreSQL, the edge `patch`. Useful on its own. | — |
| **S2** | The `staged_sessions` capability and the layer store (§7) in the in-memory, file and PostgreSQL backends, with contract tests. | S1 |
| **S3** | Write modes (§5), request-scope session binding (§16.1), staged writes (§9) and the permissions (§14). | S2 |
| **S4** | Composed reads (§8) across every read in §8.2. | S3 |
| **S5** | Diff, merge, discard and crash recovery (§10–§12), over REST and MCP. | S4 |
| **S6** | Frontend (§17). | S5 |
| **S7** | End-to-end workshop acceptance: isolation, composed search and traversal, concurrent main-graph edits, selective merge, discard, and unchanged direct mode. | S6 |
