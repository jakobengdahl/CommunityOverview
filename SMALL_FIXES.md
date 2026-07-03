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

### [2026-07-01] `FloatingSearch` icon resolution ignores per-profile schema `icon` overrides
- **File(s):** `frontend/web/src/components/FloatingToolbar.jsx:366-371`, `frontend/web/src/components/FloatingSearch.jsx:4,277`
- **Context:** Discovered during `claude/icon-config-docs-lkte9g` (icon registry expansion + `docs/ICONS.md`)
- **Issue:** `FloatingToolbar.jsx` exports two different icon-resolution paths: `resolveIcon()` (used by the toolbar itself) checks the schema's `icon` field first, falling back to `LEGACY_ICON_MAP` by node-type name. But the exported `ICON_MAP` — used by `FloatingSearch.jsx` for search result icons — is built *only* from `LEGACY_ICON_MAP`, keyed by node-type name, and never looks at `schema.node_types[type].icon` at all. So a profile that overrides a node type's icon via config gets the override in the toolbar but not in search results, which is an inconsistent and surprising behavior for anyone configuring custom icons. Fix: build `ICON_MAP` from the loaded schema the same way `resolveIcon()` does (or have `FloatingSearch` call a shared `resolveIcon`-based helper instead of a static map).
- **Effort:** S

### [2026-07-02] `GroupNode`'s local context menu doesn't close on keyboard-only Tab focus
- **File(s):** `packages/ui-graph-canvas/src/components/GroupNode.jsx:34-58`
- **Context:** Discovered during review of `claude/context-menu-search-close-5w7tqy`
- **Issue:** `GroupNode`'s own context menu (right-click on a group) dismisses on `mousedown`/`contextmenu` outside the menu, which already covers clicking into the search box or chat input with the mouse. But if a user reaches those inputs via Tab (keyboard-only, no mousedown), the group's context menu stays open. The new `closeMenusSignal` mechanism added for the node/edge/multi-select context menus doesn't cover this either, since `GroupNode` manages its menu state independently. Fix: have `GroupNode` also listen for `focusin` on `document`, or subscribe to the same `closeMenusSignal` store field.
- **Effort:** S

### [2026-06-30] `t()` fallback pattern in FloatingHeader.jsx silently breaks
- **File(s):** `frontend/web/src/components/FloatingHeader.jsx:134,140`
- **Context:** Discovered during i18n audit / docs session
- **Issue:** Code uses `t('key') || 'fallback'` expecting a null/undefined return when a key is missing, but `t()` returns the key name as a string (truthy) when no translation is found. The `|| 'fallback'` branch never fires. The two immediately affected keys (`menu.view_section`, `menu.show_minimap`) were fixed by adding them to the JSON files. Any future missing key will silently show its key name in the UI. Fix: either update the fallback pattern to use `t('key') === 'key' ? 'fallback' : t('key')`, or make `t()` return null on a miss (breaking change to the hook contract).
- **Effort:** S

---

## Fixed

*(resolved entries moved here after merge, for reference)*

### [2026-07-03] Fixed in branch `claude/next-pbug-small-fixes-zoij5e`

- **`expert_agents[].icon` was dead config and `ess-expert` used a non-existent icon name** — `frontend/web/src/components/ExpertAgentSelector.jsx`, `config/stat-metadata/schema_config.json`. Wired the expert-agent list to resolve each agent's `icon` from the shared `ICON_REGISTRY` (falling back to `Robot`), colored by `agent.color`, replacing the generic hardcoded `Robot` and the redundant color dot. Corrected the `ess-expert` entry's invalid `GlobeFill` value to `GlobeEuropeAfricaFill`.

### [2026-07-02] Fixed in PR #187 (`claude/small-fix-md-review-jy2iln`)

- **`dialogOpenRef` is one render cycle behind actual dialog state** — `frontend/web/src/App.jsx`. Moved the `dialogOpenRef` update from `useEffect` to `useLayoutEffect` so it runs synchronously after commit (before the next paint / keydown), closing the double-Escape race.
- **`clearGroupsFlag` reset race if two clears fire within 100 ms** — `frontend/web/src/store/graphStore.js`. Replaced the two independent `setTimeout` calls with a shared module-level timer (`scheduleClearGroupsReset`) that cancels the previous timer before rescheduling, so overlapping clears no longer lower the flag prematurely.
- **`_parse_datetime` produces naive datetimes from timezone-unaware strings** — `backend/core/models.py`. Attach `timezone.utc` when a parsed ISO string has no timezone info, so datetimes loaded from pre-migration JSON stay aware and comparable. Regression tests added in `backend/core/tests/test_models.py`.
- **`federationDepthFlow.test.jsx` strict `toEqual` on the chat payload** — `frontend/web/tests/federationDepthFlow.test.jsx`. Changed the `sendChatMessage` options assertion from `toEqual` to `toMatchObject`, tolerating the extra context fields now sent alongside `federationDepth`. (Resolved the duplicate 2026-07-01 and 2026-07-02 entries for this same line.)
- **`GraphCanvas.test.jsx` edge-delete test asserts a stale Swedish label** — `packages/ui-graph-canvas/tests/GraphCanvas.test.jsx`. Matched the edge-delete button by its actual English default label (`/delete/i`) instead of `/ta bort/i`. (Resolved the duplicate 2026-07-02 and 2026-07-03 entries for this same test.)
