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

### [2026-07-11] Session-lookup `429` on the SSE handshake can leave the EventSource dead
- **File(s):** `frontend/web/src/services/sessionSyncClient.js:460` (`connect`/`onerror`)
- **Context:** Discovered during `claude/structure-backlog-next-e9w7tx` (STRUCTURE_REVIEW A3 step 1 review)
- **Issue:** The stream handshake can now return `429` (lookup rate limit). Per
  the WHATWG spec a non-2xx *initial* EventSource response fails the connection
  and does **not** auto-reconnect (only a drop of an already-open 200 stream
  does). `onerror` only sets `_ready = false` and relies on native reconnect, so
  a handshake `429` leaves a dead stream until a manual page reload. Low impact
  in practice — per-client keying (60 burst + 2/s) means only an abusive client
  hits it — but the client should schedule a backoff reconnect when the handshake
  is rejected rather than assuming the browser recovers.
- **Effort:** S

### [2026-07-11] `_TokenBucket` per-key state grows unbounded
- **File(s):** `backend/core/session_manager.py:81` (`_TokenBucket._tokens`/`._last`)
- **Context:** Discovered during `claude/structure-backlog-next-e9w7tx` (STRUCTURE_REVIEW A3 step 1 review)
- **Issue:** The token-bucket dicts are never evicted. The ops bucket is keyed on
  `client_id` (bounded by live sessions), but the new lookup bucket is keyed on
  client IP — an attacker rotating source IPs both defeats the per-IP limit and
  inflates the dicts without bound (memory pressure). Add idle eviction or an LRU
  size cap. (The per-IP-rotation weakness itself is inherent to IP rate limiting;
  A3 step 2's high-entropy stream token is the real remedy.)
- **Effort:** S

### [2026-07-11] Legacy `/sessions/{id}/stream` MCP-push channel is auth-bypassed but not rate-limited
- **File(s):** `backend/api_host/server.py:605` (`/sessions/{session_id}/stream`), bypass at `server.py:366`
- **Context:** Discovered during `claude/structure-backlog-next-e9w7tx` (STRUCTURE_REVIEW A3 step 1 review)
- **Issue:** A3 step 1 throttles the new `/api/sessions` lookup endpoints but the
  legacy visualization-push channel exposes the same `get_or_create`/enumeration
  oracle over the identical `\d{4}-\d{4}` space with no `check_lookup_rate`. It is
  slated for removal (design §3.8); until then it is the same weakness left
  unthrottled. Apply the same lookup rate limit, or prioritise its removal.
- **Effort:** S

### [2026-07-11] `EditEdgeDialog` uses hardcoded English strings instead of i18n
- **File(s):** `frontend/web/src/components/EditEdgeDialog.jsx` (`Edit Connection`, `Connection`, `Type`, `Label`, `Delete`, `Cancel`, `Save`, placeholder text)
- **Context:** Discovered during `claude/graph-history-ui`
- **Issue:** Unlike other dialogs, `EditEdgeDialog` does not use `useI18n()` for its
  existing labels — they are hardcoded in English, so the edge editor is not
  translated. (The History tab added in this branch does use i18n.) Should be
  migrated to `t()` with matching keys added to `en.json`/`sv.json`.
- **Effort:** S

### [2026-07-11] USER_GUIDE says double-click opens the edit dialog
- **File(s):** `docs/USER_GUIDE.md` (section 2.3)
- **Context:** Discovered during `claude/graph-history-ui`
- **Issue:** Double-clicking a node actually opens the *detail* dialog
  (`NodeDetailDialog`), from which Edit is a button — not the edit dialog
  directly. Updated the wording in this branch to match; noting here in case the
  screenshot/flow descriptions elsewhere assume the old behaviour.
- **Effort:** XS

### [2026-07-11] No per-collection opt-out for persisting CollectionResponse
- **File(s):** `backend/ui/chat_service.py` (`process_message`, response-tool install gate)
- **Context:** Discovered during review of `claude/active-collector-gui-inputs-0qj15m`
- **Issue:** `save_collection_response` is installed for every *resolved* collection (gated only on `collection_short_name and effective_prefix is not None`), independent of `node_type_permissions`. This is intentional — response-collecting is the collection's purpose and the write target type is hardcoded to `CollectionResponse` (no type-injection risk) — but an owner who configures a purely informational, read-only collection has no way to disable response persistence. Consider an explicit per-collection `collect_responses` flag (default on) if that use case materialises.
- **Effort:** S

### [2026-07-11] present_form action wins over a co-occurring pure-action tool
- **File(s):** `backend/ui/chat_logic.py` (`_handle_tool_use` final `pending_form` overlay)
- **Context:** Discovered during review of `claude/active-collector-gui-inputs-0qj15m`
- **Issue:** `toolResult` carries a single `action`. If the LLM calls `present_form` in the same turn as another pure-action tool (`mark_nodes`, `clear_visualization`, `start_guide`, `save_view`), the overlay makes `present_form` win and the other action is dropped (the frontend dispatches on `action ===`). Node/edge-returning tools are unaffected (their data rides in `nodes`/`edges`). This is an inherent single-`action` channel limitation, not specific to present_form; very rare in practice. A general fix would let the response carry multiple actions.
- **Effort:** M
- **File(s):** `frontend/web/src/components/CreateActiveKnowledgeCollectionDialog.jsx:6` (`EXCLUDED_TYPES`)
- **Context:** Discovered during `claude/active-collector-gui-inputs-0qj15m`
- **Issue:** `get_schema` returns all node types including system ones, and the AKC permissions table filters only via `EXCLUDED_TYPES`, which omits `Skill`. So `Skill` (a system type users never create in a collection) shows up as a create/update/delete row in the collection permissions table. `CollectionResponse` was added to `EXCLUDED_TYPES` in this branch; `Skill` should likely be excluded too, but it is pre-existing and out of scope here. Consider excluding all `category: system` types generically instead of maintaining a hardcoded list.
- **Effort:** XS

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

### [2026-07-12] Fixed while extracting the remote-position logic (STRUCTURE_REVIEW B5 slice 1)

- **`pendingRemotePositionsRef` in `GraphCanvas` is never pruned for a node that's deleted or never mounts** — `packages/ui-graph-canvas/src/hooks/useRemotePositions.js`. The pending-positions logic moved out of `GraphCanvas.jsx` into a `useRemotePositions` hook; each pending entry now carries an arrival timestamp and the catch-up effect drops entries older than a 30s TTL, so a position whose node was removed or never mounts is pruned instead of leaking for the session. Regression test in `packages/ui-graph-canvas/tests/useRemotePositions.test.jsx`.

### [2026-07-07] Fixed in small-fix session (PRs #207-#210)

- **Remaining hardcoded tooltip strings in `FloatingHeader`** (PR #207) — `frontend/web/src/components/FloatingHeader.jsx`. Routed `title="Menu"`, the session-id tooltip, and the clear-canvas `title`/`aria-label` through `useI18n()`, adding `header.menu`, `header.session_id_tooltip`, `header.clear_canvas_tooltip`, `header.clear_canvas_aria` to both `en.json` and `sv.json`. Also added a dev-only `console.warn` in `frontend/web/src/i18n/index.jsx`'s `t()` when a key falls back to itself in both languages, as a non-breaking safeguard against future missing keys.
- **Backend test suite mutates checked-in `backend/test_graph_auth.json`** (PR #208) — `backend/tests/test_security_execute_tool.py`. Pointed the `unauthenticated_app`/`authenticated_app`/`auth_enabled_no_password_app` fixtures at `tmp_path` instead of a hardcoded relative filename, and removed the three stray checked-in JSON files (`test_graph_auth.json`, `test_graph_no_pw.json`, `test_graph_unauth.json`) they were writing to.
- **Op-batch byte cap measures after FastAPI has parsed the whole body** (PR #209) — `backend/service/rest_api.py`, `backend/core/session_manager.py`. `apply_session_ops` now takes the raw `Request`, checks `Content-Length` against the cap before reading the body, and re-checks the actual byte length before handing it to `SessionOpsRequest.model_validate_json()`. Added `SessionManager.max_op_batch_bytes` as a public property. Review also caught a 422-vs-500 bug in the new manual-parsing error path (an unencoded bytes value in a JSON-decode error), fixed with `jsonable_encoder()` and covered by new regression tests in `backend/api_host/tests/test_session_api.py`.
- **Remote-added node can land at (0,0) if its move op has not arrived yet** (PR #210) — `packages/ui-graph-canvas/src/components/GraphCanvas.jsx`. A remote position for a node that hasn't mounted yet is now held in a `pendingRemotePositionsRef` instead of being dropped when `remotePositions` clears; a new effect re-applies any pending entry once the node's mount changes the node list. Regression test in `packages/ui-graph-canvas/tests/GraphCanvasRemote.test.jsx` (verified to fail on the pre-fix code). A follow-on gap (the ref isn't pruned for a node that's deleted or never mounts) is logged as a new Open entry above.

### [2026-07-06] Fixed in branch `claude/small-fixes-list-0njxzz`

- **Hardcoded English default title in `FloatingHeader`** — `frontend/web/src/components/FloatingHeader.jsx`. The `title` prop default now falls back to `t('header.title')`; the key was added to both `en.json` and `sv.json`. The remaining hardcoded tooltip strings in the same component are logged as a new Open entry.
- **Teardown flush drops queued ops while sync client is in `_forceSingle` mode** — `frontend/web/src/services/sessionSyncClient.js`. `flush()` now drains the whole queue as sequential single-op batches when in force-single recovery, holding its own copy of the queue so a following `close()` cannot clear it. Regression test in `frontend/web/tests/sessionSyncClient.test.js`.
- **`GroupNode` label input lacks ReactFlow `nodrag` class** — `packages/ui-graph-canvas/src/components/GroupNode.jsx`. Added `nodrag` so drag-selecting text in the inline label editor no longer drags the group.
- **Debug `console.log` left in `GraphCanvas.onNodeDragStop`** — `packages/ui-graph-canvas/src/components/GraphCanvas.jsx`. Removed the three drag/group-enter/group-exit debug logs.
- **`GroupNode`'s local context menu doesn't close on keyboard-only Tab focus** — `packages/ui-graph-canvas/src/components/GroupNode.jsx`. The dismiss handler now also listens for `focusin` on `document` (capture phase), so Tab-focusing the search box or chat input closes the menu. Tests in `packages/ui-graph-canvas/tests/GroupNode.test.jsx`.

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
