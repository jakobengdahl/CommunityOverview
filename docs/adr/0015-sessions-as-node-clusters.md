# ADR 0015 — Sessions as node clusters and saved views

- **Status:** Accepted (option 3), 2026-11-25
- **Date:** 2026-11-25
- **Scope:** Open-source core
- **Related:** [`MULTI_USER_SESSIONS_DESIGN.md`](../MULTI_USER_SESSIONS_DESIGN.md)
  decision D13; [`MCP_SESSION_LIFECYCLE_CONTRACT.md`](../MCP_SESSION_LIFECYCLE_CONTRACT.md);
  [ADR 0005](0005-sessions-may-stage-graph-changes.md)

## Context

Node types classify individual nodes and edges describe relationships between
nodes. A session's `node_refs` can provide a separate, overlapping grouping axis:
any set of nodes can belong to a session regardless of type or edges, and one
node can belong to many sessions. A named session can represent a topic, a
working set, or the materialized result of a query. Its positions, hidden state
and annotations provide presentation context without changing the nodes or
creating graph edges. Canvas group annotations are smaller visual groupings
inside a session; they are not the same as membership in the session itself.

Sessions already persist server-side and can be created and populated over MCP
without a browser. Their references resolve to current graph data, not copies of
nodes. They are mutable collaborative workspaces, not immutable snapshots. D13
keeps sessions until explicit deletion and leaves convergence with SavedView open.
SavedView and VisualizationView nodes instead store view metadata in the graph.
Today `save_view(name)` only signals a browser to capture its current state; it
does not itself save a view. Existing saved views can include positions, hidden
nodes, groups and annotations, and their readers resolve referenced graph nodes.

## Options

1. **Keep both models.** Add server-side capture into a SavedView graph node
   from an identified session. This preserves graph export and existing readers,
   but duplicates membership and presentation state and leaves two lifecycles.
2. **Use the session itself as the saved view.** Name and share the existing
   session, and treat `save_view` as naming it. This needs no copy, but later
   edits and deletion change or remove what a caller thought was saved; it also
   does not preserve the existing graph-node storage and export behavior.
3. **Save a session copy and converge on sessions.** Capture an identified
   session as a new, independently editable named session. Keep legacy view
   readers during migration. This uses one long-term representation while
   retaining a point-in-time capture of membership and presentation; the copy
   still references live graph nodes.

## Decision

Option 3 was accepted on 2026-11-25, with retiring SavedView as the first goal.
Behavior changes still follow a detailed compatibility contract, written and
reviewed before the implementation slices.

- Treat a session ID as the identity of a cluster. Names are display labels and
  need not be unique. Membership is explicit `node_refs`, including hidden
  nodes; visibility and layout do not determine membership. Adding or removing
  a reference does not mutate the graph, and deleting the session does not delete
  its nodes. A query can be run once and its resulting node IDs added to a
  session: the resulting membership is fixed until edited, not a live query or
  an automatic refresh. Query provenance could be recorded separately in a
  future contract; it must not imply refresh semantics.
- Offer an explicit server-side save operation taking a source session ID and a
  name, returning a new session ID and the server-provided link when available.
  It copies the source's membership and supported presentation state, including
  positions, hidden state and annotations, at one consistent session revision.
  The source and copy can then diverge. A revision precondition should reject a
  stale capture, with no partial copy on failure. The caller may use an empty
  session intentionally. Saved node content and graph edges are not frozen:
  opening the copy resolves current authorized nodes and edges; missing nodes
  must be handled without silently changing stored membership.
- Keep the current browser-signal form of `save_view(name)` working during
  migration. Introduce an unambiguous session-ID form or a separate tool for
  server-side capture, with a structured result that distinguishes a saved
  session from a browser capture request. Do not claim success for the latter
  until a browser has actually saved it. Define exact names, errors and client
  transition in the compatibility contract rather than silently changing the
  existing call's meaning.
- Retain legacy SavedView and VisualizationView reads and graph exports until
  an explicit migration and deprecation decision. Provide an explicit,
  repeatable import into a new session that converts supported legacy fields;
  report missing references and unsupported or invalid metadata rather than
  silently discarding them. Preserve the original node, and record enough mapping
  to avoid duplicate imports on retry. Name collisions must not select a view
  arbitrarily: use its graph ID for import. Do not remove legacy writers or
  readers until users have a documented migration and rollback path. Session
  copies live outside the graph, so graph-only export is not a backup of them;
  session backup and restore must be specified before retiring the old format.

## Boundaries and consequences

A session is a useful cluster only within the session store; this proposal does
not make membership a graph property, add graph traversal edges, or make
sessions searchable as graph nodes. Discovery and filtering by membership would
need explicit session APIs and authorization rules. Existing session limits,
revision ordering and deletion semantics apply; long-lived copies consume
session capacity under D13. Retention remains unchanged until separately decided.

Capture and import must check authorization for the source, destination and
referenced graph data, including narrowed access, without exposing inaccessible
IDs through results or annotations. Validate and sanitize imported presentation
metadata as existing view readers do. Access to a session is not by itself a
grant to read every referenced graph node. A capture must not copy a staged
session's unmerged graph changes as if they were committed: initially reject a
source with staged changes unless a separately approved contract defines how to
copy its overlay and lifecycle safely. The session document's references and
presentation state remain distinct from the staged layer described by ADR 0005.

Before implementation, settle the exact atomic capture mechanism, annotation
filtering, legacy conversion and export/restore formats, and failure behavior
for limits and concurrent edits. These choices require a contract and tests;
this note changes no runtime behavior or existing decision.
