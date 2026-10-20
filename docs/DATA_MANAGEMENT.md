# Data Management

This document describes how graph data is managed in the Community Knowledge Graph application.

## Directory Structure

```
data/
  examples/          # Example graph data files (tracked in git)
    default.json     # Default example dataset
  active/            # Active graph data used by the running app (git-ignored)
    graph.json              # Currently active graph file
    graph.journal.ndjson    # Mutations not yet folded into graph.json (see below)
    graph.embeddings.bin    # Embedding vectors for that graph (see below)
    graph.history.ndjson    # Mutation history sidecar
    graph.attachments/      # Files attached to nodes (see below)
```

`graph.attachments/` is the node attachment store
([ADR 0008](adr/0008-node-attachment-storage.md)): one directory per node,
named by a hash of the node id, holding an `index.json` and the files under
server-minted ids. It is never part of `graph.json` or `GET /export`, so back
it up on its own; `ATTACHMENT_DIR` moves it (see `backend/DEVELOPMENT.md`).
Files left behind by a deleted node are moved to a `.quarantine/` directory
beside the node directories when a new node is created with the same id, so
they never show up as the new node's attachments.

## How It Works

The application uses a **two-location model** for graph data:

1. **Example data** (`data/examples/`) - Pre-built datasets that ship with the repository. These are tracked in git and serve as starting points.
2. **Active data** (`data/active/graph.json`) - The graph file actually used by the running application. This file is **git-ignored** so your working data is never accidentally committed.

When the application starts via `./start-dev.sh`, the following logic applies:

```
1. If --data <source> is provided:
   → Copy/download that source to data/active/graph.json (overwrites existing)

2. Else if data/active/graph.json does NOT exist:
   → Check for config/<profile>/graph.json (profile-specific seed data)
   → Else copy data/examples/default.json to data/active/graph.json

3. Else (data/active/graph.json exists):
   → Use the existing file as-is
```

Profiles can include a `graph.json` seed file that is used on first startup. See [PROFILES.md](./PROFILES.md) for details.

This means:
- **First run**: automatically starts with the default example data
- **Subsequent runs**: preserves your working data
- **Explicit data loading**: `--data` flag always overwrites

## Loading Data

### From a local file

```bash
# Load from an example file
./start-dev.sh --data data/examples/default.json

# Load from any local file
./start-dev.sh --data /path/to/my/graph.json

# Load from a relative path
./start-dev.sh --data ../other-project/graph.json
```

### From a URL

```bash
# Load from a remote URL (e.g. GitHub Pages, raw GitHub, any HTTP endpoint)
./start-dev.sh --data https://example.github.io/community-data/graph.json

# Load from raw GitHub content
./start-dev.sh --data https://raw.githubusercontent.com/org/repo/main/data/graph.json
```

### Reset to default example data

```bash
# Delete active data and restart to get the default
rm data/active/graph.json
./start-dev.sh

# Or explicitly load the default
./start-dev.sh --data data/examples/default.json
```

## Adding Example Datasets

To add a new example dataset:

1. Create your graph JSON file with the standard format:
   ```json
   {
     "nodes": [...],
     "edges": [...],
     "metadata": {
       "version": "1.0",
       "last_updated": "2026-01-01T00:00:00"
     }
   }
   ```

2. Save it to `data/examples/` with a descriptive name:
   ```bash
   cp my-dataset.json data/examples/my-community.json
   ```

3. Load it at startup:
   ```bash
   ./start-dev.sh --data data/examples/my-community.json
   ```

## Graph JSON Format

Each graph file follows this structure:

```json
{
  "nodes": [
    {
      "id": "uuid-string",
      "type": "Actor",
      "name": "Organization Name",
      "description": "Longer description text",
      "summary": "Short label for visualization",
      "tags": ["tag1", "tag2"],
      "subtypes": ["Government agency"]
    }
  ],
  "edges": [
    {
      "id": "uuid-string",
      "source": "source-node-id",
      "target": "target-node-id",
      "type": "RELATES_TO"
    }
  ],
  "metadata": {
    "version": "1.0",
    "last_updated": "ISO-8601 timestamp"
  }
}
```

### Node Types

Node types fall into two categories:

**Domain types** (configurable per profile via `schema_config.json`, see [PROFILES.md](./PROFILES.md)):

The default profile includes: Actor, Initiative, Capability, Resource, Legislation, Theme, Goal, Event, Data, Risk. Other profiles can define additional types (e.g., the SCB profile adds Dataset, Undersökning, Variabel, etc.).

**System types** (foundational to the application):
- **SavedView / VisualizationView** (gray) - Saved graph view snapshots
- **EventSubscription** (violet) - Webhook subscriptions for graph mutation events
- **Agent** (pink) - AI agent configurations
- **Groups** - Visual grouping of nodes in the canvas

All domain node types support an optional **subtypes** field for sub-classification within each type. Subtypes are stored as a list of strings (e.g., `["Government agency", "Regulatory body"]`). The UI provides autocomplete with case normalization based on existing subtypes in the graph, helping maintain consistency. Example subtypes:
- Actor: "Government agency", "Municipality", "International organisation", "Steering group"
- Initiative: "Research project", "Pilot program", "Working group"
- Risk: "Cybersecurity", "Compliance", "Operational"
- Data: "Open data", "Register", "API", "Statistics"

Domain types can be freely modified, added, or removed in the schema configuration file. System types are integral to application functionality and should not be removed. See [PROFILES.md](./PROFILES.md) for how to create custom profiles with different node types.

## Embedding Sidecar

Semantic search needs a vector per node. Those vectors are **not** part of the
graph file — they live in a separate binary file next to it, by default
`<graph stem>.embeddings.bin` (so `data/active/graph.embeddings.bin` for the
default layout), overridable with `EMBEDDINGS_FILE`.

They are kept out of `graph.json` because a 384-dimension vector costs about
1.5 kB as float32 and roughly 7.5x that as JSON text. Written into the graph
file, they dominated it, and every graph mutation rewrote all of them even when
none had changed. The sidecar is rewritten only when a vector actually changes.

What this means in practice:

- **Backup.** Back up the sidecar alongside `graph.json`. It is the only copy
  of the vectors.
- **Restore.** A restore without the sidecar still works: the graph loads, and
  semantic search returns nothing until the vectors are regenerated. A sidecar
  that cannot be read is ignored with a warning for the same reason — the graph
  never fails to load because of it. Regenerate with
  `python scripts/generate_embeddings.py`, which needs the optional ML extras
  (`pip install -r backend/requirements-ml.txt`). That script and
  `scripts/migrate_embeddings.py` both honour `EMBEDDINGS_FILE`, and take
  `--embeddings-file` to override it, so they act on the same sidecar the
  running app reads.
- **Migration.** A `graph.json` written before the split still carries its
  vectors inline. Those are read on load and moved into the sidecar on the next
  save; nothing needs to be run by hand.
- **Portability.** Vectors are derived from node text, so a graph file moved
  without its sidecar is complete data — only the search index is missing.
- **Replacing the graph.** A sidecar belongs to the graph it was built from.
  Swapping in a different dataset while leaving the old sidecar in place would
  give any node id present in both the *old* dataset's vector, and nothing
  regenerates a vector for a node that is only loaded. `start-dev.sh` deletes
  the sidecar whenever it puts a different graph in place — `--data`, or seeding
  a missing `graph.json` from a profile or example — and follows
  `EMBEDDINGS_FILE` when doing so. It deletes the graph journal at the same
  points (see *Graph Journal* below). Do the same when replacing a graph file
  by hand.

## Importing a graph

`POST /api/import` (see [ADR 0006](adr/0006-graph-import-replace-mode.md) for
the full design) replaces the ENTIRE active graph with a posted document in
the same shape `GET /api/export` returns — a `nodes` list and an `edges` list.
This is a **REPLACE, not a merge**: every id in the document becomes the live
graph's content exactly, and any node or edge from the previous graph that
does not also appear in the document is gone. To see what a document would
do to the live graph *without* replacing it, use the merge preview below.

What happens, in order:

1. **Validate.** The whole document is checked before anything is written:
   every node/edge type must be one this instance's schema configures, no two
   nodes (or two edges) in the document may share an id, and every edge's
   source/target must reference a node id present in the SAME document (there
   is no old graph left for a dangling reference to resolve against, since
   this is a replace). Any problem: the graph is left completely untouched,
   and every problem found is reported, not just the first.
2. **Back up.** The current graph is snapshotted to
   `<graph dir>/import-backups/import-backup-<timestamp>-<id>.json`, in the
   same nodes/edges shape, before anything changes. If this backup cannot be
   written, the import is refused rather than proceeding unprotected.
3. **Replace.** The graph is swapped in atomically and saved through the same
   whole-graph, atomic (temp file + rename) write a checkpoint already uses.
   Like any whole-graph save, this folds the journal into the new snapshot and
   empties it — but, because the import replaces the graph in place within the
   same running process rather than swapping in a different file from outside
   it, the file's `journal_id` is **preserved**, not re-minted (see *Graph
   Journal* below, "The file keeps its identity across whole-graph saves"). If
   the write fails partway, the live graph is restored to what it was before
   the import; the file on disk was never at risk either way.
4. **Embeddings, asynchronously.** Existing vectors are dropped (a replaced
   graph is a new dataset — see *Embedding Sidecar* above for why keeping a
   reused id's old vector would be wrong), and a durable background job
   regenerates them. The request returns as soon as step 3 lands — it does
   NOT wait for embeddings, which can be the slowest part of an import by a
   wide margin. Poll `GET /api/import/{job_id}` for progress; its `status` is
   `queued` / `running` / `succeeded` / `failed` / `cancelled`, and a
   terminal job additionally reports `embeddings_status`:
   - `succeeded` — vectors were generated.
   - `unavailable` — the optional ML extras
     (`backend/requirements-ml.txt`) are not installed. The graph itself
     imported successfully and is complete data; semantic search degrades to
     name-based matching until embeddings are generated, e.g. with
     `scripts/generate_embeddings.py`.
   - `superseded` (job `status` is `cancelled`, never `succeeded` or
     `failed`) — a second `POST /import` replaced the graph again before this
     job could commit its embeddings, whether because its own encode ran
     longer than the gap between the two imports or because it was a
     crash-recovered job resumed after the fact. Nothing was written back for
     it; the newer import's own job (re)generates embeddings for the content
     that is actually live.

   A `failed` job means embedding generation itself failed after retries —
   the graph replace is **not** rolled back for this, since it already
   committed and is valid on its own.

   Rarely, the graph replace succeeds but the embedding job cannot even be
   started (e.g. the durable job store is unreachable). `POST /import` still
   reports `success: true` and `graph_replaced: true` — the graph really was
   replaced — but with `job_id: null` and `embeddings_status: "not_started"`
   plus an explanatory `embeddings_message`; re-run
   `scripts/generate_embeddings.py`, or import again later, rather than
   retrying immediately (a retry would only repeat the same replace).

The background job is durable: it is backed by a SQLite store
(`import_jobs.db` next to the graph file by default, overridable with
`IMPORT_JOBS_DB`) and is resumed automatically if the process restarts while a
job is still running.

To undo an import, re-run `POST /api/import` with the content of the backup
file it wrote.

## Previewing a merge import

`POST /api/import/merge/preview` (see
[ADR 0013](adr/0013-graph-import-merge-mode.md)) plans merging a document
*into* the live graph instead of replacing it, and **writes nothing**: no
graph change, no backup, no job, no events. A previewed plan is applied with
`POST /api/import/merge` (see *Committing a merge import* below).

```http
POST /api/import/merge/preview
{ "document": { "nodes": [...], "edges": [...] },
  "policy": { "default": "fail", "by_type": { "Actor": "overwrite" } },
  "include_system_types": false }
→ 200 { "success": true, "plan": {...}, "plan_digest": "sha256:…" }
```

- **Collisions.** A document node or edge whose id already exists live follows
  its policy: `fail` (the default — a conflict that blocks the merge), `skip`
  (keep the live entity), `overwrite` (take the document's name, description,
  summary, tags, subtypes, aliases and metadata; for an edge its type, label
  and metadata), or `rename` (create it under a new id minted at commit, with
  the document's own edges rewritten to it). `by_type` overrides the default
  per node type or relationship type. A node whose type differs from the live
  node's, or an edge whose endpoints differ, is a conflict unless its policy
  is `rename`.
- **Edges** may attach to live nodes; an endpoint that is neither in the
  document nor live is rejected as dangling, never created as a placeholder.
  A document edge repeating a live edge's source, target and type under
  another id is skipped as a duplicate. An overwrite that changes a node's
  subtypes re-checks its live edges, and one that would become inapplicable
  is a conflict.
- **System types** (`Agent`, `EventSubscription`, `SavedView`, `Skill`, …) and
  every edge touching one are skipped unless `include_system_types` is `true`.
- **Validation** is v1's: an unknown node or relationship type, a duplicate id
  within the document or a malformed entity is listed under `rejected` and
  blocks the merge. The v1 size caps apply.

The `plan` lists `adds`, `overwrites` (each changed field's `main` = live and
`incoming` = document value), `renames`, `skips`, `conflicts`, `rejected`,
attachment flags (overwritten nodes that have attachments, and new nodes whose
id has leftover attachment files), `edges_to_archived_nodes`,
`edges_to_skipped_nodes` (document edges attached to a live node a `skip`
kept), how many nodes
would be (re)embedded and how many graph events a commit would emit. Each list
holds at most 1,000 entries; its `count` is always exact. `committable` is
`false` while anything is rejected or in conflict. `plan_digest` changes when
the request or the document changes (not when it is merely reordered, and not
for an entry's `revision` field, which a write ignores), or the
graph is replaced, and — unless something is rejected, which blocks any
commit — when a live entity the plan relied on changes, so a future commit can
refuse a plan that is no longer the one previewed.

The preview reveals which ids exist and live field values, so it needs the
access a commit will — graph mutation rights — and read rights, both with
unnarrowed graph access.
`stamp_provenance: true` is refused until provenance stamping ships. The
`preview_graph_merge` MCP tool does the same for small documents an assistant
assembles — at most 10,000 nodes and 20,000 edges, since it plans on the
server's event loop; there is no MCP tool that commits a merge. One preview
runs at a time; a second is refused with 503 `import_preview_busy` and can
simply be retried.

## Committing a merge import

`POST /api/import/merge` applies a previewed plan. It takes the preview's
request plus the `plan_digest` the preview returned:

```http
POST /api/import/merge
{ "document": {...}, "policy": {...}, "include_system_types": false,
  "plan_digest": "sha256:…" }
→ 200 { "success": true, "import_id": "merge-…", "plan": {...},
        "plan_digest": "sha256:…", "counts": {...}, "renamed": {...},
        "backup_path": "…", "job_id": "…", "embeddings_status": "queued",
        "audit_recorded": true }
→ 409 { "error_code": "import_plan_stale", "plan": {...}, "plan_digest": "…" }
→ 422 { "error_code": "import_merge_blocked", "plan": {...} }
```

What happens, in order:

1. **Authorize** as the preview does: graph mutation and read rights, both
   with unnarrowed graph access.
2. **Re-plan and compare.** The plan is recomputed from the request and the
   live graph. A plan with rejected or conflicting entries is refused with
   `import_merge_blocked`; a digest that no longer matches — the document,
   the parameters or a live entity the plan relied on changed, or the graph
   was replaced — is refused with `import_plan_stale`, carrying the current
   plan and its digest to review and commit instead. Nothing has been
   written either way. A plan that writes nothing — every entry skipped,
   unchanged or excluded — returns here with `success: true`, zero counts
   and no `import_id`: no backup, no lock, no record.
3. **Back up.** The whole current graph is written to `import-backups/`
   exactly as `POST /api/import` does, and the merge is refused
   (`backup_failed`) if it cannot be. Every entity the merge overwrites is
   in it at the version the merge replaces. Restoring it with
   `POST /api/import` is a *replace*: it also undoes every write made after
   the backup was written, including a write to an unrelated entity that
   landed while the merge was being applied.
4. **Commit, all-or-nothing.** Under the storage lock the plan is computed
   once more and must still have the same digest, so a write landing
   between step 2 and here fails the commit with `import_plan_stale` instead
   of being overwritten. Creates, overwrites and renames are then applied
   and persisted as one batch (one journal record on the file backend).
   Written entities take fresh revisions; nothing else changes, the graph's
   `generation` included. Renamed nodes and edges get new ids, returned in
   `renamed` as old → new pairs (at most 1,000 listed; `count` is exact).
   Live edges the document does not name are never deleted. Other writes
   wait while this step holds the lock (reads do not); it grows with the
   size of the document and the number of entities merged, not the size of
   the graph — see ADR 0013,
   "Slice 2 measurements", for figures.
5. **Events.** One ordinary `node.create`, `node.update`, `edge.create` or
   `edge.update` event per written entity, after the batch lands. Their
   origin is the request's `x-event-origin` header, else
   `graph-import-merge`; their correlation id is `x-correlation-id`, else
   the merge's `import_id`. A large merge fires as many events as it writes
   entities — the preview's `events.count` says how many.
6. **Embeddings.** Created and renamed nodes, and overwritten nodes whose
   name, description, summary, tags or aliases changed, lose any vector in
   the commit itself, so a stale vector is never served for new text; on
   the file backend the embedding sidecar is rewritten with the same write
   as the batch. The same background job a replace uses then embeds every
   node without a vector (`embeddings_status: "queued"`, `job_id` set); a
   merge that changed no embedded text queues nothing (`"not_needed"`). If
   the job cannot be started the merge still stands (`"not_started"`).
7. **Audit record.** A terminal record under the `import_id` in the same
   job store (`GET /api/import/{import_id}`, and listed by `GET /api/import`
   with `mode: "merge"`) keeps the digest, the parameters, the counts, the
   backup path, the embeddings job id and every created, overwritten and
   renamed entity with its resulting revision (none for an
   `outcome_unknown` record, below). It is written after the
   graph commit, in a separate store, so it is best-effort:
   `audit_recorded: false` reports a merge that committed without one (a
   commit that writes nothing returns `null`). The record also keeps the
   request's attribution (actor and scope) as the request context reports
   it; in the open core that comes from request headers, so it is only as
   trustworthy as whatever sets them in front of the service. Its
   `merge_status` is `committed`, or `outcome_unknown` for a merge answered
   with 503 `write_outcome_unknown`. Whether such a merge survives is not
   known when it is answered: it is applied in memory, it may already have
   reached the store, and the next write that lands (or a clean shutdown)
   saves the whole graph with it, while a crash before then can lose what
   had not reached the store. It fires no events and queues no embeddings
   job, so the nodes whose vectors it dropped (step 6) stay without one
   until they are embedded again. The record, whose `import_id` the error
   response carries, is the trace it leaves: check it and the live graph
   before retrying (see retries below).

A commit may omit `plan_digest` only when every policy is `fail` or `skip`
and `include_system_types` is false — a merge that can only add and so can
be retried blindly; anything else is refused with `plan_digest_required`.

**Retries.** The digest proves a plan is still current, not that it has not
been applied. A commit retried after a lost response, against a merge that
did land, is not applied again when the first commit wrote under any of
the document's own ids — a create or an overwrite — since that write moved
what the digest covers: a `fail`-policy retry is refused as
`import_merge_blocked`, a retry carrying the digest as `import_plan_stale`,
and a digest-less `skip`-policy retry plans nothing and returns zero
counts. A commit whose only writes are **renames** writes nothing under
the document's ids and can leave everything the digest covers unchanged —
a node rename always does — and when it does, the same request with the
same digest is applied again and creates the renamed copies a second time. Check `GET /api/import`
for the first commit's record before retrying any commit. One commit runs
at a time (503
`import_merge_busy`). A store failure is answered as for any direct write —
502 `write_failed` (nothing changed) or 503 `write_outcome_unknown` — with
the backup path. Merging is refused in PostgreSQL mode (501
`import_merge_not_supported`) until its embedding commit exists. There is no
MCP tool that commits a merge.

## Importing a vector-aware archive

`GET /api/export/archive` and `POST /api/import/archive` (see
[ADR 0007](adr/0007-vector-aware-export-archive.md)) extend the plain
`graph.json` export/import above with an optional ZIP archive that also
carries the embedding sidecar, so a restore does not have to fall back to
regenerating every vector from scratch. **The plain `GET /api/export` and
`POST /api/import` are unchanged** — the archive is a second, additive pair of
endpoints, kept for the "lightweight interchange" case the plain endpoints
already serve well.

### Archive layout

A ZIP with up to three members:

| Member | Always present? | Content |
|---|---|---|
| `graph.json` | Always | The same `nodes`/`edges` document `GET /api/export` returns |
| `embeddings.bin` | Only when the live graph has vectors | The embedding sidecar's own binary format (see *Embedding Sidecar* above), byte-for-byte — not a second format |
| `manifest.json` | Always (written by `GET /api/export/archive`; a hand-made archive may omit it) | Schema version, export date, embedding model/dimension, per-member SHA-256 checksums, node/edge counts |

`manifest.json` shape:

```json
{
  "schema_version": "1.0",
  "export_date": "2026-09-23T12:00:00+00:00",
  "embedding_model": "all-MiniLM-L6-v2",
  "embedding_dimension": 384,
  "node_count": 42,
  "edge_count": 17,
  "checksums": {
    "graph.json": "sha256:<hex>",
    "embeddings.bin": "sha256:<hex>"
  }
}
```

`embedding_model`/`embedding_dimension` are `null` and `embeddings.bin` is
omitted entirely when the exporting instance had no vectors loaded — there is
nothing to restore, and the archive degrades to the plain-`graph.json` shape
below. `checksums` only lists the members actually present.

### What happens on import, in order

1. **Open and checksum-verify.** The ZIP is opened and every checksum
   `manifest.json` declares is verified against the member's actual bytes —
   **before** anything is decoded for use. A ZIP that cannot be opened, an
   archive with no `graph.json` at all, or any member whose bytes disagree
   with the checksum declared for it, is rejected with
   `archive_integrity_failed` and **nothing is written to the live graph**
   — the same "touch nothing on failure" guarantee `POST /api/import`'s own
   validation already gives.
2. **Import the graph content.** The archive's `graph.json` member goes
   through the exact same validate → backup → replace pipeline
   `POST /api/import` uses (see *Importing a graph* above) — the same
   validation errors, the same pre-import backup, the same atomic replace.
3. **Restore or regenerate embeddings**, depending on what the manifest said:
   - **Compatible and fully covered** (a readable manifest, every declared
     checksum verified, `embeddings.bin` present and non-empty, its
     `embedding_model` matches this instance's live model, and it has a
     vector for every node id in the imported graph) — vectors are restored
     directly from `embeddings.bin` into the live vector store,
     synchronously, with no background job: `embeddings_status: "restored"`,
     `job_id: null`, `archive_compatible: true`.
   - **Compatible but only partially covered** (same as above, but
     `embeddings.bin` has a vector for only SOME of the imported graph's node
     ids — e.g. the exporting instance itself had some never-embedded nodes)
     — the covered subset is restored the same way, synchronously, **and** a
     background job is enqueued to (re)generate embeddings for the rest, so
     the gap is never silently left open: `embeddings_status:
     "restored_partial"`, `job_id` set, `archive_compatible: true`,
     `embedded_count` (how many were restored directly), `pending_node_ids`
     and `pending_count` (what the job still owes). The job needs no explicit
     node-id scoping — the background worker already skips any node that
     already has a vector, which is exactly the subset just restored.
   - **Incompatible or absent** (a different `embedding_model`, no
     `embeddings.bin` in the archive at all, or a manifest that is missing or
     unreadable — including the "someone zipped a plain `graph.json` export
     themselves, with no `manifest.json` or `embeddings.bin`" case, which is
     legitimate, not an error) — the graph still imports successfully, and
     the same background regeneration job `POST /api/import` always uses is
     enqueued: `embeddings_status: "queued"`, `job_id` set,
     `archive_compatible: false`, and `embeddings_message` states plainly
     *why* it is regenerating (e.g. "archive embedding_model 'x' does not
     match this instance's model 'y'"), not just a bare "queued".

Compatibility is judged on `embedding_model` name equality against this
instance's live `VectorStore.model_name` — dimension is not compared
separately, since a fixed model name determines its output width
deterministically; `embedding_dimension` in the manifest is informational
(and, incidentally, cross-checked implicitly: an `embeddings.bin` whose actual
row width disagrees with what its own bytes claim fails to parse as a sidecar
at all).

A checksum is only enforced for a member the manifest actually names — an
archive with no manifest, or one that does not cover a given member, has made
no promise about that member's bytes and so cannot have broken one; whether
its embeddings can still be read is instead judged when they are opened,
same as `embeddings.bin` always has been (a damaged sidecar degrades to "no
vectors" rather than failing).

### Undoing an archive import

Exactly like a plain import: `POST /api/import` (or `/api/import/archive`)
with the pre-import backup written under `import-backups/` — the archive
import writes the same backup the plain path does, of the graph the archive
replaced.

## Graph Journal

`graph.json` is the graph, and it is still written whole and atomically (a temp
file, then a rename). What changed is *when*. A mutation no longer rewrites it:
it is appended as one JSON line to `graph.journal.ndjson` beside it, with an
`fsync`, so a one-word edit to a large graph costs one small append rather than
a serialisation of every node. The journal is folded back into `graph.json` — a
**checkpoint** — every 100 mutations, whenever the app flushes, and at shutdown.
On startup the app reads `graph.json` and replays whatever the journal holds.

What this means in practice:

- **Durability.** Nothing the backend has written is lost on a crash. (What
  a crash can lose is a write still queued behind the app's single writer
  thread — the same window the whole-file write always had.) A crash
  mid-append leaves an incomplete last line, which is detected, dropped
  whole and cut off the file at the next start — a
  multi-entity mutation is one line, so it lands entirely or not at all. A
  crash mid-checkpoint leaves the previous `graph.json` intact (the rename is
  atomic) and the journal still complete; replaying it onto a snapshot that
  already contains its records is harmless.
- **`graph.json` on its own is the graph as of the last checkpoint.** After a
  clean shutdown the journal is empty and `graph.json` is complete; a
  checkpoint that fails at shutdown is reported in the log, and the journal
  then still holds the difference. While the
  app is running, or after a crash, the journal holds the difference. **Back up
  the journal alongside `graph.json`**, or checkpoint first; a copy of
  `graph.json` taken while the journal is non-empty is behind by up to 100
  mutations.
- **Replacing the graph.** A journal belongs to the graph it was written
  against. Replayed onto a different dataset it would resurrect nodes of the
  old one and overwrite same-id nodes of the new one with stale payloads. So
  `graph.json` carries a `journal_id` in its metadata — minted the first time
  the app writes the file — and every journal line names the id it extends.
  The id is kept for the file's lifetime, with one exception: if the
  checkpoint that mints it writes the snapshot but is interrupted before it
  empties the journal, that id sits in `graph.json` beside a journal that
  still predates it — refused at load like any other mismatch, described
  below — and the next successful append mints and writes a replacement,
  superseding it once the journal is emptied too. No journal line ever names
  the superseded id, so nothing already acknowledged is lost, but it is not
  the id the file ends up keeping. A journal whose lines name a different id,
  or none where the file has one, or one where the file has none, is **refused at
  startup** with a message naming both ids; the graph is not loaded, and
  nothing is deleted. If you replaced
  `graph.json` on purpose, delete the journal (its mutations belong to the old
  graph); if not, put back the `graph.json` it belongs to. One more shape
  reads the same way: a journal whose lines carry *no* id beside a stamped
  file, where nobody replaced anything, is the write that stamped the file
  (a checkpoint, which folds those lines in first, or a whole-graph save,
  which supersedes them) interrupted before it emptied the journal — either
  way deleting the journal loses nothing; the message says so. `start-dev.sh`
  still deletes the journal whenever it puts a different graph in place, for
  the same reasons and at the same points as the embedding sidecar, and doing
  the same when replacing `graph.json` by hand saves a refused start.
  A `graph.json` from before the stamp loads as before: with a journal from
  before the stamp it replays, and the file is stamped at the next
  checkpoint — which happens before the first new mutation is journaled.
  The id identifies the file's *lineage*, not a point in time: a copy of
  `graph.json` taken after the file was stamped, restored beside a newer
  journal of the same lineage, replays that journal — the crash-recovery
  shape, and correct only if the copy is older than the journal. A copy
  taken *before* the stamp (a pre-upgrade backup) has no id, so a stamped
  journal beside it is refused: put the stamped `graph.json` back, or accept
  losing the journal. The backup guidance above stands.
- **Damage.** An incomplete or unreadable *last* line is the crash shape and
  is skipped with a warning. An unreadable line anywhere else is not, and the
  records after it may depend on it, so the app refuses to load and names the
  line rather than silently losing mutations; `graph.json` still holds the
  graph as of the last checkpoint.
- **Object-store mounts.** On a FUSE-mounted bucket an append re-uploads the
  file, so the journal's cost there is proportional to its size — which the
  checkpoint cadence keeps small.

## Mutation History

Every graph mutation is appended to a history sidecar next to the graph file
(`graph.history.ndjson` for the default layout). It is an audit trail: append-only,
newest records last, one self-contained JSON record per line.

**Retention.** The sidecar is capped at `HISTORY_MAX_EVENTS` records (default
100000). When the cap is exceeded, a compaction pass rewrites the file keeping the
newest N, atomically. `HISTORY_MAX_AGE_DAYS` additionally drops records older than
a given age; it is unset by default, because "delete records older than X" is a
retention policy an operator should choose rather than inherit. Setting
`HISTORY_MAX_EVENTS=0` removes the count cap; trimming then stops altogether
unless `HISTORY_MAX_AGE_DAYS` is also set, in which case age-based trimming
still runs. **To keep every record you must set `HISTORY_MAX_EVENTS=0`
explicitly and leave `HISTORY_MAX_AGE_DAYS` unset** — leaving both unset
applies the default cap, it does not disable trimming.

The default is deliberately generous, so that upgrading a typical deployment does
not trim anything. It is a cap, not a promise: a sidecar that already holds more
than `HISTORY_MAX_EVENTS` records loses the excess on the first compaction after
the upgrade. Set `HISTORY_MAX_EVENTS=0` before upgrading if that matters. Note
also that the resulting file size depends on the record size, so the cap bounds
the record count rather than the bytes directly.

**When trimming runs.** A compaction pass reads and rewrites the whole sidecar
while holding the store lock, so it is throttled rather than run on every append:
once per tenth of the records the previous pass left on disk. Deriving it from
the file rather than from the cap keeps the work per mutation roughly constant at
any size — on the order of twenty records read per append, since a pass makes two
forward passes plus the rewrite — including under age-based retention, where
there is no count cap to derive it from. The cost is a bounded overshoot — between passes the sidecar holds what
retention keeps plus at most one interval, about 110%.

One consequence worth knowing if you rely on `HISTORY_MAX_AGE_DAYS`: passes are
counted in mutations, never in time. A record past the cutoff survives until the
next pass — up to a tenth of the history's worth of appends — so on a quiet
instance aged-out records stay on disk indefinitely. Age retention bounds records
in mutations, not in wall-clock time; it is not a deletion deadline.

**Reads.** History queries return a page at a time and read the file backwards, so
answering one costs memory proportional to the page rather than to the file. That
matters on a small container: the previous implementation parsed every record to
return the newest 50.

**What a record holds.** A **node** update record keeps the `patch` — the fields
that actually changed — plus, in `before` and `after`, those same fields and the
entity's display name. That is exactly what the history views read: the name to
title the entry, and each patched field's before-value to render a
`before → after` diff. Fields the mutation left alone are not repeated into the
record, so a one-word edit to a large node costs a small record rather than two
copies of the node.

Every other record keeps both snapshots whole. Creates and deletes have no patch
that could describe what appeared or vanished. Edge updates have no patch either
— one is currently computed for node updates only — so the views fall back to
diffing the pair, which needs both sides intact.

An `embedding` is dropped from every payload of every record the current code
writes. Vectors live in the embedding sidecar; a copy here would put them back
into every mutation record. Note that this is not retroactive: an instance that
ran history before the vectors moved to the sidecar wrote records with the vector
inline, and those records are left as they are (see below).

Older records — written before the trimming, or before the sidecar — keep their
full snapshots and whatever they carried at the time. They are read exactly as
they always were, since the record shape did not change, only how much of it is
filled in. No migration is needed for correctness and none is offered; an
operator who wants the old vectors out of the sidecar can truncate or discard the
history file, which the graph does not depend on.

**Backup.** The sidecar is independent of `graph.json` and can be backed up,
truncated or discarded on its own; the graph does not depend on it.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GRAPH_FILE` | `data/active/graph.json` | Path to the active graph file |
| `EMBEDDINGS_FILE` | `<graph stem>.embeddings.bin` next to the graph | Path to the binary embedding sidecar (see *Embedding Sidecar* above) |
| `HISTORY_MAX_EVENTS` | `100000` | Mutation-history records retained (see *Mutation History* above); `0` removes the count cap |
| `HISTORY_MAX_AGE_DAYS` | *(unset)* | Age-based history retention, opt-in |
| `GRAPH_SCHEMA_CONFIG` | `config/default/schema_config.json` | Path to schema configuration |
| `SCHEMA_FILE` | *(auto-resolved from profile)* | Alternative env var for schema path |

When using `./start-dev.sh --profile <name>`, the `SCHEMA_FILE` variable is automatically set based on the profile's `schema_config.json` (with fallback to default).

## Current state vs future hosted direction

The current application still uses file-based graph persistence as the default runtime model. That remains appropriate for:
- local development
- standalone open source deployment
- early hosted-readiness work where the focus is on seams, contracts, and operational hooks

For the long-term shared SaaS architecture, the target is different:
- multiple graphs or workspaces should eventually be served within a shared application/service environment, each graph by its own core runtime process or processes — a core process binds exactly one graph, though federation can add search over others ([ADR 0009](./adr/0009-dual-mode-graph-storage-seam.md)), and routing a request to a process that binds its graph lives outside the core
- user access to graphs should be controlled through application-managed identity and authorization
- the storage layer should offer shared persistence with row-level access constraints

Shared persistence exists as an optional backend: `GRAPH_BACKEND=postgres` stores a graph in PostgreSQL, one schema per graph, and an opaque `GRAPH_POSTGRES_SCOPE` value restricts a process to its own rows through row-level security that is enabled and forced on the graph tables. The file backend stays the default, and nothing about the hosted direction makes it go away. What this document does not cover is the hosting layer around it (provisioning, identity, routing a request to the process that binds its graph). The seam a shared backend plugs into, what it has to implement, and how the PostgreSQL backend keeps scopes apart are described in [PERSISTENCE_BACKENDS.md](./PERSISTENCE_BACKENDS.md).
