# ADR 0006 — Graph import: REPLACE mode over the ExecutionStore seam

- **Status:** Accepted
- **Date:** 2026-09-23
- **Scope:** Open-source core only
- **Related:** [`DURABLE_EXECUTION_CONTRACT.md`](../DURABLE_EXECUTION_CONTRACT.md),
  [`DATA_MANAGEMENT.md`](../DATA_MANAGEMENT.md) (graph.json format, embedding
  sidecar, graph journal), [ADR 0001](0001-local-durable-execution-store.md)
  (the durable store this feature is the first non-agent consumer of)

## Context

There was no import path for `graph.json` anywhere in this repository: no CLI,
REST route, MCP tool or admin UI. The only way to load a different dataset was
to stop the process, replace the file on disk, and restart — which is also how
`start-dev.sh --data` and the sspcloud init container behave today, and why
`docs/DATA_MANAGEMENT.md` already documents "replacing the graph" as an
operation with sharp edges (the embedding sidecar and the graph journal both
belong to a specific graph's lineage, and both must be reset when the graph
under them changes).

This ADR is for adding that path as a live, in-process REST endpoint —
`POST /import` — with three requirements that pull in different directions:

1. **Never commit a broken graph.** Validation must be complete before any
   write.
2. **Never block the request on embedding generation.** Encoding node text
   with the CPU-only sentence-transformers model is, by a wide margin, the
   slowest part of loading a graph, and this codebase already treats
   embeddings as a best-effort, regeneratable index rather than as part of the
   graph itself.
3. **Never leave the graph half-written**, whether validation fails, the
   commit itself fails partway, or the (asynchronous) embedding step fails.

A prior Corp graph decision,
`dec-graph-import-v1-scoped-to-replace-whole-graph-20260923`, settled the
scope question ahead of this ADR: this application has no multi-graph /
workspace concept — one `graph.json` per running instance — so "new graph"
means REPLACING the whole active graph atomically, not merging into it.
Merge-with-id-collision-policy is out of scope here and tracked as
`task-graph-import-merge-mode-followup-20260923`.

## Decision

### 1. Validate as a closed document, not against the live graph

Because this is REPLACE, not merge, the imported document is validated
entirely against itself: node/edge type validity, duplicate ids, relationship
applicability and dangling references all check the document's own node set,
never the graph currently running. `backend/service/graph_import.py` does this
as a pure function with no `GraphStorage` dependency, so a validation failure
is, by construction, incapable of writing anything — there is no live object
in scope for it to mutate.

### 2. Commit is synchronous; embeddings are not

`POST /import` performs validate → backup → replace synchronously and returns
as soon as the graph has been durably replaced. It does not wait for
embeddings. This mirrors how `GraphStorage.add_nodes` already treats
embedding generation as a step that "should not fail the write" — except here
it is pulled fully off the request path rather than swallowed inline, because
an import can touch orders of magnitude more nodes than a single `add_nodes`
call.

### 3. `replace_all_nodes_and_edges` is a new, narrow `GraphStorage` method

Rather than route an import through the incremental `add_nodes` path (which
resolves names to ids, checks per-node existence against the live store, and
journals each entity individually), `GraphStorage.replace_all_nodes_and_edges`
swaps the entire in-memory graph the same way `load()` does when reading a
file from disk, then persists it through the **existing** `save()` path — the
same whole-graph, atomic (temp file + rename) write every checkpoint already
uses, which also folds the graph journal into the new snapshot and empties it,
exactly as an ordinary checkpoint does. Unlike replacing `graph.json` from
outside the running process, this does **not** mint a fresh `journal_id`: the
import replaces the graph in place within the same `GraphStorage` instance, so
the file's lineage identity is preserved across it, the same way it survives
any other whole-graph save (see `docs/DATA_MANAGEMENT.md`, "Graph Journal",
"The file keeps its identity across whole-graph saves"). No new persistence
mechanism was added; import reuses the one that already existed for exactly
this shape of write.

On a save failure, the in-memory graph is restored to its pre-import snapshot
before the exception propagates — the on-disk file is never at risk either way
(the failed write's temp file is simply discarded by the atomic rename), so
this in-memory restore is what keeps the LIVE graph consistent with what is
durably on disk. The swap itself is a handful of single-statement pointer
reassignments (`self.nodes = new_nodes`, etc.), built off to the side and
published only once everything is ready, so a read that takes no lock of
`self.nodes` / `self.edges` / `self.graph` / the searchable-text cache —
every such read path in `GraphStorage` except the writers — always sees
either the fully old graph or the fully new one for those containers, never a
window with, say, nodes cleared but edges not yet rebuilt. Restoring on
failure is then just putting the old references back, not rebuilding them.
The vector index swap below is a separate step with its own, weaker
guarantee — see the note after it.

`GraphStorage.generation` is a counter bumped once, atomically, in the same
swap: a caller that starts slow work against "the graph as it stands now" (the
embedding job below) captures it first and checks it again — via
`commit_generation_embeddings`, under the same lock the swap itself uses —
before writing its result back, so a job whose graph a later import has
already superseded is detected and discarded rather than silently overwriting
that later import's content. The counter is stamped into `graph_metadata` as
`graph_generation` on every replace and read back on `load()`, so it survives
a process restart: without that, a crash-recovered job (`recover_import_jobs`)
carrying a real, pre-crash generation would be compared against an in-memory
counter that forgot everything on restart, and wrongly treated as superseded
even when no later import ever happened.

Existing vectors are dropped unconditionally on replace
(`vector_store.load_vectors({})`) rather than pruned to the surviving ids. A
node whose id happens to match one in the old dataset would otherwise inherit
that old dataset's vector for what may be entirely different text — exactly
the footgun `docs/DATA_MANAGEMENT.md` already calls out for `start-dev.sh
--data`, which deletes the sidecar whenever it swaps in a different graph.
This clear is not covered by the atomic-pointer-swap guarantee above:
`VectorStore.load_vectors` (pre-existing code, not introduced by this ADR)
makes two separate assignments with no lock of its own, and
`find_similar_nodes` (unlike its `_batch` sibling) reads the vector store
without `GraphStorage._lock` either. Clearing to `{}` specifically cannot
produce a wrong answer through that window — both sides of it mean "no
results" — so this is a documentation-precision note about a pre-existing
read pattern, not a bug this ADR introduces or needs to fix.

### 4. A pre-import backup file, in addition to in-memory rollback

The in-memory restore above only protects the live process. If the backup step
fails, the import is refused outright — an import that cannot be protected by
a backup does not run. The backup is a plain JSON file next to the graph
(`<graph dir>/import-backups/import-backup-<timestamp>-<id>.json`), written in
the same `nodes`/`edges` shape `GET /export` returns, so it can be re-imported
as-is to undo an import after the fact — including after the process has since
restarted, which the in-memory rollback cannot help with.

### 5. Embeddings run as the first non-agent consumer of `ExecutionStore`

`docs/DURABLE_EXECUTION_CONTRACT.md` defines `ExecutionStore` as a general
seam, but every current producer (`AgentRunRecorder`) enqueues a job already
`RUNNING`, purely as durable history — nothing in this codebase has ever
called `claim_next` for real. Import is the first real producer/consumer pair:
`POST /import` enqueues one `ExecutionJob` (a new `ExecutionKind.IMPORT`) in
`PENDING` state and starts a background thread that drains it via
`claim_next`; `GET /import/{job_id}` reads it back, mapped to the same
`queued`/`running`/`succeeded`/`failed`/`cancelled` vocabulary
`GET /agents/runs/{run_id}` already uses.

Because nothing else in this codebase enqueues a `PENDING` job, an import
worker never has to arbitrate with unrelated work over the same queue today.
That is a real, load-bearing assumption, called out with a defensive check in
`backend/agents/execution/import_worker.py` (a claimed non-`IMPORT` job is
failed back rather than processed) and with its own SQLite database
(`import_jobs.db`, next to the graph file) rather than sharing the agent-run
history database — so the two producers are also physically separated, not
just conventionally.

Retries use the store's default `RetryPolicy` (3 attempts, capped exponential
backoff), unlike the `max_attempts=1` policy `AgentRunRecorder` uses for
history — an import's embedding step is real, retryable work, not an outcome
record.

### 6. A degraded outcome is still a successful import

Four distinct outcomes are represented on the job, all under the terminal
states the execution contract already defines:

| Case | Job terminal state | `embeddings_status` |
|---|---|---|
| Embeddings generated | `SUCCEEDED` | `succeeded` |
| ML extras not installed (`ImportError` from `sentence-transformers`) | `SUCCEEDED` | `unavailable` |
| A real failure (retried, then exhausted) | `DEAD_LETTER` (surfaced as `failed`) | — (`error` carries the reason) |
| A later import replaced the graph before this job could commit (see below) | `CANCELLED` (surfaced as `cancelled`) | `superseded` |

The middle row is deliberate: an environment without the optional ML extras —
which is what this repo's own CI runs against — must not report a successful
graph replace as a failed import. The graph is fully valid without embeddings;
semantic search degrades to name-based matching, exactly as it already does
for an unreadable embedding sidecar. An `ImportError` therefore completes the
job (with a message pointing at `scripts/generate_embeddings.py`), while any
other exception goes through `fail()` — real retryable failure, not a permanent
environment fact.

The last row is not a failure either: a job that has fallen behind a later
import — its own (potentially minutes-long) encode outlasted by a second
`POST /import`, or a crash-recovered job resumed after one — must not write
stale embeddings over the newer graph's content, nor report itself as
`succeeded` for work it never actually committed, nor be retried as `failed`
(there is nothing wrong to retry; it is simply obsolete). `cancel` is the one
terminal state that already means neither of those, so the worker uses it, with
`result.message` explaining why. `ExecutionStore.cancel` was extended to accept
an optional `result`, exactly like `complete`'s, to carry that explanation. See
`backend/agents/execution/import_worker.py`'s generation guard and
`GraphStorage.commit_generation_embeddings`.

The graph replace itself is **never** rolled back because of an embeddings
outcome of any kind: by the time embeddings run, the graph has already been
durably committed and is independently valid data.

### 7. A degraded outcome when embeddings never even start

Enqueuing the embedding job and starting its drain thread happen right after
`replace_all_nodes_and_edges` has already succeeded. If either of those two
steps itself fails (e.g. the durable job store cannot be reached), the import
is still reported as `success: true` / `graph_replaced: true` — the graph
genuinely was replaced — but with `job_id: null` and
`embeddings_status: "not_started"`, plus an `embeddings_message` explaining
what to do (re-run `scripts/generate_embeddings.py`, or import again once the
job store is reachable). Raising instead would read to a caller as the import
itself having failed, and the natural response — retrying the import — would
perform another full graph replace to fix a problem that has nothing to do
with the graph.

## Consequences

- `POST /import` and `GET /import/{job_id}` join `GET /export` under the same
  router registration, sharing its authorization path
  (`GRAPH_ACTION_MUTATE`) — a caller whose access is narrowed to a subset of
  the graph (`GraphAccessNarrowing.enabled`) is refused outright, because a
  REPLACE cannot honor "leave what I can't see alone".
- `import_jobs.db` is a new SQLite file next to `graph.json` in every
  deployment; `IMPORT_JOBS_DB` overrides its location the same way
  `EMBEDDINGS_FILE` overrides the sidecar's.
- No MCP tool was added in this slice. `POST /import` accepts an arbitrary
  JSON body sized like a whole graph export, which does not fit MCP's
  tool-call parameter model well; REST-only is the scoped choice here, and MCP
  parity (if ever wanted) is a separate slice.
- Merge-mode import, live-session/event-stream notification of a replace (a
  connected browser session does not currently learn that the graph under it
  changed), and a dedicated CLI import script are explicitly out of scope —
  see the corresponding Task nodes on the Corp graph.
