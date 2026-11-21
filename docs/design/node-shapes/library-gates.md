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

### Gate run (2026-11-21, after the memory and render optimisation)

The full matrix: 1,000 and 5,000 nodes, scales 0.1, 1 and 10, both themes,
five runs per scene, seed 20261120, at commit e966b27 (this change). It ran on
a shared host (72 cores, other work running) and the browser reported
SwiftShader (software rendering). Pan frame times move in steps of one 60 Hz
frame (16.7 ms), so a one-frame change shows as a large percentage. The heap
rows do not depend on host load.

Result: 42 rows pass and 30 fail, against 22 and 50 at d90a04f the day
before. The Budget column is this run's verdict per scene, failures
included. Heap is now 2 to 4 % over the legacy card in every scene (it was 12
to 17 %), within the 10 % budget. Initial render, Load More, pan and edge
update frame time miss the budget in part of the scenes. These verdicts hold
for software rendering on this host; a run with GPU rendering has not been
made, so how they compare there is not measured. Library writes stay off.

- **Pan frame at 1,000 nodes is one frame slower.** At scales 0.1 and 1 the
  shaped p50 is 50.0 ms against 33.3 ms for the legacy card, and p95 is 66.7
  against 50.0 ms, in all four scenes. At scale 10 p50 is equal and p95 is
  one frame slower. The d90a04f run shows the same steps in the same scenes.
  A result that repeats in every scene of both runs is not host noise: on
  this renderer the shaped scene drops one more frame per pan frame at 1,000
  nodes. The cause has not been measured. One difference the table does show
  is that the shaped scene has 44 % more DOM nodes (23,079 against 16,079).
- **5,000 nodes, dark, scale 10: the Load More p95 is an outlier.** With
  five runs, the nearest-rank p95 is the slowest run. So 478,944.3 ms is one
  shaped run, 5.7 times the scene's own p50 of 83,748.6 ms. No click hit the
  600 s settle timeout, and no run failed. The runner keeps only p50/p95 per
  scene, not per-run values, so the cause of that one slow run was not
  captured. To check it, the scene was run again on its own (five runs, seed
  20261120, same host and renderer, load average 25 to 29). That run used
  commit 73ce1e4, which adds only the command box's toolbox activation to the
  canvas package since e966b27. The slow run did not repeat:

  | Metric | Legacy p50 / p95 | Shaped p50 / p95 | Change p50 / p95 | Budget |
  | --- | --- | --- | --- | --- |
  | loadAll | 78956.2 / 79901.2 | 84592.0 / 92108.9 | +7.1% / +15.3% | fail |

  The scene's Load More row still fails: p50 is within the budget in both
  runs, and p95 is over it in both.

| Run | Value |
| --- | --- |
| commit | e966b27b00abfb2787aa6932747975d7485ad9a6 |
| seed | 20261120 |
| runs | 5 |
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
| loadAverage | {"start":[26.38,26.57,25.8],"end":[33.02,35.17,32.1]} |

| Nodes | Theme | Scale | Metric | Legacy p50 / p95 | Shaped p50 / p95 | Change p50 / p95 | Budget |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1000 | light | 0.1 | initialRender | 197.5 / 255.5 | 224.3 / 250.6 | +13.6% / -1.9% | fail |
| 1000 | light | 0.1 | loadAll | 3623.7 / 3759.1 | 4100.6 / 4647.3 | +13.2% / +23.6% | fail |
| 1000 | light | 0.1 | panFrame | 33.3 / 50.0 | 50.0 / 66.7 | +50.2% / +33.4% | fail |
| 1000 | light | 0.1 | edgeUpdateFrame | 266.6 / 333.4 | 283.3 / 350.0 | +6.3% / +5.0% | pass |
| 1000 | light | 0.1 | shapeApply | 587.7 / 644.8 | 718.5 / 794.0 | not comparable | info |
| 1000 | light | 0.1 | heapMB | 42.0 / 42.0 | 43.2 / 43.2 | +2.9% / +2.9% | pass |
| 1000 | light | 0.1 | domNodes | 16079 / 16079 | 23079 / 23079 | — | info |
| 1000 | light | 0.1 | draggedEdges | 4 / 4 | 4 / 4 | — | info |
| 1000 | light | 0.1 | requests | 0 | 0 | — | pass |
| 1000 | light | 0.1 | failed runs (rerun) | 0 | 0 | — | info |
| 1000 | light | 0.1 | completed runs | 5 | 5 | — | info |
| 1000 | light | 1 | initialRender | 210.8 / 224.6 | 221.7 / 242.5 | +5.2% / +8.0% | pass |
| 1000 | light | 1 | loadAll | 3582.9 / 3768.0 | 3945.1 / 3979.4 | +10.1% / +5.6% | fail |
| 1000 | light | 1 | panFrame | 33.4 / 50.0 | 50.0 / 66.7 | +49.7% / +33.4% | fail |
| 1000 | light | 1 | edgeUpdateFrame | 266.6 / 316.7 | 266.7 / 333.3 | +0.0% / +5.2% | pass |
| 1000 | light | 1 | shapeApply | 559.0 / 608.1 | 648.3 / 737.6 | not comparable | info |
| 1000 | light | 1 | heapMB | 42.0 / 42.0 | 43.0 / 43.0 | +2.3% / +2.3% | pass |
| 1000 | light | 1 | domNodes | 16079 / 16079 | 23079 / 23079 | — | info |
| 1000 | light | 1 | draggedEdges | 4 / 4 | 4 / 4 | — | info |
| 1000 | light | 1 | requests | 0 | 0 | — | pass |
| 1000 | light | 1 | failed runs (rerun) | 0 | 0 | — | info |
| 1000 | light | 1 | completed runs | 5 | 5 | — | info |
| 1000 | light | 10 | initialRender | 214.3 / 233.7 | 217.8 / 281.6 | +1.6% / +20.5% | fail |
| 1000 | light | 10 | loadAll | 3506.2 / 3645.1 | 3624.3 / 3802.4 | +3.4% / +4.3% | pass |
| 1000 | light | 10 | panFrame | 33.3 / 33.4 | 33.3 / 50.0 | +0.0% / +49.7% | fail |
| 1000 | light | 10 | edgeUpdateFrame | 250.0 / 300.0 | 233.4 / 299.9 | -6.6% / -0.0% | pass |
| 1000 | light | 10 | shapeApply | 564.3 / 624.3 | 627.4 / 689.7 | not comparable | info |
| 1000 | light | 10 | heapMB | 42.1 / 42.1 | 43.0 / 43.0 | +2.3% / +2.2% | pass |
| 1000 | light | 10 | domNodes | 16079 / 16079 | 23079 / 23079 | — | info |
| 1000 | light | 10 | draggedEdges | 4 / 4 | 4 / 4 | — | info |
| 1000 | light | 10 | requests | 0 | 0 | — | pass |
| 1000 | light | 10 | failed runs (rerun) | 0 | 0 | — | info |
| 1000 | light | 10 | completed runs | 5 | 5 | — | info |
| 1000 | dark | 0.1 | initialRender | 216.8 / 223.5 | 221.9 / 235.2 | +2.4% / +5.2% | pass |
| 1000 | dark | 0.1 | loadAll | 3432.3 / 3504.4 | 3979.0 / 4557.6 | +15.9% / +30.1% | fail |
| 1000 | dark | 0.1 | panFrame | 33.3 / 50.1 | 50.0 / 66.7 | +50.2% / +33.1% | fail |
| 1000 | dark | 0.1 | edgeUpdateFrame | 250.0 / 316.6 | 283.4 / 366.6 | +13.4% / +15.8% | fail |
| 1000 | dark | 0.1 | shapeApply | 550.7 / 606.0 | 646.0 / 766.3 | not comparable | info |
| 1000 | dark | 0.1 | heapMB | 42.0 / 42.0 | 43.2 / 43.2 | +2.7% / +2.9% | pass |
| 1000 | dark | 0.1 | domNodes | 16079 / 16079 | 23079 / 23079 | — | info |
| 1000 | dark | 0.1 | draggedEdges | 4 / 4 | 4 / 4 | — | info |
| 1000 | dark | 0.1 | requests | 0 | 0 | — | pass |
| 1000 | dark | 0.1 | failed runs (rerun) | 0 | 0 | — | info |
| 1000 | dark | 0.1 | completed runs | 5 | 5 | — | info |
| 1000 | dark | 1 | initialRender | 206.0 / 293.3 | 234.6 / 237.7 | +13.9% / -19.0% | fail |
| 1000 | dark | 1 | loadAll | 3794.0 / 3838.5 | 4027.2 / 4255.1 | +6.1% / +10.9% | fail |
| 1000 | dark | 1 | panFrame | 33.4 / 50.0 | 50.0 / 66.7 | +49.7% / +33.4% | fail |
| 1000 | dark | 1 | edgeUpdateFrame | 266.6 / 333.3 | 250.0 / 316.7 | -6.2% / -5.0% | pass |
| 1000 | dark | 1 | shapeApply | 558.7 / 627.3 | 638.8 / 756.1 | not comparable | info |
| 1000 | dark | 1 | heapMB | 42.0 / 42.0 | 43.0 / 43.0 | +2.3% / +2.3% | pass |
| 1000 | dark | 1 | domNodes | 16079 / 16079 | 23079 / 23079 | — | info |
| 1000 | dark | 1 | draggedEdges | 4 / 4 | 4 / 4 | — | info |
| 1000 | dark | 1 | requests | 0 | 0 | — | pass |
| 1000 | dark | 1 | failed runs (rerun) | 0 | 0 | — | info |
| 1000 | dark | 1 | completed runs | 5 | 5 | — | info |
| 1000 | dark | 10 | initialRender | 196.4 / 213.4 | 224.7 / 246.4 | +14.4% / +15.5% | fail |
| 1000 | dark | 10 | loadAll | 3489.7 / 3580.1 | 3688.7 / 3963.8 | +5.7% / +10.7% | fail |
| 1000 | dark | 10 | panFrame | 33.3 / 33.4 | 33.3 / 50.0 | +0.0% / +49.7% | fail |
| 1000 | dark | 10 | edgeUpdateFrame | 250.0 / 316.7 | 233.4 / 300.1 | -6.6% / -5.2% | pass |
| 1000 | dark | 10 | shapeApply | 558.2 / 617.1 | 586.9 / 684.7 | not comparable | info |
| 1000 | dark | 10 | heapMB | 42.1 / 42.1 | 43.0 / 43.0 | +2.3% / +2.3% | pass |
| 1000 | dark | 10 | domNodes | 16079 / 16079 | 23079 / 23079 | — | info |
| 1000 | dark | 10 | draggedEdges | 4 / 4 | 4 / 4 | — | info |
| 1000 | dark | 10 | requests | 0 | 0 | — | pass |
| 1000 | dark | 10 | failed runs (rerun) | 0 | 0 | — | info |
| 1000 | dark | 10 | completed runs | 5 | 5 | — | info |
| 5000 | light | 0.1 | initialRender | 215.6 / 235.6 | 227.6 / 255.8 | +5.6% / +8.6% | pass |
| 5000 | light | 0.1 | loadAll | 83381.4 / 85720.1 | 84864.2 / 88538.6 | +1.8% / +3.3% | pass |
| 5000 | light | 0.1 | panFrame | 116.7 / 183.3 | 133.4 / 200.1 | +14.3% / +9.2% | fail |
| 5000 | light | 0.1 | edgeUpdateFrame | 1266.6 / 1599.9 | 1283.2 / 1566.7 | +1.3% / -2.1% | pass |
| 5000 | light | 0.1 | shapeApply | 3419.2 / 4169.5 | 3900.4 / 4581.7 | not comparable | info |
| 5000 | light | 0.1 | heapMB | 255.9 / 256.0 | 265.7 / 265.8 | +3.9% / +3.9% | pass |
| 5000 | light | 0.1 | domNodes | 80078 / 80078 | 115078 / 115078 | — | info |
| 5000 | light | 0.1 | draggedEdges | 5 / 5 | 5 / 5 | — | info |
| 5000 | light | 0.1 | requests | 0 | 0 | — | pass |
| 5000 | light | 0.1 | failed runs (rerun) | 0 | 0 | — | info |
| 5000 | light | 0.1 | completed runs | 5 | 5 | — | info |
| 5000 | light | 1 | initialRender | 198.6 / 217.8 | 232.7 / 244.8 | +17.2% / +12.4% | fail |
| 5000 | light | 1 | loadAll | 78357.2 / 80445.1 | 86982.2 / 92813.8 | +11.0% / +15.4% | fail |
| 5000 | light | 1 | panFrame | 116.7 / 166.7 | 150.0 / 233.3 | +28.5% / +40.0% | fail |
| 5000 | light | 1 | edgeUpdateFrame | 1183.3 / 1499.9 | 1299.9 / 1699.9 | +9.9% / +13.3% | fail |
| 5000 | light | 1 | shapeApply | 3347.4 / 3775.8 | 3796.0 / 4360.9 | not comparable | info |
| 5000 | light | 1 | heapMB | 255.8 / 255.9 | 264.5 / 264.7 | +3.4% / +3.4% | pass |
| 5000 | light | 1 | domNodes | 80078 / 80078 | 115078 / 115078 | — | info |
| 5000 | light | 1 | draggedEdges | 5 / 5 | 5 / 5 | — | info |
| 5000 | light | 1 | requests | 0 | 0 | — | pass |
| 5000 | light | 1 | failed runs (rerun) | 0 | 0 | — | info |
| 5000 | light | 1 | completed runs | 5 | 5 | — | info |
| 5000 | light | 10 | initialRender | 200.8 / 212.7 | 203.6 / 237.1 | +1.4% / +11.5% | fail |
| 5000 | light | 10 | loadAll | 79427.9 / 83754.5 | 84868.6 / 89359.9 | +6.8% / +6.7% | pass |
| 5000 | light | 10 | panFrame | 100.1 / 183.3 | 116.7 / 200.0 | +16.6% / +9.1% | fail |
| 5000 | light | 10 | edgeUpdateFrame | 1250.0 / 1599.9 | 1216.6 / 1616.6 | -2.7% / +1.0% | pass |
| 5000 | light | 10 | shapeApply | 3626.1 / 3986.9 | 3596.4 / 3987.9 | not comparable | info |
| 5000 | light | 10 | heapMB | 255.9 / 256.0 | 264.6 / 264.9 | +3.4% / +3.5% | pass |
| 5000 | light | 10 | domNodes | 80078 / 80078 | 115078 / 115078 | — | info |
| 5000 | light | 10 | draggedEdges | 5 / 5 | 5 / 5 | — | info |
| 5000 | light | 10 | requests | 0 | 0 | — | pass |
| 5000 | light | 10 | failed runs (rerun) | 0 | 0 | — | info |
| 5000 | light | 10 | completed runs | 5 | 5 | — | info |
| 5000 | dark | 0.1 | initialRender | 201.4 / 208.9 | 238.0 / 251.4 | +18.2% / +20.3% | fail |
| 5000 | dark | 0.1 | loadAll | 78590.1 / 86831.8 | 86724.7 / 90405.2 | +10.4% / +4.1% | fail |
| 5000 | dark | 0.1 | panFrame | 116.7 / 183.3 | 150.0 / 216.7 | +28.5% / +18.2% | fail |
| 5000 | dark | 0.1 | edgeUpdateFrame | 1216.6 / 1616.6 | 1266.6 / 1749.9 | +4.1% / +8.2% | pass |
| 5000 | dark | 0.1 | shapeApply | 3365.7 / 3641.8 | 3962.2 / 4351.2 | not comparable | info |
| 5000 | dark | 0.1 | heapMB | 255.9 / 256.1 | 265.6 / 265.7 | +3.8% / +3.7% | pass |
| 5000 | dark | 0.1 | domNodes | 80078 / 80078 | 115078 / 115078 | — | info |
| 5000 | dark | 0.1 | draggedEdges | 5 / 5 | 5 / 5 | — | info |
| 5000 | dark | 0.1 | requests | 0 | 0 | — | pass |
| 5000 | dark | 0.1 | failed runs (rerun) | 0 | 0 | — | info |
| 5000 | dark | 0.1 | completed runs | 5 | 5 | — | info |
| 5000 | dark | 1 | initialRender | 198.7 / 213.9 | 225.3 / 233.2 | +13.4% / +9.0% | fail |
| 5000 | dark | 1 | loadAll | 79268.7 / 99067.3 | 84170.0 / 93001.1 | +6.2% / -6.1% | pass |
| 5000 | dark | 1 | panFrame | 133.4 / 250.0 | 150.0 / 233.3 | +12.4% / -6.7% | fail |
| 5000 | dark | 1 | edgeUpdateFrame | 1250.0 / 1699.9 | 1250.0 / 1650.0 | +0.0% / -2.9% | pass |
| 5000 | dark | 1 | shapeApply | 3338.2 / 3906.7 | 3969.2 / 4616.1 | not comparable | info |
| 5000 | dark | 1 | heapMB | 255.8 / 256.2 | 264.5 / 264.5 | +3.4% / +3.3% | pass |
| 5000 | dark | 1 | domNodes | 80078 / 80078 | 115078 / 115078 | — | info |
| 5000 | dark | 1 | draggedEdges | 5 / 5 | 5 / 5 | — | info |
| 5000 | dark | 1 | requests | 0 | 0 | — | pass |
| 5000 | dark | 1 | failed runs (rerun) | 0 | 0 | — | info |
| 5000 | dark | 1 | completed runs | 5 | 5 | — | info |
| 5000 | dark | 10 | initialRender | 195.5 / 236.7 | 212.1 / 254.6 | +8.5% / +7.6% | pass |
| 5000 | dark | 10 | loadAll | 78931.7 / 83523.3 | 83748.6 / 478944.3 | +6.1% / +473.4% | fail |
| 5000 | dark | 10 | panFrame | 100.1 / 183.3 | 116.7 / 183.3 | +16.6% / +0.0% | fail |
| 5000 | dark | 10 | edgeUpdateFrame | 1183.3 / 1583.3 | 1150.0 / 1683.3 | -2.8% / +6.3% | pass |
| 5000 | dark | 10 | shapeApply | 3251.7 / 3902.3 | 3693.9 / 4339.9 | not comparable | info |
| 5000 | dark | 10 | heapMB | 255.8 / 255.9 | 264.5 / 264.6 | +3.4% / +3.4% | pass |
| 5000 | dark | 10 | domNodes | 80078 / 80078 | 115078 / 115078 | — | info |
| 5000 | dark | 10 | draggedEdges | 5 / 5 | 5 / 5 | — | info |
| 5000 | dark | 10 | requests | 0 | 0 | — | pass |
| 5000 | dark | 10 | failed runs (rerun) | 0 | 0 | — | info |
| 5000 | dark | 10 | completed runs | 5 | 5 | — | info |

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
| Memoize definitions by key and revision, and the resolved look by node and type revision | Partial | Added in e966b27 and measured in the 2026-11-21 gate run: `GraphCanvas` keeps each node's resolved look (definition, area texts, geometry, accessible name) per graph node object, so Load More and highlight, dim, mark and presence changes reuse it, and `CustomNode` shares its geometry-derived styles and SVG layer markup across nodes with the same look. Any change to the schema, saved shapes, overrides or type defaults still drops every look, and definitions are not cached per key and revision (see the invalidation row) |
| Cache normalized geometry per base and scale; batch measurements | Pass | A module cache in `nodeShapes.js`, keyed by base and scale, returns frozen descriptors. Geometry is computed, not measured in the DOM |
| Reuse inline paths; no per-node filters or effects; lightweight shadows | Pass | Every layer reuses the one cached `geometry.path`. The shadow is an offset path fill, and the shaped CSS uses no `filter` |
| Use viewport culling | Fail | React Flow's `onlyRenderVisibleElements` is not enabled. Above 200 nodes the canvas shows them 100 at a time (Load More) instead |
| No attribute fetches on render; bindings come from loaded node data | Pass | `nodeShapeAreaValues` reads only the node and its metadata |
| Bound label lines and cap text processing before layout | Partial | The title is clamped to two lines and other text is clipped by the box. Values reach layout and the accessible name at full length |
| A type-default change invalidates that type; a library edit only its users | Fail | Any change to the type defaults or the saved shapes rebuilds every node, as in the memoization row |
| Edge updates limited to the affected endpoints | Fail | `SimpleFloatingEdge` subscribes to the whole `nodeInternals` map, so every edge re-renders when any node changes |

The three failures and the partial rows are open findings against this gate. In
the scenes above they show up as whole-scene costs: `shapeApply` re-resolves
every node, not just the Actor quarter, and `edgeUpdateFrame` re-renders every
edge, not just those of the dragged node.
