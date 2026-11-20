# Node shapes: performance scenes and accessibility review

The checks [NODE_SHAPES.md](../NODE_SHAPES.md) asks for before saved-shape
library writes are rolled out (§ Theme, accessibility and scale; § Migration
and small implementation phases, phase 6). This record states what was
measured and reviewed, and on which commit; it does not change whether
library writes are accepted. The code has no separate switch for them: since
phase 6d-a the REST library routes accept writes from an authenticated
principal and refuse them otherwise, and this change leaves that as it is.

## Performance scenes

### What a scene is

`frontend/web/perf/node-shapes/scene.js` builds a fixed synthetic graph from
seed 20261120: 1,000 or 5,000 nodes of four types on a grid, every node but
the first linked to an earlier one and every third node to a second, with
seeded names, summaries and a declared `level` field. A **shaped** scene gives
each type a saved library shape as its session type default, one base per
type (rectangle, rectangle-flat, triangle-up, triangle-down), all at the
scene's scale of 0.1, 1 or 10. Its **legacy** baseline is the same graph,
positions and edges with no shape selection, so every node draws the built-in
card. Grid spacing grows with the scale, so shapes never overlap and no layout
runs.

The page (`main.jsx`) mounts the production `GraphCanvas` with stable props,
as the app does, and the runner (`run.js`) drives a production build of it in
headless Chromium through Playwright. Each run opens a fresh page; legacy and
shaped alternate within a run, and the order flips every run. Themes are the
browser's `prefers-color-scheme`, as in the app.

### Metrics

| Metric | Measured as | Samples |
| --- | --- | --- |
| `initialRender` | Mount until the first 100 nodes and their edges are drawn and two frames painted (GraphCanvas shows 100 nodes at a time above 200) | 1 per run |
| `loadAll` | Pressing **Load More** until the whole scene and its edges are on the canvas | 1 per run |
| `panFrame` | Frame-to-frame time during a 60-frame right-button pan of the empty pane | 60 per run |
| `edgeUpdateFrame` | Frame-to-frame time during a 60-frame drag of the scene's fixed drag node, the one whose grid position is nearest the grid centre. It is chosen from the scene data, so both scenes drag the same node and redraw the same edges; `draggedEdges` reports how many | 60 per run |
| `shapeApply` | Switching the Actor type default (a quarter of the scene) to another saved shape and back, until drawn. The legacy card has no shape to apply, so its scene times a highlight of the same nodes instead; that is a different operation, reported for reference and never as a budget verdict | 4 per run (on, off, on, off) |
| `heapMB` | Used JS heap after the full scene loaded and a forced garbage collection | 1 per run |
| `requests` | Network requests the page made after it loaded | 1 per run |

The DOM node count and the dragged node's edge count are reported for
information. p50 and p95 are nearest-rank
percentiles over every sample of every run. A gesture that did not move the
view or the node is not counted, and a metric with no samples is reported as
not measured rather than as a number. The budget passes for a metric when the
shaped p50 and p95 are both less than 10% above the legacy p50 and p95, and
for requests when the shaped scene made no more than the legacy one. A run
that fails is counted and run again. After three such reruns the variant
stops, reports only the runs it completed, and the matrix goes on; a variant
with no completed run reports every metric, requests included, as not
measured. Each Load More click has its own 600 s timeout, so a slow 5,000-node
load is measured rather than counted as a failure.
`shapeApply` has no legacy equivalent, so its change is reported as not
comparable and it gets no verdict.

The runner records the commit (and whether the working tree had changes),
the CPU, cores, memory, OS and Node.js version, and asks the browser itself
for its version, user agent and WebGL renderer, so a software renderer
shows up as such rather than being assumed. These head the printed report, so
the table is never recorded without them.

### Running it

From `frontend/web`, after `npm ci` and `npx playwright install chromium`:

```bash
npm run perf:node-shapes -- --sizes 1000,5000 --scales 0.1,1,10 --themes light,dark --runs 5 --out results.json
```

It prints the environment and results tables below and writes the raw
summary as JSON. The full matrix is long-running and is not part of CI; CI runs only
`perf/node-shapes/scene.test.js`, which checks the scenes and the budget
arithmetic.

## Results

### Gate run: not yet recorded

The matrix the gate asks for has not been run: 1,000 and 5,000 nodes, three
scales, both themes, five runs. None of the p50 or p95 values here is a
budget result yet, and the performance gate stays open until that run is
pasted below, with the environment table the runner prints.

### Smoke run

This run checks that the harness works end to end. It is not a budget
verdict. It used one run per scene and small graphs: 150 nodes, drawn whole on
mount, and 250 nodes, which goes through Load More. It ran at scale 1 only, in
both themes, on a shared host whose load average was 27 to 34. The commit is
3eeaa0d with this change's harness fixes not yet committed (`main.jsx`,
`run.js`; the measured canvas code is unchanged). The smoke run therefore
measured canvas code from before 7b08a1c, which only reorders the shape shadow
under the boundary. The renderer the browser
reported is SwiftShader, which is software rendering.

| Run | Value |
| --- | --- |
| commit | 3eeaa0d4b6fade1b9f2b745f1116bf156fbe6dbf (uncommitted changes) |
| seed | 20261120 |
| runs | 1 |
| cpu | Intel(R) Xeon(R) Gold 6140 CPU @ 2.30GHz |
| logicalCores | 72 |
| memoryGB | 629 |
| os | Linux 6.12.107+deb13-amd64 |
| node | v22.23.4 |
| browser | chromium 158.0.8127.18 |
| userAgent | Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/158.0.8127.18 Safari/537.36 |
| webglRenderer | ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero) (0x0000C0DE)), SwiftShader driver) |
| viewport | {"width":1280,"height":800} |
| gestureFrames | 60 |
| loadAverage | {"start":[27.04,24.9,23.94],"end":[33.89,26.98,24.68]} |

| Nodes | Theme | Scale | Metric | Legacy p50 / p95 | Shaped p50 / p95 | Change p50 / p95 | Budget |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 150 | light | 1 | initialRender | 246.6 / 246.6 | 254.2 / 254.2 | +3.1% / +3.1% | pass |
| 150 | light | 1 | loadAll | not measured | not measured | not measured | — |
| 150 | light | 1 | panFrame | 16.7 / 16.8 | 16.7 / 16.8 | +0.0% / -0.0% | pass |
| 150 | light | 1 | edgeUpdateFrame | 16.7 / 16.7 | 16.7 / 16.8 | +0.0% / +0.6% | pass |
| 150 | light | 1 | shapeApply | 83.8 / 92.5 | 111.8 / 130.3 | not comparable | info |
| 150 | light | 1 | heapMB | 8.6 / 8.6 | 9.0 / 9.0 | +4.4% / +4.4% | pass |
| 150 | light | 1 | domNodes | 2477 / 2477 | 3527 / 3527 | — | info |
| 150 | light | 1 | requests | 0 | 0 | — | pass |
| 150 | light | 1 | failed runs (rerun) | 0 | 0 | — | info |
| 150 | dark | 1 | initialRender | 238.2 / 238.2 | 290.8 / 290.8 | +22.1% / +22.1% | fail |
| 150 | dark | 1 | loadAll | not measured | not measured | not measured | — |
| 150 | dark | 1 | panFrame | 16.7 / 16.7 | 16.7 / 16.7 | +0.0% / +0.0% | pass |
| 150 | dark | 1 | edgeUpdateFrame | 16.7 / 16.8 | 16.7 / 16.8 | +0.0% / +0.0% | pass |
| 150 | dark | 1 | shapeApply | 76.5 / 77.2 | 113.4 / 128.2 | not comparable | info |
| 150 | dark | 1 | heapMB | 8.6 / 8.6 | 9.0 / 9.0 | +4.3% / +4.3% | pass |
| 150 | dark | 1 | domNodes | 2477 / 2477 | 3527 / 3527 | — | info |
| 150 | dark | 1 | requests | 0 | 0 | — | pass |
| 150 | dark | 1 | failed runs (rerun) | 0 | 0 | — | info |
| 250 | light | 1 | initialRender | 192.8 / 192.8 | 303.7 / 303.7 | +57.5% / +57.5% | fail |
| 250 | light | 1 | loadAll | 339.0 / 339.0 | 458.8 / 458.8 | +35.3% / +35.3% | fail |
| 250 | light | 1 | panFrame | 16.7 / 16.8 | 16.7 / 33.4 | +0.0% / +98.8% | fail |
| 250 | light | 1 | edgeUpdateFrame | 16.7 / 33.3 | 16.7 / 33.3 | +0.0% / -0.0% | pass |
| 250 | light | 1 | shapeApply | 135.0 / 147.3 | 167.2 / 175.5 | not comparable | info |
| 250 | light | 1 | heapMB | 13.4 / 13.4 | 14.6 / 14.6 | +8.5% / +8.5% | pass |
| 250 | light | 1 | domNodes | 4079 / 4079 | 5829 / 5829 | — | info |
| 250 | light | 1 | requests | 0 | 0 | — | pass |
| 250 | light | 1 | failed runs (rerun) | 0 | 0 | — | info |
| 250 | dark | 1 | initialRender | 184.5 / 184.5 | 237.9 / 237.9 | +28.9% / +28.9% | fail |
| 250 | dark | 1 | loadAll | 333.4 / 333.4 | 434.3 / 434.3 | +30.3% / +30.3% | fail |
| 250 | dark | 1 | panFrame | 16.7 / 16.7 | 16.7 / 33.2 | +0.0% / +98.8% | fail |
| 250 | dark | 1 | edgeUpdateFrame | 16.7 / 33.3 | 16.7 / 33.3 | +0.0% / +0.0% | pass |
| 250 | dark | 1 | shapeApply | 147.7 / 164.0 | 197.5 / 214.1 | not comparable | info |
| 250 | dark | 1 | heapMB | 13.4 / 13.4 | 14.6 / 14.6 | +8.4% / +8.4% | pass |
| 250 | dark | 1 | domNodes | 4079 / 4079 | 5829 / 5829 | — | info |
| 250 | dark | 1 | requests | 0 | 0 | — | pass |
| 250 | dark | 1 | failed runs (rerun) | 0 | 0 | — | info |

One run per scene cannot separate noise from regression on a loaded host, so
the fail rows above are not budget results. They do show where to look first
in the gate run: initial render and Load More cost more with shapes, the
shaped scene has about 40% more DOM nodes, and `shapeApply` rebuilds every
node (see the review below).

## Accessibility review

This review checks each rule of NODE_SHAPES.md § Theme, accessibility and scale
(paragraphs one to three; paragraph four is the performance scenes above)
against the code at commit 7b08a1c, which includes the shadow-under-boundary fix
and its test; the smoke run under Results used the earlier commit 3eeaa0d. It
was done by reading the
code and its tests. No screen reader or assistive-technology session was run.
**Pass** means the code implements the rule. **Partial** names the part that
is missing. **Fail** means the code does not do what the rule asks. Contrast ratios are
WCAG ratios computed from the CSS colours. The canvas is `#1a1a1a` on dark and
`#f5f5f5` on light.

### Theme and contrast

| Rule | Verdict | Evidence |
| --- | --- | --- |
| Semantic card tokens: surface, primary and secondary text, border, band foreground, focus and shadow | Pass | `--graph-card-*` in `CustomNode.css`, and the `.graph-card-theme-*` classes that pin a preview subtree to one theme; tested in `CustomNode.test.jsx` (preview theme tokens, theme shadow cascade) |
| Honour the existing media-query theme behaviour | Pass | Shaped nodes use the same `prefers-color-scheme: light` block as the card |
| White or dark band text at 4.5:1 or more, else an opaque text backing | Pass | `bandTextStyle` in `nodeShapes.js`; a triangle's outside label always sits on the surface; tested ("picks band text with at least 4.5:1 contrast or an opaque backing") |
| 3:1 visible boundary | Pass | `.graph-shape-boundary` draws a neutral hairline around every shape, whatever the type colour: `#808080` on dark (4.4:1) and `#767676` on light (4.2:1). The translucent drop shadow is painted under it, so nothing dims its outer band. Tested ("draws a 3:1 boundary against the canvas in both themes, whatever the type colour") |
| 3:1 focus treatment | Partial | The keyboard focus ring is solid `#646cff` (4.3:1 on dark, 3.8:1 on light). The selection ring uses the translucent `--graph-card-focus`, so its contrast depends on what is under it, and no test checks it |
| Colour never carries state alone | Partial | Remote, staged and skill states each have a dashed outline plus a badge or ★. Selected is drawn as a wider ring. A mark is shown only by a coloured ring and dot; its label is in the tooltip and the accessible name. Highlighted and selected differ only by ring width |
| Marked, staged, skill and remote indicators identifiable in both themes | Partial | They keep the same colours in both themes. The staged amber `#f0b429` is 1.7:1 on the light canvas, and its dashed outline and text badge carry the state on light. Nothing checks the indicators per theme |
| Preserve reduced motion | Pass | The `prefers-reduced-motion` block stops the pulse animations that shaped nodes share with the card, and shaped nodes have no hover transform. No test covers shaped nodes |
| Preserve stronger contrast | Pass | Under `prefers-contrast: more`, shaped nodes keep the card's only treatment, the raised opacity of dimmed nodes. The boundary, rings and badges get no further strengthening |

### Editor and keyboard

| Rule | Verdict | Evidence |
| --- | --- | --- |
| Labelled modal | Pass | `NodeAppearanceDialog.jsx`: `role="dialog"`, `aria-modal`, `aria-labelledby` |
| Focus trap, inert canvas, Escape/Cancel, focus return | Pass | Tab wraps inside the dialog, focus that leaves it is pulled back, its siblings are made `inert` while it is open, Escape and Cancel close it, and focus returns to the opener. Tested in `MetamodelExplorerDialog.test.jsx` ("supports keyboard tabs and traps focus inside the modal", "keeps a draft isolated and restores focus on Escape") |
| Tabs support arrow, Home and End keys and expose the selected state | Pass | `aria-selected` and roving `tabIndex`; Left, Right, Home and End move between tabs. The test presses Home and End only |
| Every selector has a label | Pass | Every control is wrapped in a `<label>`, and the scope radios sit in a `<fieldset>` with a `<legend>`. The tests find controls by their label |
| A preview change announces a short status, not the whole text again | Pass | Two alternating `role="status"` live regions announce a short status; the preview stage is `aria-hidden`. Tested ("validates scale and applies a type draft while preserving bindings") |
| The accessible name gives type, name and the three mapped values in order, even when clipped; None adds nothing | Pass | `nodeShapeAccessibleName` gives the name, and `GraphCanvas` sets it as the node wrapper's `aria-label`. An area bound to the type or the name is not repeated. Tested ("names the node by type, name and mapped values once, hiding the SVG decoration") |
| Detail access still shows the original full node content | Pass | `GraphCanvas` builds a shaped node's `data` from the whole original node (`nodeCardData` spreads it) and only adds `shapeRender`, so double-click, **View change history** and **Edit node** hand the app's detail and edit dialogs the same full node as a card. Tested ("reaches Node appearance and the full detail of a shaped node by keyboard") |
| Selection, appearance editing and the related-node action work by keyboard | Pass | Keyboard selection and the related-node action are tested ("selects by keyboard and then offers the related-node action without a pointer"). Shift+F10 or the Menu key on a focused node makes the browser fire `contextmenu` at the node wrapper, where React Flow binds the canvas's node menu handler; the canvas does not intercept the key for graph nodes (its Shift+F10 handler only serves annotations, whose own menus sit on a descendant). The menu focuses its first item, arrow keys reach **Node appearance**. Tested from the `contextmenu` event on ("reaches Node appearance and the full detail of a shaped node by keyboard"); the browser's own key-to-event step is not exercised in jsdom |
| SVG decoration hidden from screen readers, read once | Pass | The shape SVG is `aria-hidden` and `focusable="false"`, and the wrapper's `aria-label` stands in for its content. Asserted in the accessible-name test |
| UI strings in English and Swedish | Partial | All `node_appearance.*` editor keys exist in both `en.json` and `sv.json`, kept in step by `keyParity.test.js`. The canvas node's own strings are English props defaults with no translated value passed in: the skill title, "Show related nodes", "Edit node" and "Communities:" |

### Rendering cost

| Rule | Verdict | Evidence |
| --- | --- | --- |
| Memoize definitions by key and revision, and the resolved look by node and type revision | Fail | `GraphCanvas` rebuilds one resolver whenever the schema, saved shapes, overrides or type defaults change. It then resolves every node again in the node `useMemo`, which also reruns on highlight, dim, mark and presence changes. Nothing is cached per key, revision or node |
| Cache normalized geometry per base and scale; batch measurements | Pass | A module cache in `nodeShapes.js`, keyed by base and scale, returns frozen descriptors. Geometry is computed, not measured in the DOM |
| Reuse inline paths; no per-node filters or effects; lightweight shadows | Pass | Every layer reuses the one cached `geometry.path`. The shadow is an offset path fill, and the shaped CSS uses no `filter` |
| Use viewport culling | Fail | React Flow's `onlyRenderVisibleElements` is not enabled. Above 200 nodes the canvas shows them 100 at a time (Load More) instead |
| No attribute fetches on render; bindings come from loaded node data | Pass | `nodeShapeAreaValues` reads only the node and its metadata |
| Bound label lines and cap text processing before layout | Partial | The title is clamped to two lines and other text is clipped by the box. Values reach layout and the accessible name at full length |
| A type-default change invalidates that type; a library edit only its users | Fail | Any change to the type defaults or the saved shapes rebuilds every node, as in the memoization row |
| Edge updates limited to the affected endpoints | Fail | `SimpleFloatingEdge` subscribes to the whole `nodeInternals` map, so every edge re-renders when any node changes |

The four failures and the partial rows are open findings against this gate. In
the scenes above they show up as whole-scene costs: `shapeApply` re-resolves
every node, not just the Actor quarter, and `edgeUpdateFrame` re-renders every
edge, not just those of the dragged node.
