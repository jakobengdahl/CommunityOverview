# Session and saved-view compatibility contract

**Status:** Accepted design; implementation pending. This contract specifies the
transition under [ADR 0015](adr/0015-sessions-as-node-clusters.md). It does not
change current APIs. The existing session lifecycle and canonical link rules
are in [MCP session lifecycle](MCP_SESSION_LIFECYCLE_CONTRACT.md); session
state and sequencing are in [the multi-user design](MULTI_USER_SESSIONS_DESIGN.md).

## Identity and capture

A saved view becomes a new named session, identified by its session ID, not its
non-unique name. A save is an explicit server-side operation and needs no
browser. `node_refs` is the fixed membership, including hidden nodes, until a
subsequent edit; it is not a live query, graph property or set of graph edges.
The copy and source are independently editable and deletable. Graph node content
and edges are not copied or frozen: resolution uses current authorized graph
data. A missing node is omitted from resolved results and reported, but its
stored reference is not silently removed. An intentionally empty source is valid.

Introduce MCP `save_session_view(session_id, name, expected_revision)` and
`POST /api/sessions/{session_id}/save-view` with body `name` and
`expected_revision`. All three arguments are required; `name` must be a valid,
non-empty session name and `expected_revision` a non-negative integer. The
revision is the source session's `seq` as returned by session inspection. The
result is an object with `success: true`, `kind: "saved_session"`,
`source_session_id`, `source_revision`, `session_id`, `revision` (the new
session's initial revision), and `session_url` (the canonical server-generated
link or null when unavailable). The caller must use the returned link, not
construct one. Names may collide. This operation is distinct from existing
`copy_visualization_session` and `POST /api/sessions/{id}/copy`, which currently
also copy staged changes and do not require a revision.

Capture copies the source description and supported session presentation state:
`node_refs`, `positions`, `hidden_node_ids`, `hidden_edge_ids`,
`dimmed_node_ids`, `dimmed_edge_ids`, `edge_intensity`, shape overrides and
type shapes, and annotations including groups. Copy only valid presentation
associated with the authorized membership; exclude transient presence,
selections, leases, activity, undo history, operation receipts and tombstones.
Initialize new session bookkeeping and annotation versions; give annotations
fresh IDs and remap their internal annotation references, as session copy does.
The destination starts at its own initial revision and has no staged overlay.

All source state, the revision comparison, staged-layer check, destination
capacity reservation, access assignment and durable publication form one
serialized operation. Hold the source mutation lock and the persistence
transaction across the check and snapshot; coordinate staged-layer mutations
under the same exclusion. If the source has unmerged staged changes, reject the
save with `staged_changes` (409) and publish no destination. Publish the
destination before assigning access. The session can briefly be visible before
its grants exist; publication and access assignment are not atomic. If grant
assignment fails, revoke the grants made for the save and remove the destination
before reporting failure, so no grant or capacity outlives a failed save. A
crash before grant assignment leaves no grant; a failure before publication
leaves no visible destination or consumed capacity.
All source edits, including staged changes, must serialize with this boundary;
otherwise the operation must refuse rather than capture a mixture. A successful
capture is from the stated revision even if an edit follows immediately.

Source read, destination creation and access assignment, and graph read access
for every reference and annotation target must be authorized in the caller's
current scope. Assign destination access no broader than the caller's rights;
session access alone never grants graph access. If a referenced ID exists but is
inaccessible, fail the entire capture with `access_denied`, without exposing the
ID, count, position, or annotation content. Missing references are preserved
only when the authorization policy can establish that reporting them does not
disclose inaccessible data; otherwise use the same generic denial. Report
permitted missing IDs in `missing_node_ids` on success. Recheck authorization at
publication, or hold a policy-consistent authorization snapshot through commit.

Validate annotations using the session write rules and saved-view sanitization,
including image and reference URL restrictions. Preserve free-standing valid
annotations; retain group members only when they are authorized members. Remove
or reject an annotation or attachment that would reveal an inaccessible target;
the initial save uses whole-operation denial, not silent filtering. Invalid or
unsafe source annotations fail the save. Never expose hidden IDs through nested
annotation fields, group membership, shape overrides, errors or reports.

Failures return `success: false` and a stable `error` code, never a destination
ID: `invalid_request` (REST 400), `access_denied` (403), `session_not_found`
(404, also usable to conceal a denied source), `revision_conflict` (409),
`session_busy` (409), `staged_changes` (409), `invalid_presentation` (422),
`session_too_large` (413), `too_many_sessions` (503), or `storage_error` (503).
A revision conflict includes `current_revision` only after source authorization.
The source is unchanged on failure. A destination may be briefly visible before
access assignment; if assignment fails, it is removed before failure is reported,
as described above. A busy caller may retry; a stale caller must inspect and
explicitly retry with the new revision. Existing session count, document and
annotation limits apply; do not truncate to fit. Deletion, retention and normal
revision ordering remain as before. No automatic retry after an ambiguous
transport failure is safe without an idempotency key; callers must reconcile
before repeating a save.

## Existing save and read surfaces

Today MCP `save_view(name)` and REST `POST /api/views/save` with `{name}`
return an `action: "save_view"` signal for a browser to capture; they do not
persist a view. Keep that call and its existing response shape working during
migration. New clients treat the response as `browser_capture_requested`, not a
completed save, and must not claim a view was saved without a browser's actual
save confirmation. The new `save_session_view` result is explicitly
`kind: "saved_session"`; no overload of the name-only call is permitted. A
future versioned signal envelope may add `kind: "browser_capture_requested"`
without removing the existing fields. Legacy clients that rely on the signal
continue to receive it until their migration and the retirement decision below.

Today `list_saved_views` / `GET /api/views` list graph views and
`get_saved_view(name)` / `GET /api/views/{name}` load by name; duplicate names
can select an arbitrary match. Preserve those semantics during the compatibility
period, including graph export of both legacy types. New clients list and open
sessions by ID using session APIs, and use the ID-based import below rather than
name lookup. Do not silently mix sessions into legacy lists or change what a
name-based read resolves.

## Legacy import

Introduce explicit MCP `import_saved_view(view_id)` and REST
`POST /api/views/{view_id}/import-session`. The ID identifies a committed graph
node of type `SavedView` or `VisualizationView`, never a name or staged graph
view. Require permission to read the legacy node, its referenced graph data and
to create and access the destination. Preserve the original node and graph
export. Import creates an independently editable session; it never updates an
already imported session or deletes the legacy node.

| Legacy source | Destination and conversion |
|---|---|
| Node `name`, `description` | Session name and description; names are not unique. `summary` and other node properties are reported as unsupported, not inferred. |
| `view_data.nodes` with `id` and `position` | When present, takes precedence as in `get_saved_view`: graph IDs become `node_refs` and valid positions become `positions`. `group-*` entries are group presentation, not graph membership. |
| Otherwise `node_ids`, `positions` | Graph IDs become `node_refs` and valid member positions become `positions`; `group-*` IDs are group presentation. Deduplicate IDs. |
| `view_data.hidden_nodes` in the first form, otherwise `hidden_nodes` | `hidden_node_ids`, restricted to members. |
| `annotation_document` and `annotation_schema_version` | Supported v1 document becomes session `annotations`, including groups. Validate and sanitize before conversion; assign fresh annotation IDs and remap internal references. |
| `groups`, `parentIds`, `annotations` | If no usable annotation document exists, convert legacy groups and annotations to session annotations, using `parentIds` for group `member_node_ids`; group positions from `group-*` entries are a fallback when no groups are supplied. If a document exists it is authoritative; report conflicting or extra legacy fields as skipped. |
| `edge_ids`, `edges` | Do not create or freeze graph edges. Report as skipped; current authorized edges resolve from the graph. |
| Other metadata, malformed entries, unsupported versions or annotation kinds | Report each permitted field or entry as unsupported or invalid; do not silently discard it. |

Validate structures and finite positions, membership, group links, annotation
limits and safe image/reference targets using the existing reader sanitization
and session write rules. Report sanitization and every skipped or defaulted
field with a reason. Never fetch a remote image as part of import. Missing graph
references remain in membership and are reported when disclosure is authorized;
invalid IDs are skipped and reported. Apply the capture authorization and
non-disclosure rules to nested references and reports. If safe conversion cannot
be established, fail without publishing a partial session. An empty converted
membership is allowed, including a presentation-only view.

Return `success`, `kind: "imported_session"`, `view_id`, `session_id`,
`revision`, `session_url`, and a `report` containing `converted`, `skipped`
(with field/entry and reason), and `missing_node_ids`. The report must describe
the actual committed conversion, not a preview. For inaccessible targets return
only generic `access_denied`; neither IDs nor counts appear in any report.
Limit or persistence failures use the save error codes and publish nothing.

Persist a mapping keyed by graph identity and legacy node ID, with source
revision or content fingerprint, destination ID and conversion report, in the
same atomic commit as the destination. Concurrent imports and retries return the
same destination and report, including after a lost response or restart. Check
current authorization before returning a mapping or report. If the source
changes after import, return `source_changed` (409) with no new copy; an
explicit new import/reset workflow must be separately designed. If the mapped
session is deleted, return `import_target_deleted` (409), not a silent
recreation. Mapping retention and deletion must survive backup and restore.
Take a consistent committed graph snapshot for conversion; detect a concurrent
source change before publication and fail with `source_changed`. The graph and
session stores need a transaction or equivalent durable recovery protocol; an
implementation unable to meet these guarantees must refuse import.

## Backup, migration and retirement

Graph-only export does not contain sessions. Before legacy retirement, provide a
versioned session backup as a JSON document with a format version, session IDs,
names, descriptions, revisions, complete persistent session presentation state,
write-mode metadata and import mappings/reports. Include all referenced embedded
assets, with integrity checks; exclude transient presence, leases and activity.
A backup must be a consistent authorized snapshot and must not expose inaccessible
references or annotation targets. Reject a backup containing unmerged staged
changes until a separate overlay backup contract exists. Graph data is backed up
separately; the operator must preserve both artifacts and their association.

Restore validates version, integrity, authorization, limits, annotation safety
and references before any publication. Preserve IDs and revisions on an empty
target so links and import mappings survive; an ID or mapping collision fails
the whole restore without overwriting. Missing graph references remain stored and
are reported under the same disclosure rules. Restore all session records,
assets and mappings atomically, or recover to the pre-restore state after a
failure. Unsupported versions, unsafe content, limits and collisions fail
explicitly; no silent truncation. Test a backup and restore against a separate
store before relying on it for rollback.

1. Ship the new save, import and backup/restore alongside all legacy writers,
   readers and graph exports. Document limits, access requirements, reports and
   recovery; verify imports and restored sessions before switching consumers.
2. Inventory legacy nodes and links, import by ID, inspect reports and retain the
   graph originals and a verified backup. Update browser save UI, assistants and
   API clients to save and open sessions by ID. Update Active Knowledge
   Collection kiosks and sorting-round list links to use server-provided session
   links; verify each consumer before removing its legacy dependency.
3. Keep `?view=` working during migration, including its current behavior when
   `?session=` is also present: wait for bootstrap, then apply the named view
   only if the session and canvas have not changed. Do not silently redirect an
   ambiguous name. Replace published links with `?session=` links using the
   verified ID mapping; provide an explicit choice or failure for ambiguous or
   unimported names. Preserve a documented route back to legacy links and readers.
4. Only after migration, backup/restore and rollback have been verified, seek an
   explicit deprecation decision for `SavedView` and `VisualizationView` types,
   legacy writers, `?view=`, `list_saved_views`, `get_saved_view` and REST
   counterparts. Announce the compatibility window and removal version. Until
   then keep legacy reads and graph exports. A later removal must include a
   supported way to recover original graph data and restore sessions.

Rollback during the compatibility window restores the prior clients and legacy
readers/writers and uses retained graph views and links. Session edits made after
import are not written back to legacy nodes; preserve a fresh session backup
before rollback and restore it when returning to session clients. Do not delete
legacy data or mappings as part of switching clients.
