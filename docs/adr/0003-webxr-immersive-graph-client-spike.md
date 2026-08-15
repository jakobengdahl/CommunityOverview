# ADR 0003 — WebXR immersive graph client (Quest) — spike

- **Status:** Proposed (spike / exploratory)
- **Date:** 2026-08-12
- **Scope:** Open-source core only — a new, additive frontend client. No change
  to the backend, the REST/MCP contracts, the session-sync protocol, or the
  existing 2D web client.
- **Related:**
  [`MULTI_USER_SESSIONS_DESIGN.md`](../MULTI_USER_SESSIONS_DESIGN.md) (the
  shared-session sync protocol this client reuses),
  [`MCP_SESSION_LIFECYCLE_CONTRACT.md`](../MCP_SESSION_LIFECYCLE_CONTRACT.md),
  [`MCP_VISUALIZATION_LAYOUT_CONTRACT.md`](../MCP_VISUALIZATION_LAYOUT_CONTRACT.md)

## Context

The graph today is rendered entirely by **React Flow** (`reactflow ^11.11` +
`dagre`) inside `packages/ui-graph-canvas`. React Flow is a 2D DOM/SVG library;
it cannot render into a WebXR immersive session, which requires a WebGL
framebuffer. There is no incremental "make React Flow immersive" path.

We want to explore running the graph on Quest-class standalone headsets through
the device's WebXR-capable browser, with the same core loop the desktop client
already offers: **create or connect to a session, then navigate it** — but with
head/controller/hand input instead of mouse and keyboard.

The important architectural fact is that everything *below* the rendering layer
is transport- and rendering-agnostic and therefore reusable:

- The **shared multi-user session** is an SSE stream
  (`GET /api/sessions/{id}/stream`) over a server-owned `SessionStore`, driven by
  a small op vocabulary: `nodes_added` / `nodes_removed`, `node_moved`,
  `layout_applied`, `nodes_hidden` / `nodes_shown`, `edges_hidden` /
  `edges_shown`, `annotation_*`, `group_membership_changed`, and the ephemeral
  presence + `selection_claimed` / `selection_released` claims.
- `frontend/web/src/services/sessionSyncClient.js` is **pure protocol**: it
  parses those ops and maintains session state, fully decoupled from React Flow.
- `frontend/web/src/services/api.js` covers the REST reads (node details,
  neighbours, search) an immersive client needs.
- Node positions in the protocol are 2D `{x, y}` in the shared session's own
  coordinate space.

An immersive client that speaks this same protocol interoperates with desktop
users for free: a headset user's `selection_claimed` / `node_moved` is seen by a
desktop user and vice versa.

## Decision

Build a **separate, additive `frontend/xr` workspace** (a WebGL client using
`three` via `react-three-fiber` + `@react-three/xr`) that reuses the existing
session-sync protocol and REST API. Do **not** attempt to port React Flow into
XR, and do **not** try to force the existing 2D canvas code to run in the
headset. Reuse the protocol and data layer; re-implement only the rendering and
interaction layer, which is inherently device-specific.

### Spatial model — a curved "dome", not a z-axis

We deliberately **do not** introduce a third position dimension in this spike.
The graph keeps its 2D `{x, y}` layout (the existing `dagre` output), and the XR
client maps that plane onto the **inside of a curved dome/cylinder section** that
wraps partway around the seated user:

- `x` maps to azimuth (angle around the user), `y` maps to elevation.
- Nodes are billboarded so their faces stay toward the viewer.
- **Zoom maps to dome radius**: zooming in shrinks the dome radius so the node
  shell comes closer and subtends a larger visual angle; zooming out pushes it
  further away. This gives a natural "the graph comes to me / recedes from me"
  navigation without ever needing per-node depth.

This keeps positions 100% compatible with the 2D protocol — a headset never
emits a coordinate a desktop client cannot represent — and defers the hard,
divergent question of a genuinely spatial 3D layout (and an optional protocol
`z`) to a later, separate decision if the spike justifies it.

### Input model

- **Phase 1 (navigation + read):** head-look, controller/hand **ray-cast
  selection** (point-and-pinch to select a node), dome rotate/zoom to navigate,
  and read-only in-world panels for the current selection and node detail. The
  client emits `selection_claimed` / `selection_released` and `node_moved`
  (drag a node along the dome surface) back into the shared session, so
  collaboration with desktop works immediately.
- **Phase 2 (deferred): a voice + AI-assistant input mode.** Rather than porting
  the ~40 DOM edit dialogs and fighting a virtual keyboard in VR, the plan is to
  route creation/editing/linking through a voice-driven assistant that performs
  the same graph mutations the dialogs perform today (via the existing REST/MCP
  write paths). This is explicitly **out of scope for the spike** and is
  captured as later work.

### What is explicitly out of scope for this spike

- Any port of React Flow, or reuse of `GraphCanvas.jsx`, into the headset.
- A z-axis / true 3D force-directed layout, and any protocol change to carry it.
- Full parity with the desktop dialog/menu surface (create/edit forms, chat
  panel, subscriptions, kiosk).
- Text entry via virtual keyboard.
- Fan-out of the MCP-push visualization registry (its single-consumer V1
  limitation is unchanged here; multi-viewer fan-out is noted as a dependency for
  simultaneous desktop+VR push scenarios).

## Reuse map

| Layer | Disposition |
|---|---|
| Backend (REST, MCP, `SessionStore`, SSE sync, session registry) | **Unchanged, reused as-is** |
| `sessionSyncClient.js` (op parsing + session state) | **Reused** (extract to a shared module or import across workspaces) |
| `api.js` (REST reads) | **Reused** |
| `graphLayout.js` (dagre wrapper) / `constants.js` (node type colours) | **Reused / shared** |
| `GraphCanvas.jsx` + React Flow rendering + DOM dialogs | **Not reused** — re-implemented in WebGL |

## Consequences

- **Positive:** the spike is self-contained, touches no backend and cannot
  regress the 2D client; desktop↔headset collaboration falls out of the shared
  protocol; the risky, device-specific work is isolated in one new workspace.
- **Cost / risk:**
  - WebXR requires a secure context (HTTPS). Deployed pilots already serve over
    HTTPS; local development against a headset needs an HTTPS dev server or a
    device tunnel (documented in the workspace README).
  - Legible in-world text needs SDF/MSDF text (e.g. `troika-three-text`); the
    graph is label-heavy and multilingual.
  - Large graphs need instancing + level-of-detail to hold framerate on
    standalone hardware.
  - Hand-tracking precision for fine node manipulation is uncertain on Quest
    Browser; the spike measures this rather than assuming it.
  - `sessionSyncClient.js` currently lives inside `frontend/web`; sharing it
    cleanly across workspaces may mean lifting it into a small shared package.
    The spike may start by importing it directly and defer extraction.

## Validation — what the spike must answer

1. Can a Quest-class headset connect to an existing session by short ID and
   render its live nodes/edges on the dome, staying in sync with a desktop user?
2. Is dome-radius zoom + ray-select a comfortable, usable navigation loop?
3. Does the reused `sessionSyncClient` drive the WebGL scene without changes to
   the protocol?
4. What is the realistic node-count ceiling before framerate degrades on-device?

If the answers are positive, a follow-up ADR will decide on Phase 2 (voice/AI
assistant input) and on whether a genuine 3D spatial layout is worth the protocol
divergence.
