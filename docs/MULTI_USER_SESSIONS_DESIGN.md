# Multi-User Shared Sessions — Design & Implementation Plan

**Status:** In progress — the backend foundation (steps 1–3), the step-4
frontend cutover (server-backed session lifecycle) and the step-5 annotation
kinds (note, label, arrow) are implemented; realtime op sync, presence UI and
hardening (steps 6–8) have not started.
**Scope:** Open-source core only. SaaS-specific extensions (multi-instance scale-out,
account-bound session history, workspace ACLs) are designed in the private SaaS
repository and are explicitly out of scope here (see "Out of scope" below).

This document is the source of truth for the multi-user shared session feature in
the open core. Each implementation step below is sized to be executed by one
development session as one branch + one PR against `dev`, following the Standard
Development Workflow in `CLAUDE.md`. Update the step status table as steps complete.

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

Two "session" concepts coexist today, keyed by the same `DDDD-DDDD` ID format:

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
  id: "DDDD-DDDD",            # unchanged format, crypto-random
  name: string | null,
  created_at, updated_at: iso8601,
  seq: int,                    # monotonic revision, incremented per applied op
  state: {
    node_refs: [node_id],      # graph nodes shown in this session (references only)
    positions: { node_id: {x, y} },
    hidden_node_ids: [..], hidden_edge_ids: [..],
    annotations: [ Annotation ],
    manual_edges: [ {id, source, target, label, type} ]   # session-local edges if any
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
  recent session, the client calls `POST /api/sessions` and enters the new session.
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

- Body caps per op batch (reuse `_SESSION_STATE_MAX_BYTES` scale), max annotations
  per session, max ops/second per client (token bucket) with `429` + client backoff.
- Session IDs remain unguessable enough for the core's trust model
  (`crypto.getRandomValues`, 10^8 space) — acceptable for open deployments already
  exposing connect-by-ID; SaaS adds real authorization.
- All new endpoints respect the existing optional HTTP Basic Auth.
  - **Resolved (step 4, alternative A):** the CRUD/ops endpoints honour Basic
    Auth via request headers, but a browser `EventSource` cannot send an
    `Authorization` header, so `GET /api/sessions/{id}/stream` bypasses the auth
    middleware — protected instead by the unguessable session id, the same
    rationale as the legacy `/sessions/{id}/stream` bypass. Only the stream is
    exempt; the fetch-reachable CRUD/ops endpoints stay guarded.

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

Each step is one branch + one PR to `dev`, owning its own tests and doc updates per
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
| 6 | not started | Frontend: realtime op emit/apply + canvas events |
| 7 | not started | Presence UI + selection claims |
| 8 | not started | Hardening, multi-client e2e, docs sweep |

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

### Step 7 — Presence UI + selection claims

- Presence roster in the header/drawer (colored dots + names), remote selection
  markers on canvas (`remoteSelections` prop, colored outline + badge), claim
  emit/renew/release from local selection, auto-expiry handling.
- i18n for all labels (props-with-defaults in the canvas package).
- Tests: claim lifecycle in sync client, marker rendering in canvas tests.
- Docs: `docs/USER_GUIDE.md` new "Collaborating in a session" section.

### Step 8 — Hardening, multi-client e2e, docs sweep

- Playwright multi-context e2e: two pages, one session — node add/move, annotation
  create, rename, delete-with-warning, claim markers, reconnect catch-up.
- Remove the `PATCH /sessions/{id}/state` shim; rate-limit tuning.
- Docs sweep: `backend/DEVELOPMENT.md` final endpoint table,
  `docs/USER_GUIDE.md` §§2.5/2.6/5/8.3 consistency,
  `docs/DEPLOYMENT_AND_CONCURRENCY_ANALYSIS.md` — document the single-instance
  constraint and the two SaaS seams explicitly.

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

### Open (owner: project owner — resolve before the step that needs them)

None currently — O1–O5 were resolved 2026-07-04 and recorded as D9–D13 above.

## 7. Documentation & i18n obligations (summary)

- `backend/DEVELOPMENT.md`: endpoint table — steps 1, 3, 8.
- `docs/USER_GUIDE.md`: session menu (§5), live control (§8.3), groups (§2.5), new
  collaboration section — steps 4, 5, 7; screenshots flagged in PR bodies.
- `frontend/web/src/i18n/en.json` + `sv.json`: every step touching UI.
- `packages/ui-graph-canvas`: all user-visible text via props with English defaults.
- `docs/DEPLOYMENT_AND_CONCURRENCY_ANALYSIS.md`: step 8.
