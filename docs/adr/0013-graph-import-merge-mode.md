# ADR 0013 — Graph import: MERGE mode beside REPLACE

- **Status:** Proposed
- **Date:** 2026-10-15
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

This ADR designs a second import mode. It implements nothing. Each section
headed **Decision point** lists options, trade-offs and a recommendation; the
recommendations are proposals for the reviewer to accept or change, not
decisions already taken. When the ADR is accepted, the chosen option for each
point is recorded in a short "Decisions on acceptance" table at the top, the way
ADR 0009 records its sub-questions.

## Context

### What exists today

`POST /import` (ADR 0006) and `POST /import/archive` (ADR 0007) both **replace
the whole active graph**. Their pipeline, in `backend/service/import_service.py`
(`_validate_backup_replace`), is:

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
| **overwrite** | The document's node replaces the live node, every field (the session contract's `take_session` on `id_taken`). | The only way to "update from a newer export". Destructive: live edits since the export are lost (backed up, §4). |
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
retyping under `overwrite`.

**Edge collisions** (same edge id): the same options apply, with `rename`
meaning "new edge id". An edge whose endpoints differ from the live edge with
that id is treated like a type mismatch — an edge's endpoints never change
(`SESSION_OVERLAY_CONTRACT.md` §6.3).

**Recommendation.** Default policy **`fail`** for nodes and edges, so nothing
is overwritten or duplicated without an explicit choice; granularity **(b)** in
the first implementation, with **(c)** left for a later slice once the preview
exists; type and endpoint mismatch **(ii)** — a conflict under every policy
except `rename`. Retyping a node silently invalidates edges whose applicability
was judged against the old type, which is too much to hide behind
`overwrite`.

### 2. Decision point — Edge handling

Every document edge falls into one of these cases, judged against **the live
graph plus the nodes this merge will create** (not the document alone, as
replace does, and not the live graph alone):

| Endpoints | Proposed outcome |
|---|---|
| both in the document (created or overwritten) | Normal case; applicability judged against the resulting node types. |
| one or both only in the live graph | Allowed — attaching new content to existing nodes is the point of merge. Applicability is judged against the **live** node's type and subtypes. |
| an endpoint the document names under a node that is **skipped** | The edge attaches to the live node that holds the id (the session contract's `take_session` for a dependant of a dropped create). Applicability is re-judged against the live node; if it fails, the edge is a conflict. |
| an endpoint the document names under a node that is **renamed** | Rewritten to the new id (§1). |
| an endpoint in neither the document nor the live graph | **Rejected** (`dangling`). Never created as a placeholder node. |
| an endpoint that is a live node archived (`archived: true`) | Options: allow (archive is reversible) or conflict. Recommendation: allow, reported in the plan. |

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

Merge must be previewable: the node description that motivated this ADR asks
for "what would change" before commit, and an overwrite is destructive.

**Plan report** (returned by the preview, and again by the commit):

- `adds` — nodes and edges to create (counts, plus ids up to a cap);
- `overwrites` — per entity, the fields that change (`base` = live value,
  `incoming` = document value), in the shape of the session diff's `fields`;
- `renames` — old id → new id, and the edges rewritten with it;
- `skips` — collisions kept as live, and duplicate edges skipped;
- `conflicts` — entries that block the commit under the chosen policy (type
  mismatch, endpoint mismatch, `fail` collisions, applicability failures after
  attaching to a live node);
- `rejected` — validation errors (unknown types, dangling edges, bad fields),
  located as `ImportIssue` already does (`nodes[3]`, `edges[7] (id=…)`);
- `embeddings` — how many nodes would be (re)embedded (§6);
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
  canonical plan *including the live revision of every entity it reads or
  expects absent*. Commit sends the document, the policy and the digest; the
  server recomputes the plan, and refuses with `import_plan_stale` (carrying
  the new plan) if the digest differs.

Recomputing the plan does not by itself close the window between recompute and
apply. That window is closed the way `SESSION_OVERLAY_CONTRACT.md` §11.3 closes
it for a staged merge: every write the commit issues carries an expectation —
`expected_revision` for an overwrite, `expect_absent` for a create — so a live
write that lands in between fails the commit as a whole rather than being
overwritten. Revisions make this cheap: the digest is over revisions, not over
entity content.

**Recommendation:** **(c)**, with expectations on every write. It needs no new
storage, works unchanged across instances sharing a PostgreSQL store, and uses
the revision machinery S1 already ships. A commit without a digest is allowed
only when the policy cannot overwrite (`fail` or `skip` everywhere), because
then nothing the user did not see can be lost.

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

1. checks every expectation against the in-memory model;
2. captures undo state for every entity it will touch (the existing
   `_capture`/`_undo` pair);
3. applies creates and overwrites to the in-memory model, assigning fresh
   revisions as any write does (§4.2 of the session contract — a merge write is
   an ordinary write, not a whole-graph replace, so untouched entities keep
   their revisions);
4. persists **one** batch: `apply_batch` on a backend declaring
   `incremental_writes`/`transactions` (PostgreSQL), where a backend declaring
   `revision_enforcement` re-checks expectations in the store; the existing
   journal batch / `save()` path on the file backend;
5. on any failure, restores memory from the undo state and re-raises, so the
   live graph is the pre-merge graph.

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

**Concurrency with live writes.** Planning and validation run without `_lock`
and against a consistent read of the entities they inspect; the commit holds
`_lock` for steps 1–5, so on the file backend live writes queue behind it for
its duration (as they already do behind a replace). In PostgreSQL mode other
instances keep writing; their writes either land before the commit's
expectations are checked (and fail the commit with `import_plan_stale`) or
after it (and are ordinary later writes).

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
the property ADR 0007 §6a relies on for partial archives.

One gap must be closed in the implementation: the worker checks only
`generation` before committing. A node overwritten by a direct write *while*
the job is encoding would receive a vector computed from its older text.
Options: (a) accept it (the same gap exists for any slow embedding today); (b)
have the job record each node's revision when it reads the text, and have the
commit skip any node whose revision has moved since. Recommendation: **(b)** —
revisions make it a per-node comparison under the lock the commit already
takes.

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

**Audit record.** The import job (in `import_jobs.db`) carries the plan digest,
the policy, the counts, the backup path and per-entity outcome (created /
overwritten / renamed old → new, with resulting revisions) — the analogue of
the session contract's merge record (§11.5). Recommendation: always.

**Graph events — options:** (a) none, as replace; (b) one ordinary
`node.create`/`node.update`/`edge.create`/`edge.update` event per entity after
the commit lands, with `event_origin` set to a fixed value (for example
`graph-import-merge`) and `event_correlation_id` set to the import id; (c) a
single summary event — a new event type, which changes the event contract in
`docs/EVENT_SUBSCRIPTIONS.md`.

Merge changes part of a graph other clients are looking at, so (a) leaves
sessions, webhooks and agents blind to it. (b) keeps every existing consumer
correct with no contract change, and lets a subscription opt out with its
existing `ignore_origins` filter; its cost is volume — a large merge fires one
event per entity to every matching subscription and into mutation history
(file backend only, ADR 0009). Recommendation: **(b)**, with the event count
included in the plan so a large merge's fan-out is visible before commit.

### 8. Decision point — Size limits and performance

Limits: reuse `MAX_IMPORT_NODES`/`MAX_IMPORT_EDGES` for the document. Options
for a separate, lower cap on *overwrites* per commit: (a) none; (b) a
configurable cap that forces a large overwrite to be split. Recommendation:
**(a)** initially — the preview, the digest and the backup already guard a
large overwrite; revisit with measurements.

Targets to **measure** in the implementation slices (none of these are
measured facts; they are what the slices should benchmark against, using the
fixtures `docs/CAPACITY.md` already uses):

- preview of a 10,000-node document against a 100,000-node graph returns in a
  few seconds and allocates at most a small multiple of the document's own
  size beyond the resident graph;
- the commit holds `_lock` for time proportional to the merged entities, not
  to the live graph — except for the whole-graph backup of §4 option (a),
  whose cost is to be measured and reported;
- the event fan-out of §7 (b) at 10,000 entities does not stall the request.

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
```

`GET /import/{job_id}` and `GET /import` are reused unchanged for the
embeddings job.

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
  did not attach them to; a rename creates a node with none. The plan should
  flag overwritten nodes that have attachments, because ADR 0010 lets agents
  read attachment text by node.
- **Ids.** Document ids are opaque strings, as in v1; rename never derives a new
  id from the old one (§1).

## Proposed implementation slicing

After acceptance, in dependency order:

1. **Plan engine and preview (read-only).** A pure planner beside
   `validate_import_document` that takes the document, the policy and a read
   view of the live graph, and returns the plan and digest (§1, §2, §3, §5);
   `POST /import/merge/preview`; the MCP preview tool if §9 (b) is chosen.
   Writes nothing, so it can ship and be used for dry runs on its own.
2. **Atomic commit.** `GraphStorage.apply_import_merge` with expectations,
   undo and one-batch persistence on both backends (§4); the backup reuse;
   `POST /import/merge` with digest checking and `import_plan_stale`; the audit
   record; per-entity events (§7). Contract tests in the in-memory, file and
   PostgreSQL backends.
3. **Embeddings for merged nodes.** Selective vector drop for overwritten
   nodes and the per-node revision guard in the worker (§6).
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

- Every **Decision point** above — they are the reviewer's to choose.
- A "sync" mode that also deletes live entities absent from the document.
- Merging attachments (they are not part of `graph.json`; ADR 0008 §10).
