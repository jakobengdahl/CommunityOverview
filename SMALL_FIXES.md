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

### [2026-07-04] Shared-session SSE stream not reachable via EventSource under Basic Auth
- **File(s):** `backend/service/rest_api.py` (`/api/sessions/{id}/stream`), `backend/api_host/server.py:362-366`
- **Context:** Discovered during review of `claude/multi-user-sessions-step-3-ojk3mi` (multi-user sessions step 2/3)
- **Issue:** The auth middleware bypasses `/sessions/` because a browser `EventSource` cannot send an `Authorization` header; the new shared-session stream lives under the API prefix at `/api/sessions/{id}/stream`, which is **not** bypassed. When `auth_enabled` is on, the new SSE stream is unreachable by EventSource at the step-4 frontend cutover. Design §3.9 says new endpoints should respect Basic Auth, so this is a genuine tension needing an owner decision: either bypass the new stream path too (it is already protected by the unguessable session id, the same rationale as the legacy bypass), or adopt a query-param / cookie token scheme for the stream only. CRUD/ops endpoints are unaffected (fetch can send headers). **Resolve before step 4.**
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
