# ADR 0013 — Graph import: MERGE mode beside REPLACE

- **Status:** Accepted
- **Date:** 2026-10-15 (proposed and accepted 2026-10-15)
- **Scope:** Open-source core only
- **Related:** [ADR 0006](0006-graph-import-replace-mode.md) (the REPLACE
  pipeline this sits beside), [ADR 0007](0007-vector-aware-export-archive.md)
  (the archive variant), [ADR 0008](0008-node-attachment-storage.md) §8
  (attachments across an import), [ADR 0009](0009-dual-mode-graph-storage-seam.md)
  (one graph per core process; file and PostgreSQL modes),
  [ADR 0010](0010-attachment-content-access-for-agents-and-assistant.md)
  (attachment content reaching agents), [`SESSION_OVERLAY_CONTRACT.md`](../SESSION_OVERLAY_CONTRACT.md)
  §4 (entity revisions and expectations) and §10–§11 (the staged-merge design
  this borrows its vocabulary from), [`DURABLE_EXECUTION_CONTRACT.md`](../DURABLE_EXECUTION_CONTRACT.md)

This ADR designs a second import mode. Each section headed **Decision point**
lists options, trade-offs and a recommendation. The ADR was proposed with the
recommendations as proposals; on acceptance the recommended option was chosen
for every point, as recorded below.

## Decisions on acceptance

Accepted on 2026-10-15: the **recommended option of every decision point** is
the decision. Nothing was changed from the recommendations.

| Point | Decision |
|---|---|
| §1 ID-collision policy | Default `fail` for nodes and edges; granularity (b), one default plus per-type overrides; type and endpoint mismatch (ii), a conflict under every policy except `rename`; subtype change under overwrite (a), re-judge live incident edges |
| §2 Edge handling | Endpoints in the live graph allowed; dangling rejected; archived live endpoint allowed and reported; edges of skipped nodes (a), attached to the live node; duplicate triples (b), skipped and reported; live edges never deleted |
| §3 Preview and binding | (c) stateless digest, with expectations and checks on every commit; a commit without a digest only when every policy is `fail`/`skip` and `include_system_types` is false |
| §4 Atomicity | (a) all-or-nothing through a new `GraphStorage` method; backup (a), the v1 whole-graph backup before every merge; `generation` not bumped |
| §5 Validation | Unknown types (a), reject the whole import; system types (b), excluded unless `include_system_types: true` |
| §6 Embeddings | Per-node comparison in the worker (b); PostgreSQL entity-upsert embedding commit (a) in slice 3, slice 2 refusing PostgreSQL merges until then |
| §7 Provenance and events | Provenance (c), opt-in and off; audit record (a), best-effort in `import_jobs.db`; events (b), one ordinary event per entity |
| §8 Size limits | v1 document caps; no separate overwrite cap (a); thresholds below |
| §9 API surface | REST (b), separate `/import/merge/preview` and `/import/merge` routes; MCP (b), preview tool only |
| §10 Security | Narrowed callers (a), refused; the preview needs the commit's authorization |

### Slice 1 as implemented

Slice 1 (the planner, `POST /import/merge/preview` and the
`preview_graph_merge` MCP tool) ships in `backend/service/graph_merge_plan.py`.
Choices it made inside the decisions above:

- `policy.by_type` keys may name a node type or a relationship type; an edge
  collision follows its relationship type's entry, else `policy.default`. A key
  that names neither is refused, as are unknown request or policy fields.
- An `overwrite` whose document entity equals the live one in every
  caller-writable field is listed under `skips` with reason `unchanged`, so a
  re-import of an unchanged export writes and announces nothing.
- An edge whose endpoint resolves to a live system-type node is excluded with
  the system nodes, not only an edge whose endpoint is a document system node.
- `attachments.checked` is `false` when no attachment store is configured,
  rather than reporting no flags as if the check had run; an attachment index
  that cannot be read flags its node rather than failing the preview.
- Edges the commit would write onto a node a `skip` collision kept are listed
  in `edges_to_skipped_nodes`, as well as under `adds`, `overwrites` or
  `renames` (§2 (a): "every such edge listed"). A `rename` whose live edge already has the same source, target
  and type is a duplicate-triple skip, since the renamed copy would repeat it.
- The REST route plans in a worker thread, as planning at the document caps
  takes tens of seconds. The MCP tool plans on the server's event loop, so it
  takes documents of at most 10,000 nodes and 20,000 edges (§9: "for small
  documents an assistant assembles").
- A `SavedView` overwrite's metadata values pass through the same sanitizer
  every other read applies before they are returned.
- The digest covers every document entry as sent, valid or not, as a sorted
  multiset, so any edit to the document — a skipped entity included — makes a
  preview stale, and reordering it does not. Per-entity outcomes, conflicts
  (by entity) and live reads enter it as §3 (c) describes; a plan with any
  rejected entry, which no commit can apply, digests only the request, the
  document and `generation`.
- A live edge the same plan retypes under `overwrite` is not what a document
  edge duplicates. The planner reads without the storage lock; a live write
  that changes an adjacency map mid-read, or changes or deletes an entity the
  plan read twice — by id, or once in a listing and once by id — restarts the
  plan like a replace.
- One preview runs at a time per process; another is refused with 503
  `import_preview_busy` and can be retried, since a preview changes nothing.
- A `SavedView`/`VisualizationView` document node whose annotations the write
  path refuses (`saved_view_annotation_error`) is listed under `rejected`.
- Besides the commit's `GRAPH_ACTION_MUTATE`, the preview also asks the hook
  for `GRAPH_ACTION_READ` on the whole graph, since it returns live values; a
  hook that grants mutation without unnarrowed reads is refused (§10).
- For slice 2: the digest records every live entity the plan read and its
  revision, not absences a listing found — the incident edges walked for a
  subtype re-judge, or a duplicate-triple lookup that matched nothing. A write
  to a *different* entity between two reads is an ordinary concurrent write,
  not a restart. Live nodes are updated in place, so one read can also see a
  concurrent update's fields under the revision it had before, or the fields
  of an update that is then undone. The commit's checks (§3) must cover all
  of these.
- Each node an edge names is resolved once per plan attempt and keeps only
  the subtypes some relationship rule names: `subtypes` has no size limit, so
  per-edge work must not grow with its length.

### Slice 1 measurements and thresholds (§8)

Measured with `scripts/measure_merge_preview.py`, which builds the
`scripts/measure_capacity.py` fixture: a 10,000-node, 17,998-edge document
(half colliding with live ids under `overwrite`, half new, 1,000 duplicate
triples) against a 100,000-node, 169,997-edge graph on the file backend. A
local development container — 4 vCPU at 2.80 GHz, 15 GB RAM, Python 3.13 — not
a tuned benchmark machine; three runs.

| Figure | Measured | Pass/fail threshold |
|---|---|---|
| Preview latency, median of 3 after one warm-up | 1.90–1.98 s, of which ~0.8 s is one full garbage collection over the resident graph that the preview's allocations trigger (1.1–1.3 s without it) | ≤ 2.5 s |
| Peak memory beyond the resident graph (`tracemalloc` peak during one preview) | 74.9 MB | ≤ 160 MB |

Whether a full collection lands inside a preview depends on how many objects
the process allocated before it, so the latency threshold leaves room for one.
Later slices re-run the script and fail a threshold they exceed. The two other
figures §8 names, how long the commit holds `_lock` and the event fan-out at
10,000 entities, need the commit to exist; slice 2 sets their thresholds from
its first measurement (below).

### Slice 2 as implemented

Slice 2 (the commit, `POST /import/merge`) ships in
`GraphStorage.apply_import_merge` (`backend/core/storage.py`, write shapes in
`backend/core/storage_merge.py`) and `commit_merge_import`
(`backend/service/import_service.py`). Choices it made inside the decisions
above:

- **How the §3 checks are verified.** The commit plans twice: once before
  the backup, to answer `import_merge_blocked` or `import_plan_stale` with a
  plan before anything is written, and once more inside the locked unit,
  through a `prepare` callback `apply_import_merge` runs under `_lock` before
  it changes anything. The locked plan must have the bound digest. Since no
  writer can move the in-memory model while the lock is held, that one
  comparison verifies every read the digest records — each expectation, each
  check and `generation` — and also what the digest does not record: the
  absences a listing found (a new incident edge on a re-judged node, a new
  duplicate triple) and fields a lock-free read saw mid-update, which the
  slice-1 notes above leave to the commit. The storage method still checks
  each write's expectation against memory and passes it to `_commit`. The
  alternative, checking the recorded reads plus re-listing each listing under
  the lock, was rejected: it needs the planner to export its listings and
  still cannot catch a field read mid-update. The cost is that `_lock` is held
  for a plan of the document (linear in the document, not the graph; the
  report's attachment and vector counts are skipped under the lock).
- **Without a digest** the commit binds to the plan it computed before the
  backup, so a write landing between that plan and the lock still fails it.
- **A digest is not single-use.** A committed create or overwrite moves what
  the digest covers, so the same digest is stale afterwards; but a commit
  whose every write is a rename (fresh ids, live entities untouched) leaves
  the digest current, and resending it creates the renamed copies again.
  §3 rules out only the digest-less rename retry; making a digest
  single-use is the caller-chosen idempotency key §3 and §7 (c) defer, so
  it is left to that decision and documented for callers.
- A stale plan found under the lock is answered with a plan recomputed after
  the lock is released, as for one found before it.
- **Vectors.** The commit drops the vectors of created and renamed nodes (a
  stray vector under a reused id included) and of overwrites whose embedding
  text changed, in memory inside the unit; an undone merge puts them back
  (`_undo`, and the commit itself for a stray vector, which `_undo` cannot
  restore under an id no node held). On the
  file backend the drop moves the vector store's revision, so the batch's
  write carries the embedding sidecar and writes it before the journal
  record, as every write that moves a vector does; a failed sidecar write is
  retried by the next write. The IMPORT job is enqueued only when the plan
  embeds at least one node, stamped with the generation read inside the
  commit's lock, so a replace landing just after it supersedes the job.
- **Failure after hand-over** on the file backend: a batch whose journal
  write and whole-graph re-save both failed raises `write_outcome_unknown`
  with memory keeping the merge, no events, and the next write re-saving the
  whole graph. Since that merge can still become durable unannounced (and
  without an embeddings job), it gets an audit record too, with
  `merge_status: "outcome_unknown"` and no revisions.
- **A plan that writes nothing** (every entry skipped, unchanged or
  excluded) is answered after the first plan with zero counts: no backup, no
  lock, no record. There is no write for the §3 checks to protect, and a
  digest-less `skip` commit repeated in a loop would otherwise write a
  whole-graph backup each time.
- **Audit record.** A terminal IMPORT job whose id is the `import_id`. Its
  job state is `SUCCEEDED` (the record was written); `result.merge_status`
  says whether the merge was confirmed. Its payload carries `phase: "merge"`
  and the request's attribution (actor and scope) as the request context
  reports it, which in the open core is header-derived and so only as
  trustworthy as what sets those headers; the merge's
  embeddings job carries `mode: "merge"` and the `import_id`. `GET /import`
  rows gain `mode` (`replace` for every row written before this slice).
- **Events** fire in write order (node creates, node overwrites, edge
  creates, edge overwrites) under the lock, as every direct write's do.
- One commit runs at a time per process (503 `import_merge_busy`), as one
  preview does; the two slots are separate.
- `POST /import/merge` asks for the preview's authorization, `GRAPH_ACTION_READ`
  included, since its response carries the plan's live values.
- **Not in this slice:** PostgreSQL mode is refused with 501
  `import_merge_not_supported` before anything is planned or backed up
  (slice 3), the worker's per-node revision guard (§6, slice 3), and an MCP
  commit tool (§9 (b)).

### Slice 2 measurements and thresholds (§8)

Measured with `scripts/measure_merge_commit.py`: the slice-1 document (10,000
nodes, half overwriting live nodes; 17,998 edges, of which 1,000 duplicate
triples are skipped) committed under `overwrite` into the same 100,000-node,
169,997-edge file-backend graph, with two `EventSubscription`s that between
them match every event. The commit writes 24,532 entities (5,000 nodes
created, 2,534 overwritten, 16,998 edges created). Same container as slice 1;
three runs, each against a freshly seeded graph.

| Figure | Measured | Pass/fail threshold |
|---|---|---|
| `_lock` held by the commit (`apply_import_merge`) | 12.1–13.8 s, median 12.8 s | ≤ 16 s |
| Whole commit request (two plans, backup, locked unit, job and audit record) | 23.9–27.3 s, median 25.1 s | ≤ 32 s |
| Whole-graph backup, before the lock | 8.9–12.4 s | reported, not gated: proportional to the graph (§4 (a)) |
| Events fired | 24,532 — one per written entity | exactly one per written entity |
| Webhook deliveries enqueued (§7 (b) fan-out) | 24,532 — one per event per matching subscription | one per event per matching subscription |

Inside the lock, per-event work dominates: a profile of a 5,000-node commit
puts most of the locked time in `_emit_event` — the dispatcher's copy of each
event and one mutation-history append, which opens the history file, per
event — and a smaller share, about 1.5 s, in the plan recomputed under the
lock. That is
the existing per-event cost every direct write pays, now paid once per
merged entity while live writes queue behind the lock (reads take no lock).
The lock hold scales with the document (the plan recomputed under it) and
the merged entities (the writes and their events), not with the graph, as §8
asks.
Whether a large merge should emit a capped or batched form instead (§7) is
an event-contract change and is left to a decision on these figures; the
delivery-queue depth at return was 0 only because the measurement's
loopback target is refused at once, so the enqueued count is the depth a
slower target sees.

## Context

### What exists today

`POST /import` (ADR 0006) and `POST /import/archive` (ADR 0007) both **replace
the whole active graph**. Their pipeline, in `backend/service/import_service.py`
(`_validate_backup_replace` for steps 1–4, then `_enqueue_and_start_regeneration`
for step 5 — or, for a compatible archive whose `embeddings.bin` covers every
node, a direct vector restore with no job),
is:

1. **Authorize** with `GRAPH_ACTION_MUTATE`, and refuse a caller whose graph
   access is narrowed (`import_requires_full_graph_access`).
2. **Validate** the document as a *closed* graph in
   `backend/service/graph_import.py` (`validate_import_document`): node and
   relationship types against the running schema config, duplicate ids,
   pydantic construction of every `Node`/`Edge`, every edge endpoint present
   *in the same document*, and relationship applicability via
   `config_loader.relationship_type_allows_node_types`. Every problem is
   collected; nothing touches `GraphStorage`. Size is capped at
   `MAX_IMPORT_NODES = 200_000` and `MAX_IMPORT_EDGES = 400_000`; the archive
   upload is capped at 200 MB.
3. **Back up** the current graph to `<graph dir>/import-backups/` in the
   `GET /export` shape; refuse the import if the backup cannot be written.
4. **Replace** via `GraphStorage.replace_all_nodes_and_edges`: build new
   containers off to the side, publish them by pointer assignment under
   `_lock`, restamp every entity's `revision` (or zero it when the store
   assigns revisions), drop **all** vectors, bump and persist
   `graph_generation`, and persist with `save()`; on a save failure restore the
   previous references.
5. **Embeddings** run afterwards on a durable `ExecutionKind.IMPORT` job in
   `import_jobs.db`, drained by `backend/agents/execution/import_worker.py`.
   The worker embeds every node for which `vector_store.has_embedding` is
   false and commits through `commit_generation_embeddings`, which discards
   the result if a later replace has moved `generation` on. The import
   response never waits for this job.

The replace path emits **no graph events** (ADR 0006 lists live-session
notification of a replace as out of scope), adds no MCP tool (a whole-graph
body does not fit the tool-call model), and leaves the attachment store
untouched, so nodes that do not survive leave orphan attachment directories
(ADR 0008 §8).

### What exists that a merge can lean on

- **Entity revisions and expectations** (`SESSION_OVERLAY_CONTRACT.md` §4,
  slice S1, implemented). Every node and edge carries a server-owned
  `revision` from one graph-wide counter. `add_nodes(..., expect_absent=True)`,
  `add_edge(..., expect_absent=True)` and `update_node(..., expected_revision=…)`
  refuse a write whose expectation does not hold with
  `EntityRevisionConflict` (`entity_revision_conflict`). A backend that
  declares `revision_enforcement` re-checks the expectation in the statement
  that applies it (`RevisionEnforcingBackend.apply_batch`), and a backend that
  declares `transactions` applies a batch all-or-nothing.
- **A staged-merge design** (`SESSION_OVERLAY_CONTRACT.md` §10–§11, not yet
  implemented — slice S5). It already names the cases a merge meets — `clean`,
  `conflict`, `orphaned`, `id_taken`, `dangling` — and the resolutions
  `take_session`, `keep_main`, `as_new`, `manual`; it blocks the whole merge on
  an unresolved entry (`merge_blocked`) and fails it when an expectation no
  longer holds (`merge_raced`). An import merge is a different feature (the
  "layer" is an uploaded document, not a session), but reusing the vocabulary
  and the race model keeps the two from drifting into two dialects of the same
  idea.
- **The incremental write path is not all-or-nothing.** `add_nodes` documents
  that a failure part-way "returns a failure result while leaving what was
  added in place". Routing a merge through repeated `add_nodes`/`update_node`
  calls would therefore give partial-apply semantics by default; atomicity has
  to be designed in, not inherited.
- **Edges are unique by id only.** `add_edge` refuses an existing edge id but
  does not deduplicate `(source, target, type)` triples; the in-memory graph is
  a `networkx.MultiDiGraph`.
- **One graph per process** (ADR 0009). A merge, like a replace, targets the
  single graph the process holds; there is no "target graph" parameter to
  design.

### Why merge is not a small extension of replace

Replace never has to reason about the live graph: it validates a closed
document and swaps. Merge has to answer, per entity, *what happens when the
document and the live graph disagree*, keep those answers stable between the
moment a user reviews them and the moment they are applied, and touch only part
of the graph while live writes continue around it. Each of those is a decision
point below.

## Terms

- **Document** — the uploaded `graph.json`-shaped body (`nodes`, `edges`).
- **Live graph** — the graph the process holds when the plan is computed.
- **Plan** — the per-entity outcome of merging the document into the live
  graph, computed without writing (§3).
- **Collision** — a document entity whose id already exists in the live graph.

## Decision points

### 1. Decision point — ID-collision policy

A document node whose id exists in the live graph.

| Option | Effect | Trade-offs |
|---|---|---|
| **skip** | Keep the live node; drop the document's version. | Safe and idempotent (re-running an import is a no-op). Silently ignores updated content unless the plan surfaces it. |
| **overwrite** | The document's node replaces the live node, every caller-writable field (the session contract's `take_session` on `id_taken`); server-owned fields such as `revision` are never taken from the document, as on every write (`SESSION_OVERLAY_CONTRACT.md` §4.1). | The only way to "update from a newer export". Destructive: live edits since the export are lost (backed up, §4). |
| **rename** | Create the document's node under a fresh id; rewrite every *document* edge that names the old id to the new id (the session contract's `as_new`). | Never loses data, but duplicates entities, and a node that was meant to be the same entity now exists twice. Renamed ids must be reported (old → new) so a caller can follow up. |
| **fail** | Any collision blocks the whole import with the colliding ids listed. | Most conservative; makes "import into an empty-ish graph" the only easy case. |

**Rename and edges.** Only edges *in the document* are rewritten; a live edge
keeps the endpoint it has. The new id is minted the way ordinary creates mint
one (a fresh UUID), never derived from the old id, so a second import cannot
collide with it predictably.

**Granularity — options:**
(a) one policy per import;
(b) one default per import plus per-node-type overrides (`{"default": "skip",
"by_type": {"Actor": "overwrite"}}`);
(c) (b) plus per-id resolutions supplied after a preview, as the session merge
does.

**Type mismatch on the same id** (document says `Actor`, live says `Goal`):
(i) always `fail`, whatever the policy; (ii) allowed only under `rename`, which
creates a separate node and so never retypes anything; (iii) follow the policy,
retyping under `overwrite` — which needs a retype path that does not exist
today: `type` is a reserved field `update_node` never changes.

**Subtype changes under overwrite.** Relationship applicability also depends
on subtypes (`relationship_type_allows_node_types` takes both endpoints'
subtypes), and `subtypes` is caller-writable, so an overwrite can make a live
incident edge inapplicable even when the type matches. Options: (a) re-judge
every live incident edge of an overwritten node against its new subtypes, a
failure being a conflict; (b) accept it, as `update_node` does today for a
direct subtype change. Recommendation: **(a)** — the plan already lists the
node's changed fields, and a merge should not leave edges behind that the same
edge could not be created under.

**Edge collisions** (same edge id): the same options apply, with `rename`
meaning "new edge id" (the session contract's `as_new` is for nodes only; this
extends it to edges). An edge whose endpoints differ from the live edge with
that id is treated like a type mismatch — an edge's endpoints never change
(`SESSION_OVERLAY_CONTRACT.md` §6.3).

**Recommendation.** Default policy **`fail`** for nodes and edges, so nothing
is overwritten or duplicated without an explicit choice; granularity **(b)** in
the first implementation, with **(c)** left for a later slice once the preview
exists; type and endpoint mismatch **(ii)** — a conflict under every policy
except `rename`. Retyping a node silently invalidates edges whose applicability
was judged against the old type, which is too much to hide behind
`overwrite`, and needs a write path the core does not have.

### 2. Decision point — Edge handling

Every document edge falls into one of these cases, judged against **the live
graph plus the nodes this merge will create** (not the document alone, as
replace does, and not the live graph alone):

| Endpoints | Proposed outcome |
|---|---|
| both in the document (created or overwritten) | Normal case; applicability judged against the resulting node types. |
| one or both only in the live graph | Allowed — attaching new content to existing nodes is the point of merge. Applicability is judged against the **live** node's type and subtypes. |
| an endpoint the document names under a node that is **skipped** | See *Edges of skipped nodes* below. |
| an endpoint the document names under a node that is **renamed** | Rewritten to the new id (§1). |
| an endpoint in neither the document nor the live graph | **Rejected** (`dangling`). Never created as a placeholder node. Endpoints resolve by id only, as in replace — never by node name, the fallback `add_edge` has. |
| an endpoint that is a live node archived (`archived: true`) | Options: allow (archive is reversible) or conflict. Recommendation: allow, reported in the plan. |

**Edges of skipped nodes — options:** (a) attach the edge to the live node
that holds the id, re-judging applicability against the live node (a failure
is a conflict); (b) treat it as `dangling` unless the caller resolves it, which
is what `SESSION_OVERLAY_CONTRACT.md` §11.2 does for a dependant of a dropped
create ("Nothing attaches it silently"). Recommendation: **(a)** under an
explicit `skip` policy, with every such edge listed in the plan, because
choosing `skip` already says "the live node *is* this entity"; this is a
deliberate divergence from the
session contract, whose staged create was never asserted to be the same entity
as the one that later took its id. Under §1 (c), per-id resolutions, the
session behaviour (b) becomes available too.

**Edges of excluded system nodes.** When system types are excluded (§5), an
edge incident to an excluded document node is excluded with it and listed
under `skips`, never reported as `dangling` — otherwise importing another
instance's export (an `Agent` → `Skill` edge, say) would block under the
recommended defaults.

**Duplicate edges** — a document edge whose `(source, target, type)` equals a
live edge's but whose id differs. Storage does not prevent this today. Options:
(a) create it (today's semantics for direct writes); (b) skip it as a
duplicate, reported in the plan; (c) treat it as a conflict. Recommendation:
**(b)** — an export → edit → re-import cycle otherwise doubles every edge whose
id was regenerated, and a duplicate triple carries no information the existing
edge lacks. Two edges *in the document* with the same triple are left as they
are, as replace does.

**Live edges not in the document** are never deleted. Merge only adds or
changes; deletion stays a direct-write or replace operation. (A "sync" mode
that also deletes would be a third mode and is out of scope.)

### 3. Decision point — Preview (dry run) and plan binding

Merge must be previewable: unlike replace, its outcome depends on the live
graph, so a caller cannot know "what would change" from the document alone,
and an overwrite is destructive.

**Plan report** (returned by the preview, and again by the commit):

- `adds` — nodes and edges to create (counts, plus ids up to a cap; the
  digest covers their full content, so a document edited after the preview
  does not match);
- `overwrites` — per entity, the fields that change (`main` = live value,
  `incoming` = document value; `main` as in the session diff, which also has
  a `base` this feature has no equivalent for);
- `renames` — the old ids that will be renamed, and the edges rewritten with
  each. New ids are **not** in the plan: they are minted at commit and
  returned only in the commit response, so recomputing a plan stays
  deterministic (see the digest below). The same holds for the values a
  provenance stamp would write (§7 (c): import id, time) — minted at commit,
  never part of the plan;
- `skips` — collisions kept as live, and duplicate edges skipped;
- `conflicts` — entries that block the commit under the chosen policy (type
  mismatch, endpoint mismatch, `fail` collisions, applicability failures after
  attaching to a live node);
- `rejected` — validation errors (unknown types, dangling edges, bad fields),
  located as `ImportIssue` already does (`nodes[3]`, `edges[7]`);
- `embeddings` — how many merged nodes would be (re)embedded (§6), plus how
  many live nodes already lack a vector, since the job embeds those too;
- `events` — how many graph events the commit would emit (§7);
- `plan_digest` — see below.

Lists are capped (for example the first 1,000 ids per category) with exact
counts always present, so a preview of a large import stays a bounded
response.

**Binding the commit to the plan — options:**

- **(a) No binding.** Commit recomputes the plan and applies whatever it now
  says. Simplest; the user may approve one plan and get another.
- **(b) Server-stored plan.** Preview stores the plan under a `plan_id` with a
  TTL; commit names it. Needs plan storage that survives the request and, in
  PostgreSQL mode with several instances (ADR 0009), storage every instance can
  read.
- **(c) Stateless digest.** Preview returns `plan_digest`, a SHA-256 over the
  canonical request parameters that change writes (`policy`,
  `include_system_types`, `stamp_provenance`) and the full, uncapped
  canonical plan (not the capped report): every outcome that determines a
  write, the full content of every document entity the commit writes
  (renamed creates included), *the live revision of every entity it reads
  or expects absent, and the graph's `generation`* — sampled before the
  first entity is read and re-checked after the last, so a replace landing
  mid-plan restarts the plan rather than producing a mixed one.
  Informational counts that do not determine a write (the embeddings
  backlog, the event count) are reported but left out of the digest, since
  they change without anything the commit writes changing. Commit sends the
  document,
  the policy and the digest; the server recomputes the plan, and refuses with
  `import_plan_stale` (carrying the new plan) if the digest differs.

**Why `generation` is in the digest.** Revisions alone do not detect a replace
between preview and commit when the store assigns revisions: there
`replace_all_nodes_and_edges` sets every imported entity's revision to `0`
("never written under revisions"), so an untouched entity reads `0` before
and after a second replace, and an `expected_revision=0` would still hold
against content the user never previewed. Every replace bumps `generation`,
so the commit also checks that `generation` is unchanged, inside the same
locked unit as the writes. In a shared PostgreSQL store several instances
write, so the implementation must check the generation persisted in the store
(`graph_generation` inside the stored graph metadata document), not only the
instance's in-memory counter;
how to read it race-free there is part of slice 2.

**Closing the window between recompute and apply.** Recomputing the plan does
not by itself close it. It is closed the way `SESSION_OVERLAY_CONTRACT.md`
§11.3 closes it for a staged merge, with both of its parts:

- **expectations** on every write the commit issues — `expected_revision` for
  an overwrite, `expect_absent` for a create — so a live write that lands in
  between fails the commit as a whole rather than being overwritten;
- **checks** on every live entity whose presence or revision decided an
  outcome without being written: every live node a document edge attaches to
  (applicability was judged against its type and subtypes), every live entity
  a `skip` collision kept, every live edge a duplicate-triple skip deferred to,
  and every live incident edge re-judged under §1's subtype rule — each must
  still exist at the revision the plan read. Otherwise a delete inside the
  window would leave a skipped document entity neither kept nor created. An
  expectation alone does not cover these, because the merge does not write
  those entities. In PostgreSQL mode today's endpoint lock (`FOR KEY SHARE`
  in `postgres_backend.py`) is not enough on either count: it holds a row
  that is present but does not fail on one that is absent, and it conflicts
  with a delete but not with an ordinary update, so a concurrent subtype
  change could commit between the check and the merge. The store must
  verify these checks inside the unit, holding each checked row against
  updates too: a locking read (`FOR SHARE`) inside the unit that compares
  the revision. The existence checks are the machinery the session contract
  assigns to slice S2; the revision checks on entities the merge does not
  write go **beyond** S2 and are new work for this feature. (The alternative
  is to accept the subtype race on attach targets as today's direct-write
  semantics — `add_edge` does not hold its endpoints against updates
  either — and check existence only; the reviewer may prefer that.)

Revisions make the check cheap: what the commit verifies inside the unit is
revisions, existence and `generation`, not entity content (the digest itself
covers the plan, field values included).

**Recommendation:** **(c)**, with expectations and checks on every commit. It
needs no new plan storage, works across instances sharing a PostgreSQL store
once the generation check above is in place, and builds on the revision
machinery S1 already ships. A commit without a digest is allowed only when the
policy is `fail` or `skip` everywhere **and** `include_system_types` is false
(§5): neither changes a live entity, no configuration-bearing node can be
created unseen, and a blind retry is harmless — a retry after a lost response
(`write_outcome_unknown`) finds the first commit's entities and fails or skips
them. `rename` is excluded: such a retry would rename, and so duplicate, the
whole document. A caller-chosen idempotency key, like the session merge's
`merge_id` (§11.4), would make rename retries safe too, but needs the durable
audit record of §7 (c); it is listed as an option there, not recommended for
the first slices.

### 4. Decision point — Atomicity, rollback and concurrency

**Atomicity — options:** (a) all-or-nothing; (b) best-effort, applying what
validates and reporting the rest.

Recommendation: **(a)**. A partially merged import is hard to reason about and
harder to undo, and the plan already lets a caller drop problem entities
before committing (fix the document, or change the policy).

**Mechanism.** The commit cannot be `replace_all_nodes_and_edges`, which
restamps every entity and drops every vector, and it should not be a loop over
`add_nodes`/`update_node`, which keep what landed on failure. The proposed
shape is a new, narrow `GraphStorage` method (working name
`apply_import_merge`) that, under `_lock`:

1. checks `generation`, every expectation and every check (§3) against the
   in-memory model;
2. captures undo state for every entity it will touch (the existing
   `_capture`/`_undo` pair);
3. applies creates and overwrites to the in-memory model; fresh revisions are
   stamped at persist time, as `_commit` stamps every direct write's (from
   the counter, or from the store's reply under PostgreSQL) — §4.2 of the
   session contract: a merge write is an ordinary write, not a whole-graph
   replace, so untouched entities keep their revisions;
4. persists **one** batch through `apply_batch`, which both shipped backends
   declare atomic (`incremental_writes` and `transactions`; on the file
   backend it is one journal record). PostgreSQL also declares
   `revision_enforcement`, so it re-checks expectations in the store; the §3
   checks must be verified there too;
5. on a refused expectation or check, or any failure before the batch is
   handed to the store, restores memory from the undo state and re-raises, so
   the live graph is the pre-merge graph. A shared-store failure after the
   batch was handed over may have landed with only the acknowledgement lost;
   it is resolved the way every direct write's is (`_commit` /
   `_resolve_failed_write`, `SESSION_OVERLAY_CONTRACT.md` §4.4): landed,
   `write_failed` (undo), or `write_outcome_unknown` (reload). The merge writes
   only upserts, so every touched entity carries the write token as evidence.
   On the file backend a write that fails after hand-over is not undone
   either: the existing landing path keeps memory as it is and re-saves the
   whole graph (`_await_landing`), and the merge inherits that; if that
   re-save itself fails, memory keeps the merge, no events fire, and the
   response is `write_outcome_unknown`.

**Order.** Recompute the plan and check the digest, then write the pre-merge
backup (below), then run the locked unit — the backup outside the lock, as v1
writes its backup before `replace_all_nodes_and_edges`. Because the plan, and
so the expectations, are taken before the backup, an overwritten entity that
changes after the backup was written fails its expectation; the backup's copy
of every overwritten entity is therefore the version the merge replaces.

It does **not** bump `generation`. `generation` means "the whole graph was
replaced"; bumping it on a merge would make an in-flight replace job report
itself `superseded` for a graph that was only extended.

**Backup — options:** (a) reuse the v1 whole-graph backup unchanged before
every merge; (b) a scoped pre-image file holding only the entities the merge
overwrites (plus the ids it creates, to delete on undo); (c) no file backup,
in-memory undo only.

Recommendation: **(a)** in the first slice — it is already written, already
re-importable via `POST /import`, and its "refuse if the backup fails" rule
carries over. Its cost is proportional to the whole graph, not to the merge;
(b) is the follow-up if measurement shows that cost matters for small merges
into large graphs. Note that restoring an (a) backup with `POST /import` is a
*replace*: it also undoes every live write made after the merge, so the
response should say so.

**Concurrency with live writes.** Planning and validation run without `_lock`,
reading entities one at a time (there is no unlocked multi-entity snapshot);
consistency is enforced at commit by the digest, expectations and checks of
§3. The commit holds
`_lock` for steps 1–5, so on the file backend live writes queue behind it for
its duration (as they already do behind a replace). In PostgreSQL mode other
instances keep writing; a write to an entity the commit expects or checks
that lands before they are verified fails the commit with
`import_plan_stale`; any other write, and any write landing after, is an
ordinary concurrent write.

### 5. Decision point — Schema validation and system types

**Validation** reuses `validate_import_document`'s per-entity checks against
the running schema config, with two changes: edge endpoints resolve against
the live graph plus the document (§2), and duplicate ids *within* the document
stay an error, as in v1.

**Unknown node or relationship types — options:** (a) reject the whole import
(v1's behaviour); (b) skip the offending entities, listed under `rejected`,
and import the rest. Recommendation: **(a)**, consistent with v1 and with
all-or-nothing; the plan names every offender so the document can be fixed.

**System node types** (`SYSTEM_NODE_TYPES` in `backend/config/config_loader.py`:
`SavedView`, `EventSubscription`, `Agent`, `Skill` and others). These are valid
types, so v1 replace imports them like any other node. In a merge they matter
more: `EventSubscription` nodes define webhook targets, and an overwrite could
silently repoint an existing subscription. Options: (a) treat them like domain
types; (b) exclude them by default and require an explicit
`include_system_types: true`; (c) never merge them. Recommendation: **(b)**,
listing excluded entities in the plan.

### 6. Decision point — Embeddings

Which nodes need a vector after a merge:

- **created** nodes — always;
- **overwritten** nodes — only when a field that feeds the embedding text
  changed; otherwise the live vector stays valid;
- **renamed** nodes — created under a new id, so always;
- **skipped** nodes and untouched live nodes — never.

Mechanics: the commit drops the vectors of overwritten nodes whose embedded
text changed (so a stale vector is never served for new text), then enqueues
the existing `ExecutionKind.IMPORT` job. The worker needs no node list: its
`has_embedding` filter already lands it on exactly the nodes without a vector,
the property ADR 0007 §6a relies on for partial archives. That set also
includes any live node that already lacked a vector before the merge, so the
job may do more work than the merge alone implies; the plan reports both
counts (§3).

One gap must be closed in the implementation: the worker checks only
`generation` before committing. A node overwritten by a direct write *while*
the job is encoding would receive a vector computed from its older text.
Options: (a) accept it (the same gap exists for any slow embedding today); (b)
have the job record each node's embedding text (or its revision) when it reads
it, and at commit compare per node: if the node now has a vector, or its
embedding text changed, drop the computed vector; otherwise commit it. A
revision alone is not enough to skip on — `update_node` re-embeds only when
`name`, `description`, `summary`, `tags` or `aliases` change, so a metadata-
or subtype-only update moves the revision without producing a vector, and a
node skipped on revision alone would be left with none (the worker makes one
pass over nodes lacking a vector). A node whose text changed and still has no
vector is re-encoded in the same job or left for a re-enqueued one.
Recommendation: **(b)**, a per-node comparison under the lock the commit
already takes.

A second gap concerns PostgreSQL mode. The worker commits through
`commit_generation_embeddings`, which persists with `save()`, and under
PostgreSQL `save()` is `save_graph_data`: it rewrites every row from this
instance's memory in one transaction (vectors travel inline in node rows,
ADR 0009). For a replace that is harmless — the graph was just replaced
wholesale. For a merge it is not: §4 is designed around other instances
continuing to write, and a whole-graph save from one instance's memory would
overwrite concurrent writes it has not yet received, and make every other
instance reload. The core cannot tell how many instances share a store, so
this applies to any backend declaring `revision_enforcement` — all of
PostgreSQL mode. Options:

- **(a)** persist only the embedded nodes as entity upserts through
  `apply_batch`, each carrying the expectation from the first gap's option
  (b). A batch is all-or-nothing, so one node edited elsewhere would fail the
  whole commit; the rule is to re-read on conflict, drop the moved nodes and
  re-issue (or to commit in per-node batches). Its costs: every upsert takes
  a new revision, so each embedded node's revision moves — making other
  callers' expectations and outstanding merge previews stale — and every
  other instance receives each upsert as an `external-change` `node.update`
  event, volume the plan's `events` count does not include.
- **(b)** refuse merge in PostgreSQL mode until (a) exists.

Recommendation: **(a)**, in the third slice, with slice 2 refusing a merge in
PostgreSQL mode until then. (The same whole-graph save already follows a
replace's embedding job in PostgreSQL mode; that is a property of today's
replace path, outside this ADR.)

Merge via `POST /import/archive` (restoring vectors from `embeddings.bin` for
created nodes) is out of scope for the first slices; plain `graph.json` only.

### 7. Decision point — Provenance and events

**Provenance on nodes — options:**
(a) none — the import job record is the audit trail;
(b) stamp a metadata key on every created or overwritten node (for example
`metadata.import = {"import_id": …, "at": …}`);
(c) (b) as an opt-in flag.

Node metadata travels into `graph.json`, `GET /export` and federated copies
(ADR 0008 Context), so a default stamp would spread an internal job id to
every consumer and change content on every overwrite. Recommendation:
**(c)**, off by default.

**Audit record.** Every committed merge writes a record carrying the import
id, the plan digest, the policy, the counts, the backup path and per-entity
outcome (created / overwritten / renamed old → new, with resulting revisions)
— the analogue of the session contract's merge record (§11.5). It cannot live
only on the embeddings job: a merge that needs no embeddings creates no job,
and the job can fail to start (ADR 0006 §7, `not_started`). **Where — options:**
(a) a terminal `ExecutionKind.IMPORT` record in `import_jobs.db` written right
after the commit, separate from any embeddings job; (b) a JSON file next to
the backup in `import-backups/`; (c) a record written in the **same unit** as
the graph batch, in the graph store, as the session contract keeps its merge
record with the layer (§7, §11.6).

(a) and (b) are written after the graph commit, in a store separate from the
graph, so a crash between the two leaves a committed merge with no record;
they are best-effort. `import_jobs.db` is also per instance, so in PostgreSQL
mode with several instances `GET /import` lists only the local instance's
merges. (c) is crash-safe and shared but needs a new record kind in both
backends' stores; it is also what a caller-chosen `import_id` idempotency key
would need (a retried commit finds the record and returns it, as a retried
session merge does with its `merge_id`), which would let a `rename` commit
skip the digest safely (§3). Recommendation: **(a)** in the first slices, stated as
best-effort, with (c) adopted if the staged-session merge record (S2) lands
first and can be reused; retention is the same open question as for import
jobs today.

**Graph events — options:** (a) none, as replace; (b) one ordinary
`node.create`/`node.update`/`edge.create`/`edge.update` event per entity after
the commit lands, with `event_origin` and `event_correlation_id` taken from the
caller's `x-event-origin` / `x-correlation-id` headers when given — as
`POST /import` already forwards both — falling back to a fixed
`graph-import-merge` and the import id; (c) a single summary event — a new event type, which
changes the event contract in `docs/EVENT_SUBSCRIPTIONS.md`. Keeping the
caller's origin matters: an agent that sets `agent:<id>` and lists it in its
own `ignore_origins` (the loop-prevention pattern in
`docs/EVENT_SUBSCRIPTIONS.md`) would otherwise receive an event for every
entity its own merge wrote.

Merge changes part of a graph other clients are looking at, so (a) leaves
sessions, webhooks and agents blind to it. (b) uses the existing
event types and envelope, so no contract changes (`edge.update` exists in
`backend/core/events/models.py` but is missing from the event table in
`docs/EVENT_SUBSCRIPTIONS.md`; the slice adds it there), and lets a subscription opt
out with its existing `ignore_origins` filter; its cost is volume — a merge at
the document caps fires hundreds of thousands of events to every matching
subscription and into mutation history (file backend only, ADR 0009), which
is an amplification risk for webhook targets and the delivery worker.
Recommendation: **(b)**, with the event count in the plan so a large merge's
fan-out is visible before commit; whether a large merge should instead emit a
capped or batched form is left to measurement in slice 2 (§8).

### 8. Decision point — Size limits and performance

Limits: reuse `MAX_IMPORT_NODES`/`MAX_IMPORT_EDGES` for the document. Options
for a separate, lower cap on *overwrites* per commit: (a) none; (b) a
configurable cap that forces a large overwrite to be split. Recommendation:
**(a)** initially — the preview, the digest and the backup already guard a
large overwrite; revisit with measurements.

What to **measure** in the implementation slices (none of these are measured
facts). Slice 1 sets numeric pass/fail thresholds for each from a first
measurement, using the fixtures `docs/CAPACITY.md` already uses, and later
slices benchmark against them:

- preview latency and peak memory beyond the resident graph, for a
  10,000-node document against a 100,000-node graph;
- how long the commit holds `_lock`, which should scale with the merged
  entities, not the live graph (the whole-graph backup of §4 (a) runs before
  the lock and is measured separately);
- request latency and delivery-queue depth for the §7 (b) event fan-out at
  10,000 entities; and, in slice 3 once PostgreSQL merges are allowed, with
  several instances, the cost on the
  others: each re-emits a change it receives as an `external-change` event
  (so the plan's `events` count is per instance, and an agent's own
  `ignore_origins` entry does not stop that echo), and a batch too large to
  announce reaches them as an unknown change, which makes each reload
  (`docs/EVENT_SUBSCRIPTIONS.md`, "Limitations").

### 9. Decision point — API surface

Backwards compatible: `POST /import` and `POST /import/archive` keep REPLACE
when no mode is given, and their responses are unchanged.

**REST — options:**
(a) a `mode` field on the existing route — `POST /import?mode=merge`;
(b) separate routes — `POST /import/merge/preview` and `POST /import/merge`.

Sketch for (b):

```http
POST /import/merge/preview
{ "document": { "nodes": [...], "edges": [...] },
  "policy": { "default": "fail", "by_type": { "Actor": "skip" } },
  "include_system_types": false, "stamp_provenance": false }
→ 200 { "plan": {...}, "plan_digest": "sha256:…" }

POST /import/merge
{ "document": {...}, "policy": {...}, "plan_digest": "sha256:…", ... }
→ 200 { "success": true, "plan": {...}, "import_id": "…", "job_id": "…",
        "backup_path": "…", "embeddings_status": "queued" }
→ 409 { "error_code": "import_plan_stale", "plan": {...}, "plan_digest": "…" }
→ 422 { "error_code": "import_merge_blocked", "plan": {...} }
→ 502 { "error_code": "write_failed", ... }            # §4 step 5
→ 503 { "error_code": "write_outcome_unknown", ... }   # §4 step 5
```

`GET /import/{job_id}` is reused for the embeddings job. If the audit record
of §7 (a) is chosen, `GET /import` also lists merge records, so its rows gain
a `mode` discriminator (`replace` / `merge`) and merge rows carry no
embeddings fields of their own.

Recommendation: **(b)** — merge takes parameters replace does not have, and
returns a different response shape; overloading `/import` with a query flag
would make the destructive default (replace) one typo away from the intended
call.

**MCP — options:** (a) none, as ADR 0006/0007; (b) a preview-only tool; (c)
preview and commit tools taking a document inline. Recommendation: **(b)**,
`preview_graph_merge`, for small documents an assistant assembles itself — it
writes nothing. Commit stays REST-only for now, for the reason ADR 0006 gave
(a whole-graph body does not fit the tool-call model) and because a
destructive overwrite reached through an assistant needs its own review of
that path.

### 10. Decision point — Security

No security control changes with this ADR, which ships no code. It designs a
future destructive operation — overwrite of live entities — and these are the
controls proposed for it:

- **Authorization.** `GRAPH_ACTION_MUTATE`, through the same
  `use_request_authorization` / `evaluate_graph_access` path as replace.
  **Narrowed callers — options:** (a) refuse, as replace does; (b) allow a
  merge whose every touched entity (created, overwritten and every live
  endpoint it attaches an edge to) is inside the caller's scope. A merge,
  unlike a replace, *can* honour "leave what I cannot see alone", but (b)
  needs the narrowing to be evaluated per entity in the plan and again at
  commit. Recommendation: **(a)** in the first slices; (b) as a later slice.
- **Overwrite is destructive.** Overwrite only after a preview (§3 digest
  required whenever the policy can overwrite); pre-merge backup required (§4);
  the plan names every overwritten field.
- **Payload limits.** The document caps of v1; the preview's list caps (§3);
  no request may expand to unbounded work (rename and duplicate detection are
  linear in the document with hash lookups into the live graph).
- **Configuration-bearing nodes.** System types excluded by default (§5), so a
  document cannot silently create or repoint a webhook (`EventSubscription`)
  or an `Agent`.
- **Attachments.** Attachments are keyed by node id (ADR 0008). An overwrite
  keeps the live node's attachments, which then sit under content the uploader
  did not attach them to; a rename creates a node with none; and a create
  whose id has an orphan attachment directory (ADR 0008 §8 keeps those after a
  replace) would inherit files uploaded for unrelated content. The plan should
  flag overwritten nodes with attachments and creates whose id has an orphan
  directory, because ADR 0010 lets agents read attachment text by node.
- **Ids.** Document ids are opaque strings, as in v1, and go through the same
  `Node`/`Edge` construction as v1 and ordinary creates; the attachment store
  never uses a node id as a path segment as-is (ADR 0008). Rename never
  derives a new id from the old one (§1).
- **The preview reads the live graph.** It returns live field values
  (`overwrites.main`) and reveals which ids exist, so it is a read of the
  graph and needs the same authorization as the commit — `GRAPH_ACTION_MUTATE`
  and full graph access in the first slices, including the MCP preview tool.
  Under narrowed callers (option (b) above) it must not reveal entities
  outside the caller's scope.
- **System types, when included.** `include_system_types: true` requires a
  digest (§3). Today system nodes get only generic schema validation on every
  create path; webhook safety is enforced when an event is delivered (the
  delivery-time address checks in `backend/core/events/delivery.py`), not
  when an `EventSubscription` is written. Delivery-time checks still apply to
  merged subscriptions, but a merge can repoint many at once, so whether to
  add create-time validation for system types is part of this decision point
  — new work, not reuse.
- **Server-owned fields.** A document cannot supply `revision` (§1) or event
  attribution: the event origin comes from the request's `x-event-origin`
  header, as `POST /import` already reads it, or the server default (§7).
  Ordinary REST writes take an `event_origin` field in their request body; a
  merge must not read one from the document.
- **Backups accumulate.** Every merge writes a whole-graph backup (§4 (a));
  repeated small merges grow `import-backups/` without bound, as repeated
  replaces already do. Retention is a follow-up shared with replace.

## Proposed implementation slicing

After acceptance, in dependency order:

1. **Plan engine and preview (read-only).** A pure planner beside
   `validate_import_document` that takes the document, the policy and a read
   view of the live graph, and returns the plan and digest (§1, §2, §3, §5);
   `POST /import/merge/preview`; the MCP preview tool if §9 (b) is chosen.
   `stamp_provenance: true` is rejected until stamping ships.
   Writes nothing, so it can ship and be used for dry runs on its own.
2. **Atomic commit.** `GraphStorage.apply_import_merge` with expectations,
   undo and one-batch persistence on both backends (§4); the backup reuse;
   `POST /import/merge` with digest checking and `import_plan_stale`; the audit
   record; per-entity events (§7); the selective vector drop for overwritten
   nodes as part of the commit and enqueuing the `ExecutionKind.IMPORT` job
   for created and changed nodes (§6) — so an overwritten node does not serve
   a stale vector, as `update_node` normally does not (it re-embeds
   synchronously, keeping the old vector only when embedding fails). On the
   file backend vectors persist through the embedding sidecar, separately
   from the journal batch, so the drop is atomic in memory and the slice must
   say how it is made durable. Contract tests in the in-memory, file and PostgreSQL backends.
   Until slice 3 lands, slice 2 refuses a merge in PostgreSQL mode
   (§6, second gap).
3. **Embedding commit for merges.** The per-node revision guard in the worker,
   and the entity-upsert embedding commit for PostgreSQL mode (§6); lifts
   slice 2's PostgreSQL refusal.
4. **Optional, later:** per-id resolutions after preview (§1 (c)), merge for
   narrowed callers (§10 (b)), merge from an archive (§6), opt-in provenance
   stamping (§7 (c)) if not already folded into slice 2.

## Consequences

If accepted as recommended:

- Two import modes exist side by side; REPLACE is unchanged and remains the
  default of the existing routes.
- The core gains its first write path that applies a caller-supplied batch
  all-or-nothing on top of the live graph with per-entity expectations. The
  staged-session merge (session contract S5) solves the same problem for a
  session layer; whichever lands second should reuse the first's commit
  method rather than add a third.
- Large merges produce event volume proportional to their size.
- `docs/DATA_MANAGEMENT.md`, `backend/DEVELOPMENT.md` and `docs/USER_GUIDE.md`
  are updated by the slices that ship the routes, not by this ADR.

## What this ADR does not decide

- A "sync" mode that also deletes live entities absent from the document.
- Merging attachments (they are not part of `graph.json`; ADR 0008 §10).
