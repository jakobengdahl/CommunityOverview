# Multi-User Shared Sessions — Design & Implementation Plan

**Status:** Design defined and implemented in the open core. The backend
foundation (steps 1–3), the step-4 frontend cutover (server-backed session
lifecycle), the step-5 annotation kinds (note, label, arrow), the step-6
realtime op emit/apply loop, the step-7 presence UI + selection claims, and the
step-8 hardening (transitional shims removed, op-batch body cap, multi-client
e2e, docs sweep) are all implemented. The post-implementation code review
(2026-07-06) findings in §8 (R1–R15) are all now resolved (see the per-item
"Fixed" annotations). This document is the **foundational** design: the hosted
realtime slices (Postgres-backed session store, Redis event bus,
identity/presence, access/history, durability) are separate downstream slices
that build on the open-core extension seams catalogued in §9 — they are not
implemented here.
**Scope:** Open-source core only. SaaS-specific extensions (multi-instance scale-out,
account-bound session history, workspace ACLs) are designed in the private SaaS
repository and are explicitly out of scope here (see "Out of scope" below).

This document is the source of truth for the multi-user shared session feature in
the open core. Each implementation step below is sized to be executed by one
development session as one branch + one PR against `main`, following the Standard
Development Workflow in `CLAUDE.md`. Update the step status table as steps complete.

**Related contract:** the MCP-facing session **lifecycle, ownership seam and
canonical deep-link** semantics (assistant-created sessions, CRUD tools and the
server-generated `?session=<id>` URL) are specified in
[`MCP_SESSION_LIFECYCLE_CONTRACT.md`](MCP_SESSION_LIFECYCLE_CONTRACT.md). The
session-store data model here remains authoritative where the two overlap.

---

## 1. Goal

Move visualization sessions from per-browser localStorage to server-side storage so
that several users can share one session — by session ID or by URL — and collaborate
in real time:

- A new session is created automatically when a user starts using the app.
- Sessions can be renamed and deleted from the left session drawer. Deleting the
  active session auto-creates a new one and switches the user into it.
- Session content (node membership, positions, annotations such as notes, labels,
  group boxes, arrows) is stored **separately from the knowledge graph** — annotations
  are collaboration/overview artifacts, not graph content.
- Every state-changing event in a session is mirrored to all connected clients with
  sub-second latency (target: < 500 ms server round-trip on LAN, never seconds).
- Concurrent conflicting actions (two users moving the same node) are resolved by the
  server deterministically; the resolved state is what all clients converge on.
- Other users' selections are shown as colored markers so users avoid grabbing a node
  someone else is working with; these claims auto-expire and are released on disconnect.
- Deleting a session with other users connected shows an extra warning.
- The list of recently used sessions stays in localStorage (it is personal); the
  session *data* moves to the server.
- The core stays **single-instance by default**. All cross-instance concerns (Redis
  pub/sub, shared DB) are hidden behind seams that the SaaS layer implements.

## 2. Current state (facts)

Two "session" concepts coexist today, keyed by the same grouped-digit ID
format. New IDs use four groups (`DDDD-DDDD-DDDD-DDDD`, ~10^16 address space);
the legacy two-group form `DDDD-DDDD` is still accepted so previously-shared
URLs keep resolving. The wider space is a deliberate hardening: the session
CRUD/stream endpoints bypass the auth middleware and are protected only by the
unguessable ID plus per-source lookup rate limiting (D7 / §3.9), so the ID must
be large enough that enumeration is infeasible even under that rate limit.

| Concern | Where | Notes |
|---|---|---|
| Session snapshots (nodes, edges, positions, parentIds, groups, hidden ids) | localStorage via `frontend/web/src/services/sessionStore.js` | Keys `graph_sessions_index` and `graph_session_snapshot_<id>`; max 30 sessions, 4 MB/snapshot, LRU eviction; no delete API, no viewport, no selection |
| Session list UI | `frontend/web/src/components/SessionDrawer.jsx` | New / search / connect / rename; **no delete button** |
| Auto-save | `frontend/web/src/App.jsx` (`scheduleAutoSave` → `saveViewSignal` → `handleSaveView`) | 1.5 s debounce; round-trips through the canvas because positions/groups live inside ReactFlow, not the Zustand store |
| Server session registry | `backend/core/session_registry.py` | In-memory, single `asyncio.Queue` per session, **single consumer by design**, 1 h TTL |
| Server session endpoints | `backend/api_host/server.py` (`PATCH /sessions/{id}/state`, `GET /sessions/{id}/stream`) | SSE is one-directional (server→browser) and exists so MCP tools can push visualization commands; uploaded state is only `{visible_node_ids, selected_node_ids, node_count}` |
| Persisted named views | `SavedView` graph nodes via `backend/service/service.py:save_view/get_saved_view` | Static snapshots stored *inside* the graph file — the opposite of the separation this feature requires |
| Annotations | Group boxes only (`packages/ui-graph-canvas/src/components/GroupNode.jsx`), plus node marks and edge labels | No sticky notes, free labels, or arrows exist; group membership changes are only observable via the full `onSaveView` snapshot, not discrete events |
| Storage | `backend/core/storage.py` + `backend/core/storage_backends.py` | Single `graph.json` file, process-local `RLock` + file lock; no DB, no broker, no WebSocket |

Key implications:

1. The SSE channel and session-ID plumbing already exist end-to-end — they need a
   fan-out upgrade, not a green-field build.
2. Session state must stop storing full node copies. The server-side session stores
   **node references + layout + annotations** and rehydrates node content from the
   graph on load (same pattern `get_saved_view` already uses).
3. `packages/ui-graph-canvas` lacks discrete events for several mutations (group
   rename/resize/membership); realtime mirroring needs those callbacks added.

## 3. Target architecture

### 3.1 Session as a first-class server-side entity

A session is stored **outside the graph** in a new session store:

```
Session {
  id: "DDDD-DDDD-DDDD-DDDD",  # crypto-random (legacy DDDD-DDDD still accepted)
  name: string | null,
  created_at, updated_at: iso8601,
  seq: int,                    # monotonic revision, incremented per applied op
  state: {
    node_refs: [node_id],      # graph nodes shown in this session (references only)
    positions: { node_id: {x, y} },
    hidden_node_ids: [..], hidden_edge_ids: [..],
    annotations: [ Annotation ]
  }
}

Annotation {
  id: string,                  # server-assigned uuid
  kind: "group" | "note" | "label" | "arrow",
  position: {x, y},
  # kind-specific payloads:
  #   group: { label, description, color, size: {w,h}, member_node_ids: [..] }
  #   note:  { text, color, size }
  #   label: { text, style }
  #   arrow: { from: {x,y}|{anchor: node_or_annotation_id}, to: ..., style }
  created_by: client_id, updated_at
}
```

Existing group boxes migrate into `annotations` with `kind: "group"`. Group
membership moves from ReactFlow `parentId` maps into the group annotation
(`member_node_ids`), which becomes the persisted source of truth; the canvas still
uses `parentId` internally.

### 3.2 Persistence and fan-out seams (core vs SaaS)

Two protocols, mirroring the existing `GraphPersistenceBackend` pattern:

- `SessionPersistenceBackend` — `load(id)`, `save(session)`, `delete(id)`,
  `list_meta()`. Core ships `FileSessionPersistenceBackend`: one JSON file per
  session under `data/sessions/<id>.json` (atomic temp+rename, same locking style as
  `storage_backends.py`). Per-session files keep write amplification low under
  frequent op-driven saves. **No automatic retention/eviction in v1** (D13): sessions
  persist until explicitly deleted, because long-lived sessions may evolve into
  de-facto saved visualizations; revisit retention once session/SavedView
  convergence is decided.
- `SessionEventBus` — `publish(session_id, event)`, `subscribe(session_id)`.
  Core ships `InProcessEventBus` (asyncio, per-subscriber queues). This is the seam a
  SaaS deployment replaces with a Redis-backed bus for multi-instance fan-out. The
  core makes no attempt to work across instances and documents that constraint.

### 3.3 Transport and op protocol

**Decision D1 — SSE down, REST ops up (no WebSocket in core v1).**
The existing `GET /sessions/{id}/stream` SSE channel is upgraded to multi-subscriber
fan-out; clients send mutations as ops via `POST /api/sessions/{id}/ops`. Rationale:
SSE is already wired end-to-end and proxy-friendly; a POST per op gives the server a
natural serialization point and lets the response carry the authoritative `seq`.
WebSocket remains a documented alternative if live-drag streaming (see D9) is ever
promoted into scope and demands it.

Op envelope (client → server):

```
POST /api/sessions/{id}/ops
{ "client_id": "...", "base_seq": 41, "ops": [ {op}, ... ] }
→ 200 { "applied": [ {op, seq} ], "seq": 43 }
```

Op catalogue (v1):

| Op | Payload | Conflict rule |
|---|---|---|
| `nodes_added` / `nodes_removed` | node_ids | set union / set removal, idempotent |
| `node_moved` | node_id, position | LWW per node (server arrival order) |
| `edges_added` / `edges_removed` / `edges_updated` | edges (`edges_removed`: edge_ids) | fan-out only — broadcast to all subscribers, no session state mutated (edges live in the graph, R14); a peer whose node set is unchanged never re-hydrates, so a drag-drawn edge, a deletion, or an attribute change between two present nodes needs this to render for everyone |
| `nodes_hidden` / `nodes_shown`, `edges_hidden` / `edges_shown` | ids | set ops, idempotent |
| `annotation_created` / `annotation_updated` / `annotation_deleted` | annotation | LWW per annotation id; update on deleted annotation is dropped |
| `group_membership_changed` | group_id, member_node_ids | LWW per group |
| `session_renamed` | name | LWW |
| `selection_claimed` / `selection_released` | element_ids | claim map with TTL, see 3.5 |
| `layout_applied` | positions map | LWW batch (single op so it wins/loses atomically) |

Server behavior: ops are applied under a per-session lock in arrival order, each gets
a monotonic `seq`, the session is persisted (debounced write-behind, flush ≤ 1 s),
and every applied op is broadcast on the event bus to **all** subscribers including
the originator (clients apply idempotently; the echo confirms ordering).

**Decision D2 — server-ordered last-write-wins, no CRDT/OT.** The entities are
coarse (a position, an annotation), users see each other's markers, and the server is
the single serialization point. LWW per entity in server arrival order is sufficient
and keeps state convergent and simple. Documented trade-off: a lost "simultaneous
move" is acceptable because the loser sees the node snap to the winner's position.

Client catch-up: on SSE (re)connect the client sends `?since_seq=N`; the server
replies with either the missed ops or a full state snapshot event if `N` is too old
(ops are retained in a small ring buffer per session, e.g. last 500).

### 3.4 Presence

Each connected client registers with `{client_id, display_name, color}`.
`client_id` is generated per browser and kept in localStorage; `display_name` is
user-editable in settings (default "Guest-<n>"); color is assigned by the server.
The hub broadcasts `presence_joined` / `presence_left` and exposes the current
roster in `GET /api/sessions/{id}` responses. Presence is ephemeral (never persisted).

**Decision D7 — no accounts in core.** Anyone with the session ID can join
(capability-URL model, same trust level as today's connect-by-ID). The SaaS layer
adds real identity and ACLs behind its own boundary.

### 3.5 Selection markers / soft locks

- Selecting elements emits `selection_claimed`; other clients render a colored
  outline + name badge on those elements.
- Claims are **advisory** (the server never rejects a move on a claimed node —
  conflicts still resolve via D2). Their purpose is social avoidance, exactly as
  requested.
- TTL: claims expire server-side after 30 s unless renewed (renewal piggybacks on
  activity/heartbeat); all claims are released on disconnect. This guarantees no
  element appears frozen by a departed user.

### 3.6 Session lifecycle & URL sharing

- **Auto-create:** on first load without a valid `?session=` URL param or usable
  recent session, the client generates an id locally
  (`generateVisualizationSessionId`) and enters the new session; the session is
  materialised server-side lazily on the first non-empty save / stream connect
  (see the lazy-connect note in the D-decisions), not via an eager
  `POST /api/sessions`.
- **URL sharing:** the active session ID is reflected in the URL
  (`?session=DDDD-DDDD`); opening such a URL joins that session (creating it
  server-side if it does not exist, preserving today's connect-by-ID behavior).
- **Rename / delete:** drawer gains a delete action next to rename. Delete calls
  `DELETE /api/sessions/{id}`; the server broadcasts `session_deleted` before
  removing state.
- **Deleting the active session:** the deleting client auto-creates a new session
  and switches into it. Other **actively connected** clients receive
  `session_deleted`, each auto-creates its **own** new session, and shows a notice
  ("Session was deleted by <name>") — they are not herded into one shared
  replacement. Users who are not connected get no takeover flow: the session simply
  disappears from their recents list the next time the drawer refreshes names from
  the server (D11).
- **Multi-user delete warning:** if the presence roster shows other connected
  clients, the confirm dialog states how many users are connected.
- **Recents:** `graph_sessions_index` in localStorage becomes a pure
  recently-visited list `{id, name, updatedAt}` — no snapshots. Names shown in the
  drawer are refreshed from the server when listed.
- **Legacy data:** no import of old localStorage snapshots (D10) — the
  localStorage session feature has not been rolled out to shared deployments, so
  leftover `graph_session_snapshot_*` keys are simply removed on upgrade. The
  recents index is kept as-is (names refresh from the server).

### 3.7 Frontend architecture changes

- `sessionStore.js` shrinks to the recents index.
- New `frontend/web/src/services/sessionSyncClient.js`: owns the SSE subscription,
  op sending (with small batching, e.g. flush every 100 ms), reconnect + catch-up,
  presence and claims state. Exposes an event interface `App.jsx` binds to the
  Zustand store and canvas signals.
- Remote-op application must be **echo-safe**: applying a remote op must not re-emit
  a local op (guard flag around store mutations, same pattern as `pendingGroups`).
- `packages/ui-graph-canvas` gains discrete callbacks (props, English-default
  labels per i18n rules): `onGroupRenamed`, `onGroupResized`, `onGroupMoved`,
  `onGroupMembershipChanged`, `onGroupDeleted`, plus render support for presence
  markers (`remoteSelections` prop) and new annotation kinds. The `onSaveView`
  round-trip remains only for explicit SavedView export.
- Viewport stays personal — pan/zoom is **not** synced (each collaborator frames
  their own view; positions and content are shared). "Follow user" is a possible
  later enhancement, out of v1 scope.
- Node moves sync on drag-end in v1 (D9); live drag streaming (~10 Hz throttled) is
  optional later polish, not part of the plan.

### 3.8 Compatibility with the MCP/SSE push channel

MCP tools (`search_graph`, `get_saved_view`, `connect_to_visualization_session`, …)
keep pushing visualization commands into sessions. After the fan-out upgrade these
pushes reach *all* connected clients of the session — which is the desired behavior
(an AI agent adding nodes is just another collaborator). The
`PATCH /sessions/{id}/state` "what is the browser showing" endpoint becomes
redundant once the server owns session state; it is kept as a thin shim during the
transition and removed in the final step.

### 3.9 Limits & safety

- Body caps per op batch (`_DEFAULT_MAX_OP_BATCH_BYTES`, 256 KB → `413`), max ops
  per batch (`_DEFAULT_MAX_OPS_PER_BATCH`, 500), max annotations per session, max
  ops/second per client (token bucket) with `429` + client backoff.
- Session IDs remain unguessable enough for the core's trust model
  (`crypto.getRandomValues`, 10^8 space) — acceptable for open deployments already
  exposing connect-by-ID; SaaS adds real authorization.
  - **Brute-force throttle (hardening).** The 10^8 space is only ~26.6 bits, so
    the auth-bypassed lookup endpoints (`GET /api/sessions/{id}` and the SSE
    stream handshake) apply a per-source token bucket
    (`SessionManager.check_lookup_rate`, 60 burst + 2/s) keyed on the real client
    IP and return `429` when exhausted. Normal open/reconnect traffic stays far
    under budget; an attacker is capped at ~2 guesses/second, turning a full
    enumeration into years of effort. Behind a reverse proxy set
    `TRUSTED_PROXY_HOPS` (Cloud Run: `1`) so the key is derived from
    `X-Forwarded-For` (counted from the right, spoof-resistant) instead of
    collapsing to the proxy address — one shared bucket for the whole internet.
    A high-entropy per-session stream token (so the short id is only a join code)
    was the planned follow-up, but it presupposes an authenticated
    session-creation channel: because sessions are materialised lazily over this
    same auth-bypassed stream (client-generated ids, no `POST /api/sessions`
    call), there is no authenticated point at which to deliver the creator its
    token, and its value is conditional on Basic Auth being active. The token
    scheme therefore needs a session-creation-flow decision first — tracked in
    `STRUCTURE_REVIEW.md` item A3 (step 2).
- All new endpoints respect the existing optional HTTP Basic Auth.
  - **Resolved (step 4, alternative A):** the CRUD/ops endpoints honour Basic
    Auth via request headers, but a browser `EventSource` cannot send an
    `Authorization` header, so `GET /api/sessions/{id}/stream` bypasses the auth
    middleware — protected instead by the unguessable session id, the same
    rationale as the legacy `/sessions/{id}/stream` bypass. Only the stream is
    exempt; the fetch-reachable CRUD/ops endpoints stay guarded.
- CORS defaults to same-origin only. `CORS_ALLOWED_ORIGINS` is unset by default,
  which now means *no* cross-origin access rather than a `*` wildcard — an
  operator opts specific origins (or `*`) in explicitly. This stops any website
  from driving an auth-bypassed instance from a victim's browser.

## 4. Out of scope for the open core (SaaS features)

Designed in the private SaaS repo (`docs/architecture/multi-user-session-collaboration.md`):

- Multi-instance scale-out: Redis-backed `SessionEventBus`, shared Postgres-backed
  `SessionPersistenceBackend`.
- Account-bound, server-stored recent-session history (replacing localStorage recents).
- Workspace/ACL-based session access control and named-identity presence
  (IdentityContext instead of guest names).
- Entitlement gating (plan tiers) of collaboration capabilities.

The core's obligation to SaaS is only: keep the two seams in 3.2 stable, and pass
through an optional identity context on session endpoints when present.

## 5. Implementation plan

Each step is one branch + one PR to `main`, owning its own tests and doc updates per
`CLAUDE.md` (review loop, full backend suite before PR, merge on green). Steps 1–3
are backend-only and invisible to users; the localStorage path keeps working until
step 4 switches the frontend over. Annotations come early (step 5, decision D12) so
realistic session content exists when realtime sync and presence are tested in
steps 6–8.

| Step | Status | Title |
|---|---|---|
| 1 | done | Server-side session store + REST CRUD |
| 2 | done | SSE fan-out hub + presence (backend) |
| 3 | done | Op protocol, conflict rules, catch-up |
| 4 | done | Frontend: server-backed session lifecycle |
| 5 | done | New annotation kinds (note, label, arrow) |
| 6 | done | Frontend: realtime op emit/apply + canvas events |
| 7 | done | Presence UI + selection claims |
| 8 | done | Hardening, multi-client e2e, docs sweep |

> **Implementation note (steps 1–3 landed together).** Step 3's op endpoint has
> nothing to apply ops to or broadcast through without step 1's store and step 2's
> event bus, so the three backend-only steps were built as one branch/PR:
> `backend/core/session_store.py` (store + persistence seam + op state transforms),
> `backend/core/session_hub.py` (event bus + presence + claims), and
> `backend/core/session_manager.py` (op orchestration, catch-up, rate limiting),
> wired through `/api/sessions*` in `rest_api.py`. Two deferrals, by design: the
> legacy `/sessions/{id}/state|stream` MCP-push channel is left fully intact (it
> still serves the current frontend until the step-4 cutover, and is removed only
> in step 8 per §3.8), and MCP pushes are *mirrored* to the new hub rather than
> moved off the legacy registry, so no live behaviour changes before the frontend
> switches over.

### Step 1 — Server-side session store + REST CRUD

- New `backend/core/session_store.py`: `Session` model (3.1),
  `SessionPersistenceBackend` protocol, `FileSessionPersistenceBackend`
  (`data/sessions/<id>.json`, atomic writes; no retention/eviction per D13).
- REST in `backend/service/rest_api.py`: `POST /api/sessions`,
  `GET /api/sessions/{id}` (meta + state + roster placeholder),
  `PATCH /api/sessions/{id}` (rename), `DELETE /api/sessions/{id}`,
  `GET /api/sessions` (meta list). Size caps and ID validation.
- Rehydration endpoint behavior: `GET .../{id}?resolve=true` returns state with node
  refs resolved to node objects (reuse `get_saved_view` fetch logic; keep `group-*`
  filtering consistent).
- Tests: `backend/core/tests/` (store, retention, atomicity) +
  `backend/api_host/tests/` or service tests (CRUD, caps). Update
  `backend/DEVELOPMENT.md` endpoint table (add the new endpoints **and** the
  pre-existing undocumented `/sessions/{id}/state|stream`).

### Step 2 — SSE fan-out hub + presence

- Rework `backend/core/session_registry.py` (or successor module) from
  single-consumer queue to a broadcast hub: per-subscriber queues,
  `SessionEventBus` protocol + `InProcessEventBus` impl, slow-consumer policy
  (drop + force snapshot resync), TTL only for presence, not stored sessions.
- Client registration on `GET /sessions/{id}/stream?client_id=..&name=..`; server
  assigns color; `presence_joined`/`presence_left` events; roster in session GET.
- MCP `_push_to_session` now publishes via the bus (all subscribers receive pushes).
- Tests: multi-subscriber delivery, disconnect cleanup, MCP push regression
  (`test_session_registry.py`, `test_session_endpoints.py`).

### Step 3 — Op protocol, conflict rules, catch-up

- `POST /api/sessions/{id}/ops`: per-session asyncio lock, op validation, apply
  functions per op type (table in 3.3), monotonic `seq`, debounced persistence,
  broadcast with `seq`, ring buffer (500 ops) + `since_seq` catch-up on stream
  connect, full-snapshot fallback event.
- Selection claim map with 30 s TTL + release on disconnect (server side only; UI
  comes in step 7).
- Tests: ordering under concurrent posts, LWW per entity, idempotent set ops,
  claim expiry, catch-up vs snapshot fallback, rate limiting.

### Step 4 — Frontend: server-backed session lifecycle

- Auto-create on load; `?session=` URL param read/write; drawer delete action with
  confirm (multi-user warning wired but roster may be empty until step 7 UI);
  delete-active → auto-create + switch; rename via PATCH.
- `sessionStore.js` reduced to recents index; legacy `graph_session_snapshot_*`
  keys removed without import (D10); load session state from server (resolved),
  save via full-state `PUT` **temporarily** (ops arrive in step 6) reusing the
  existing auto-save debounce.
- i18n: all new strings in both `en.json` and `sv.json`.
- Tests: rework `sessionStore.test.js`, `sessionFlow.test.jsx`,
  `SessionDrawer.test.jsx` (mock fetch); docs: `docs/USER_GUIDE.md` §5 + §8.3.

> **Implementation notes (step 4 as built).**
> - **Full-state save endpoint.** The temporary full-state save is
>   `PUT /api/sessions/{id}/state` (`{client_id, state}`), added in
>   `session_store.normalize_state` / `SessionStore.replace_state`,
>   `SessionManager.replace_state`, and `rest_api.py`. It validates the whole
>   state at the boundary (positions, annotations, size cap) and bumps `seq`.
>   Removed together with the other transitional shims when ops land (step 6/8).
> - **Lazy server materialisation (refinement of "auto-create").** The default /
>   new-session flow does **not** eagerly `POST /api/sessions` on every page
>   load; the client keeps generating the id locally and the session is created
>   server-side on its first non-empty save (`PUT` → `get_or_create`). Under D13
>   (no auto-eviction) an eager POST-per-load would accumulate empty session
>   files. `POST /api/sessions` remains available for SaaS/explicit use.
>   **Refined (R1 fix, §8.1):** this laziness applies only to a locally
>   generated id — an explicit join (`?session=` share URL, "Connect to
>   session") now connects eagerly regardless of whether anything has been
>   saved yet; see the step-6 note below.
> - **Groups via annotations.** Group boxes round-trip through the generic
>   `annotations` list as `kind: "group"` (label, color, size, `member_node_ids`)
>   so they survive the server save/load without waiting for the step-5
>   annotation UI.
> - **SSE stays on the legacy channel in step 4.** The browser still opens the
>   legacy `/sessions/{id}/stream` for MCP pushes and the realtime op-apply loop
>   over the new `/api/sessions/{id}/stream` arrives in step 6; the new stream's
>   auth bypass (§3.9, alternative A) is in place so that cutover is unblocked.

### Step 5 — New annotation kinds (note, label, arrow)

- `note` (sticky note), free-floating `label`, and `arrow` components in
  `packages/ui-graph-canvas`; creation via context menu; arrows anchored to a
  point, node, or annotation. If arrow endpoint UX proves heavy, arrows may land
  as a second PR within this step — but they stay in v1 scope (D12).
- Persistence via step 4's full-state save (server annotation model from step 1 is
  already generic); op wiring follows in step 6.
- Runs early so realistic session content (notes, labels, arrows, groups) exists
  for testing realtime sync and presence in later steps.
- i18n keys, USER_GUIDE update (screenshot note for Jakob in PR body), canvas
  package tests.

> **Implementation notes (step 5 as built).**
> - **Overlay nodes.** Notes, labels and arrows are ReactFlow node types
>   (`note`/`label`/`arrow`, components in `packages/ui-graph-canvas`), mirroring
>   the existing group node. A shared `AnnotationContext` gives them a
>   `notifyChange` hook (schedules the host's session save) and English-default
>   labels (props-with-defaults i18n rule); host strings are wired through
>   `App.jsx` `contextMenuLabels` + `en/sv.json`.
> - **Creation via pane context menu.** Right-clicking empty canvas opens an
>   "add note / label / arrow" menu (a plain right-click; a right-drag still
>   pans, per `panOnDrag`). Groups keep their toolbar button.
> - **Persistence.** `GraphCanvas.onSaveView` now also emits an `annotations`
>   array; `App.jsx` translates all overlay kinds to/from the generic server
>   annotation model (`annotationsToOverlays`/`overlaysToAnnotations`) and stores
>   them via the step-4 full-state PUT. Restore uses a new
>   `annotationsToRestore` prop (`pendingAnnotations` store field), parallel to
>   groups. Op wiring still follows in step 6.
> - **Arrow scope (D12).** v1 arrows are free-floating point-to-point pointers
>   (stored as absolute `from`/`to` points, the base "anchored to a point"
>   case). Re-anchoring an arrow endpoint to a node or annotation is deferred as
>   later polish; the server model already carries the shape for it.

### Step 6 — Frontend: realtime op emit/apply + canvas events

- `sessionSyncClient.js` (3.7): op batching, SSE apply loop, echo-safe store
  application, reconnect + catch-up; replace step 4's full-state PUT with ops.
- `packages/ui-graph-canvas`: discrete group callbacks (3.7), emit position ops on
  drag-end, annotation CRUD ops for all kinds from step 5.
- Tests: vitest for sync client (fake EventSource), canvas package tests for new
  callbacks.

> **Implementation notes (step 6 as built).**
> - **`sessionSyncClient.js` — transport + state-diff op derivation.** The client
>   (`frontend/web/src/services/sessionSyncClient.js`) owns the op-protocol SSE
>   stream (`GET /api/sessions/{id}/stream`) and the upstream `POST /ops` channel.
>   Outgoing ops are derived by `computeOps(baseline, next)`: the host hands the
>   client a full-state snapshot (the same object the step-4 PUT used) and the
>   client diffs it against the last-synced baseline into the minimal op set.
>   This routes *every* mutation path (search, expand, drag-end, annotation and
>   group edits) through one place instead of wiring an explicit op emitter into
>   each, and inherently collapses a bulk position change into a single
>   `layout_applied` op (keeping big layouts under the server's per-batch cap).
>   Ops batch on a short debounce; transient POST failures requeue with backoff,
>   `400`/`413` drop to avoid poisoning the queue.
> - **Echo-safety without a store guard flag.** The baseline mirrors what the
>   server holds. A remote op is folded into the baseline (`applyOpToMirror`)
>   *before* the host applies it locally, so the resulting store change diffs to
>   nothing on the next snapshot — no bounce-back. This replaces the design's
>   "guard flag around store mutations" with a single authoritative mirror.
> - **Incremental remote apply (not reload).** Foreign ops are applied
>   entity-by-entity onto the store + canvas (`applyRemoteOp` in `App.jsx`):
>   `nodes_added` resolves the new nodes via `getNodeDetails`; positions,
>   annotations and group membership are pushed to the canvas through new props
>   (`remotePositions`, `remoteAnnotationOp`). This touches only the entities an
>   op names, so a concurrent local edit is never clobbered — unlike a wholesale
>   reload. A full reload + baseline reset is used *only* on reconnect catch-up
>   (`onResync`), when the local user was disconnected and not editing.
> - **Canvas events.** Drag-end already emitted `onNodePositionChange`; group
>   edits now notify the host too — `GroupNode` calls the shared annotation
>   `notifyChange` on rename, recolour, resize and delete (groups are annotations,
>   design 3.1), so those edits schedule a snapshot and thus the right ops
>   (`annotation_updated`, `annotation_deleted`, `group_membership_changed`).
>   Rather than five separate `onGroup*` callback props that the host would only
>   funnel back into the same snapshot/diff, group changes reach the server
>   through this one notifier + the central reconciler — the design's realtime
>   goal for group edits without redundant prop plumbing.
> - **Lazy connect preserved for locally generated ids.** The stream connects on
>   the first non-empty save or when loading an existing session — never
>   eagerly on load for a freshly generated id — so an empty, never-edited
>   session started fresh in this browser is still never materialised
>   server-side (the step-4 behaviour under D13). **Refined (R1 fix, §8.1):** a
>   join by explicit id — a `?session=` share URL or the drawer's "Connect to
>   session" — now connects the op stream eagerly even when the session does
>   not exist server-side yet, because opening the stream is itself what
>   materialises it (`get_or_create` in the stream endpoint); staying lazy
>   there left the joining collaborator permanently offline (no presence, no
>   ops) until a manual reload.
> - **Full-state PUT retired from the frontend.** `putSessionState` is removed;
>   `api.js` gains `getSessionStreamUrl`, `getSessionOpsUrl`. The `PUT
>   /api/sessions/{id}/state` endpoint itself is left in place for removal in
>   step 8 (§3.8), now unused by the client.
> - **Known v1 limitations (deferred to step 7/8 polish).** Live position apply
>   for a node being *re-parented* by a remote group-membership change does not
>   recompute the absolute↔relative offset, so such a node can jump until the next
>   position op; and a remote apply that lands within the local autosave debounce
>   of a brand-new local annotation could, in the reconnect-resync path only,
>   race it. Neither affects the common drag/add/remove/hide/annotate flows.

### Step 7 — Presence UI + selection claims

- Presence roster in the header/drawer (colored dots + names), remote selection
  markers on canvas (`remoteSelections` prop, colored outline + badge), claim
  emit/renew/release from local selection, auto-expiry handling.
- i18n for all labels (props-with-defaults in the canvas package).
- Tests: claim lifecycle in sync client, marker rendering in canvas tests.
- Docs: `docs/USER_GUIDE.md` new "Collaborating in a session" section.

> **Implementation notes (step 7 as built).** Backend-only step: the presence
> roster, colour assignment and the claim map (with 30 s TTL + disconnect
> release) already shipped in steps 2–3, so step 7 is purely the frontend that
> consumes them.
> - **Presence + claims in `sessionSyncClient.js`.** The sync client now tracks
>   the roster and the live claim map. It seeds both from the `roster`/`claims`
>   fields already present on the `snapshot`/`catch_up` events, updates the roster
>   on `presence_joined`/`presence_left`, and folds remote `selection_claimed` /
>   `selection_released` ops into the claim map (own echoes ignored — the local
>   user sees their own selection natively). `onPresence(roster)` and
>   `onSelections(map)` handlers push the derived state to the host;
>   `getRemoteSelections()` resolves each foreign claim to `{ color, displayName }`
>   and excludes the local client.
> - **Claim emit / renew / expire.** `setLocalSelection(ids)` diffs against the
>   previous selection and enqueues `selection_claimed` for added elements and
>   `selection_released` for removed ones through the existing `/ops` channel. A
>   15 s renewal timer re-claims the current selection so the server's 30 s TTL
>   never lapses mid-selection; a client-side prune drops any remote claim whose
>   mirrored TTL passes, so a departed collaborator's marker can never linger even
>   if its disconnect event is missed.
> - **Markers on the canvas.** `App.jsx` passes a `remoteSelections` map to
>   `GraphCanvas`, which injects `data.remoteSelection` onto the matching node.
>   `CustomNode` renders a coloured outline plus a name badge in the collaborator's
>   colour. Only real graph nodes carry claims (annotations/groups are excluded).
> - **Roster UI + identity.** A compact presence-dot row lives in
>   `FloatingHeader` (shown only once another user is present). `display_name` is
>   user-editable under Settings → Your presence (stored in `localStorage`,
>   applied on the next stream connect); when unset the server assigns
>   `Guest-<n>` as before.

### Step 8 — Hardening, multi-client e2e, docs sweep

- Playwright multi-context e2e: two pages, one session — node add/move, annotation
  create, rename, delete-with-warning, claim markers, reconnect catch-up.
- Remove the `PATCH /sessions/{id}/state` shim; rate-limit tuning.
- Docs sweep: `backend/DEVELOPMENT.md` final endpoint table,
  `docs/USER_GUIDE.md` §§2.5/2.6/5/8.3 consistency,
  `docs/DEPLOYMENT_AND_CONCURRENCY_ANALYSIS.md` — document the single-instance
  constraint and the two SaaS seams explicitly.

> **Implementation notes (step 8 as built).**
> - **Transitional state shims removed.** Both "full-state save" paths are gone:
>   the step-4 `PUT /api/sessions/{id}/state` (`replace_state` / `normalize_state`
>   in the store + manager) and the legacy `PATCH /sessions/{id}/state` browser
>   upload. Session state is now written **only** as incremental ops, so there is
>   a single source of truth (design §3.8).
> - **MCP query tools read server-owned state.** `connect_to_visualization_session`,
>   `get_visualization_session_state` and `clear_visualization` no longer read the
>   browser-uploaded blob (which no longer exists). Visible nodes come from the
>   shared-session store's `node_refs`. `get_visualization_session_state` also
>   reports the current selection, read from the advisory claim map and narrowed
>   to those same `node_refs` — claims are taken on *elements*, so an edge claim
>   never reaches a field named `selected_node_ids`.
>   A registry entry now means only "a browser is connected to
>   receive MCP pushes" — the gate those tools use for "session is open".
> - **Legacy push channel kept (scope boundary).** The legacy
>   `GET /sessions/{id}/stream` MCP-push channel stays: §3.8 keeps MCP command
>   pushes, the browser opens it eagerly on load, and the op stream (which
>   materialises lazily under D13) is not a drop-in replacement for reaching an
>   empty, never-edited session. Only the redundant *state upload* was removed, not
>   the push transport. Migrating pushes onto the hub is a possible later cleanup.
>   **Fixed (R5 fix, §8.1):** the sync client now applies the hub's broadcast
>   `command` events, and the browser stops opening the legacy stream once the
>   op stream has connected for the session — so a session with two or more
>   collaborators has every command reach everyone, not just whichever browser
>   wins the legacy channel's single queue. The legacy stream is still opened
>   (and still the only delivery path) before the op stream first connects, so
>   an unmaterialised session can still receive MCP pushes.
> - **Op-batch body cap (hardening + rate-limit tuning).** `apply_ops` now bounds a
>   batch by **size** (≤ 256 KB → `413`) as well as op **count** (≤ 500), catching a
>   single oversized op such as a `layout_applied` with tens of thousands of
>   positions that the count cap alone would miss (design §3.9). The per-client
>   token bucket (200 burst, 100 ops/s refill → `429`) is unchanged — generous for
>   the drag-end op cadence (D9) while still throttling a runaway client.
> - **Multi-client testing.** A deterministic multi-client integration test
>   (`backend/core/tests/test_session_multiuser.py`) drives two clients through one
>   session (presence, add/move fan-out, annotation create, claims, rename, delete
>   broadcast, reconnect catch-up) in CI; a Playwright multi-context spec
>   (`frontend/web/tests/e2e/shared-session.spec.js`) exercises the same scenarios
>   through the real UI + SSE transport (run locally, outside the core pytest CI).

## 6. Decisions

### Taken (revisit only with cause)

- **D1** Transport: SSE downstream + REST ops upstream; no WebSocket in core v1 (3.3).
- **D2** Conflicts: server-ordered LWW per entity; no CRDT/OT (3.3).
- **D3** Selection claims are advisory soft locks with 30 s TTL + disconnect release (3.5).
- **D4** Session state stores node **references** + layout + annotations, never node copies; annotations are a unified typed list (3.1).
- **D5** Two seams for SaaS: `SessionPersistenceBackend` and `SessionEventBus`; core ships file + in-process implementations only (3.2).
- **D6** localStorage keeps only the recents index; snapshots are server-side (3.6).
- **D7** Core identity is anonymous guest identity; session ID is the capability (3.4).
- **D8** Viewport is personal, not synced (3.7).
- **D9** *(2026-07-04)* Node moves sync on drag-end only in v1; live-drag streaming
  is optional later polish outside this plan (3.7).
- **D10** *(2026-07-04)* No import of legacy localStorage snapshots — the feature
  was never rolled out; leftover snapshot keys are removed on upgrade (3.6).
- **D11** *(2026-07-04)* On deletion of a shared session, actively connected
  clients each get their own new session with a notice; non-connected users just
  see it disappear from their recents list (3.6).
- **D12** *(2026-07-04)* All three annotation kinds (note, label, arrow) are in v1
  scope and are implemented early (step 5) so realistic content exists for testing
  realtime and presence.
- **D13** *(2026-07-04)* No automatic session retention/eviction in v1 — sessions
  persist until explicitly deleted, since some sessions may evolve into de-facto
  saved visualizations. Revisit together with session/SavedView convergence (3.2).
- **D14** *(2026-07-06)* The empty-canvas persistence guard (R4, §8.1) applies
  **only** while a session has never materialised server-side (no connected
  sync client): a fresh, never-edited session must not register (D13). Once a
  sync client is connected for the session, an intentionally empty state (last
  node removed, double-Escape clear, an MCP `clear_visualization`) is real
  content and must sync/save like any other state — it must not be resurrected
  from the last non-empty snapshot on reload, and it must reach collaborators.

### Open (owner: project owner — resolve before the step that needs them)

None currently — O1–O5 were resolved 2026-07-04 and recorded as D9–D13 above.

## 7. Documentation & i18n obligations (summary)

- `backend/DEVELOPMENT.md`: endpoint table — steps 1, 3, 8.
- `docs/USER_GUIDE.md`: session menu (§5), live control (§8.3), groups (§2.5), new
  collaboration section — steps 4, 5, 7; screenshots flagged in PR bodies.
- `frontend/web/src/i18n/en.json` + `sv.json`: every step touching UI.
- `packages/ui-graph-canvas`: all user-visible text via props with English defaults.
- `docs/DEPLOYMENT_AND_CONCURRENCY_ANALYSIS.md`: step 8.

## 8. Code review findings (2026-07-06)

Full review of every change landed for this feature (PRs #194–#200, commits
`bdf30b7..58d161a`). Verified during the review: full i18n key parity between
`en.json` and `sv.json` (and no missing keys used from the touched components);
all frontend unit tests green (155 web + 47 canvas package); the removed state
shims (`PUT /api/sessions/{id}/state`, `PATCH /sessions/{id}/state`) are gone
from both code and the `DEVELOPMENT.md` endpoint table; the auth-middleware
stream bypass matches §3.9 alternative A and is covered by tests.

The issues below are **to be fixed in follow-up sessions**, not in the review
branch. Ordered by severity within each group. Effort uses the
`SMALL_FIXES.md` scale (XS / S / M).

### 8.1 Functional bugs

- **Fixed** (`claude/multi-user-sessions-r1-r6-nuku0m`) **R1 — Joining a share URL for a not-yet-materialised session never connects
  the op stream.** `loadSessionFromServer` treats a 404 as "start empty and do
  not connect" and nothing ever retries, so a second user who opens
  `?session=<id>` before the first user's first save stays permanently offline
  (no presence, no ops) until a manual reload — contradicting §3.6 URL sharing.
  Consequence: the Playwright spec's first scenario
  (`frontend/web/tests/e2e/shared-session.spec.js` asserts presence dots
  *before* any content exists) cannot pass against the current lazy-connect
  behaviour, which suggests the spec was not re-run after the lazy-connect
  refinement. Fix direction: connecting the stream is what materialises a
  session server-side (`get_or_create` in the stream endpoint), so joining an
  explicit share URL should connect eagerly — lazy connect need only apply to
  locally generated ids. **File(s):** `frontend/web/src/App.jsx:1212-1229`
  (`loadSessionFromServer` catch), `504-512` (bootstrap). **Effort:** S–M.
- **Fixed** (`claude/multi-user-sessions-r1-r6-nuku0m`) **R2 — Slow-consumer resync is dead code.** `InProcessEventBus.publish` sets
  `Subscription.needs_resync` and enqueues a `{"type": "resync"}` sentinel
  (`backend/core/session_hub.py:98-105`), but `needs_resync` is read nowhere,
  the stream endpoint forwards the sentinel verbatim, and
  `sessionSyncClient._handleEvent` ignores unknown types — so a client whose
  queue overflowed (1000 events) silently diverges forever. Either the stream
  handler should translate the sentinel into a fresh `catch_up(session_id,
  None)` snapshot, or the client should treat `resync` as an `onResync`
  trigger. **File(s):** `backend/core/session_hub.py:101`,
  `backend/service/rest_api.py` (stream generator),
  `frontend/web/src/services/sessionSyncClient.js:637-639`. **Effort:** S.
- **Fixed** (`claude/multi-user-sessions-r1-r6-nuku0m`) **R3 — `annotation_created` is not idempotent server-side.** The op appends
  without checking for an existing id (`backend/core/session_store.py:477-486`).
  A lost POST response (server applied, network dropped the reply) makes the
  client requeue and resend the batch, producing two annotations with the same
  id; `annotation_updated` then only updates the first match while
  `annotation_deleted` removes both. Treat a create with an existing id as an
  update (upsert) to make client retries safe. **File(s):**
  `backend/core/session_store.py:477`. **Effort:** XS.
- **Fixed** (`claude/multi-user-sessions-r1-r6-nuku0m`, D14) **R4 — Empty canvas states are never persisted or propagated.**
  `persistSessionSnapshot` returns early when the store has zero nodes
  (`frontend/web/src/App.jsx:905`), so removing the *last* node, the
  double-Escape clear, and an MCP `clear_visualization` are (a) never sent to
  collaborators and (b) never saved — the content resurrects on reload and
  other clients keep diverged state. The guard's rationale (don't materialise
  unused sessions, don't lose content to an accidental clear) only applies to
  *never-materialised* sessions; once a session exists server-side, an
  intentional empty state must sync like any other. Needs a small design
  decision (e.g. keep the guard only while `syncRef` is unconnected).
  **File(s):** `frontend/web/src/App.jsx:900-934`. **Effort:** S (plus
  decision).
- **Fixed** (`claude/multi-user-sessions-r1-r6-nuku0m`) **R5 — MCP pushes do not reach all collaborators (design §3.8).** The legacy
  `/sessions/{id}/stream` channel is single-consumer: with two browsers in one
  session, each pushed command is consumed by exactly one of the two SSE
  connections, nondeterministically. The hub mirror
  (`backend/service/mcp_tools.py:658`) does broadcast to everyone, but
  `sessionSyncClient` explicitly ignores `command` events
  (`sessionSyncClient.js:637-639`, "handled by the legacy stream"). Node
  additions still converge indirectly (the receiving client's autosave emits
  ops), but `clear_visualization` / `load_visualization` leave clients
  diverged (compounded by R4). Fix direction: have the sync client apply
  `command` events (dedup against the legacy channel, or stop opening the
  legacy stream once the op stream is connected). **File(s):**
  `frontend/web/src/services/sessionSyncClient.js`, `frontend/web/src/App.jsx`
  (legacy EventSource effect), `backend/service/mcp_tools.py:631-661`.
  **Effort:** M.
- **Fixed** (`claude/multi-user-sessions-r1-r6-nuku0m`) **R6 — MCP `visible_node_ids` now includes hidden nodes.**
  `_session_view_state` (`backend/service/mcp_tools.py:43-58`) reports all
  `node_refs` without subtracting `hidden_node_ids`; the browser-uploaded state
  it replaced reported only visible nodes. `get_visualization_session_state`
  and `connect_to_visualization_session` therefore overcount. **File(s):**
  `backend/service/mcp_tools.py:43`. **Effort:** XS.

### 8.2 Smaller correctness / UX gaps

- **Fixed** (`claude/session-r8-r10-r13-r14`) **R7 — Renaming an unmaterialised session loses the name.**
  `handleRenameSession` PATCHes the server and swallows the 404
  (`App.jsx:1312-1318`); when the session later materialises (name = null) the
  drawer's name-refresh overwrites the locally kept name with null
  (`App.jsx:516-531` writes `s.name` unconditionally). Either materialise on
  rename (get-or-create semantics for PATCH) or skip null server names in the
  refresh. Fixed via the first option (`SessionManager.rename_session` now
  calls `get_or_create` before applying the rename); the null-name refresh
  guard is added separately as defense in depth in the frontend/sync PR.
  **Effort:** S.
- **Fixed** (`claude/session-r8-r10-r13-r14`) **R8 — Renames are invisible to catch-up.** `SessionManager.rename_session`
  (`session_manager.py:141`) publishes a live event but does not bump `seq` or
  enter the ring buffer, so a client that reconnects through the `catch_up`
  (ops) path misses a rename that happened while it was away. Routing the REST
  rename through `apply_ops` (`session_renamed` is already a STATE_OP — today
  unreachable, no client emits it) would fix both this and the op's dead-path
  status. **Effort:** S.
- **Fixed** (`claude/session-r7-r9-r12-r15`) **R9 — Terminally rejected ops are dropped silently.** The sync client
  supports an `onDropped` handler, but `App.jsx` does not wire it
  (`App.jsx:1235-1256`), so a 400/413 drop (annotation limit, oversized
  `layout_applied`) leaves canvas state that silently never persists. The
  client also never splits an oversized queue proactively against the server's
  500-op/256 KB batch caps — it only falls back to one-at-a-time after a
  rejection. Wire `onDropped` to a notification + resync, and consider
  chunking flushes. **File(s):** `frontend/web/src/App.jsx`,
  `frontend/web/src/services/sessionSyncClient.js:513-558`. **Effort:** S.
- **Fixed** (`claude/session-r8-r10-r13-r14`) **R10 — Delete/rename race with in-flight op batches.**
  `delete_session` (`session_manager.py:150`) mutates the store without taking
  the per-session asyncio lock and pops the lock object; an in-flight
  `apply_ops` that already fetched the `Session` can `persist()` after the
  delete and resurrect the session file on disk. Similarly a concurrent REST
  rename during a failing batch is reverted by the batch's rollback
  (`saved_name`). Take the per-session lock in `delete_session` /
  `rename_session`. **Effort:** S.
- **Fixed** (PR #212, `fix: session switch drops queued ops and misreports load errors`) **R11 — Network errors treated like "session does not exist".**
  `loadSessionFromServer`'s catch clears the canvas and resets the baseline to
  empty for *any* failure; after a transient backend blip on an existing
  session, subsequent edits diff against an empty baseline and union stale +
  new state server-side. Distinguish 404 from other errors (retry/back off
  instead of clearing). **File(s):** `frontend/web/src/App.jsx:1220-1228`.
  **Effort:** S.
- **Fixed** (`claude/session-r7-r9-r12-r15`) **R12 — Group `description` is silently lost.** The canvas keeps a
  description on group nodes, but `groupsToAnnotations` drops it, and both the
  restore path and remote upserts hardcode `description: ''`
  (`App.jsx:65-79`, `GraphCanvas.jsx:878,949`). Under the old localStorage
  snapshots (full node copies) it survived reloads. Carry it through the group
  annotation payload (the server model is schemaless here) or remove the field
  from the canvas. **Effort:** XS.

### 8.3 Hardening / cleanup

- **Fixed** (`claude/session-r8-r10-r13-r14`) **R13 — `max_sessions` cap is in-memory only.** `SessionStore.session_count`
  counts the in-memory map, which starts empty on every restart while session
  files persist (D13: no eviction), so the unauthenticated stream endpoint
  (auth-bypassed by design) can grow `data/sessions/` beyond the cap across
  restarts. Also `list_meta` re-reads every session file on each
  `GET /api/sessions` / drawer open. Count the backend's files (cache the
  count) and consider a cheap meta cache. **File(s):**
  `backend/core/session_store.py:396-430`. **Effort:** S.
- **Fixed** (`claude/session-r8-r10-r13-r14`) **R14 — `manual_edges` is a dead session-state field.** Present in the
  `Session` model (§3.1) but no op writes it, the sync client's mirror ignores
  it, and the full-state PUT that could have populated it was removed in step
  8 — manually drawn edges persist in the graph itself since PR #186. Removed
  the field from the model and §3.1.
  **File(s):** `backend/core/session_store.py:77`. **Effort:** XS.
- **Fixed** (`claude/session-r7-r9-r12-r15`) **R15 — Duplicate/stale event delivery is tolerated but unguarded.** Events
  published between the stream's `connect` (subscribe) and the `catch_up`
  computation are delivered twice (once inside the snapshot/catch-up, once as
  queued events), and a late POST response can move `_seq` backwards past a
  newer broadcast seq. Everything is currently re-applied idempotently so this
  is benign, but an explicit `if (data.seq <= this._seq) return` guard on
  sequenced ops in `_handleEvent` would make the invariant robust instead of
  incidental. **File(s):**
  `frontend/web/src/services/sessionSyncClient.js:596-611`. **Effort:** XS.
  **Implementation note:** the guard is keyed on a new `_appliedSeq` (highest
  seq actually applied from a stream event), not `_seq` — `_flush()` also
  advances `_seq` optimistically to the ops-POST response's `body.seq` (the
  server's global seq right after the batch lands), which can already be ahead
  of a concurrent op whose broadcast is still in flight on the separate SSE
  connection; guarding on `_seq` there would have silently and permanently
  dropped that op instead of applying it once delivered (review caught this
  during the fix, regression test added). A related, narrower pre-existing gap
  in `since_seq` on *reconnect* (not touched by this fix) is logged in
  `SMALL_FIXES.md`.

### 8.4 Already tracked elsewhere

Related issues found (or re-confirmed) during this review that were logged as
entries in `SMALL_FIXES.md` — not repeated above. All are now **Fixed** (see
`SMALL_FIXES.md`'s Fixed section for the branch/PR that resolved each): the
MCP hub mirror's missing cross-thread delivery, presence/claims clobbering for
two concurrent connections with one `client_id` (also hit by two *tabs*
sharing the localStorage `client_id`, not just fast reconnects), the
synchronous fsync on the event loop, the post-parse op-batch byte cap, the
remote-added node (0,0) race, and the teardown flush in `_forceSingle` mode.

## 9. Extension seams for the hosted realtime layer

This section is the stable contract surface the open core promises to a hosted
deployment. It consolidates the seams that are otherwise described piecemeal in
§3.2 (persistence/fan-out), §3.4 (presence/identity) and §4 (out of scope) into
one catalogue, so a downstream realtime slice can bind to a named seam instead of
re-deriving it from the prose. Everything here is **general technical
enablement**: the core defines *where* a hosted layer plugs in and *what shape*
the plug is. Commercial packaging, plan/tier gating, tenancy and pricing live in
the private SaaS repository and are deliberately absent here.

The whole hosted realtime layer attaches at **one construction point** —
`backend/api_host/server.py`, where `SessionStore(...)` and
`SessionManager(...)` are built. A deployment overrides the two backends there
and threads an identity context through the request layer; nothing else in the
core needs to change.

| Downstream SaaS slice | Open-core seam it binds to | Core-shipped default | Where it is injected |
|---|---|---|---|
| Postgres-backed session store | `SessionPersistenceBackend` Protocol — `load` / `save` / `delete` / `list_meta` (`backend/core/session_store.py`) | `FileSessionPersistenceBackend` (one JSON file per session, atomic temp+rename) | `SessionStore(<backend>)` |
| Durability / retention | same `SessionPersistenceBackend` seam; the core adds **no** auto-eviction (D13), so a durable backend owns its own retention policy | file persistence, kept until explicit delete | `SessionStore(<backend>)` |
| Redis event bus (multi-instance fan-out) | `SessionEventBus` Protocol — `publish` / `subscribe` / `unsubscribe` (`backend/core/session_hub.py`) | `InProcessEventBus` (per-subscriber asyncio queues, slow-consumer drop→resync) | `SessionManager(store, event_bus=<bus>)` |
| Identity / named-identity presence | request-actor pass-through (`service.get_request_actor_info(headers=...)`) feeding the per-client `{client_id, display_name, color}` presence registration (§3.4) | anonymous guest identity (`Guest-<n>`, server-assigned colour); session id is the capability (D7) | session CRUD/stream endpoints in `rest_api.py` |
| Access control / session history | (a) session id as capability + optional HTTP Basic Auth + per-source lookup rate limit (§3.9); (b) the localStorage recents index as the *personal* history seam (§3.6) | no accounts, no ACLs, no server-stored cross-user history | endpoints honour an identity context **when present**, otherwise fall through to the capability model |

Contract obligations the core commits to, so hosted slices can depend on them:

1. **Seam signatures are stable.** The two Protocols above (`SessionPersistenceBackend`, `SessionEventBus`) are the versioned boundary. Method shapes change only with a documented migration note (per `CLAUDE.md` → Schema and Config Changes), because a hosted backend implements them out-of-tree.
2. **State is references + layout + annotations, never node copies** (D4/§3.1). A durable store persists exactly the `Session` shape in §3.1; node content is rehydrated from the graph on load. A Postgres backend therefore stores the same JSON document the file backend does, and needs no knowledge of graph internals.
3. **Ops are the single write path** (§3.8). Every state mutation is an op with a monotonic `seq`; a hosted bus fans out the identical applied-op events. Multi-instance ordering is the bus implementation's responsibility — the core guarantees per-session serialization only within one instance.
4. **Identity is optional and pass-through.** The core never *requires* an identity context; when a hosted layer supplies one (via the request-actor seam), the core carries it onto presence and endpoint handling without interpreting authorization. ACL enforcement is entirely the hosted layer's concern.

Related core contracts a hosted slice reads alongside this document:
[`MCP_SESSION_LIFECYCLE_CONTRACT.md`](MCP_SESSION_LIFECYCLE_CONTRACT.md) (session
lifecycle, ownership seam, canonical deep-link) and
[`DEPLOYMENT_AND_CONCURRENCY_ANALYSIS.md`](DEPLOYMENT_AND_CONCURRENCY_ANALYSIS.md)
(single-instance constraint and the two scale-out seams).
