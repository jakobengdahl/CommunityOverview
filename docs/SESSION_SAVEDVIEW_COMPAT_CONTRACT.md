# Session and saved-view compatibility contract

**Status:** Retired and complete. Frontend removal (8a, #1264) and backend
removal (8b, #1265) are complete; 8c completes the documentation. Sessions are
the supported way to save and share views. No legacy import was implemented.
SavedView and VisualizationView system types and internal legacy readers remain
for graph data compatibility; they do not provide a saved-view feature. This
contract records the transition under [ADR 0015](adr/0015-sessions-as-node-clusters.md).
Session lifecycle and links are in [MCP session lifecycle](MCP_SESSION_LIFECYCLE_CONTRACT.md).

## Identity and capture

A saved view becomes a new named session, identified by its session ID, not its
non-unique name. A save is an explicit server-side operation and needs no
browser. `node_refs` is the fixed membership, including hidden nodes, until a
subsequent edit; it is not a live query, graph property or set of graph edges.
The copy and source are independently editable and deletable. Graph node content
and edges are not copied or frozen: resolution uses current authorized graph
data. A missing node is omitted from resolved results and reported, but its
stored reference is not silently removed. An intentionally empty source is valid.

Use MCP `save_session_view(session_id, name, expected_revision)` and
`POST /api/sessions/{session_id}/save-view` with body `name` and
`expected_revision`. All three arguments are required; `name` must be a valid,
non-empty session name and `expected_revision` a non-negative integer. The
revision is the source session's `seq` as returned by session inspection. The
result is an object with `success: true`, `kind: "saved_session"`,
`source_session_id`, `source_revision`, `session_id`, `revision` (the new
session's initial revision), and `session_url` (the canonical server-generated
link or null when unavailable). The caller must use the returned link, not
construct one; when it is null the browser shows no link. Names may collide. This operation is distinct from existing
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

The former saved-view tools and REST endpoints have been removed. Use
`save_session_view` to capture a session and server-provided session links to
open it. No import or conversion of legacy views to sessions is available.

## Backup, migration and retirement

Graph exports and session backups are separate artifacts. Follow the operator
[backup and restore guide](SESSION_BACKUP.md), including its requirements for a
coordinated graph backup and verification before relying on recovery.

A versioned session backup is a JSON document with a format version, session IDs,
names, descriptions, revisions, complete persistent session presentation state,
write-mode metadata and import mappings/reports. Include all referenced embedded
assets, with integrity checks; exclude transient presence, leases and activity.
A backup must be a consistent authorized snapshot and must not expose inaccessible
references or annotation targets. Reject a backup containing unmerged staged
changes until a separate overlay backup contract exists. Graph data is backed up
separately; the operator must preserve both artifacts and their association.
Version 2 backups include authorized mutable templates and, when present, a
`template_tombstones` list of deleted template records (ID, deletion flag and
revision). The integrity hash covers both lists. Restore preserves tombstones
and checks their IDs for collisions, so deleted IDs remain reserved. Profile
templates are excluded from backups. Version 2 backups without tombstones and
version 1 backups remain supported.

### Release note: SavedView removal

SavedView and VisualizationView nodes are no longer read as views by any client or tool, and a `?view=` link no longer opens a view. There is no migration, and existing nodes are not deleted. Before upgrading, export the graph: a plain graph export keeps saved-view nodes as data. Also take a [session backup](SESSION_BACKUP.md) and retain it with the corresponding graph backup. To recover the original graph data, [import the graph export](DATA_MANAGEMENT.md#importing-a-graph); to recover sessions, [restore the session backup](SESSION_BACKUP.md#restore) with its paired graph backup. Integrations calling removed saved-view tools or endpoints must move to sessions, using `save_session_view` and server-provided session links.
