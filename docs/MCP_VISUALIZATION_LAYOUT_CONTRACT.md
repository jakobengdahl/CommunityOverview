# MCP Visualization Geometry & Movement Contract

**Contract version:** v1
**Status:** Accepted. The read/write layout tools that implement this contract
are already shipped (`get_visualization_layout`, `apply_visualization_layout` —
see `backend/service/mcp_tools.py` and `SessionManager.apply_layout` in
`backend/core/session_manager.py`). This document is the source of truth for the
*semantics* those tools expose; the wire shapes of individual tools are
documented at implementation time in `backend/DEVELOPMENT.md` and the tool
docstrings.
**Scope:** Open-source core. The geometry and movement model is generic
technical enablement and identical in every deployment; nothing here is
SaaS-specific. Session ownership, tenancy and canonical URLs are governed by the
companion [`MCP_SESSION_LIFECYCLE_CONTRACT.md`](MCP_SESSION_LIFECYCLE_CONTRACT.md),
not this document.

This contract complements [`MULTI_USER_SESSIONS_DESIGN.md`](MULTI_USER_SESSIONS_DESIGN.md),
which owns the realtime op protocol, session store, sequencing and presence
model. Where the two overlap, the op-sequence and persistence model in that
document is authoritative; this document adds the **coordinate model, movement
semantics and animation seam** that an MCP-connected assistant relies on to
arrange a session without colliding nodes or fighting concurrent editors.

---

## 1. Motivation

An MCP-connected assistant can populate a visualization session (via
`search_graph` / `get_related_nodes` with a `visualization_session_id`) but then
needs to **arrange** it: place nodes as a left-to-right DAG, a grid, or
swimlanes. To do that safely it must agree with the server and the canvas on a
single coordinate model, on how a bulk move is applied, and on how concurrent
edits and animation are handled.

Without a written contract each consumer — the read tool, the write tool, the
canvas, the animation work and the end-to-end tests — could assume a different
origin, anchor, or batch semantics, and agent-computed layouts would silently
drift or overlap. This document fixes those semantics so the remaining
animation, layout-testing and MCP-documentation tasks share one geometry.

## 2. Coordinate & geometry model

These are the invariants an agent must assume when reading positions and
computing new ones.

- **Model space.** All coordinates are in *model space*: pixels at zoom 1,
  independent of any user's current zoom and pan. An agent never sees or sets
  screen coordinates. Two agents reading the same session at different client
  zoom levels read identical numbers.
- **Origin and axes.** The origin is `(0, 0)`. **+x points right, +y points
  down** (screen/DOM convention, matching React Flow). There is no inverted-y
  math to reconcile.
- **Anchor.** `x`/`y` is the node's **top-left corner**, not its centre. This is
  the React Flow node-position convention and is what the canvas stores and
  renders. An agent computing a grid must offset by node size, not by half size,
  to leave a gap (see §3).
- **Zoom independence.** Because positions are model space, an agent may lay out
  a session without knowing anyone's viewport, and the result is stable across
  clients and across zoom changes.
- **Unset positions.** A node that has been added to the session but never
  positioned has `x`/`y` reported as `null`. An agent must treat `null` as "no
  server-owned position yet" and assign one, not as `(0, 0)`.

## 3. Node dimensions are not server-owned

The server does **not** own or measure node width/height — those depend on the
node label, the active theme and the client's rendered `CustomNode`. Therefore:

- The read tool does **not** return a per-node `width`/`height` or bounding box.
- Instead it advertises a single **`assumed_node_size`** — currently
  `{ "width": 220, "height": 120 }` — that an agent uses as a conservative box
  for **collision-free spacing**. Grid pitch and DAG rank/served spacing should
  be computed from this size plus the agent's chosen padding, e.g. a horizontal
  grid step of `assumed_node_size.width + gap`.
- `assumed_node_size` is a server-provided constant so that if the default node
  size changes, agents pick up the new value from the read tool rather than
  hard-coding `220 × 120`.

Precise per-node measured dimensions and true collision bounds are a **reserved,
deferred** extension (§10): they would require the canvas to report measured
sizes back to the server, which is out of scope for v1 and not needed for the
DAG/grid/swimlane layouts the initiative targets.

## 4. The layout read projection (`get_visualization_layout`)

`get_visualization_layout(session_id)` returns:

| Field | Type | Notes |
|---|---|---|
| `session_id` | string | Echo of the requested id. |
| `revision` | int | The session's monotonic op sequence (`seq`). Pass back as `expected_revision` to the write tool for optimistic concurrency (§7). |
| `node_count` | int | Number of nodes referenced by the session. |
| `nodes` | object[] | One entry per referenced node: `{ id, x, y, hidden, type, status }`. `x`/`y` are model-space top-left, or `null` when unset (§2). `hidden` is `true` when the node is currently hidden in the session (§8). `type`/`status` are the semantic projection below. |
| `selected_node_ids` | string[] | The elements currently selected in the session — the same advisory claim map `get_visualization_session_state` reports (§4.2). |
| `assumed_node_size` | object | `{ width, height }` for collision spacing (§3). |
| `coordinate_space` | string | Human-readable restatement of §2, e.g. `"model-space, pixels at zoom 1, x/y = node top-left"`. |
| `connected_clients` | int | How many browsers are attached. When `> 0`, prefer placing nodes relative to related nodes over guessing a viewport (§8). |

Viewports are **deliberately not reported** (§8). Callers must treat unknown
fields as forward-compatible additions and must not depend on field order.

### 4.1 Semantic projection (`type`, `status`)

Arranging a session *by meaning* — type columns, status swimlanes — must not
require an agent to parse node id strings or to issue one `get_node_details`
call per node, so the read carries a minimal semantic projection:

- **`type`** is the node's graph type (e.g. `"Initiative"`). It is `null` when
  the session's node reference does not resolve to a node this caller may read.
- **`status`** is whatever the deployment stores under the node's
  `metadata["status"]`, reported only when that value is a string. `status` is a
  **convention, not a schema field** — the core schema defines none — so `null`
  means *unknown*, not *no status*, and an agent must not treat it as a lane of
  its own without saying so.

Both fields honour the same graph-scope narrowing as every other read: a node the
caller may not read still appears with its geometry, with `type`/`status` `null`.
A layout therefore never silently drops a node it must still position.

Per-node measured `width`/`height` remain **out of this projection** (§3): the
server has no measured size to report, and inventing one would be worse than the
honest `assumed_node_size` constant. That extension stays reserved (§10, §14).

### 4.2 Why the selection is merged but the visible set is not

`selected_node_ids` is included so that "what is here, and where is it" is a
single call. The **visible set is deliberately not** duplicated into this
response: it is exactly the `nodes` entries with `hidden` false, and carrying the
same fact in two shapes invites the two to disagree. `get_visualization_session_state`
remains the tool for reading session state on its own; it is unchanged.

## 5. Movement semantics (`apply_visualization_layout`)

A write moves one or more nodes in a single call. Its shape:

```
apply_visualization_layout(
  session_id,
  positions | deltas,          # exactly one of the two
  expected_revision = null,    # optional optimistic-concurrency guard (§7)
  animate = true,              # animation hint (§9)
  duration_ms = 400,           # animation hint (§9)
  easing = "ease-in-out",      # animation hint (§9)
)
```

- **Absolute vs. delta.** Provide **exactly one** of:
  - `positions`: `{ node_id: { "x": <n>, "y": <n> } }` — absolute model-space
    targets.
  - `deltas`: `{ node_id: { "dx": <n>, "dy": <n> } }` — relative moves from each
    node's **current server-owned position**. When a node has no current
    position, its delta base is the **origin `(0, 0)`**, so `{dx, dy}` becomes an
    absolute placement at `(dx, dy)`.
  Supplying both, neither, or an empty map is a validation error (§11).
- **Non-empty, numeric.** Each entry must carry numeric coordinates; a
  non-numeric `x/y` or `dx/dy` is rejected before any state changes.
- **Only listed nodes move.** Nodes not named in the call are untouched. A write
  is a partial update of the position map, not a replacement of it.
- **Return value.** On success: `{ success: true, session_id, moved, revision }`
  where `moved` is the number of nodes moved and `revision` is the new session
  `seq`. Re-read is unnecessary after a successful write — the returned
  `revision` is the value to pass to the next write.

## 6. Atomicity, batching and limits

- **One atomic op.** The whole batch is applied as a **single** `layout_applied`
  op: it bumps the session `seq` exactly once and is broadcast to every connected
  browser as one change, so a bulk re-layout arrives as one transition rather
  than node-by-node jumps. There is no partial-batch mode — a batch either
  applies in full or not at all.
- **All-or-nothing rollback.** If persistence fails, in-memory state, `seq`,
  `updated_at` and the op ring buffer are rolled back to their pre-call values;
  the caller sees an error and the session is unchanged.
- **Maximum batch size.** A single write is bounded by **two** independent caps,
  either of which triggers a `too_large` error (§11):
  - at most **500** node moves (`_DEFAULT_MAX_OPS_PER_BATCH`), and
  - at most **256 KiB** of serialized move payload
    (`_DEFAULT_MAX_OP_BATCH_BYTES`).
  An agent laying out a session larger than the node cap must split the work into
  successive writes, threading the returned `revision` into the next call's
  `expected_revision`.
- **Rate limiting.** Writes consume from a per-client token bucket sized to the
  number of moves; exhaustion yields a `rate_limited` error (§11). Layout writes
  are expected to be infrequent (agent-driven), so this bounds abuse without
  affecting normal use.
- **Serialization against realtime edits.** The synchronous layout write must not
  interleave with an in-flight realtime op batch for the same session (doing so
  could broadcast a higher `seq` before a lower one and make seq-gating clients
  drop ops). When such a batch holds the session lock, the write returns `busy`
  and the caller retries (§11).

## 7. Revision and optimistic concurrency

- `revision` is the session's monotonic `seq`. Every applied op (a layout write,
  a realtime edit, a rename) advances it.
- Passing `expected_revision` makes the write **conditional**: it is applied only
  if `expected_revision` equals the session's current `revision`. Otherwise it is
  rejected with `revision_conflict`, returning both `expected_revision` and
  `current_revision` so the agent can re-read the layout and retry against the
  new value.
- Omitting `expected_revision` performs an unconditional last-write-wins move —
  appropriate when the agent is the only editor or is intentionally overriding.

## 8. Locked, hidden, grouped nodes and viewports

What v1 implements, and what it deliberately reserves:

- **Hidden nodes.** Reported per node as `hidden: true/false` in the read
  projection (§4). Hidden state is a session-level concern (`hidden_node_ids`);
  the layout tools do not change visibility, and a hidden node can still be
  moved. An agent may reposition hidden nodes but should generally lay out the
  visible set.
- **Locked nodes — reserved.** The server does not own a per-node "locked" flag
  and does **not** reject a move on the grounds that a node is locked. Node
  locking, where it exists, is a client-side UX affordance. A future revision may
  add server-enforced locks; until then agents must not assume a lock will block
  a write. This is called out explicitly so the animation and test tasks do not
  depend on locked-node semantics that the core does not yet provide.
- **Groups — reserved.** Group annotations exist in the session model
  (`group` annotations, `group_membership_changed`), but the layout contract
  operates on **individual node positions** only; it does not move a group as a
  unit or reflow membership. Group-aware layout is a deferred extension.
- **Viewports — deliberately not exposed.** Connected clients' viewport
  transforms are **not** reported and cannot be set through this contract. With
  several clients attached, "the viewport" is ambiguous, and moving nodes to
  chase one client's viewport would disrupt others. Agents place nodes in model
  space **relative to related nodes**; `connected_clients` is surfaced so an
  agent can prefer relative placement when others are watching.

## 9. Animation seam

The write tool accepts `animate` (default `true`), `duration_ms` (default `400`)
and `easing` (default `"ease-in-out"`). These are a **hint** the canvas honors:

- **Today:** the canvas tweens the batch from each node's current position to the
  target over `duration_ms` with the given `easing`, so a correlated re-layout
  reads as one coherent motion. A human bulk drag arrives *without* an animation
  hint (§10) and is applied instantly.
- **The seam:** the hint travels intact inside the broadcast op (§10), so the
  canvas animation (`task-frontend-animated-layout-transitions`) honors it
  *without a contract or tool change* — it reads `animation` off the
  `layout_applied` op and tweens from the previous to the new positions.
- **Reduced motion.** `prefers-reduced-motion` is a **client-side** decision:
  when set, the canvas must snap to the final positions regardless of the
  `animate` hint. The contract's job is only to *carry* the hint; honoring or
  overriding it is the canvas's responsibility. Agents should send the hint they
  intend and must not try to detect reduced-motion themselves.
- **Cancellation / replacement.** A subsequent `layout_applied` op supersedes an
  in-flight transition for the same nodes; the canvas animates from wherever the
  nodes currently are toward the newest targets. Agents do not manage animation
  lifecycles — they issue moves, and the canvas renders the latest state.

## 10. Broadcast op / SSE event shape

A successful write broadcasts exactly one event to every connected client:

```json
{
  "type": "op",
  "client_id": "mcp-agent",
  "op": {
    "op": "layout_applied",
    "positions": { "<node_id>": { "x": <n>, "y": <n> }, "...": {} },
    "animation": { "animate": true, "duration_ms": 400, "easing": "ease-in-out" },
    "seq": <new-revision>
  },
  "seq": <new-revision>
}
```

- `positions` is the **normalised absolute** target map — deltas are resolved to
  absolute positions server-side before broadcast, so every client applies the
  same coordinates regardless of what it thought a node's prior position was.
- `animation` is present when the write supplied animation fields. The canvas
  tweens the batch when the hint is present (routing it to the animation channel);
  a consumer that does not animate can still ignore `animation` and apply
  `op.positions` directly, so the field stays backward-compatible.
- `seq` is the new revision and equals the `op.seq`; seq-gating clients
  (`sessionSyncClient`) apply the op in sequence and reconnecting clients replay
  it from the ring buffer via the `since_seq` catch-up path.

The MCP write path uses the reserved client id `mcp-agent`, so a human client can
distinguish agent-driven layout from its own edits.

## 11. Error model

Results are structured (never a bare exception). `success: false` carries a
machine-readable `error` and, where useful, a `message` and extra fields:

| Condition | `error` | Extra |
|---|---|---|
| Invalid `session_id` format | `"Invalid session ID format …"` | |
| Session not found | not-found message | |
| Neither/both of `positions`/`deltas`, empty map, or non-numeric coordinate | `OpError` message | validation detail |
| Stale `expected_revision` | `revision_conflict` | `expected_revision`, `current_revision` |
| Realtime batch mid-flight holds the lock | `busy` | retry guidance |
| Token bucket exhausted | `rate_limited` | |
| Over the node or byte cap | `too_large` | split guidance |

A `revision_conflict`, `busy` or `rate_limited` is **retryable**; a validation
error or `too_large` requires the agent to change the request.

## 12. Undo and reversibility

- A layout write does not ship an undo payload. Reversibility is achieved by the
  read-then-write pattern: an agent that captures the `nodes` positions from
  `get_visualization_layout` before a move can restore them by writing those
  positions back.
- Because deltas are additive, a `deltas` move is reversible by applying the
  negated deltas, provided no other write intervened (guard with
  `expected_revision` to be sure).
- Client-side single-user undo of a drag is governed by the realtime protocol in
  `MULTI_USER_SESSIONS_DESIGN.md`, not by this contract.

## 13. Public/private boundary

The geometry and movement model is **entirely open core** — there is no premium
geometry. The layout tools route through the same authorization and request-actor
seams as every other session operation (permissive/anonymous by default; the
hosted layer may narrow the `layout` capability per its authorization decision,
exactly as for the CRUD tools). No coordinate, batching or animation semantics
differ between the open core and the hosted layer.

## 14. Versioning and change policy

- This is contract **v1**. Additive fields (a new read field, a new reserved
  animation key) are non-breaking and do not bump the version.
- Changing the coordinate space, the `x`/`y` anchor, the absolute/delta
  semantics, the atomicity guarantee, or the batch caps is a **breaking change**
  requiring a new contract version and a migration note.
- Realizing per-node measured dimensions, server-enforced locks, group-aware
  layout, or viewport control (all reserved above) are **additive** extensions
  that a later version can introduce without breaking v1 consumers.
- The requirement node `req-mcp-layout-contract` and the decision
  `dec-visualization-layout-contract` in the Corp planning graph govern this
  document; status and evidence live there, not here.

## 15. Realizing and dependent tasks

| Task (Corp graph) | Relationship to this contract |
|---|---|
| `task-implement-mcp-layout-read` | Implements §2–§4 — `get_visualization_layout` (shipped, PR #287). |
| `task-implement-mcp-layout-write` | Implements §5–§7, §10, §11 — `apply_visualization_layout` / `SessionManager.apply_layout` (shipped, PR #287). |
| `task-frontend-animated-layout-transitions` | Consumes §9–§10 — tweens the `layout_applied` op and honors reduced motion. |
| `task-test-agent-layout-workflow` | Verifies §2–§12 end to end — geometry accuracy, collision-free layout, concurrency, limits, reduced motion. |
| `task-document-agent-visualization-tools` | Publishes §2–§11 into the tool docstrings and `backend/DEVELOPMENT.md`. |
| `task-mcp-layout-read-semantic-metadata` | Extends §4 additively with the semantic projection (§4.1) and the merged selection (§4.2); no version bump per §14. |
