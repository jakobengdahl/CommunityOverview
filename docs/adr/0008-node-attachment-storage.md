# ADR 0008 — Node attachment storage layer and metadata index

- **Status:** Proposed
- **Date:** 2026-09-29
- **Scope:** Open-source core only
- **Related:** [`PERSISTENCE_BACKENDS.md`](../PERSISTENCE_BACKENDS.md) (the
  graph persistence seam this deliberately stays beside, not inside),
  [`DATA_MANAGEMENT.md`](../DATA_MANAGEMENT.md) ("Directory Structure",
  "Mutation History"), [ADR 0006](0006-graph-import-replace-mode.md) and
  [ADR 0007](0007-vector-aware-export-archive.md) (import/export this has to
  coexist with), `backend/core/storage_backends.py`,
  `backend/core/history_store.py`, `backend/core/image_ingest.py`

## Context

Users want to attach supporting files (a PDF, a spreadsheet, a scanned
agreement) directly to a node, and to get them back later from the node's
view. Files must live in their own storage area, separate from the graph
document, and they must be backed up and restorable independently of it.

Several pieces of work build on the answer to "where do the bytes go, what
records them, and how does a node know it has any":

1. a per-node-type `allows_attachments` flag in the schema config;
2. REST endpoints to upload, list, download and delete a node's files;
3. an attachments panel in the node detail view;
4. an operator backup of the attachment area and a restore routine that
   validates it against the graph.

This ADR fixes that shape so those can proceed without a further design
round. It does not implement anything.

### What the code does today that constrains the answer

- **The persistence seam is graph-shaped.** `GraphPersistenceBackend` and
  `IncrementalGraphPersistenceBackend` (`backend/core/storage_backends.py`)
  load and save nodes, edges and graph metadata — `load_graph_data`,
  `save_graph_data`, `upsert_node`, `apply_batch` and so on. Nothing in the
  contract moves opaque bytes, and `GraphStorage` keeps the whole graph in
  memory, so anything routed through it is held in memory too.
- **Sidecars exist for the file backend alone.** `GraphStorage` creates the
  embedding sidecar (`<stem>.embeddings.bin`) and the history sidecar
  (`<stem>.history.ndjson`) only when the backend is a
  `FileGraphPersistenceBackend` (`_init_embedding_sidecar`,
  `_init_history_store`). With the PostgreSQL backend
  (`GRAPH_BACKEND=postgres`) there is no history sidecar at all, and vectors
  travel inline in the node payload. A design that copied the sidecar pattern
  literally would give the PostgreSQL backend no attachments.
- **Node ids are not path-safe.** `Node.id` is a free string (a UUID by
  default, but any caller-supplied id is accepted), and federated nodes carry
  ids of the form `federated::<graph_id>::<origin_node_id>`
  (`backend/federation/manager.py`). A node id cannot be used as a path
  segment as-is.
- **Node metadata travels everywhere.** Whatever sits in `node.metadata` is
  written to `graph.json`, returned by `GET /export`, recorded in mutation
  history, and — for a graph another instance federates — copied wholesale
  into the consumer's cached node (`metadata = dict(source_node.get("metadata")
  or {})` in `FederationManager`). Lexical search does not read metadata
  (`storage_search` matches name, description, summary, tags, subtypes,
  aliases and type), so putting file names there would not make them
  searchable either.
- **Schema config ignores unknown keys.** `NodeTypeConfig`
  (`backend/config/config_loader.py`) is a Pydantic model with the default
  extra-field policy, so an `allows_attachments` key added to
  `schema_config.json` today is silently dropped. The flag needs a declared
  field, not just a config edit.
- **There is prior art for untrusted uploads.** `backend/core/image_ingest.py`
  validates image bytes by decoding them (never by trusting a declared content
  type), caps the raw source before decoding, and rejects SVG because it can
  carry script. `POST /import/archive` caps its upload at 200 MB, but it does
  so after `await file.read()` has already buffered the whole body.
- **"Attachment" already means something else.** Annotation content carries
  an `attachment` field — the binding of an annotation to a node anchor
  (`docs/ANNOTATION_CONTRACT.md`). The two concepts share a word and nothing
  else; this ADR uses "node attachment" where the difference matters.

## Options considered

### Option A — File-backend sidecar, path-addressed by file name

Mirror the history sidecar: a directory beside `graph.json`
(`<stem>.attachments/`), one sub-directory per node, each file stored under
`<attachment_id>_<filename>`, and the list of a node's attachments kept in
`node.metadata.attachments`.

- **For:** smallest change; the layout is human-browsable; backup is "copy
  the directory".
- **Against:**
  - Exists only with the file backend, exactly like the other sidecars —
    a PostgreSQL-backed instance gets no attachments.
  - The user-supplied file name becomes part of a filesystem path. That is
    the path-traversal and encoding surface (`../`, NUL, reserved names on
    Windows, Unicode normalisation, length limits) that has to be defended at
    every reader, including operator scripts.
  - The node id is used as a path segment, which is unsafe (above).
  - Two sources of truth: the node's metadata list and the files on disk.
    There is no transaction spanning a graph write and a file write, so they
    drift on every partial failure.
  - The list in node metadata leaks through `GET /export` and federation as
    references the reader cannot resolve, and every upload rewrites the node
    and adds a history record for a change the user did not make to the node.

### Option B — Content-addressed blob store

Store each file under its SHA-256 (`blobs/ab/cdef…`), with a separate index
mapping `(node_id, attachment_id)` to the hash plus file name, size and type.

- **For:** integrity is intrinsic — the key *is* the checksum; identical
  files are stored once; a blob key carries no user input, so it is
  traversal-proof by construction.
- **Against:**
  - Deletion needs reference counting or a mark-and-sweep across every node's
    index; deleting a node can no longer simply remove its files, and a bug in
    the count deletes a file another node still points at.
  - Deduplication is a cross-node (and, in a shared store, cross-graph)
    equality oracle: whether an upload was already stored is observable
    through timing or storage accounting.
  - The expected volume is low. The storage saved by deduplication does not
    pay for the reference-counting machinery.

### Option C — Separate `AttachmentStore` seam, id-addressed, with recorded checksums (recommended)

A new, narrow storage seam beside the graph persistence seam — not an
extension of it. Blobs are addressed by server-minted ids; the metadata index
is the single source of truth for which files a node has; each record carries
the SHA-256 of its bytes. The file-backed default lives next to the graph
file, as the other sidecars do, but the seam is chosen independently of
`GRAPH_BACKEND`, so a PostgreSQL-backed graph can use it too.

- **For:**
  - Works with every graph backend; the graph backend contract is unchanged.
  - No user input reaches a storage key.
  - One source of truth (the index), so there is nothing to keep in step.
  - Deleting a node's attachments is deleting one prefix.
  - The recorded checksum gives backup and restore an integrity check without
    content addressing's deletion problem.
- **Against:**
  - A second seam to document and for a third-party backend to implement.
  - The file layout is less browsable than Option A (hashed directory names).
  - Duplicate uploads are stored twice.

## Decision

**Option C.** The rest of this section is the contract the dependent work
builds against.

### 1. The seam: `AttachmentStore`

A new module, `backend/core/attachment_store.py`, defines a protocol and a
file-backed default. It is deliberately not part of
`GraphPersistenceBackend`: that contract moves graph entities that
`GraphStorage` holds in memory, while attachment bytes must stream and never
be held whole.

```python
class AttachmentStore(Protocol):
    def list(self, graph: str, node_id: str) -> list[AttachmentRecord]: ...
    def get_record(self, graph: str, node_id: str, attachment_id: str) -> AttachmentRecord | None: ...
    def open_read(self, graph: str, node_id: str, attachment_id: str) -> BinaryIO: ...
    def put(self, graph: str, node_id: str, stream: BinaryIO, record: NewAttachment) -> AttachmentRecord: ...
    def delete(self, graph: str, node_id: str, attachment_id: str) -> bool: ...
    def delete_node(self, graph: str, node_id: str) -> int: ...
    def iter_nodes(self, graph: str) -> Iterator[str]: ...
```

- `graph` is the graph's name as the persistence backend reports it
  (`default_graph_name()`: the file stem for the file backend, the configured
  graph name for PostgreSQL), so one store can serve several graphs without
  their keys colliding.
- `put` reads the stream in chunks, enforces the size limits (§4) while it
  reads, computes the SHA-256 as it goes, and makes the blob and its index
  record visible together or not at all. A blob with no record is garbage; a
  record with no blob must never be observable.
- `iter_nodes` exists for reconciliation and restore validation (§8), not for
  request handling.
- A deployment that needs object storage or a database-held store implements
  the protocol; that backend is out of scope for the core. Selection follows
  the persistence backend's pattern: an `ATTACHMENT_BACKEND` setting
  (default `file`) read at boot, with an unknown value refused at boot.

### 2. Addressing: id-addressed keys, never user input

A blob's storage key is

```
<graph_key>/<node_key>/<attachment_id>
```

- `attachment_id` — a server-minted UUID4 (hex). Never client-supplied.
- `node_key` — the lowercase hex SHA-256 of the UTF-8 node id. Node ids are
  free strings and federated ids contain `::`; hashing them gives a fixed,
  path-safe segment for every id without an escaping scheme to get wrong. The
  real node id is kept in the index.
- `graph_key` — the same treatment for the graph name.
- The original file name is **metadata only**. It is never used to build a
  path, a key or a URL segment.

This is path-addressing on server-minted ids, not content-addressing:
deleting a node's files is deleting `<graph_key>/<node_key>/`, with no
reference counting. The SHA-256 is recorded in the index (§3) for integrity,
not used as the key.

### 3. The metadata index — the single source of truth

Each node with attachments has one index document, stored at
`<graph_key>/<node_key>/index.json` in the file backend:

```json
{
  "schema_version": 1,
  "graph": "graph",
  "node_id": "5f0c…",
  "attachments": [
    {
      "id": "3b1e9c0f4a7d4c8e9f2a6b5d4c3e2f1a",
      "filename": "agreement-2026.pdf",
      "size": 482113,
      "content_type": "application/pdf",
      "declared_content_type": "application/pdf",
      "sha256": "…",
      "uploaded_at": "2026-09-29T08:00:00Z",
      "uploaded_by": "…"
    }
  ]
}
```

- **How it relates to the node:** the node carries nothing. There is no
  list, count or pointer in `node.metadata`. "Which files does node X have"
  is answered by `AttachmentStore.list`. This keeps attachments out of
  `graph.json`, out of `GET /export`, out of mutation history, and out of
  federation (§6). Uploading or deleting a file does not change the node or
  its `updated_at`.
- `content_type` is the *sniffed* type (§5); `declared_content_type` is what
  the client sent, kept for diagnosis only and never served.
- `uploaded_by` is the actor the request's attribution resolves to, where one
  exists; otherwise it is omitted rather than invented.
- The index document is rewritten whole and atomically (temp file plus
  rename, the same pattern `FileGraphPersistenceBackend` uses for
  `graph.json`), under a per-node lock. Per-node documents keep that rewrite
  small, and a corrupted index damages one node rather than the graph.
- **Why this shape is enough for backup and restore:** each node directory is
  self-describing (its real node id, and a checksum per blob). A restore
  validator can check, with no other input than the graph, that every
  record's blob exists with a matching SHA-256 and that every `node_id` exists
  in the graph.

### 4. Size and count limits

Enforced by `put` while it streams, not after a whole-body read. The route
must not `await file.read()` the upload the way `POST /import/archive` does.

| Setting | Default | Meaning |
|---|---|---|
| `ATTACHMENT_MAX_FILE_BYTES` | 25 MiB | one file |
| `ATTACHMENT_MAX_FILES_PER_NODE` | 10 | files on one node |
| `ATTACHMENT_MAX_BYTES_PER_NODE` | 100 MiB | total bytes on one node |

- These are conservative defaults. An operator may raise or lower them. A
  value of `0` or less is refused at boot rather than read as "unlimited".
- A limit breach is `413` for the file-size case and `409` for the per-node
  count/total case, with a machine-readable `error` code in each case. No
  partial blob is left behind.

### 5. Type policy and content-type sniffing

- The type is determined from the bytes, as `image_ingest` does for images,
  never from the client's declared type or the file extension alone. The core
  ships a small signature table (magic bytes) rather than a dependency on a
  system library. For ZIP-container formats (OOXML and ODF documents), the
  detected container is confirmed by its expected internal member, and the
  extension then picks among the allowed container types.
- **Default allowlist:** PDF, PNG, JPEG, WebP, plain text, CSV, and the
  OOXML/ODF document, spreadsheet and presentation formats. Anything that
  sniffs to another type, or fails to sniff, is refused with `415`.
- **Always refused, whatever the allowlist says:** HTML, SVG, XML and any
  other type a browser may render as an active document, and executables.
  They are refused rather than served defensively, because a rule that is
  never relaxed cannot be misconfigured.
- The allowlist is configurable (`ATTACHMENT_ALLOWED_TYPES`) but cannot
  re-admit the always-refused set.
- Text types are stored as uploaded. Nothing re-encodes or rewrites a file:
  unlike images, an attachment is evidence, and the bytes returned must be the
  bytes received.

### 6. Serving files

Downloads are served by the application, never from a static file mount, so
authorization runs on every request. Every download response carries:

- `Content-Type:` the stored sniffed type. Plain text is sent as
  `text/plain; charset=utf-8`.
- `Content-Disposition: attachment; filename="<ascii fallback>";
  filename*=UTF-8''<percent-encoded name>`. Always `attachment`, never
  `inline`, so the browser saves the file rather than rendering it in the
  application's origin.
- `X-Content-Type-Options: nosniff`
- `Content-Security-Policy: default-src 'none'; sandbox`
- `Cache-Control: private, no-store`

The file name in the header is sanitised when served: control characters,
quotes, path separators and CR/LF are stripped, so a stored name cannot
inject a header. It is also sanitised on upload (§7).

### 7. API surface and authorization

The routes below are what the REST task implements. They belong under the
existing `/api` router.

| Method | Path | Authorization | Notes |
|---|---|---|---|
| GET | `/nodes/{node_id}/attachments` | read | list the index records (without `declared_content_type`) |
| POST | `/nodes/{node_id}/attachments` | mutate | multipart, one file per request |
| GET | `/nodes/{node_id}/attachments/{attachment_id}` | read | streams the bytes with the §6 headers |
| DELETE | `/nodes/{node_id}/attachments/{attachment_id}` | mutate | |

- Authorization uses the existing graph authorization hook
  (`GRAPH_ACTION_READ` / `GRAPH_ACTION_MUTATE` in
  `backend/runtime/authorization.py`), entered the same way as the other
  routes (`use_request_authorization`). It does not grow a second check.
- Upload is refused with `404` if the node does not exist, and with `422`
  (`error: "attachments_not_allowed"`) if the node's type does not have
  `allows_attachments: true` — a new declared field on `NodeTypeConfig`,
  default `false`, exposed by `get_schema`. `403` stays reserved for the
  authorization hook's denial, so the two causes stay distinguishable.
- Upload is also refused for federated nodes (ids starting `federated::`):
  they are read-only replicas of another graph's nodes.
- On upload the file name is normalised to NFC, stripped of path components
  and control characters, and capped at 255 bytes. An empty result becomes
  `attachment`.
- No MCP tool is added in this slice: multipart bodies do not fit MCP's
  tool-call parameter model, the same reason ADR 0006 and ADR 0007 gave for
  their REST-only endpoints. A read-only listing tool can follow later
  without changing this design.

### 8. Lifecycle: delete, archive, import

- **Node archived:** attachments are untouched. Archiving hides the node from
  search and traversal and is reversible, so its attachments must survive it.
- **Node deleted:** the service calls `AttachmentStore.delete_node` after the
  graph delete has succeeded, never before. A crash between the two leaves an
  orphan directory, never a live node that has lost its files.
- **Graph import in REPLACE mode (ADR 0006):** the attachment store is not
  touched. Nodes that do not survive the import leave orphans. They are not
  deleted automatically, because the import may be the restore of a backup
  that brings those nodes back. A reconcile routine, built on `iter_nodes`,
  reports orphans and deletes them only when explicitly asked to.

### 9. Federation

Attachments are not federated. Because nothing about them lives in
`node.metadata`, `FederationManager`'s copy of a remote node's metadata
carries no dangling reference. The consumer's node detail view shows no
attachment panel for a federated node. Exposing a remote graph's attachment
list, or proxying its downloads, would need its own design for cross-instance
authorization and is out of scope here.

### 10. Export and backup

- `GET /export` and `graph.json` are unchanged. They never contained
  attachments and do not start to.
- Adding attachments to the ADR 0007 archive, as an optional
  `attachments/` member set covered by the manifest's per-member SHA-256
  checksums, is a natural extension but is **not** part of this decision. Such
  an archive would grow with every file ever attached, which is the wrong
  default for an export.
- Operator backup copies the file backend's attachment root as-is. Every
  index record carries the checksum a restore validator needs (§3).
  Timestamped, write-once copies follow the same pattern as a graph snapshot
  backup, but run on their own schedule, since the two differ in size and in
  how often they change.

### 11. File-backend layout

```
data/active/
  graph.json
  graph.journal.ndjson
  graph.embeddings.bin
  graph.history.ndjson
  graph.attachments/                  # ATTACHMENT_DIR overrides
    <graph_key>/<node_key>/index.json
    <graph_key>/<node_key>/<attachment_id>
    .tmp/                             # in-flight uploads; swept at start-up
```

- Uploads stream into `.tmp/`, on the same filesystem, and are renamed into
  place before the index is rewritten, so a crash leaves at most a temp file.
- With the PostgreSQL graph backend there is no graph file to sit beside, so
  `ATTACHMENT_DIR` is required and is refused at boot if unset.
- Several instances sharing one PostgreSQL graph must share the attachment
  root, or use a backend that is itself shared. A per-instance local
  directory would silently give each instance a different set of files.

## Consequences

- **Purely additive.** There is a new module, new routes, and a new schema
  field that defaults to `false`, so no node type gains attachments until
  someone enables them. The graph persistence contract, `graph.json`,
  `GET /export`, the archive format and federation are all unchanged.
- **Rollback:** set `allows_attachments` back to `false` (or deploy a build
  without the routes). Uploads stop. Stored files stay inert on disk, and
  because the graph never references them, an older build loads the graph
  unchanged. Re-enabling brings the same files back.
- **Moving to another backend:** list, read and put through the seam, one
  node at a time, then verify the SHA-256 of each record. There is no graph
  migration, because the graph holds nothing to rewrite.
- **Search.** Surfacing attachment file names in graph search means querying
  the index; they do not appear by indexing `node.metadata`. That is a
  follow-up, not part of this slice. Content indexing of file bodies is
  explicitly not planned here.
- **Naming.** Code for this feature uses `AttachmentStore` /
  `node attachment`. The annotation `content.attachment` binding keeps its
  name. The two do not share code.
- **Docs to update when the implementation lands:** `backend/DEVELOPMENT.md`
  (endpoint table), `docs/DATA_MANAGEMENT.md` (directory structure),
  `docs/PERSISTENCE_BACKENDS.md` (the second seam, and its PostgreSQL
  requirement), `docs/PROFILES.md` (`allows_attachments`), and
  `docs/USER_GUIDE.md` (the panel).
