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
