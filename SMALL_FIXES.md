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

### [2026-06-30] `dialogOpenRef` is one render cycle behind actual dialog state
- **File(s):** `frontend/web/src/App.jsx:111-120`
- **Context:** Discovered during `claude/clear-canvas-button`
- **Issue:** `dialogOpenRef` is updated via `useEffect`, which runs after the commit phase. In the narrow window between a dialog opening (state set) and the next React render+commit, `dialogOpenRef.current` is still `false`. A rapid double-Escape immediately after opening a dialog (e.g. stats dialog) could pass the guard and trigger `clearVisualization`. Fix: use a synchronous update approach — either replace the `useEffect` with a `useLayoutEffect`, or derive `dialogOpenRef` directly from a single computed value updated via `useLayoutEffect`.
- **Effort:** S

### [2026-06-30] `clearGroupsFlag` reset race if two clears fire within 100 ms
- **File(s):** `frontend/web/src/store/graphStore.js:196`, `frontend/web/src/store/graphStore.js:250`
- **Context:** Discovered during `claude/clear-canvas-button`
- **Issue:** Both `updateVisualization` and `clearVisualization` set `clearGroupsFlag: true` then schedule a `setTimeout` to reset it to `false` after 100 ms. If a second clear fires before the first timeout elapses, the first timer will reset the flag prematurely on the second call's window, causing ReactFlow to miss the group-clearing signal. Pre-existing in `updateVisualization`; `clearVisualization` now shares the same pattern. Fix: cancel the previous timer before scheduling the next (store timer id in a module-level ref or closure variable).
- **Effort:** S

### [2026-06-29] `_parse_datetime` produces naive datetimes from timezone-unaware strings
- **File(s):** `backend/core/models.py:32-36`
- **Context:** Discovered during `claude/fix-pytest-warnings`
- **Issue:** Graph data persisted before the `utcnow()→now(timezone.utc)` migration stores timestamps without timezone info (e.g. `"2024-01-01T00:00:00"`). `_parse_datetime` passes these through unchanged, resulting in naive `datetime` objects. New nodes created after the migration carry aware datetimes. The two coexist in memory for any graph loaded from older disk data. Currently harmless — no code path compares `created_at`/`updated_at` across nodes — but a future sorting or filtering feature would hit `TypeError: can't compare offset-naive and offset-aware datetimes`. Fix: make `_parse_datetime` attach `timezone.utc` when the parsed string has no timezone info.
- **Effort:** S

### [2026-07-01] `federationDepthFlow.test.jsx` fails against current `ChatPanel` payload shape
- **File(s):** `frontend/web/tests/federationDepthFlow.test.jsx:73`
- **Context:** Discovered during `claude/icon-config-docs-lkte9g` (pre-existing failure, reproduced identically on `dev` before this branch's changes)
- **Issue:** The test asserts `sendChatMessage.mock.calls[0][2]).toEqual({ federationDepth: 2 })`, but `ChatPanel` now sends additional context fields (`selectedNodeIds`, `visibleNodeIds`, `expertAgentId`, `skillsContext`, `collectionShortName`) alongside `federationDepth`, so the exact-equality check fails. The test wasn't updated when those fields were added to the chat request payload. Fix: change the assertion to `toMatchObject({ federationDepth: 2 })` (matching the pattern already used for the `searchGraph` assertion two lines above), or list all current fields explicitly.
- **Effort:** XS

### [2026-07-01] `stat-metadata` profile references a non-existent icon name
- **File(s):** `config/stat-metadata/schema_config.json:319`
- **Context:** Discovered during `claude/icon-config-docs-lkte9g` (icon registry expansion + `docs/ICONS.md`)
- **Issue:** The `Actor` entry's `icon` field is set to `"GlobeFill"`, but `react-bootstrap-icons` has no such export — only `Globe` (no filled variant) exists. Since `resolveIcon()` in `FloatingToolbar.jsx` silently falls back to the legacy/default icon when a schema `icon` value isn't a registered key, this profile's actor nodes render with the wrong icon with no visible error. Fix: change the value to an existing icon, e.g. `GlobeEuropeAfricaFill` (now registered, see `docs/ICONS.md`).
- **Effort:** XS

### [2026-07-01] `FloatingSearch` icon resolution ignores per-profile schema `icon` overrides
- **File(s):** `frontend/web/src/components/FloatingToolbar.jsx:366-371`, `frontend/web/src/components/FloatingSearch.jsx:4,277`
- **Context:** Discovered during `claude/icon-config-docs-lkte9g` (icon registry expansion + `docs/ICONS.md`)
- **Issue:** `FloatingToolbar.jsx` exports two different icon-resolution paths: `resolveIcon()` (used by the toolbar itself) checks the schema's `icon` field first, falling back to `LEGACY_ICON_MAP` by node-type name. But the exported `ICON_MAP` — used by `FloatingSearch.jsx` for search result icons — is built *only* from `LEGACY_ICON_MAP`, keyed by node-type name, and never looks at `schema.node_types[type].icon` at all. So a profile that overrides a node type's icon via config gets the override in the toolbar but not in search results, which is an inconsistent and surprising behavior for anyone configuring custom icons. Fix: build `ICON_MAP` from the loaded schema the same way `resolveIcon()` does (or have `FloatingSearch` call a shared `resolveIcon`-based helper instead of a static map).
- **Effort:** S

### [2026-06-30] `t()` fallback pattern in FloatingHeader.jsx silently breaks
- **File(s):** `frontend/web/src/components/FloatingHeader.jsx:134,140`
- **Context:** Discovered during i18n audit / docs session
- **Issue:** Code uses `t('key') || 'fallback'` expecting a null/undefined return when a key is missing, but `t()` returns the key name as a string (truthy) when no translation is found. The `|| 'fallback'` branch never fires. The two immediately affected keys (`menu.view_section`, `menu.show_minimap`) were fixed by adding them to the JSON files. Any future missing key will silently show its key name in the UI. Fix: either update the fallback pattern to use `t('key') === 'key' ? 'fallback' : t('key')`, or make `t()` return null on a miss (breaking change to the hook contract).
- **Effort:** S

---

## Fixed

*(resolved entries moved here after merge, for reference)*
