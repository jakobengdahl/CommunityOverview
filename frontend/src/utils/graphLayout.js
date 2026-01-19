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

  // Configure the graph with improved spacing to prevent edge overlaps
  dagreGraph.setGraph({
    rankdir: direction,
    nodesep: 150,  // Increased horizontal spacing between nodes
    ranksep: 200,  // Increased vertical spacing between ranks
    edgesep: 50,   // Space to leave between edges
    ranker: 'tight-tree', // Use tight-tree ranking for better edge distribution
    marginx: 50,
    marginy: 50,
    acyclicer: 'greedy' // Better handling of cycles
  });

  // Add nodes to dagre graph
  nodes.forEach((node) => {
    dagreGraph.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  });

  // Add edges to dagre graph
  edges.forEach((edge) => {
    dagreGraph.setEdge(edge.source, edge.target);
  });

  // Calculate layout
  dagre.layout(dagreGraph);

  // Apply calculated positions to nodes
  const layoutedNodes = nodes.map((node) => {
    const nodeWithPosition = dagreGraph.node(node.id);

    return {
      ...node,
      position: {
        x: nodeWithPosition.x - NODE_WIDTH / 2,
        y: nodeWithPosition.y - NODE_HEIGHT / 2,
      },
    };
  });

  return layoutedNodes;
}

/**
 * Calculate positions for a force-directed layout (alternative to dagre)
 * This is a simpler approach that spreads nodes in a circular pattern
 * @param {Array} nodes - Array of React Flow nodes
 * @param {number} centerX - Center X coordinate
 * @param {number} centerY - Center Y coordinate
 * @param {number} radius - Radius of the circle
 * @returns {Array} Nodes with calculated positions
 */
export function getCircularLayout(nodes, centerX = 400, centerY = 300, radius = 250) {
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
 * Useful for displaying many nodes in an organized way
 * @param {Array} nodes - Array of React Flow nodes
 * @param {number} columns - Number of columns (0 = auto-calculate for square-ish grid)
 * @param {number} cellWidth - Width of each grid cell
 * @param {number} cellHeight - Height of each grid cell
 * @returns {Array} Nodes with calculated positions
 */
export function getGridLayout(nodes, columns = 0, cellWidth = 280, cellHeight = 180) {
  if (nodes.length === 0) return nodes;

  // Auto-calculate columns if not specified
  // Aim for a roughly square or 4:3 aspect ratio
  if (columns === 0) {
    // For 40 nodes: sqrt(40 * 1.2) ≈ 6.9, so 7 columns → 7x6 grid
    // For 50 nodes: sqrt(50 * 1.2) ≈ 7.7, so 8 columns → 8x7 grid
    columns = Math.ceil(Math.sqrt(nodes.length * 1.2)); // 1.2 factor gives 4:3-ish aspect ratio
  }

  return nodes.map((node, index) => {
    const row = Math.floor(index / columns);
    const col = index % columns;

    return {
      ...node,
      position: {
        x: col * cellWidth + 100, // Add offset so it's not at the edge
        y: row * cellHeight + 100,
      },
    };
  });
}
