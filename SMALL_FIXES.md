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

---

## Fixed

*(resolved entries moved here after merge, for reference)*
