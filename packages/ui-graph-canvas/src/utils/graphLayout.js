/**
 * Graph layout utilities using dagre for automatic node positioning
 */
import dagre from 'dagre';

const NODE_WIDTH = 200;
const NODE_HEIGHT = 100;

/**
 * Calculate positions for nodes using dagre hierarchical layout
 * @param {Array} nodes - Array of React Flow nodes
 * @param {Array} edges - Array of React Flow edges
 * @param {string} direction - Layout direction: 'TB' (top-bottom), 'LR' (left-right), 'BT', 'RL'
 * @returns {Array} Nodes with calculated positions
 */
export function getLayoutedElements(nodes, edges, direction = 'TB') {
  const dagreGraph = new dagre.graphlib.Graph();
  dagreGraph.setDefaultEdgeLabel(() => ({}));

  dagreGraph.setGraph({
    rankdir: direction,
    nodesep: 150,
    ranksep: 200,
    edgesep: 50,
    ranker: 'tight-tree',
    marginx: 50,
    marginy: 50,
    acyclicer: 'greedy'
  });

  nodes.forEach((node) => {
    dagreGraph.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  });

  edges.forEach((edge) => {
    dagreGraph.setEdge(edge.source, edge.target);
  });

  dagre.layout(dagreGraph);

  return nodes.map((node) => {
    const nodeWithPosition = dagreGraph.node(node.id);
    return {
      ...node,
      position: {
        x: nodeWithPosition.x - NODE_WIDTH / 2,
        y: nodeWithPosition.y - NODE_HEIGHT / 2,
      },
    };
  });
}

/**
 * Calculate positions for a circular layout
 * @param {Array} nodes - Array of React Flow nodes
 * @param {number} centerX - Center X coordinate
 * @param {number} centerY - Center Y coordinate
 * @param {number} radius - Radius of the circle
 * @returns {Array} Nodes with calculated positions
 */
export function getCircularLayout(nodes, centerX = 400, centerY = 300, radius = 250) {
  if (nodes.length === 0) return nodes;

  const angleStep = (2 * Math.PI) / nodes.length;

  return nodes.map((node, index) => {
    const angle = index * angleStep;
    return {
      ...node,
      position: {
        x: centerX + radius * Math.cos(angle),
        y: centerY + radius * Math.sin(angle),
      },
    };
  });
}

/**
 * Calculate positions for nodes in a grid layout
 * @param {Array} nodes - Array of React Flow nodes
 * @param {number} columns - Number of columns (0 = auto-calculate)
 * @param {number} cellWidth - Width of each grid cell
 * @param {number} cellHeight - Height of each grid cell
 * @returns {Array} Nodes with calculated positions
 */
export function getGridLayout(nodes, columns = 0, cellWidth = 280, cellHeight = 180) {
  if (nodes.length === 0) return nodes;

  if (columns === 0) {
    columns = Math.ceil(Math.sqrt(nodes.length * 1.2));
  }

  return nodes.map((node, index) => {
    const row = Math.floor(index / columns);
    const col = index % columns;

    return {
      ...node,
      position: {
        x: col * cellWidth + 100,
        y: row * cellHeight + 100,
      },
    };
  });
}

/**
 * Choose optimal layout based on graph characteristics
 * @param {Array} nodes - Array of nodes
 * @param {Array} edges - Array of edges
 * @returns {string} Layout type: 'dagre', 'grid', or 'circular'
 */
export function chooseLayout(nodes, edges) {
  const nodeCount = nodes.length;
  const edgeCount = edges.length;

  if (nodeCount === 0) return 'circular';
  if (edgeCount === 0) return 'circular';
  if (nodeCount > 15 && edgeCount < nodeCount * 1.5) return 'grid';
  return 'dagre';
}

/**
 * Apply automatic layout to nodes
 * @param {Array} nodes - Array of nodes
 * @param {Array} edges - Array of edges
 * @param {string} layoutType - Optional forced layout type
 * @returns {Array} Nodes with positions
 */
export function applyLayout(nodes, edges, layoutType = null) {
  const layout = layoutType || chooseLayout(nodes, edges);

  switch (layout) {
    case 'grid':
      return getGridLayout(nodes);
    case 'circular':
      return getCircularLayout(nodes, 400, 300, 250);
    case 'dagre':
    default:
      return getLayoutedElements(nodes, edges, 'TB');
  }
}
