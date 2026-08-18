# @community-graph/xr — WebXR immersive graph client (spike)

Exploratory WebGL/WebXR client that runs the graph on Quest-class headsets,
following [ADR 0003](../../docs/adr/0003-webxr-immersive-graph-client-spike.md).
This is an **additive** workspace: it does not touch the backend, the REST/MCP
contracts, the session-sync protocol, or the existing 2D web client.

## What exists so far

- A WebXR entry (`src/App.jsx`) using `@react-three/fiber` + `@react-three/xr`
  with an **Enter VR** button, session controls, and the dome scene.
- The pure **dome layout** geometry (`src/domeLayout.js`, unit-tested in
  `src/domeLayout.test.js`): it maps the graph's 2D `{x, y}` layout onto a
  curved dome around the viewer, with **zoom mapped to dome radius** and **no
  z-axis** — keeping positions compatible with the 2D protocol.
- **Shared-session sync**, reusing the existing protocol with no change to it:
  - `src/sceneSession.js` opens `GET /api/sessions/{id}/stream` through
    `frontend/web/src/services/sessionSyncClient.js` — imported across the
    workspace boundary, unmodified — and reloads the authoritative session over
    REST on connect and on every resync, exactly as the 2D client does.
  - `src/sceneModel.js` reduces the op stream (`nodes_added` / `nodes_removed`,
    `node_moved`, `layout_applied`, `nodes_hidden` / `nodes_shown`,
    `session_renamed`) plus presence and remote selection claims into the flat
    scene the renderer draws. Pure and unit-tested in `src/sceneModel.test.js`.

Until a session is connected the scene shows placeholder nodes, so the dome
geometry is still walkable on-device with no backend running.

### Connecting to a session

**New session** asks the backend for one; the short ID field joins an existing
one (`0000-0000-0000-0000`, or the legacy two-group form). The connected ID is
reflected into `?session=<id>`, so the desktop client's share link opens the
headset straight into the same session — typing sixteen digits in VR is the
worst part of the workflow.

A node a collaborator adds arrives as an id only, so its name and type are
fetched over REST; nodes render once a position op has placed them.

### Not yet wired (next tasks)

The client is **read-only on the protocol**: it renders the shared session but
emits no ops of its own. Controller/hand ray-select (and with it the outgoing
`selection_claimed` / `node_moved` ops), edges + SDF text labels, dome-radius
zoom navigation, and the in-world node panel are still to come — as is lifting
`sessionSyncClient.js` and the session helpers of `api.js` out of
`frontend/web` into a shared package, now that a second consumer exists. Edge
and annotation ops are therefore ignored by the scene model rather than reduced
into state nothing draws. See ADR 0003 for the scope boundary.

## Run

```bash
npm install                 # from the repo root, once
npm run dev -w @community-graph/xr
```

The dev server proxies `/api` to `http://localhost:8000`, so run the backend
(`./start-dev.sh` or the usual uvicorn command) alongside it to create or join
sessions.

On a desktop browser at `http://localhost:5173`, **Enter VR** does not fail for
lack of a headset: `@react-three/xr` defaults to injecting the IWER emulator on
`localhost`, so the button drops you into a simulated Quest 3 with emulated
controllers. That is the flat-preview development path, and it is why the build
emits an `emulate` chunk plus one per bundled emulator scene (`living_room`,
`music_room`, `office_small`, and so on). On a real headset the emulator bails
out as soon as native `immersive-vr` is reported, so the smoke test below
exercises the genuine runtime.

### Smoke test on a Quest headset

WebXR requires a **secure context**. The simplest path needs no certificates:

1. Enable Developer Mode on the headset and connect it over USB.
2. `adb reverse tcp:5173 tcp:5173`
3. Open `http://localhost:5173` in the Quest Browser (`localhost` is a secure
   context) and tap **Enter VR**.

For a wireless workflow, expose the dev server over the LAN (`host: true` is
already set) behind an HTTPS tunnel, or serve with a locally-trusted certificate
(e.g. `mkcert`). USB + `adb reverse` is the recommended default.

## Test

```bash
npm run test -w @community-graph/xr
```

The tests cover the pure layers — dome geometry, the op-stream reduction, and
the session wiring against a fake sync client. The R3F scene itself is validated
on-device via the smoke test above.
