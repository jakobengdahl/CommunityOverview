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
| `edgeUpdateFrame` | Frame-to-frame time during a 60-frame drag of the node nearest the viewport centre (the same node in both scenes), which redraws its edges | 60 per run |
| `shapeApply` | A type-wide look change on every Actor node (a quarter of the scene), until drawn: the shaped scene switches the type default to another saved shape; the legacy card has no shape, so its baseline highlights those nodes, which rebuilds the same node data | 4 per run (on, off, on, off) |
| `heapMB` | Used JS heap after the full scene loaded and a forced garbage collection | 1 per run |
| `requests` | Network requests the page made after it loaded | 1 per run |

The DOM node count is reported for information. p50 and p95 are nearest-rank
percentiles over every sample of every run. A gesture that did not move the
view or the node is not counted, and a metric with no samples is reported as
not measured rather than as a number. The budget passes for a metric when the
shaped p50 and p95 are both less than 10% above the legacy p50 and p95, and
for requests when the shaped scene made no more than the legacy one.

### Running it

From `frontend/web`, after `npm ci` and `npx playwright install chromium`:

```bash
npm run perf:node-shapes -- --sizes 1000,5000 --scales 0.1,1,10 --themes light,dark --runs 5 --out results.json
```

It prints the Markdown table below and writes the raw summary as JSON. The
full matrix is long-running and is not part of CI; CI runs only
`perf/node-shapes/scene.test.js`, which checks the scenes and the budget
arithmetic.

RESULTS_PLACEHOLDER
