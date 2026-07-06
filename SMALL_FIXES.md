# Small Fixes Backlog

Issues discovered during feature development that are pre-existing and out of
scope for the active session. Addressed in dedicated small-fix sessions.

See **Small-Fix Sessions** in `CLAUDE.md` for how to process this list.

## Entry format

```
### [YYYY-MM-DD] Short description
- **File(s):** `path/to/file.py:line`
- **Context:** Discovered during <branch-name>
- **Issue:** What the problem is and why it matters
- **Effort:** XS | S | M
```

Effort scale: XS = single-line fix · S = up to ~30 lines / one file · M = multi-file or logic-heavy

---

## Open

### [2026-07-06] Hardcoded English default title in `FloatingHeader`
- **File(s):** `frontend/web/src/components/FloatingHeader.jsx` (`title = 'Community Graph View'` prop default)
- **Context:** Discovered during `claude/multi-user-sessions-review-66xabr` (pre-existing, unrelated to the sessions feature)
- **Issue:** The header title defaults to a hardcoded English string inside a `frontend/web` component, violating the i18n rule in `CLAUDE.md` ("never hardcode display strings — use `useI18n()`"). The component already imports `useI18n` (for the presence roster), so the default can move to a `t('header.title')` key added to both `en.json` and `sv.json`.
- **Effort:** XS

### [2026-07-06] `torch` (heavy ML dep) is pinned in the base `requirements.txt`
- **File(s):** `backend/requirements.txt:36-37` (`--extra-index-url https://download.pytorch.org/whl/cpu`, `torch>=2.0.0`); also `sentence-transformers`, `scikit-learn` in the same file
- **Context:** Discovered during `claude/multi-user-sessions-step-8-ntxe0r`
- **Issue:** `CLAUDE.md` → Dependency Changes says "Never add ML/heavy dependencies (torch, sentence-transformers, etc.) to the base `requirements.txt` — they belong in `requirements-ml.txt`." Today `torch>=2.0.0` (with the `download.pytorch.org` extra index), `sentence-transformers>=2.2.0` and `scikit-learn>=1.0.0` sit in the base file. This bloats every install, and the pytorch extra-index makes the base install fail in networks that only allow PyPI (the `download.pytorch.org` CONNECT is refused → the whole `pip install -r requirements.txt` errors, blocking test/dev setup). Move these into `requirements-ml.txt` (or make embeddings/similarity optional) so the base install is pure-PyPI and lightweight; ensure the code degrades gracefully when the ML stack is absent (the tests already mock the embedding model, and similarity lazily imports sklearn).
- **Effort:** M

### [2026-07-06] Op-batch byte cap measures after FastAPI has parsed the whole body
- **File(s):** `backend/core/session_manager.py` (`apply_ops`, the `json.dumps(ops)` byte cap), `backend/service/rest_api.py` (`apply_session_ops`)
- **Context:** Discovered during `claude/multi-user-sessions-step-8-ntxe0r` (step-8 review, non-blocking)
- **Issue:** The 256 KB op-batch cap is checked on the already-parsed `ops` list, so an arbitrarily large request body is read and JSON-parsed into memory before the cap can reject it (returning `413` only afterwards). The removed legacy `PATCH /sessions/{id}/state` checked `len(body)` pre-parse. Pre-existing to the ops path (since step 3); worth a pre-parse `Content-Length` / raw-body guard at the endpoint for symmetry.
- **Effort:** S

### [2026-07-05] Remote-added node can land at (0,0) if its move op has not arrived yet
- **File(s):** `frontend/web/src/App.jsx` (`applyRemoteOp` `nodes_added`), `frontend/web/src/services/sessionSyncClient.js` (`baselinePosition`)
- **Context:** Discovered during `claude/multi-user-sessions-step-6-xn4a5v` (step-6 review, non-blocking)
- **Issue:** `nodes_added` resolves node details asynchronously and seeds `_savedPosition` from the sync baseline. If `getNodeDetails` resolves before the paired `node_moved` SSE op has been folded into the baseline, the node mounts at auto-layout/(0,0) and the next autosave can emit a `node_moved {0,0}` back. The window is very narrow (the move op travels the already-open SSE stream while `getNodeDetails` is a fresh HTTP round-trip), but not impossible. Fully closing it means also applying `remotePositions` to a node once it mounts (not only at add time).
- **Effort:** S

### [2026-07-05] Teardown flush drops queued ops while sync client is in `_forceSingle` mode
- **File(s):** `frontend/web/src/services/sessionSyncClient.js` (`flush`/`close`), `frontend/web/src/App.jsx` (session-change cleanup)
- **Context:** Discovered during `claude/multi-user-sessions-step-6-xn4a5v` (step-6 review, non-blocking)
- **Issue:** On session switch/unmount the cleanup calls `flush()` then `close()`. In the rare `_forceSingle` error-recovery state `flush()` only sends one op (`splice(0,1)`) and `close()` clears the rest, so remaining queued ops are lost. `_forceSingle` is a rare state and teardown flush is best-effort, so impact is minimal.
- **Effort:** XS

### [2026-07-05] `GroupNode` label input lacks ReactFlow `nodrag` class
- **File(s):** `packages/ui-graph-canvas/src/components/GroupNode.jsx:179`
- **Context:** Discovered during `claude/multi-user-sessions-step-5-bjoyp3`
- **Issue:** The group's inline label `<input>` has no `nodrag` class, so drag-selecting text inside it drags the group node instead of selecting text. The step-5 note/label editors were given `nodrag`; GroupNode should match for consistency.
- **Effort:** XS

### [2026-07-05] Debug `console.log` left in `GraphCanvas.onNodeDragStop`
- **File(s):** `packages/ui-graph-canvas/src/components/GraphCanvas.jsx:468,513,533`
- **Context:** Discovered during `claude/multi-user-sessions-step-5-bjoyp3`
- **Issue:** `onNodeDragStop` logs three `console.log('[GraphCanvas] ...')` statements on every drag (drag stop, group enter, group exit). These are debug artifacts that spam the browser console during normal use. Remove them (or gate behind a debug flag).
- **Effort:** XS

### [2026-07-03] Backend test suite mutates checked-in `backend/test_graph_auth.json`
- **File(s):** `backend/test_graph_auth.json`
- **Context:** Discovered during `claude/session-sidebar-nav-sfs73g`
- **Issue:** Running `pytest backend/ -q` leaves `backend/test_graph_auth.json` modified in the working tree (a test writes to the checked-in fixture instead of a tmp copy). Every full-suite run dirties the repo and risks accidental commits of test-run artifacts. Fix: locate the test(s) using this path and point them at a `tmp_path` copy, or gitignore a generated location.
- **Effort:** S

### [2026-07-02] `GroupNode`'s local context menu doesn't close on keyboard-only Tab focus
- **File(s):** `packages/ui-graph-canvas/src/components/GroupNode.jsx:34-58`
- **Context:** Discovered during review of `claude/context-menu-search-close-5w7tqy`
- **Issue:** `GroupNode`'s own context menu (right-click on a group) dismisses on `mousedown`/`contextmenu` outside the menu, which already covers clicking into the search box or chat input with the mouse. But if a user reaches those inputs via Tab (keyboard-only, no mousedown), the group's context menu stays open. The new `closeMenusSignal` mechanism added for the node/edge/multi-select context menus doesn't cover this either, since `GroupNode` manages its menu state independently. Fix: have `GroupNode` also listen for `focusin` on `document`, or subscribe to the same `closeMenusSignal` store field.
- **Effort:** S

### [2026-06-30] `t()` fallback pattern in FloatingHeader.jsx silently breaks
- **File(s):** `frontend/web/src/components/FloatingHeader.jsx:134,140` — *note: as of `claude/session-sidebar-nav-sfs73g` these occurrences are gone (menu moved to `SettingsDialog.jsx`, which calls `t()` without the `|| 'fallback'` pattern). The general `t()`-returns-key-on-miss behaviour described below still applies to the hook contract.*
- **Context:** Discovered during i18n audit / docs session
- **Issue:** Code uses `t('key') || 'fallback'` expecting a null/undefined return when a key is missing, but `t()` returns the key name as a string (truthy) when no translation is found. The `|| 'fallback'` branch never fires. The two immediately affected keys (`menu.view_section`, `menu.show_minimap`) were fixed by adding them to the JSON files. Any future missing key will silently show its key name in the UI. Fix: either update the fallback pattern to use `t('key') === 'key' ? 'fallback' : t('key')`, or make `t()` return null on a miss (breaking change to the hook contract).
- **Effort:** S

### [2026-07-04] Shared-session persistence is a synchronous fsync on the event loop
- **File(s):** `backend/core/session_manager.py` (`apply_ops` → `store.persist`), `backend/core/session_store.py` (`FileSessionPersistenceBackend.save`)
- **Context:** Discovered during review of `claude/multi-user-sessions-step-3-ojk3mi`
- **Issue:** `apply_ops` persists synchronously (atomic temp+rename with `fsync`) once per batch, inside the async handler. Design §3.3 specifies "debounced write-behind, flush ≤ 1 s". Correctness is fine (writes are atomic; batches are naturally debounced at the client's 100 ms flush), but under concurrent load the fsync blocks the event loop and works against the <500 ms round-trip target. Fix: move to a debounced write-behind flush (background task, coalescing per session) or offload the fsync via `asyncio.to_thread`. Perf optimization, not a bug.
- **Effort:** M

### [2026-07-04] MCP hub mirror lacks the cross-thread delivery the legacy registry has
- **File(s):** `backend/service/mcp_tools.py` (`_push_to_session`), `backend/core/session_manager.py` (`push_command`), `backend/core/session_hub.py` (`InProcessEventBus.publish`)
- **Context:** Discovered during review of `claude/multi-user-sessions-step-3-ojk3mi`
- **Issue:** The legacy `SessionRegistry.push_command_sync` falls back to `call_soon_threadsafe` for callers off the event-loop thread; the new hub mirror uses `queue.put_nowait` directly, which is not thread-safe on an `asyncio.Queue`. FastMCP runs tools on the loop thread today, so this is fine in practice, and the `try/except` in `_push_to_session` swallows any error — but the mirror would silently drop the message if a tool ever ran in a threadpool. Revisit when MCP moves fully onto the hub (step 6): give the bus an event-loop reference and a `call_soon_threadsafe` publish path.
- **Effort:** S

### [2026-07-04] Two concurrent connections for the same client_id clobber presence/claims
- **File(s):** `backend/core/session_hub.py` (`PresenceRegistry.leave`, `ClaimMap.release_all`), `backend/core/session_manager.py` (`disconnect`)
- **Context:** Discovered during review of `claude/multi-user-sessions-step-3-ojk3mi`
- **Issue:** Presence and claims are keyed by `client_id`. On a fast reconnect (old SSE not yet torn down), both connections share one roster entry and one claim owner; when the first closes, `disconnect` removes the roster entry and releases all of that client's claims even though the second connection is still live, so the still-connected client briefly vanishes from the roster and loses selection markers until its next heartbeat/claim. Mirrors the legacy registry's documented single-consumer limitation. Address with the presence UI in step 7 (e.g. per-connection tokens or refcounting per client_id).
- **Effort:** M

---

## Fixed

*(resolved entries moved here after merge, for reference)*

### [2026-07-05] Fixed in branch `claude/frontend-session-lifecycle-xys3wl` (multi-user sessions step 4)

- **Shared-session SSE stream not reachable via EventSource under Basic Auth** — `backend/api_host/server.py`. Resolved with alternative A: the auth middleware now bypasses `GET /api/sessions/{id}/stream` (EventSource cannot send an `Authorization` header; the stream is protected by the unguessable session id, same rationale as the legacy `/sessions/` bypass). Only the stream is exempt — the fetch-reachable CRUD/ops endpoints stay guarded. Tests in `backend/api_host/tests/test_auth_middleware.py`.

### [2026-07-03] Fixed in branch `claude/next-small-fixes-bug-1izuu8`

- **`FloatingSearch` icon resolution ignored per-profile schema `icon` overrides** — `frontend/web/src/components/FloatingToolbar.jsx`, `frontend/web/src/components/FloatingSearch.jsx`. Replaced the static `ICON_MAP` export (built only from `LEGACY_ICON_MAP`) with the shared `resolveIcon(nodeType, schema)` helper, so search-result icons honor `schema.node_types[type].icon` overrides exactly like the toolbar does. Removed the now-dead `ICON_MAP` export.

### [2026-07-03] Fixed in branch `claude/next-pbug-small-fixes-zoij5e`

- **`expert_agents[].icon` was dead config and `ess-expert` used a non-existent icon name** — `frontend/web/src/components/ExpertAgentSelector.jsx`, `config/stat-metadata/schema_config.json`. Wired the expert-agent list to resolve each agent's `icon` from the shared `ICON_REGISTRY` (falling back to `Robot`), colored by `agent.color`, replacing the generic hardcoded `Robot` and the redundant color dot. Corrected the `ess-expert` entry's invalid `GlobeFill` value to `GlobeEuropeAfricaFill`.

### [2026-07-02] Fixed in PR #187 (`claude/small-fix-md-review-jy2iln`)

- **`dialogOpenRef` is one render cycle behind actual dialog state** — `frontend/web/src/App.jsx`. Moved the `dialogOpenRef` update from `useEffect` to `useLayoutEffect` so it runs synchronously after commit (before the next paint / keydown), closing the double-Escape race.
- **`clearGroupsFlag` reset race if two clears fire within 100 ms** — `frontend/web/src/store/graphStore.js`. Replaced the two independent `setTimeout` calls with a shared module-level timer (`scheduleClearGroupsReset`) that cancels the previous timer before rescheduling, so overlapping clears no longer lower the flag prematurely.
- **`_parse_datetime` produces naive datetimes from timezone-unaware strings** — `backend/core/models.py`. Attach `timezone.utc` when a parsed ISO string has no timezone info, so datetimes loaded from pre-migration JSON stay aware and comparable. Regression tests added in `backend/core/tests/test_models.py`.
- **`federationDepthFlow.test.jsx` strict `toEqual` on the chat payload** — `frontend/web/tests/federationDepthFlow.test.jsx`. Changed the `sendChatMessage` options assertion from `toEqual` to `toMatchObject`, tolerating the extra context fields now sent alongside `federationDepth`. (Resolved the duplicate 2026-07-01 and 2026-07-02 entries for this same line.)
- **`GraphCanvas.test.jsx` edge-delete test asserts a stale Swedish label** — `packages/ui-graph-canvas/tests/GraphCanvas.test.jsx`. Matched the edge-delete button by its actual English default label (`/delete/i`) instead of `/ta bort/i`. (Resolved the duplicate 2026-07-02 and 2026-07-03 entries for this same test.)
