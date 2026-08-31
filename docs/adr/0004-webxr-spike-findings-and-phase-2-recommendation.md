# ADR 0004 — WebXR spike findings and Phase 2 recommendation

- **Status:** Accepted (conditional go — see Decision)
- **Date:** 2026-08-31
- **Scope:** Open-source core only. This ADR records findings and a
  recommendation; it makes no backend, protocol, or 2D-client change itself.
- **Related:**
  [ADR 0003 — WebXR immersive graph client (Quest) — spike](0003-webxr-immersive-graph-client-spike.md)
  (the decision this ADR evaluates against its own four validation questions),
  [`frontend/xr/README.md`](../../frontend/xr/README.md) (current workspace
  state), [`MULTI_USER_SESSIONS_DESIGN.md`](../MULTI_USER_SESSIONS_DESIGN.md)

## Context

ADR 0003 authorized a self-contained spike — a new `frontend/xr` workspace
reusing the existing session-sync protocol — and posed four validation
questions to be answered before deciding on a Phase 2 (voice/AI input, or a
genuine 3D spatial layout). This ADR answers those four questions against the
spike's actual state in the repository today, and recommends whether to
proceed.

**Method.** This is a documentation-only synthesis session with no headset
available: it reads `frontend/xr/src/*.js`, `frontend/xr/src/*.test.js`,
`frontend/xr/README.md`, and the vitest suite's behaviour, and reports what is
actually built and actually tested — not what the spike originally intended to
build. Where the spike's own README states a claim (e.g. "validated on-device
via the smoke test"), that claim is repeated as *reported*, not independently
re-verified, since no physical Quest was available to this session. This
distinction matters below: two of the four questions can be answered from the
code with confidence; two cannot be answered without hands-on headset testing
that has not yet happened.

## Validation findings

### Q1 — Can a headset connect by short ID and stay in sync with a desktop user?

**Architecture: yes. On-device confirmation: not yet demonstrated in this repo.**

- `frontend/xr/src/sceneSession.js` wires `SessionSyncClient` (imported
  unmodified from `frontend/web/src/services/sessionSyncClient.js`) to open the
  same `GET /api/sessions/{id}/stream` the desktop client uses, reload the
  authoritative session over REST on connect/resync, and reduce the op stream
  plus presence/claims into a scene (`sceneModel.js`).
- `App.jsx` implements short-ID connect and create (`isValidSessionId`,
  `SessionControls`), reflects the connected id into `?session=<id>` so a
  desktop share link opens the headset straight into the same session, and
  renders the roster/node count once connected.
- This machinery is exercised by 37 unit tests across `sceneSession.test.js`
  (406 lines) and `sceneModel.test.js` (211 lines) against a **fake** sync
  client that drives the same handler surface the real SSE stream calls
  (`onReady`, `onResync`, `onRemoteOps`, `onPresence`, `onSelections`,
  `onSessionDeleted`) — including reload-generation races, buffered-ops replay
  during an in-flight reload, and delete-during-reload. This is a real and
  fairly thorough test of the *reduction logic*.
- What is **not** in the repo: any recorded result of an actual headset
  connecting to a real backend session alongside a real desktop client. The
  workspace README's own framing is explicit that this only happens through a
  manual "smoke test on a Quest headset" (USB + `adb reverse`) that "the R3F
  scene itself is validated on-device via" — i.e. by hand, not automated, and
  not captured anywhere as a result. No session log, benchmark note, or PR
  description in this repo records that smoke test having been run.

**Conclusion:** the sync mechanism is well-built and well-tested at the layer
that can be unit-tested. Whether it actually behaves correctly end-to-end on a
physical headset against a live backend, concurrently with a desktop
collaborator, is an open question this session cannot close.

### Q2 — Is dome-radius zoom + ray-select a comfortable, usable interaction loop?

**Cannot be answered — the interaction loop does not exist yet.**

- `domeLayout.js` implements the pure geometry, including `zoomToRadius()`
  (zoom scalar → clamped dome radius) — unit-tested in `domeLayout.test.js` —
  but **`App.jsx` never calls it**. `DomeNodes` calls
  `domePosition(n.x, n.y, bounds)` with no `radius` argument, so every node
  renders at the fixed `baseRadius` (6). There is no zoom state, no input that
  changes it, and no code path that would let a user shrink or grow the dome.
- There is no ray-cast selection, no controller/hand input handling, and no
  `selection_claimed` / `node_moved` emission anywhere in `frontend/xr/src`.
  The workspace is explicitly **read-only on the protocol** today — it renders
  the shared session but emits no ops of its own (per the README's own "Not
  yet wired" section, which lists ray-select, dome-radius zoom navigation, and
  the in-world node panel as still-to-come, alongside edges and SDF text).
- The only interaction currently in the scene is the flat-preview desktop
  camera used for local development (a fixed, non-interactive perspective —
  see the comment in `App.jsx` explaining its positioning), plus the
  `@react-three/xr` "Enter VR" button.

**Conclusion:** this question describes a Phase-1 feature that ADR 0003 itself
scoped as in-scope for the spike but that has not been implemented. There is
no usable navigation/selection loop to assess for comfort — on a headset or
otherwise. This is not a negative finding about the *idea*; it is a statement
that the spike, as it stands, has not yet reached the point where this question
is answerable.

### Q3 — Did the reused sessionSyncClient drive the WebGL scene with no protocol change?

**Yes — confirmed from the code.**

- `sceneSession.js` imports `SessionSyncClient` directly from
  `../../web/src/services/sessionSyncClient.js`: a cross-workspace import of
  the *same file*, not a fork or a copy. `git log` shows no modifications to
  `sessionSyncClient.js` attributable to the XR work; its handler surface
  (`onReady`, `onResync`, `onRemoteOps`, `onPresence`, `onSelections`,
  `onSessionRenamed`, `onSessionDeleted`) is used as-is.
- `sceneModel.js`'s `applyOp()` switches on the existing `STATE_OPS`
  vocabulary from `backend/core/session_store.py` (`nodes_added`,
  `nodes_removed`, `node_moved`, `layout_applied`, `nodes_hidden`,
  `nodes_shown`, `session_renamed`) and explicitly **falls through to a no-op
  default** for edge/annotation/group ops rather than inventing new op types
  for anything XR-specific — the code comment is direct about this: "carried
  by the protocol, not rendered by this client yet."
- No new REST endpoint, SSE event type, or op field was added. The dome
  geometry (`domeLayout.js`) consumes only the existing 2D `{x, y}` positions;
  there is no `z` field anywhere in the scene model or the ops it reduces.

**Conclusion:** ADR 0003's central architectural bet — that the protocol and
data layer are renderer-agnostic and reusable without modification — holds up
under inspection. This is the strongest and most confidently-answerable of the
four findings.

### Q4 — What is the measured/estimated node-count ceiling?

**Unmeasured. No benchmark exists in this repo.**

- `DomeNodes` in `App.jsx` renders one `<mesh>` with its own `boxGeometry` +
  `meshStandardMaterial` per node — a naive, unbatched draw call per node, with
  no instancing and no level-of-detail.
- ADR 0003 itself flagged this as an open risk ("Large graphs need instancing +
  level-of-detail to hold framerate on standalone hardware... the spike
  measures this rather than assuming it"), but no instancing/LOD work,
  framerate benchmark, or node-count test exists anywhere in `frontend/xr` —
  not in the source, not in the test suite, not in the README, not in the git
  history for this workspace.
- Labels are also unimplemented (no SDF/MSDF text component is wired in;
  `troika-three-text` is not a dependency), so even the render cost that does
  exist today is understated relative to what a labeled node would cost.

**Conclusion:** there is no data to report. Any number offered here would be
invented. The honest answer is that this question requires the missing
rendering work (at minimum: per-node meshes replaced with instancing, or a
measurement of the current naive approach) to exist before it can be measured
on real Quest hardware, and no such measurement has been attempted.

## Decision

**Conditional go: continue the spike; do not yet open a Phase 2 ADR.**

Two of the four questions the spike was meant to answer are answered
favorably and with real confidence:

- **Q3 (protocol reuse) is a clean yes.** The riskiest architectural
  assumption in ADR 0003 — that the session-sync protocol and REST layer are
  fully renderer-agnostic — is validated by direct code inspection, not just
  asserted.
- **Q1 (connect + sync) is architecturally sound and unit-tested at the
  reduction layer**, though not yet confirmed end-to-end on physical hardware.

Two are not answerable yet, and neither is a "no" — they are "not built":

- **Q2 (zoom + ray-select comfort)** cannot be judged because dome-radius zoom
  and ray-select do not exist in the workspace yet. This is Phase-1 scope per
  ADR 0003, not Phase-2 scope; it is unfinished spike work, not a deferred
  decision.
- **Q4 (node-count ceiling)** cannot be judged because no instancing/LOD or
  benchmark exists, and the current per-node-mesh approach was never intended
  to be the final answer.

Given that, deciding go/no-go on **Phase 2** (voice/AI input, or a genuine 3D
spatial layout) now would be premature: Phase 2 is explicitly gated in ADR
0003 on positive answers to all four questions, and half of them have not been
attempted yet, let alone answered. Recommending Phase 2 today would mean
guessing at Q2 and Q4 rather than measuring them.

**What "conditional go" means concretely:**

1. **Continue the Phase 1 spike work already scoped in ADR 0003** — wire
   controller/hand ray-select and the outgoing `selection_claimed` /
   `node_moved` ops, wire the dome-radius zoom control that `zoomToRadius()`
   already implements, add the in-world node panel, and add basic edge
   rendering. None of this requires a protocol change per the Q3 finding
   above, so it carries the same low architectural risk ADR 0003 already
   accepted.
2. **Run the two on-device validations this session could not perform:**
   - The README's own headset smoke test (USB + `adb reverse`, or an HTTPS
     tunnel), actually performed and its result recorded — ideally alongside a
     second, desktop client on the same session, to close Q1's open half.
   - A basic node-count/framerate check on-device — even a rough one (e.g.
     "N synthetic nodes rendered at frame rate X on a Quest 3") — to give Q4 a
     real number instead of an architectural guess. Instancing should be tried
     if the naive approach's ceiling turns out to be too low to be useful.
   - Once ray-select and zoom exist, an informal usability pass on Quest to
     give Q2 a real (even if subjective, single-tester) answer, rather than
     leaving it as "not attempted."
3. **Only then open the follow-up ADR** deciding Phase 2 (voice/AI input)
   and whether a genuine 3D spatial layout (and its protocol `z` divergence)
   is worth pursuing. That ADR should cite actual Q1/Q2/Q4 results from step 2
   rather than the architectural reasoning this ADR had to lean on for two of
   the four questions.

This is a go on the *direction* — nothing found here contradicts ADR 0003's
premise, and the strongest technical bet (protocol reuse) is confirmed — but
it is explicitly not yet a go on Phase 2 itself, because two of the four gating
questions have not been tried, not because they were tried and failed.

## Consequences

- **Positive:** the highest-risk architectural question (protocol reuse) is
  now confirmed rather than assumed, which de-risks continuing to invest in
  the spike. The remaining Phase-1 work (ray-select, zoom, panel, edges) is
  scoped, does not require backend or protocol changes per the Q3 finding, and
  builds directly on code that is already well-tested at the layers that can
  be unit-tested (dome geometry, scene reduction, session wiring).
- **Cost / risk:**
  - The spike still has no automated way to validate the parts that matter
    most for a go/no-go on Phase 2 — actual headset comfort and actual
    framerate at scale are fundamentally hands-on-hardware questions that no
    amount of additional unit testing substitutes for.
  - Deferring the Phase 2 ADR means this evaluation cycle repeats once the
    remaining Phase-1 work lands — a future session (or Jakob, hands-on) will
    need to actually run the smoke test and the node-count check before a
    Phase 2 decision can be made honestly.
  - If the on-device usability pass or the node-count ceiling turns out to be
    unfavorable, the "conditional go" in this ADR does not survive — it would
    need a superseding ADR reflecting an actual no-go, not a silent
    reinterpretation of this one.
