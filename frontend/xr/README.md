# @community-graph/xr — WebXR immersive graph client (spike)

Exploratory WebGL/WebXR client that runs the graph on Quest-class headsets,
following [ADR 0003](../../docs/adr/0003-webxr-immersive-graph-client-spike.md).
This is an **additive** workspace: it does not touch the backend, the REST/MCP
contracts, the session-sync protocol, or the existing 2D web client.

## What exists so far (scaffold)

- A WebXR entry (`src/App.jsx`) using `@react-three/fiber` + `@react-three/xr`
  with an **Enter VR** button and a placeholder scene.
- The pure **dome layout** geometry (`src/domeLayout.js`, unit-tested in
  `src/domeLayout.test.js`): it maps the graph's 2D `{x, y}` layout onto a
  curved dome around the viewer, with **zoom mapped to dome radius** and **no
  z-axis** — keeping positions compatible with the 2D protocol.

Placeholder nodes are rendered in the exact `{id, x, y}` shape the real
`sessionSyncClient` will provide, so the mapping is visible on-device before any
data plumbing exists.

## Not yet wired (next tasks)

Shared-session sync (create/connect by short ID), edges + SDF text labels,
dome-radius zoom navigation, controller/hand ray-select, and the read-only
in-world node panel. See ADR 0003 for the scope boundary.

## Run

```bash
npm install                 # from the repo root, once
npm run dev -w @community-graph/xr
```

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

The tests cover the pure dome geometry only; the R3F scene is validated on-device
via the smoke test above.
