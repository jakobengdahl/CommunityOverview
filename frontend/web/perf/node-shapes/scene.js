// Fixed synthetic scenes for the node shape performance gate
// (docs/design/NODE_SHAPES.md § Theme, accessibility and scale). The same
// seed always yields the same nodes, positions, edges and texts, so a shaped
// scene and its legacy-card baseline differ only in the shape selections.

export const SCENE_SEED = 20261120;
export const SCENE_SIZES = [1000, 5000];
export const SCENE_SCALES = [0.1, 1, 10];
export const SCENE_THEMES = ['light', 'dark'];

// One base per type, so every shaped scene mixes all four bases.
export const SCENE_TYPE_BASES = {
  Actor: 'rectangle',
  Initiative: 'rectangle-flat',
  Risk: 'triangle-up',
  Resource: 'triangle-down',
};
const TYPES = Object.keys(SCENE_TYPE_BASES);
const WORDS = ['data', 'shared', 'national', 'quality', 'register', 'open', 'survey', 'network'];

/** Deterministic PRNG (mulberry32); Math.random would make runs incomparable. */
export function seededRandom(seed) {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function sceneDefinition(base, scale) {
  const triangle = base.startsWith('triangle');
  return {
    version: 1,
    base,
    scale,
    placement: triangle ? 'centre' : 'top-left',
    areas: { first: 'type', second: 'name', third: triangle ? 'level' : 'summary' },
  };
}

/**
 * `{ nodes, edges, schema, savedShapes, typeShapes }` for GraphCanvas. A
 * shaped scene selects a saved library shape per type as the session type
 * default; the legacy scene has the same graph with no selection at all.
 * Positions sit on a grid spaced for the scene's scale, so nodes never
 * overlap and the canvas never runs a layout.
 */
export function createNodeShapeScene({ size, scale, shaped, seed = SCENE_SEED }) {
  const random = seededRandom(seed);
  const pick = (list) => list[Math.floor(random() * list.length)];
  const columns = Math.ceil(Math.sqrt(size));
  const step = { x: 320 * Math.max(1, scale), y: 300 * Math.max(1, scale) };
  const nodes = [];
  const edges = [];
  for (let i = 0; i < size; i++) {
    nodes.push({
      id: `n${i}`,
      type: TYPES[i % TYPES.length],
      name: `${pick(WORDS)} ${pick(WORDS)} ${i}`,
      summary: Array.from({ length: 4 + Math.floor(random() * 12) }, () => pick(WORDS)).join(' '),
      metadata: { level: pick(['low', 'medium', 'high']) },
      _savedPosition: { x: (i % columns) * step.x, y: Math.floor(i / columns) * step.y },
    });
    // Every node but the first links to an earlier one; every third gets a
    // second link, so a dragged node always moves at least one edge.
    const links = i === 0 ? 0 : i % 3 === 0 ? 2 : 1;
    for (let k = 0; k < links; k++) {
      const target = Math.floor(random() * i);
      edges.push({ id: `e${i}-${k}`, source: `n${i}`, target: `n${target}`, type: 'RELATES_TO' });
    }
  }
  const schema = {
    node_types: Object.fromEntries(TYPES.map((type) => [type, { fields: ['level'] }])),
    node_shapes: {},
  };
  if (!shaped) return { nodes, edges, schema, savedShapes: {}, typeShapes: {} };
  const savedShapes = {};
  const typeShapes = {};
  for (const [type, base] of Object.entries(SCENE_TYPE_BASES)) {
    savedShapes[`scene-${base}`] = {
      name: `Scene ${base}`,
      revision: 1,
      definition: sceneDefinition(base, scale),
    };
    typeShapes[type] = { ref: `saved:scene-${base}` };
  }
  return { nodes, edges, schema, savedShapes, typeShapes };
}

/** Edges GraphCanvas draws once the first `loaded` nodes are on the canvas. */
export function edgesAmong(edges, loaded) {
  const index = (id) => Number(id.slice(1));
  return edges.filter((e) => index(e.source) < loaded && index(e.target) < loaded).length;
}

/**
 * The node the edge-update drag moves: the one whose saved position is
 * nearest the centre of the grid, with the edges it redraws. It comes from
 * the scene data alone, so a shaped scene and its legacy scene drag the same
 * node with the same edges whatever their node sizes or fitted zoom.
 */
export function sceneDragNode({ nodes, edges }) {
  const xs = nodes.map((n) => n._savedPosition.x);
  const ys = nodes.map((n) => n._savedPosition.y);
  const cx = (Math.min(...xs) + Math.max(...xs)) / 2;
  const cy = (Math.min(...ys) + Math.max(...ys)) / 2;
  let best = null;
  for (const node of nodes) {
    const d = (node._savedPosition.x - cx) ** 2 + (node._savedPosition.y - cy) ** 2;
    if (!best || d < best.d) best = { d, id: node.id };
  }
  const count = edges.filter((e) => e.source === best.id || e.target === best.id).length;
  return { id: best.id, edges: count };
}
