# ADR 0007 — Vector-aware export/import archive

- **Status:** Accepted
- **Date:** 2026-09-23
- **Scope:** Open-source core only
- **Related:** [ADR 0006](0006-graph-import-replace-mode.md) (the REPLACE
  import pipeline this reuses), [`DATA_MANAGEMENT.md`](../DATA_MANAGEMENT.md)
  ("Importing a vector-aware archive", "Embedding Sidecar"),
  `backend/core/embedding_sidecar.py` (the binary format this archive embeds
  verbatim)

## Context

`POST /import` (ADR 0006) always drops the graph's existing embeddings and
regenerates them from scratch on a background job — a deliberate simplicity,
but a real cost: on a large graph, or without the optional ML extras
installed at all, a restore can leave semantic search degraded for minutes or
indefinitely. Every vector that already existed at export time is thrown away
and, best case, recomputed byte-for-byte differently (a different model
version, or float rounding from a different run) from the ones that were
actually live before the export.

The task this ADR answers asked for the plain `graph.json` interchange to stay
exactly as it is — it already serves "lightweight interchange" well — and for
an *additional*, optional archive format that also carries the embeddings, so
a restore between two instances running the same embedding model does not
need to pay the regeneration cost or accept the drift at all.

## Decision

### 1. Purely additive: two new endpoints, the old ones untouched

`GET /export/archive` and `POST /import/archive` are new routes, registered
alongside the existing `/export` and `/import`. Neither existing endpoint's
code path changed at all — this ADR adds a second, parallel format; it does
not touch the first one's guarantees, request/response shape, or tests. See
`backend/service/tests/test_import_archive_rest_endpoints.py`, which includes
a small regression check to that effect (the full behavioural coverage for
the plain endpoints is ADR 0006's own suite, `test_import_rest_endpoints.py`,
unmodified by this change).

### 2. Archive layout: three members, one of them always optional

A ZIP with:

- `graph.json` — always present, identical in shape to what `GET /export`
  returns.
- `embeddings.bin` — the *exact* bytes `backend/core/embedding_sidecar.py`'s
  binary writer produces for the live vectors, included verbatim. This ADR
  does not invent a second serializer: `FileEmbeddingSidecar.save`'s
  byte-building logic was pulled out into a pure function,
  `serialize_sidecar_bytes`, and the archive builder calls that same
  function. Reading works the same way in reverse (`parse_sidecar_bytes`).
  Present only when the exporting instance actually has vectors; omitted
  entirely otherwise, rather than shipping a valid-but-empty sidecar.
- `manifest.json` — schema version, export date, `embedding_model` +
  `embedding_dimension` (read from the live `VectorStore` at export time — see
  below), per-member SHA-256 checksums, and node/edge counts. Only for the
  members actually present.

### 3. `embedding_dimension` did not exist anywhere before this — where it now comes from

Nothing in this codebase tracked embedding dimensionality as a first-class
fact before this archive: `VectorStore.dimension` already existed (read off
the width of whatever vectors happen to be loaded), but nothing persisted it
anywhere. `views.export_graph_archive` reads it directly off the live
`VectorStore` at export time and writes it into the manifest — the archive is
the first place this value is recorded.

### 4. Compatibility is judged on `embedding_model` name equality, not a separately-computed live dimension

The obvious symmetric check would be "does the archive's `embedding_dimension`
match the *importing* instance's current dimension" — but an instance with no
vectors loaded yet has no current dimension to compare against
(`VectorStore.dimension` returns `None` when the index is empty), which is
exactly the common case for an import target. Comparing against
`VectorStore.model_name` instead works whether or not the target already has
vectors, because a fixed embedding model produces a fixed output width
deterministically — there is no such thing as the same named model producing
two different widths. `embedding_dimension` in the manifest is kept as
informational metadata (and is implicitly cross-checked anyway: an
`embeddings.bin` whose actual row width disagrees with what its own header
claims fails to parse as a sidecar at all, independent of the manifest).

### 5. Checksums are enforced only for a member the manifest actually names — degrade, don't reject, for everything else

Every checksum `manifest.json` declares is verified against the member's
actual bytes before anything is decoded for use
(`graph_archive.extract_archive`). A mismatch — on `graph.json` **or**
`embeddings.bin` — rejects the **whole** archive with
`archive_integrity_failed` before any write to the live graph, even though a
tampered `embeddings.bin` alone would not, on its own, invalidate the graph
content. This is a deliberate choice, not the narrowest possible one: a
checksum promise that has been broken is evidence the archive was corrupted
or tampered with, and trusting an *unrelated* part of the same archive at
that point is not a risk worth taking for a same-machine restore operation.

Conversely, a member with **no** checksum entry in the manifest — including
every member of an archive that has no `manifest.json` at all — has made no
integrity promise to break. This is what lets a hand-made "someone zipped a
plain `graph.json` export themselves" archive import cleanly: there is
nothing to verify, so nothing is rejected on that basis; the graph content
still goes through the same validation the plain `/import` endpoint always
runs, and embeddings simply fall back to the async regeneration job. An
unreadable or shape-invalid `manifest.json` (bad JSON, or missing a field this
format relies on) is treated exactly the same way as a missing one — it
carries no usable checksum promise either, so it degrades rather than
rejecting.

The corollary: a manifest that declares a checksum for a member the archive
does not actually contain is treated as a broken promise too (rejected, not
degraded) — an archive's own manifest asserting something the archive's
contents contradict is exactly the shape corruption or tampering takes.

### 6. Reusing ADR 0006's pipeline, not a second one

`import_service.py`'s validate → backup → replace sequence was split out of
`import_graph` into `_validate_backup_replace`, used by both `import_graph`
(plain) and the new `import_graph_archive`. The archive's `graph.json` member
goes through **exactly** the same `validate_import_document` /
`replace_all_nodes_and_edges` call as a plain `POST /import` body — same
validation errors, same pre-import backup, same atomic replace, same
`generation` counter. Only what happens to embeddings *afterward* differs
between the two callers:

- **Plain `import_graph`**: always enqueues the async regeneration job (ADR
  0006's existing behaviour, unchanged).
- **`import_graph_archive`, compatible case**: restores the archive's vectors
  directly with `GraphStorage.commit_generation_embeddings` — the same
  generation-checked commit path the async worker already uses for its own
  result, called synchronously right after the replace instead of from a
  background job. No `ExecutionJob` is created at all for this path
  (`job_id: null`, `embeddings_status: "restored"`). Vectors are filtered to
  the node ids the import actually committed, so a sidecar entry for a node id
  that did not make it into this document (never possible from this ADR's own
  exporter, but a hand-crafted archive could try) is silently dropped rather
  than sitting unused in the index.
- **`import_graph_archive`, incompatible case**: falls back to the identical
  enqueue-and-drain call the plain path uses, with an added
  `embeddings_message` explaining *why* it is regenerating rather than
  restoring (a model mismatch, a missing archive member, or an unreadable
  manifest) — never just a bare "queued" that leaves the caller to guess.

A vanishingly rare third outcome: `commit_generation_embeddings` itself
refuses the compatible-path restore (its own generation check fired because a
second import raced this one between the replace and the restore, a window
the two calls do not share a lock across). This is treated exactly like an
incompatible archive — fall back to async regeneration against the graph as
it now actually stands — rather than silently dropping the vectors or
reporting a failure for a graph that, in fact, imported successfully.

### 7. Multipart upload, size-capped like the JSON path

`POST /import/archive` takes the ZIP as a standard FastAPI
`UploadFile`/`File(...)` multipart body (the same pattern
`backend/ui/rest_api.py`'s document upload already uses), capped at 200 MB —
the same request-size-sanity role `MAX_IMPORT_NODES`/`MAX_IMPORT_EDGES` play
for the plain JSON body, not a product limit.

## Consequences

- `backend/core/embedding_sidecar.py` gained two pure functions,
  `serialize_sidecar_bytes` and `parse_sidecar_bytes`, extracted from
  `FileEmbeddingSidecar.save`/`_read` with no behaviour change (both are
  exercised by the pre-existing sidecar test suite, unmodified, plus the new
  archive tests). This is what keeps the archive's `embeddings.bin` and the
  on-disk sidecar file byte-for-byte the same format rather than two things
  that happen to agree today.
- `import_service.py`'s `import_graph` was refactored (not behaviourally
  changed — see the regression coverage) to share `_validate_backup_replace`
  with the new `import_graph_archive`, rather than duplicating ADR 0006's
  validate/backup/replace sequence.
- No MCP tool was added for either new endpoint, for the same reason ADR 0006
  gave for `/import`: a multipart/binary body does not fit MCP's tool-call
  parameter model. REST-only is the scoped choice here.
- Schema/forward-compatibility of `manifest.json` itself is not versioned
  beyond carrying a `schema_version` string this code does not yet branch on
  — a real `schema_version` migration path is out of scope for this slice and
  would be a follow-up if the manifest shape ever needs to change
  incompatibly.
