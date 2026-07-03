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

### [2026-07-01] `stat-metadata` profile's `ess-expert` agent references a non-existent icon name, and `expert_agents[].icon` is dead config
- **File(s):** `config/stat-metadata/schema_config.json:319` (`presentation.expert_agents[].icon` for the `ess-expert` entry, not a node type), `frontend/web/src/components/ExpertAgentSelector.jsx:2,42`
- **Context:** Discovered during `claude/icon-config-docs-lkte9g` (icon registry expansion + `docs/ICONS.md`)
- **Issue:** Two separate problems here. (1) The value `"GlobeFill"` isn't a real `react-bootstrap-icons` export — only `Globe`, `Globe2`, and regional variants like `GlobeEuropeAfricaFill` exist. (2) More fundamentally, `expert_agents[].icon` isn't read by the frontend at all: `ExpertAgentSelector.jsx` renders a hardcoded `Robot` icon (line 42) for every agent regardless of `agent.color`/`agent.icon`, so this field currently has zero visible effect for any profile (`config/default`'s expert agents set `icon` too, same dead field). Fix: either wire `ExpertAgentSelector.jsx` to read `agent.icon` from `ICON_REGISTRY` (like `resolveIcon()` does for node types) and then correct the `GlobeFill` value to `GlobeEuropeAfricaFill`, or remove the unused `icon` field from `expert_agents` config entries across profiles if per-agent icons aren't wanted.
- **Effort:** S

### [2026-07-01] `FloatingSearch` icon resolution ignores per-profile schema `icon` overrides
- **File(s):** `frontend/web/src/components/FloatingToolbar.jsx:366-371`, `frontend/web/src/components/FloatingSearch.jsx:4,277`
- **Context:** Discovered during `claude/icon-config-docs-lkte9g` (icon registry expansion + `docs/ICONS.md`)
- **Issue:** `FloatingToolbar.jsx` exports two different icon-resolution paths: `resolveIcon()` (used by the toolbar itself) checks the schema's `icon` field first, falling back to `LEGACY_ICON_MAP` by node-type name. But the exported `ICON_MAP` — used by `FloatingSearch.jsx` for search result icons — is built *only* from `LEGACY_ICON_MAP`, keyed by node-type name, and never looks at `schema.node_types[type].icon` at all. So a profile that overrides a node type's icon via config gets the override in the toolbar but not in search results, which is an inconsistent and surprising behavior for anyone configuring custom icons. Fix: build `ICON_MAP` from the loaded schema the same way `resolveIcon()` does (or have `FloatingSearch` call a shared `resolveIcon`-based helper instead of a static map).
- **Effort:** S

### [2026-07-02] `GraphCanvas.test.jsx` edge-delete test asserts a stale Swedish label
- **File(s):** `packages/ui-graph-canvas/tests/GraphCanvas.test.jsx:136`
- **Context:** Discovered during `claude/context-menu-search-close-5w7tqy` (reproduced identically on `dev` before this branch's changes)
- **Issue:** The test looks up the edge context menu's delete button via `screen.getByRole('button', { name: /ta bort/i })`, but the component under test (`GraphCanvas.jsx`) renders the button with the English default label `Delete` (via `cml.delete`) unless a `contextMenuLabels.delete` prop is supplied — the test renders `<GraphCanvas>` with no `contextMenuLabels`, so the regex never matches and the test fails with a `getElementError`. Fix: either pass `contextMenuLabels={{ delete: 'Ta bort' }}` in the test render, or update the regex to match the actual default label (`/delete/i`).
- **Effort:** XS

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

### [2026-07-02] `federationDepthFlow.test.jsx` fails on the current `dev` baseline
- **File(s):** `frontend/web/tests/federationDepthFlow.test.jsx:73`
- **Context:** Discovered during `claude/manual-edges-persistence-3wcpx8` (reproduced identically on `dev` before this branch's changes)
- **Issue:** The test asserts `api.sendChatMessage.mock.calls[0][2]` deeply equals `{ federationDepth: 2 }`, but the chat request options object now carries additional fields (5 more keys), so the strict `toEqual` fails. The runtime behaviour is correct — the assertion is just too strict. Fix: assert the object *contains* `federationDepth: 2` (e.g. `expect.objectContaining`) instead of an exact match.
- **Effort:** XS

### [2026-07-03] `GraphCanvas.test.jsx` edge-delete test asserts Swedish label against English default
- **File(s):** `packages/ui-graph-canvas/tests/GraphCanvas.test.jsx:168`
- **Context:** Discovered during `claude/context-menu-select-by-type-olug86` (reproduced identically on `dev` before this branch's changes)
- **Issue:** The test "calls onDeleteEdge from edge context menu" queries `getByRole('button', { name: /ta bort/i })`, but the component renders no `contextMenuLabels`, so the delete button uses the English default `Delete`. The query never matches and the test fails. Fix: query for `/delete/i` to match the English default, or pass `contextMenuLabels={{ delete: 'Ta bort' }}` to the render.
- **Effort:** XS

---

## Fixed

*(resolved entries moved here after merge, for reference)*
