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

*(no open items)*

---

## Fixed

*(resolved entries moved here after merge, for reference)*

### [2026-07-04] Fixed in branch `fix/small-fixes-session-presence-reconnect`

- **Two concurrent connections for the same client_id clobber presence/claims** — `backend/core/session_hub.py` (`PresenceRegistry`), `backend/core/session_manager.py` (`disconnect`). Added a `_conn_counts` ref-count dict to `PresenceRegistry`: `join()` increments, `leave()` decrements and returns the member only when the count hits 0 (last live connection for that client_id). `SessionManager.disconnect()` now calls `presence.leave()` first and only triggers `claims.release_all()` + `presence_left` broadcast when `leave()` confirms all connections are gone. On a fast reconnect the old SSE closing silently decrements the count while the new SSE keeps the roster entry and claims intact. Regression tests added in `test_session_hub.py` (3 unit tests on `PresenceRegistry`), `test_session_manager.py` (full 6-step reconnect race), and `test_session_multiuser.py` (two-client integration scenario).

### [2026-06-30] Fixed in branch `fix/small-fixes-floatingheader-fallback`

- **`t()` fallback pattern silently breaks** — `frontend/web/src/i18n/index.jsx`. Added a `fallback` parameter (3rd argument) to `t()` in both the `I18nProvider` callback and the outside-provider fallback in `useI18n()`. When a key is absent and a fallback is supplied, `t(key, params, fallback)` now returns the fallback string instead of the key name. Without a fallback the existing behaviour (return key name, dev-mode `console.warn`) is unchanged. Focused regression test added in `frontend/web/tests/FloatingHeader.test.jsx` verifying that `t('nonexistent.key', undefined, 'Expected Fallback')` returns `'Expected Fallback'` and that omitting the fallback still returns the key name (backward-compatible).

### [2026-07-11] Fixed in branch `fix/small-fixes-user-guide`

- **USER_GUIDE says double-click opens the edit dialog** — `docs/USER_GUIDE.md` (section 2.3 and Agents section). Double-clicking a node opens the *detail* dialog (`NodeDetailDialog`), from which Edit is a button — not the edit dialog directly. Section 2.3 wording was already correct; corrected the Agents section (line 186) which still implied double-click edits directly.

### [2026-07-17] Fixed in branch `fix/small-fixes-prune-obsolete-open-items`

- **`present_form` action wins over a co-occurring pure-action tool** — `backend/ui/chat_logic.py` (`_handle_tool_use`), `frontend/web/src/components/ChatPanel.jsx` (`applyToolResultSideEffects`). Added `pending_extra_actions` accumulator that collects pure-action tool results (`mark_nodes`, `clear_visualization`, `start_guide`, `save_view`) during the tool-execution loop. When `present_form` is also present in the same turn, these are emitted as `toolResult.extra_actions` rather than being dropped by the `pending_form` overlay. The frontend's `applyToolResultSideEffects` now iterates over `extra_actions` and applies each after the main action, so the form still renders while the co-occurring side effects execute. Existing callers that read `toolResult.action` are unaffected. Four backend regression tests added in `TestExtraActionsWithPresentForm`; three frontend tests added in `ChatPanel.test.jsx`.

### [2026-07-17] Fixed in branch `fix/small-fixes-collection-response-optout` (PR pending)

- **No per-collection opt-out for persisting CollectionResponse** — `backend/ui/chat_service.py`. Added `collect_responses` flag (default `true`) to `ActiveKnowledgeCollection` metadata. When set to `false`, `_resolve_collection` omits the `save_collection_response` instruction from the collection-mode system prompt and returns `False` in the 3-tuple; `process_message` gates tool installation on the flag, so no `CollectionResponse` node can be created for that session. Permission enforcement is unchanged. Focused regression tests added in `backend/ui/tests/test_chat_service.py` (`TestCollectResponsesOptOut`).

### [2026-07-17] Fixed in branch `fix/small-fixes-tokenbucket-eviction`

- **`_TokenBucket` per-key state grew unbounded** — `backend/core/session_manager.py`, `backend/core/tests/test_session_manager.py`. Added periodic idle-key eviction to `_TokenBucket` so stale `client_id` / IP entries are removed after a long silence instead of accumulating forever under rotating keys. The sweep is consume-triggered and rate-limited by a sweep interval, so normal active clients keep the same effective behavior while stale lookup-bucket state is reclaimed. Added focused tests for stale-key eviction, active-key survival, evicted-key reset-to-fresh behavior, and sweep-interval gating.

### [2026-07-17] Fixed in branch `fix/small-fixes-mcp-hub-threadsafe`

- **MCP hub mirror lacked cross-thread delivery safety** — `backend/core/session_hub.py` (`InProcessEventBus`). Each subscription now records the event loop that created its queue, the subscriber registry is protected by a lock, and `publish()` fans out per subscriber: direct delivery on the current loop, `call_soon_threadsafe(...)` onto a different live loop, and dead-loop subscribers are pruned. The resync-overflow behavior is unchanged. Focused regression tests cover the on-loop fast path, cross-thread delivery, and subscribers attached to different event loops.

### [2026-07-17] Fixed in branch `fix/small-fixes-session-fsync`

- **Shared-session persistence blocked the event loop with a synchronous fsync** — `backend/core/session_manager.py` (`apply_ops`). Changed the single `self.store.persist(session)` call to `await asyncio.to_thread(self.store.persist, session)` so the blocking `fsync` in `FileSessionPersistenceBackend.save` runs in the default `ThreadPoolExecutor` instead of on the event loop. The per-session `asyncio.Lock` is still held during the await, so no other coroutine can observe a partially-applied batch; if the thread raises, the existing `except` block rolls back in-memory state and the ring buffer exactly as before. Two focused regression tests added: one verifies `persist` is called from a non-event-loop thread, and one verifies that a worker-thread `OSError` still rolls back state and suppresses broadcast.

### [2026-07-17] Fixed in branch `fix/small-fixes-session-429`

- **Session-lookup `429` on the SSE handshake left the EventSource dead** — `frontend/web/src/services/sessionSyncClient.js` (`connect`/`onerror`), `frontend/web/tests/sessionSyncClient.test.js`. The `onerror` handler now checks `source.readyState`: if it is `CLOSED` (2), the connection was permanently rejected (e.g. 429 rate-limit on the initial handshake) and the browser won't auto-reconnect; the handler tears down the dead source and schedules a backoff reconnect via `_scheduleReconnect()`. A non-closed error (readyState 0 — CONNECTING) is a transient drop of an already-open stream where native reconnect handles recovery, unchanged. `close()` cancels the reconnect timer. Three focused regression tests added.

### [2026-07-17] Fixed in branch `fix/small-fixes-edit-edge-i18n`

- **`EditEdgeDialog` hardcoded English strings** — `frontend/web/src/components/EditEdgeDialog.jsx`. All remaining hardcoded UI strings (`Edit Connection`, `Connection`, `Type`, `No specific type`, `Label`, placeholder text) replaced with `t()` calls. Delete/Cancel/Save reuse existing `context_menu.delete`/`common.cancel`/`common.save` keys. New `edit_edge.*` keys added to `en.json` and `sv.json`. Focused test added in `EditEdgeDialog.test.jsx`.

### [2026-07-17] Fixed in branch `fix/small-fixes-legacy-stream-rate-limit`

- **Legacy `/sessions/{id}/stream` MCP-push channel lacked lookup rate limiting** — `backend/api_host/session_stream.py`, `backend/service/rest_api.py`, `backend/api_host/tests/test_session_api.py`. The legacy auth-bypassed SSE route now applies the same per-source lookup throttling as `/api/sessions`, reusing the trusted-proxy-aware client-IP key helper so exhausted guesses return `429 rate limit exceeded` before the infinite stream starts. Added focused regression coverage for exhausted budget, allowed budget, and trusted-proxy anti-spoof behavior.

### [2026-07-17] Fixed in PR #247 (`fix/small-fixes-webhook-ssrf`)

- **Webhook SSRF check was not re-applied across redirects** — `backend/core/events/delivery.py`, `backend/core/events/tests/test_delivery.py`. Webhook delivery now re-validates every redirect target with `is_safe_url()`, resolves relative redirects against the current URL, preserves normal redirect method semantics (`301/302/303 -> GET`, `307/308` keep POST), and drops both blocked redirect targets and redirect loops without retrying. Added focused regressions for blocked internal redirects, safe redirects, safe relative redirects, and redirect-limit handling.

### [2026-07-14] Fixed in PR #246 (`codex/small-fix-skill-exclusion`)

- **`Skill` leaked into the Active Knowledge Collection permissions table and could survive edit-mode save** — `frontend/web/src/components/CreateActiveKnowledgeCollectionDialog.jsx`, `frontend/web/src/components/CreateActiveKnowledgeCollectionDialog.test.jsx`. Added `Skill` to the excluded system types list and sanitized `node_type_permissions` both when loading edit-mode metadata and when submitting, so excluded types are neither shown nor preserved in saved metadata. Added focused regression coverage for both rendering and edit-mode save behavior.

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
