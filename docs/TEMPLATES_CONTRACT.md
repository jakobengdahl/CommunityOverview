# Template document and store contract v1

A Template (Mall in Swedish UI) is a reusable, self-contained snapshot stored
outside the graph, alongside sessions. Its visibility follows the session scope
in open core. It is not a live session or a graph node. A template carries both
source node IDs for a future reference insertion and snapshots for a future copy
insertion; copy will be the default. Reading a template does not require the
source session or nodes to exist. Capture, reference insertion, authorization and transport are implemented;
copy insertion and the UI follow later.

## JSON document

All fields below are required, including nullable fields. Unknown fields are
rejected except inside annotation payloads and allowed metadata values. JSON
numbers must be finite. `schema_version` is integer `1`; unsupported versions
are rejected. `template_id` uses the session store's grouped-digit ID format
(four groups, with the legacy two-group form also accepted). IDs are unique
within the template store. Names need not be unique.

- `template_id`, `name`, `description`, `tags`, `revision`, `created_by`,
  `created_at`: identity, display text, string tags, nonnegative integer
  revision, author and creation timestamp string. New documents start at
  revision 0 before their first save.
- `source_session_id`, `source_session_revision`: source identity and its
  nonnegative integer revision at capture. These are provenance, not live links.
- `nodes`: objects with a unique nonempty local `key`, nonempty
  `source_node_id`, `snapshot`, relative `position` (`x`, `y`), boolean
  `hidden`, and nullable `shape`. The snapshot contains `type`, `name`,
  `description`, `summary`, string arrays `tags` and `subtypes`, and `metadata`.
  Only metadata keys `color` and `icon` are allowed in v1. System node types
  from the code-defined system type set are excluded. Graph lifecycle,
  embeddings, operational metadata and arbitrary metadata are not copied.
- `shape`: null for no selection, or an object with `ref`, `inline`, and
  `builtin`. A non-builtin selection has a complete v1 shape definition in
  `inline`, and `ref` is null or a `saved:<name>` / `config:<name>` reference.
  A builtin selection has `builtin: true` and null `ref` and `inline`.
  This is a per-node override; it never changes a target type default.
- `edges`: objects with nonempty `source_edge_id`, `type`, `source`, `target`,
  and string `label`. Endpoints are local node keys. Only inner edges are
  representable; capture drops edges to nodes outside the selection before
  constructing the document.
- `annotation_document`: `{ "schema_version": 1, "annotations": [...] }`
  using the session annotation v1 payload. Annotation IDs are unique and
  distinct from node keys. Group `member_node_ids` use local node keys;
  attachments and line anchors use local node keys or annotation IDs as
  appropriate. References outside the document are invalid. Annotation
  positions are relative to the same origin as node positions. Session image
  and reference safety rules apply.
- `bounds`: relative `x`, `y`, `width`, `height`, with nonnegative dimensions.
- `thumbnail`: null, reserved for a later version; no thumbnail is generated.

At most 500 nodes, 2,000 edges and 2,000 annotations (the session default)
are accepted. Limit failures have stable codes `nodes_limit`, `edges_limit`,
and `annotations_limit`. Invalid documents fail as a whole; the validator
never truncates or silently removes content. Callers constructing a template
must explicitly handle rejected data.

## Shape resolution

On insertion, resolve a valid reference to its current definition and
use the inline copy when the reference is unknown. A tombstoned saved shape
instead resolves to the built-in card; it does not resurrect the inline copy.
An inline-only selection uses its copy and a builtin selection uses the card.
The shape library reverse index must count template references as well as other
uses; that integration belongs to a later slice.

## Persistence and profile templates

`TemplateStore` accepts a dedicated `SessionPersistenceBackend`, using the
same file or memory implementations and their transaction and atomic-write
semantics. Use a separate directory such as `data/templates/`, never the
sessions directory. Create with expected revision 0; each successful save
increments the revision, and both the supplied document revision and expected
revision must match the current revision. Delete requires the current revision
and writes a tombstone. A tombstoned ID cannot be reused, listed, or read as a
live template. A failed write leaves the previous record intact. File storage
is local to one instance with the same locking and durability characteristics as
session files; other backends must implement the same transaction contract.

Read-only profile templates belong as individual v1 JSON documents in
`config/<profile>/templates/`. Their IDs must not collide with mutable or other
profile templates; a loader must reject collisions and invalid documents rather
than silently override one. Profile loading and content are not shipped here.

## Reference insertion

`insert_template` and `POST /api/sessions/{id}/insert-template` insert existing
source nodes into a writable session without changing the graph. The caller must
be able to read the template; source nodes use the same access filter as adding
nodes to a session. Missing, archived, or inaccessible nodes are reported as
`missing`, and nodes already present are reported as `already_present` and left
unchanged. Graph edges between session nodes remain those in the graph.

Supply both `x` and `y` to place the template bounds at an origin; otherwise the
server places it to the right of existing content. Annotation IDs are minted
fresh and links remapped; links to missing nodes are removed. Shape selections
become per-node overrides, with `tombstoned` and `missing_shape` entries in the
shape report for fallbacks. The result includes `added`, `annotation_id_map`,
`skipped`, `shapes`, and the session `revision`. Optional `expected_revision`
and `template_revision` reject stale writes atomically with `revision_conflict`.
Only `reference` mode is available; `copy` returns `unsupported_mode`.
