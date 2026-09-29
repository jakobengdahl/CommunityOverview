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
  carry script. `POST /import/archive` declares its body as an `UploadFile`,
  so Starlette's multipart parser has already spooled the whole upload to a
  temporary file before the handler runs. Its 200 MB check after
  `await file.read()` therefore bounds only what is pulled into memory, not
  what reaches disk. `_read_body_within_cap` (`backend/service/rest_api.py`)
  is the existing precedent for refusing an oversized body from its
  `Content-Length` before reading it.
- **Graph identity is thin.** `default_graph_name()` is the file stem for
  the file backend. For PostgreSQL it is the constructor's `graph_name`,
  which `build_persistence_backend` never sets, so it is always `"graph"`.
  What separates PostgreSQL graphs is the schema (`GRAPH_POSTGRES_SCHEMA`:
  one database can hold several graphs, one per schema). Inside one schema,
  the optional row scope (`GRAPH_POSTGRES_SCOPE`) separates instances, and an
  unscoped instance sees only the rows that carry no scope
  (`PERSISTENCE_BACKENDS.md`).
- **Adoption makes a local copy.** Adopting a federated node creates a new
  local node with its own id, plus a local reference node that keeps the
  `federated::…` id (`adopt_federated_node` in
  `backend/service/mutations.py`).
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
  - No user input is used to build a storage key: an attachment id taken
    from a URL is only ever looked up in the index.
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

A new module, `backend/core/attachment_store.py`, defines a protocol, its
record types and errors, and a file-backed default. It is deliberately not
part of `GraphPersistenceBackend`. That contract moves graph entities that
`GraphStorage` holds in memory, while attachment bytes must stream and must
never be held whole.

```python
@dataclass(frozen=True)
class AttachmentRecord:
    id: str                      # 32 lowercase hex chars, server-minted (uuid4().hex)
    node_id: str
    filename: str                # sanitised display name (§7); never a path
    size: int                    # bytes
    content_type: str            # sniffed (§5)
    declared_content_type: str   # as sent by the client; diagnostic, never served
    sha256: str                  # lowercase hex of the stored bytes
    uploaded_at: str             # ISO-8601 UTC
    uploaded_by: Optional[str]   # actor id when the request has one, else None

@dataclass(frozen=True)
class NewAttachment:
    filename: str                # already sanitised by the service layer
    declared_content_type: str
    content_type: str            # already sniffed by the service layer
    uploaded_by: Optional[str]

@dataclass(frozen=True)
class AttachmentLimits:
    max_file_bytes: int
    max_files_per_node: int
    max_bytes_per_node: int

class AttachmentStore(Protocol):
    def list(self, namespace: str, node_id: str) -> list[AttachmentRecord]: ...
    def get_record(self, namespace: str, node_id: str, attachment_id: str) -> AttachmentRecord | None: ...
    def open_read(self, namespace: str, node_id: str, attachment_id: str) -> BinaryIO: ...
    async def stage(self, namespace: str, node_id: str, chunks: AsyncIterator[bytes], max_bytes: int) -> StagedUpload: ...
    async def commit(self, staged: StagedUpload, record: NewAttachment, limits: AttachmentLimits,
                     precondition: Callable[[], None]) -> AttachmentRecord: ...
    def discard(self, staged: StagedUpload) -> None: ...
    def delete(self, namespace: str, node_id: str, attachment_id: str) -> bool: ...
    def delete_node(self, namespace: str, node_id: str) -> int: ...
    def iter_nodes(self, namespace: str) -> Iterator[str]: ...
```

Upload is two-phase, so type policy stays out of the store:

1. `stage` writes the incoming chunks to a temporary location, computing the
   size and the SHA-256 as it goes. It raises `AttachmentTooLarge` as soon as
   more than `max_bytes` have arrived, and deletes the partial file. The
   returned `StagedUpload` exposes `size`, `sha256` and `open()`, which reads
   the staged bytes back.
2. The service layer sniffs the staged bytes (§5). A refusal raises
   `UnsupportedAttachmentType` from the sniffing module, and the service calls
   `discard`.
3. `commit` takes the per-node lock and first calls `precondition()`. The
   service passes a callback that re-reads the node from `GraphStorage` and
   raises `AttachmentNodeGone` if it no longer exists, or
   `AttachmentNodeArchived` if it is now archived. The route maps these to
   `404` and to `409 node_archived`, and the staged file is discarded.
   `commit` then re-reads the node's index and re-checks the count and total
   limits against the index *as it now stands*. That check is what stops two
   concurrent uploads that each passed an earlier check from exceeding the
   limit together. On a breach it raises `AttachmentLimitExceeded` and
   discards the staged file. Otherwise it moves the blob into place and then
   rewrites the index (§11).

Rules for readers and deleters:

- `get_record`, `open_read` and `delete` resolve `attachment_id` **only**
  through the node's index. An id that has no index record is "not found",
  whatever exists on disk.
- A record with no blob must never be observable. `delete` therefore rewrites
  the index without the record first, and only then removes the blob.
- `delete` and `delete_node` take the same per-node lock as `commit`.
  Within one instance, a commit racing a node delete therefore either
  completes before the directory is removed (and is removed with it), or
  finds the node gone in its precondition.
- Across instances, the precondition reads that instance's in-memory graph,
  which learns of another instance's delete only asynchronously. A commit
  can therefore land just after another instance's `delete_node` and
  recreate the directory. The result is an orphan node directory (§8): a
  file the graph no longer references, never a live node that has lost its
  files.
- `iter_nodes` exists for reconciliation and validation (§8, §10), not for
  request handling.

`namespace` separates graphs that share one attachment root. It is the
`ATTACHMENT_NAMESPACE` setting, which defaults to:

- **file backend:** the graph file's stem, the persistence backend's
  `default_graph_name()`;
- **PostgreSQL backend:** `pg:<GRAPH_POSTGRES_SCHEMA>`, because the schema,
  not the graph name (always `"graph"` today), is what tells PostgreSQL
  graphs apart.

**One namespace per graph.** Every graph whose files share an attachment
root must have a distinct namespace. Where the defaults would collide — two
file-backed graphs with the same stem, or two databases that use the same
schema name — the operator sets `ATTACHMENT_NAMESPACE` explicitly. A graph
keeps its namespace for life, because changing it detaches every stored
file.

**Why the row scope is not part of the namespace.** Node `id` is the primary
key of the whole table in a schema, not of one scope
(`CrossScopeWriteRefused` in `backend/core/postgres_backend.py`), so node
ids cannot collide between scopes. A row with no scope is shared, and a
scope-qualified namespace would give one shared node a separate attachment
set per scope. Isolation between scopes comes from §7 instead: every route
resolves the node through the instance's own storage and visibility check
before it touches the store, so an instance never reaches the files of a
node it cannot see.

Backend selection follows the persistence backend's pattern:

- An `ATTACHMENT_BACKEND` setting (default `file`) is read at boot.
- An unknown value is refused at boot.
- A deployment that needs object storage, or a database-held store,
  implements the protocol. That backend is out of scope for the core; a
  deployment may provide a different backend.

### 2. Addressing: id-addressed keys, never user input

In the file backend, a blob lives at

```
<namespace_key>/<node_key>/blobs/<attachment_id>
```

and the node's index at `<namespace_key>/<node_key>/index.json`. The index
sits outside the `blobs/` directory, so no attachment id can name it.

- `attachment_id` — minted by the server (`uuid4().hex`), never supplied by
  the client. Every route validates a path id against `^[0-9a-f]{32}$`, and
  anything else is `404` before the store is called. The store then still
  resolves the id only through the index (§1). Either check alone would stop
  traversal; both are required.
- `node_key` — the lowercase hex SHA-256 of the UTF-8 node id. Node ids are
  free strings and federated ids contain `::`, so hashing gives every id a
  fixed, path-safe segment without an escaping scheme to get wrong. The real
  node id is kept in the index.
- `namespace_key` — the same treatment for the namespace (§1).
- The original file name is **metadata only**. It is never used to build a
  path, a key or a URL segment.

This is path-addressing on server-minted ids, not content-addressing.
Deleting a node's files is deleting `<namespace_key>/<node_key>/`, with no
reference counting. The SHA-256 is recorded in the index (§3) for integrity;
it is not used as the key.

### 3. The metadata index — the single source of truth

Each node with attachments has one index document:

```json
{
  "schema_version": 1,
  "namespace": "graph",
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
      "uploaded_by": null
    }
  ]
}
```

- **How it relates to the node:** the node carries nothing — no list, no
  count, no pointer in `node.metadata`. "Which files does node X have" is
  answered by `AttachmentStore.list`. This keeps attachments out of
  `graph.json`, `GET /export`, mutation history and federation (§9).
  Uploading or deleting a file does not change the node or its `updated_at`.
- **Types:** `content_type` is the sniffed type (§5). `declared_content_type`
  is what the client sent, kept for diagnosis only and never served.
- **Writes:** the index is rewritten whole and atomically (temp file plus
  rename, the pattern `FileGraphPersistenceBackend` uses for `graph.json`),
  under the per-node lock.
- **Why per node:** each rewrite stays small, and a corrupted index damages
  one node rather than the graph.
- **Self-describing:** each node directory records its real node id and a
  checksum per blob. That is all a validator needs (§10).

### 4. Size and count limits

| Setting | Default | Meaning |
|---|---|---|
| `ATTACHMENT_MAX_FILE_BYTES` | 25 MiB | one file |
| `ATTACHMENT_MAX_FILES_PER_NODE` | 10 | files on one node |
| `ATTACHMENT_MAX_BYTES_PER_NODE` | 100 MiB | total bytes on one node |

These are conservative defaults, and an operator may change them. A value of
`0` or less is refused at boot rather than read as "unlimited".

Enforcement happens at four points:

1. **`Content-Length` pre-check.** The upload route reads the raw request
   body, not an `UploadFile` (§7). When `Content-Length` is present and
   exceeds the smaller of the per-file limit and the node's remaining byte
   budget, the route answers before reading anything, as
   `_read_body_within_cap` does.
2. **Count pre-check.** When the node already has
   `ATTACHMENT_MAX_FILES_PER_NODE` files, the upload is refused before any
   bytes are read.
3. **While streaming.** `stage` is given the same smaller cap as its
   `max_bytes`. A missing or false `Content-Length` is therefore still caught
   while the body arrives.
4. **At commit.** The count and total are checked again under the lock
   (§1).

When the pre-check or `stage` trips, the service reports the cap that was
the smaller of the two:

- the per-file limit gives `413 attachment_too_large`;
- the node's remaining byte budget gives `409 node_attachment_limit`;
- when the two are equal, it is reported as the per-file limit.

| Breach | Status | `error` |
|---|---|---|
| per-file size | `413` | `attachment_too_large` |
| per-node total | `409` | `node_attachment_limit` |
| per-node count | `409` | `node_attachment_limit` |

No partial blob is left behind in any of these cases.

### 5. Type policy and content-type sniffing

The type is determined from the bytes, never from the client's declared type
alone — the same stance `image_ingest` takes for images. The core ships a
small detection module, not a dependency on a system library.

| Accepted type | How it is detected |
|---|---|
| `application/pdf` | leading `%PDF-` |
| `image/png`, `image/jpeg`, `image/webp` | their signatures |
| `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | ZIP signature; central directory has `[Content_Types].xml` and `word/document.xml` |
| `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | ZIP signature; `[Content_Types].xml` and `xl/workbook.xml` |
| `application/vnd.openxmlformats-officedocument.presentationml.presentation` | ZIP signature; `[Content_Types].xml` and `ppt/presentation.xml` |
| `application/vnd.oasis.opendocument.text` | ZIP signature; the `mimetype` member's content is exactly this string |
| `application/vnd.oasis.opendocument.spreadsheet` | same, with this string |
| `application/vnd.oasis.opendocument.presentation` | same, with this string |
| `text/plain`, `text/csv` | no signature exists, so by rule (below) |

**The archive decides.** For the ZIP-based types, the archive's contents
decide the type, and the file extension is ignored. An OOXML archive
containing more than one of the three main parts is refused.

**The text rule.** The whole staged file must be valid UTF-8, with a leading
BOM allowed. It must contain no NUL byte. Its first non-whitespace character
(after any BOM) must not be `<`. The extension then picks the type: `.csv`
gives `text/csv`; anything else gives `text/plain`.

- The `<` test is what keeps HTML and XML out of the text types: both start
  with `<` in any form a browser would render.
- Text in another encoding is refused rather than served under a wrong
  charset.

**Refusals.** Anything that matches no row is refused with `415`
(`error: "unsupported_attachment_type"`). That covers HTML, SVG, XML,
executables, and bare ZIP or other archives. Refusing them, rather than
serving them defensively, keeps the rule impossible to misconfigure.

**Allowlist setting.** `ATTACHMENT_ALLOWED_TYPES` narrows the table. Its
value is a comma-separated list of MIME types from the left column; the
default is all of them. A value outside the table is refused at boot, so the
setting can never re-admit a type the table does not detect.

**No re-encoding.** Nothing re-encodes or rewrites a file. Unlike an image
annotation, an attachment is evidence, and the bytes returned must be the
bytes received.

### 6. Serving files

Downloads are streamed by the application, never from a static file mount,
so authorization runs on every request. Every download response carries:

- `Content-Type:` the stored sniffed type; the text types carry
  `; charset=utf-8`, which §5 guarantees is true.
- `Content-Disposition: attachment; filename="<ascii fallback>";
  filename*=UTF-8''<percent-encoded name>`. It is always `attachment`, never
  `inline`, so the browser saves the file rather than rendering it in the
  application's origin.
- `X-Content-Type-Options: nosniff`
- `Content-Security-Policy: default-src 'none'; sandbox`
- `Cache-Control: private, no-store`

The ASCII fallback replaces every non-ASCII character, `"` and `\` with `_`.
The stored name contains no control characters, CR or LF (§7), so it cannot
inject a header.

### 7. API surface, authorization and per-route behaviour

These are the routes the REST task implements, under the existing `/api`
router:

| Method | Path | Action / target | Success |
|---|---|---|---|
| GET | `/nodes/{node_id}/attachments` | read / `list_node_attachments` | `200` `{attachments: [...], limits: {...}, uploads_allowed: bool}` |
| POST | `/nodes/{node_id}/attachments?filename=<name>` | mutate / `add_node_attachment` | `201` with the new record |
| GET | `/nodes/{node_id}/attachments/{attachment_id}` | read / `get_node_attachment` | `200`, streamed, with the §6 headers |
| DELETE | `/nodes/{node_id}/attachments/{attachment_id}` | mutate / `delete_node_attachment` | `200` `{success: true}` |

**Records in responses.** The records omit `declared_content_type`. `limits`
reports the three §4 values; `uploads_allowed` is the upload rule below,
evaluated for this node.

**Upload body.** The request body is the raw file bytes; it is not
multipart. The declared type is the request's `Content-Type` header, and the
name is the `filename` query parameter. This is what lets §4 stream-check the
body. With multipart the framework spools the file before the handler runs,
which is the `POST /import/archive` problem.

**Authorization.** Each route runs its service call inside
`use_request_authorization(headers=request.headers)`, as the existing routes
do. That call only binds the request's inputs. The decision is made in the
service layer by `access.evaluate_graph_access(hook, action=…, target=…)`,
with the action and target from the table above. The service then resolves
the node with `storage.get_node` and requires
`access.is_node_visible(node, decision.graph_access)`, exactly as
`queries.get_node_details` does.

- A denied decision is `403`, mapped with `_raise_for_access_denied`.
- An invisible node is indistinguishable from a missing one.

**Per-route behaviour.** Checks run top to bottom, and the first that
applies wins:

| Condition | List | Download | Upload | Delete |
|---|---|---|---|---|
| authorization denied | `403` | `403` | `403` | `403` |
| node missing or not visible | `404` | `404` | `404` | `404` |
| id starts with `federated::` | `200`, empty, `uploads_allowed: false` | `404` | `422` `federated_node` | `404` |
| path `attachment_id` malformed or not in index | — | `404` | — | `404` |
| node type lacks `allows_attachments: true` | `200` | `200` | `422` `attachments_not_allowed` | `200` |
| node archived | `200` | `200` | `409` `node_archived` | `200` |
| otherwise | `200` | `200` | §4/§5 checks, then `201` | `200` |

**Why the flag gates only uploads.** Turning `allows_attachments` off must
not strand files a user already attached: they stay listable, downloadable
and deletable, so they can be retrieved or cleaned up.

**Federated nodes.** A `federated::…` node — a cached remote node, or the
local reference an adoption leaves — is a read-only replica of another
graph's node. The adopted local copy has its own id and is an ordinary local
node.

**The schema flag.** `allows_attachments` becomes a declared `bool` field on
`NodeTypeConfig`, default `false`, and `get_schema` returns it on each node
type.

**File-name sanitising.** On upload the service turns the `filename`
parameter into the stored name, in this order:

1. Normalise to NFC.
2. Keep only the part after the last `/` or `\`.
3. Remove control characters, CR and LF.
4. Strip surrounding whitespace.
5. Truncate to 255 UTF-8 bytes on a character boundary.

An empty result becomes `attachment`.

**No MCP tool in this slice.** A raw binary body does not fit MCP's tool-call
parameter model — the same reason ADR 0006 and ADR 0007 gave for their
REST-only endpoints. A read-only listing tool can follow later without
changing this design.

### 8. Lifecycle: delete, archive, import, orphans

- **Node archived:** attachments are untouched (§7 table). Archiving is
  reversible, so the files must survive it.
- **Node deleted:** the service calls `AttachmentStore.delete_node` after the
  graph delete has succeeded, never before. A crash between the two leaves an
  orphan node directory, never a live node that has lost its files.
- **Graph import in REPLACE mode (ADR 0006):** the attachment store is not
  touched. Nodes that do not survive the import leave orphan node
  directories. They are not deleted automatically, because the import may be
  the restore of a backup that brings those nodes back.
- **Three kinds of leftover:**
  - an *orphan node directory* — its `node_id` is not in the graph;
  - an *unindexed node directory* — a `<node_key>/` with blobs but no
    `index.json`. A crash between a node's first blob move and its first
    index write leaves one;
  - an *unindexed blob* — a file under `blobs/` with no index record. A crash
    between `commit`'s blob move and its index rewrite leaves one.
- **The start-up sweep** removes only entries in `.tmp/` whose modification
  time is more than one hour old (§11). An upload in flight on another
  instance that shares the root keeps writing, and so keeps its mtime fresh.
- **The reconcile routine** is the validator in §10. It reports all three
  kinds of leftover. It deletes them only when asked, under the guards in
  §10.

### 9. Federation

Attachments are not federated. Because nothing about them lives in
`node.metadata`, `FederationManager`'s copy of a remote node's metadata
carries no dangling reference. A federated node's list is empty and its
uploads are refused (§7).

Exposing a remote graph's attachment list, or proxying its downloads, would
need its own design for cross-instance authorization. It is out of scope
here.

### 10. Export, backup and the restore validator

**Export.** `GET /export` and `graph.json` are unchanged: they never
contained attachments and do not start to. Adding attachments to the ADR 0007
archive, as an optional `attachments/` member set covered by the manifest's
per-member checksums, is a possible extension but is **not** part of this
decision. Such an archive would grow with every file ever attached, which is
the wrong default for an export.

**The validator.** The core ships `scripts/validate_attachments.py` for the
file backend:

```
python scripts/validate_attachments.py --attachments <root> --namespace <ns>
                                       --graph <GET /export output> [--graph <...> ...]
                                       [--strict] [--prune] [--prune-orphan-nodes]
```

It walks every node directory in the one namespace given (the argument is
required, so one graph is never checked against another graph's files) and
classifies each finding:

| Class | Finding |
|---|---|
| **error** | an unreadable or schema-invalid `index.json` |
| **error** | a record whose blob is missing |
| **error** | a blob whose size or SHA-256 differs from its record |
| **error** | an index whose `node_id` does not hash to its directory name |
| **warning** | an orphan node directory |
| **warning** | an unindexed node directory, reported by its `node_key` |
| **warning** | an unindexed blob |

**Exit codes.** `0` when there are no errors, `1` when there are, `2` on a
usage error. `--strict` turns warnings into errors. Output is one line per
finding plus a summary, so a backup or restore job can log it verbatim.

**Why orphans are only warnings.** After a REPLACE import they are expected
(§8), and a restored attachment set is often newer or older than the graph it
is checked against.

**`--graph` may be repeated.** The orphan check uses the union of the node
ids in every `--graph` given. Each should be `GET /export` output from a
caller whose authorization is not narrowed. `GET /export` serves the
in-memory graph, so it includes mutations still sitting in the file
backend's journal, which `graph.json` alone may not yet contain.

For a PostgreSQL schema with row scopes, no single instance sees every
scope: an unscoped instance sees only unscoped rows. The operator therefore
passes one export per scope, plus one from an unscoped instance. A view that
misses nodes only produces false orphan *warnings*, which is why deleting
orphan node directories needs its own flag (below).

**Pruning.** It never touches anything classed as an error. Every deletion
takes the per-node lock (`<namespace_key>/.locks/<node_key>.lock`, §11),
re-checks the condition under the lock (still unindexed or still orphaned,
and still old enough), and only then deletes. A check made before the lock
is taken is never acted on. Pruning comes in two flags:

- **`--prune`** deletes unindexed blobs and unindexed node directories, and
  only when every file in them is more than one hour old. These need no
  graph at all to be judged, so they are safe whatever `--graph` covers.
- **`--prune-orphan-nodes`** implies `--prune` and additionally deletes
  orphan node directories. It deletes only those whose `index.json` was
  last modified before the reference time: the earliest `exportDate` among
  the `--graph` documents (the field `GET /export` writes).
  - File modification times of the `--graph` files are never used. A copied
    or re-downloaded export gets a newer mtime than its content.
  - If any `--graph` document has no `exportDate` (for example a raw graph
    file), the flag is refused as a usage error (exit `2`).
  - It is a separate, explicit opt-in because its safety depends on the
    `--graph` set being complete, which the validator cannot check.

**Backup.** An operator backup copies the attachment root to a timestamped,
write-once location, on its own schedule rather than with the graph's,
because the two differ in size and in how often they change. There is no
graph snapshot backup job in this repository to reuse; the job belongs to
whoever operates the deployment.

**A consistent copy.** Take it from a point-in-time volume or filesystem
snapshot where one is available. Otherwise copy in two passes — every
`index.json` first, then every `blobs/` directory — and run the validator on
the copy, with any current export as `--graph` (the retry decision below rests
only on error-class findings, which do not depend on the graph). The commit
and delete orderings in §1 mean a copy taken this way while writes continue
can hold only two kinds of inconsistency:

- extra unindexed blobs, or unindexed node directories created between the
  passes (warnings);
- a record whose blob was deleted between the passes (an error).

On an error, the job retries the copy at most twice more. If the third copy
still has errors, the job fails and keeps the validator output. A corrupt
index is not something a retry can fix.

**Restore.** Restore extracts into an empty directory, runs the validator
against the graph that will be live, and only then swaps the directory into
place. The graph is that graph's `GET /export`, or the restored file itself
while that graph is not yet served. A raw file carries no `exportDate`, so
it supports reporting but not `--prune-orphan-nodes`.

### 11. File-backend layout

```
data/active/
  graph.json
  graph.journal.ndjson
  graph.embeddings.bin
  graph.history.ndjson
  graph.attachments/                    # ATTACHMENT_DIR overrides
    <namespace_key>/<node_key>/index.json
    <namespace_key>/<node_key>/blobs/<attachment_id>
    <namespace_key>/.locks/<node_key>.lock
    .tmp/                               # staged uploads; stale ones swept at start-up
```

- `stage` writes into `.tmp/`, which is on the same filesystem, so `commit`'s
  move is an atomic rename. The rename happens before the index rewrite. A
  crash can therefore leave a staged file (removed by a later start-up sweep
  once stale) or an unindexed blob (reported by the validator, §8), but never
  a record without a blob.
- The per-node lock is an in-process lock plus an OS lock (`_lock_file`, as
  `history_store` uses) on `<namespace_key>/.locks/<node_key>.lock`. The lock
  file lives outside the node directory and is never deleted, so `delete_node`
  removing the directory cannot split the lock between two holders. Two
  processes sharing a root therefore never interleave commits and deletes on
  one node.
- With the PostgreSQL graph backend there is no graph file to sit beside.
  `ATTACHMENT_DIR` is therefore required, and is refused at boot if unset.
- All instances serving one PostgreSQL schema, whatever their row scopes, must
  share the attachment root, or use a backend that is itself shared. A
  per-instance local directory would silently give each instance a different
  set of files.

### 12. Node-view panel

The UI task builds against these rules:

- **Where:** the panel appears in the node detail dialog.
- **When it renders:** when the node's type has `allows_attachments: true`
  in `get_schema`, or when the list returns at least one attachment.
  Federated nodes never render it.
- **Upload control:** shown only when the list response has
  `uploads_allowed: true`.
- **Pre-checks:** the client checks the file size and the node's remaining
  count and bytes against `limits` before sending, and shows the same
  message the server's error would produce.
- **List rows:** file name, size and upload time. Each row has a download
  link (the GET route, so the §6 headers apply) and a delete action that asks
  for confirmation first.
- **Errors:** each `error` code maps to a message key:
  - `attachment_too_large`
  - `node_attachment_limit`
  - `unsupported_attachment_type`
  - `attachments_not_allowed`
  - `node_archived`
  - `federated_node`
  - a generic fallback
- **Strings:** all of them go under an `attachments.` prefix in both
  `frontend/web/src/i18n/en.json` and `sv.json`.

## Consequences

- **Purely additive.** There is a new module, new routes, a new script, and
  a new schema field defaulting to `false`. No node type gains uploads until
  someone enables them. The graph persistence contract, `graph.json`,
  `GET /export`, the archive format and federation are all unchanged.
- **Rollback, level 1:** set `allows_attachments` back to `false`. Uploads
  stop; existing files stay listable, downloadable and deletable (§7).
- **Rollback, level 2:** deploy a build without the routes. The files stay
  inert on disk. The graph never references them, so an older build loads
  the graph unchanged, and redeploying brings the same files back.
- **Moving to another backend:** list, read, stage and commit through the
  seam, one node at a time, then compare each record's SHA-256. There is no
  graph migration, because the graph holds nothing to rewrite.
- **Search:** surfacing attachment file names in graph search means querying
  the index. Indexing `node.metadata` would not do it. That is a follow-up,
  not part of this slice. Content indexing of file bodies is not planned
  here.
- **Naming:** code for this feature uses `AttachmentStore` and "node
  attachment". The annotation `content.attachment` binding keeps its name,
  and the two share no code.
- **Docs to update when the implementation lands:**
  - `backend/DEVELOPMENT.md` (the endpoint table);
  - `docs/DATA_MANAGEMENT.md` (the directory structure and the validator);
  - `docs/PERSISTENCE_BACKENDS.md` (the second seam and its PostgreSQL
    requirement);
  - `docs/PROFILES.md` (`allows_attachments`);
  - `docs/USER_GUIDE.md` (the panel).
