// The page the scene runner drives. It mounts the production GraphCanvas with
// one fixed scene and exposes `window.bench` for timing from Playwright.
import { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { flushSync } from 'react-dom';
import { GraphCanvas } from '@community-graph/ui-graph-canvas';
import '@community-graph/ui-graph-canvas/styles';
import '../../src/index.css';
import { createNodeShapeScene, edgesAmong } from './scene';

const params = new URLSearchParams(location.search);
const scene = createNodeShapeScene({
  size: Number(params.get('size')),
  scale: Number(params.get('scale')),
  shaped: params.get('variant') === 'shaped',
});
const noop = () => {};
// The type the apply step changes: a quarter of the scene.
const APPLY_TYPE = 'Actor';
// GraphCanvas rebuilds its nodes whenever these change identity; the app
// keeps them stable, while GraphCanvas's own defaults are fresh on every
// render and would keep the canvas re-rendering while idle.
const NONE = [];
const EMPTY = {};
const stable = {
  highlightedNodeIds: NONE,
  hiddenNodeIds: NONE,
  hiddenEdgeIds: NONE,
  dimmedNodeIds: NONE,
  dimmedEdgeIds: NONE,
  nodeMarks: EMPTY,
  pulsedNodeIds: EMPTY,
  contextMenuLabels: EMPTY,
  annotationToolboxLabels: EMPTY,
};
// The legacy card has no shape to apply; its type-wide look change is a
// highlight, which rebuilds the same nodes and restyles that type's cards.
const APPLY_IDS = scene.nodes.filter((n) => n.type === APPLY_TYPE).map((n) => n.id);
const LEGACY_APPLY = scene.typeShapes[APPLY_TYPE] ? NONE : APPLY_IDS;
let setApplied = null;

function Scene() {
  const [applied, setState] = useState(false);
  useEffect(() => {
    setApplied = setState;
  }, []);
  const typeShapes =
    applied && scene.typeShapes[APPLY_TYPE]
      ? { ...scene.typeShapes, [APPLY_TYPE]: { ref: 'saved:scene-rectangle-flat' } }
      : scene.typeShapes;
  return (
    <div style={{ width: '100vw', height: '100vh' }}>
      <GraphCanvas
        {...stable}
        nodes={scene.nodes}
        edges={scene.edges}
        schema={scene.schema}
        savedShapes={scene.savedShapes}
        typeShapes={typeShapes}
        highlightedNodeIds={applied ? LEGACY_APPLY : NONE}
        onExpand={noop}
        onEdit={noop}
        onAppearance={noop}
      />
    </div>
  );
}

const frame = () => new Promise((resolve) => requestAnimationFrame(resolve));

/** Milliseconds from `start` until `done()` holds and two frames have painted. */
async function settle(start, done, timeout = 600000) {
  while (!done()) {
    if (performance.now() - start > timeout) throw new Error('scene did not settle');
    await frame();
  }
  await frame();
  await frame();
  return performance.now() - start;
}

const nodeCount = () => document.querySelectorAll('.react-flow__node').length;
const loadedReady = (loaded) => () =>
  nodeCount() === loaded &&
  !document.querySelector('.react-flow__node[style*="visibility: hidden"]') &&
  document.querySelectorAll('.react-flow__edge').length === edgesAmong(scene.edges, loaded);

window.bench = {
  size: scene.nodes.length,
  async mount() {
    const start = performance.now();
    flushSync(() => createRoot(document.getElementById('root')).render(<Scene />));
    return settle(start, loadedReady(Math.min(100, scene.nodes.length)));
  },
  // GraphCanvas lazily shows 100 nodes at a time over 200; this presses
  // Load More, as a user would, until the whole scene is on the canvas.
  async loadAll() {
    const start = performance.now();
    for (;;) {
      const button = document.querySelector('.graph-load-more-button');
      if (!button) break;
      const target = Math.min(nodeCount() + 100, scene.nodes.length);
      button.click();
      await settle(start, loadedReady(target));
    }
    return performance.now() - start;
  },
  async apply(on) {
    const last = scene.nodes.findLast((n) => n.type === APPLY_TYPE).id;
    const card = () => document.querySelector(`.react-flow__node[data-id="${last}"]`);
    const done = scene.typeShapes[APPLY_TYPE]
      ? () =>
          Boolean(
            card()?.querySelector(on ? '.graph-shape-rectangle-flat' : '.graph-shape-rectangle')
          )
      : () => (card()?.querySelector('.graph-custom-node.highlighted') != null) === on;
    const start = performance.now();
    setApplied(on);
    return settle(start, done);
  },
  /**
   * Press `button` at `from`, move by `step` once per frame for `frames`
   * frames, release, and return the frame-to-frame times while it ran. The
   * events are dispatched in the page so every frame carries one move; a
   * `from.id` sends them to that node rather than to what lies under `from`.
   */
  async gesture({ from, step, frames, button }) {
    const target = from.id
      ? document.querySelector(`.react-flow__node[data-id="${from.id}"]`)
      : document.elementFromPoint(from.x, from.y);
    const init = (i) => ({
      bubbles: true,
      cancelable: true,
      view: window,
      button,
      buttons: button === 2 ? 2 : 1,
      clientX: from.x + step.x * i,
      clientY: from.y + step.y * i,
    });
    target.dispatchEvent(new MouseEvent('mousedown', init(0)));
    const deltas = [];
    let last = await frame();
    for (let i = 1; i <= frames; i++) {
      target.dispatchEvent(new MouseEvent('mousemove', init(i)));
      const now = await frame();
      deltas.push(now - last);
      last = now;
    }
    target.dispatchEvent(new MouseEvent('mouseup', init(frames)));
    await frame();
    return deltas;
  },
  viewport: () => document.querySelector('.react-flow__viewport')?.style.transform,
  nodeTransform: (id) =>
    document.querySelector(`.react-flow__node[data-id="${id}"]`)?.style.transform,
  /** A screen point on the empty pane near the centre, for a pan. */
  panePoint() {
    const cx = innerWidth / 2;
    const cy = innerHeight / 2;
    for (let r = 0; r < 400; r += 4) {
      for (const [dx, dy] of [
        [r, 0],
        [-r, 0],
        [0, r],
        [0, -r],
      ]) {
        const el = document.elementFromPoint(cx + dx, cy + dy);
        if (el?.classList.contains('react-flow__pane')) return { x: cx + dx, y: cy + dy };
      }
    }
    return null;
  },
  /**
   * The node nearest the viewport centre: the same node in a shaped scene and
   * its legacy scene. The drag goes to its wrapper, as a 0.1-scale shape is
   * too small to hit with a pointer at fit zoom.
   */
  dragPoint() {
    const cx = innerWidth / 2;
    const cy = innerHeight / 2;
    let best = null;
    for (const el of document.querySelectorAll('.react-flow__node')) {
      const r = el.getBoundingClientRect();
      const x = r.left + r.width / 2;
      const y = r.top + r.height / 2;
      const d = (x - cx) ** 2 + (y - cy) ** 2;
      if (!best || d < best.d) best = { d, x, y, id: el.dataset.id };
    }
    if (!best) return null;
    const edges = scene.edges.filter((e) => e.source === best.id || e.target === best.id).length;
    return { x: best.x, y: best.y, id: best.id, edges };
  },
};
