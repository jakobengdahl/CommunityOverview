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

/**
 * Check if two nodes overlap
 * @param {Object} pos1 - Position {x, y} of first node
 * @param {Object} pos2 - Position {x, y} of second node
 * @param {number} minDistance - Minimum distance between node centers
 * @returns {boolean} True if nodes overlap
 */
function nodesOverlap(pos1, pos2, minDistance = 250) {
  const dx = pos1.x - pos2.x;
  const dy = pos1.y - pos2.y;
  return Math.sqrt(dx * dx + dy * dy) < minDistance;
}

/**
 * Find the bounding box of existing nodes
 * @param {Array} nodes - Array of nodes with positions
 * @returns {Object} Bounding box {minX, minY, maxX, maxY}
 */
function getBoundingBox(nodes) {
  if (nodes.length === 0) {
    return { minX: 0, minY: 0, maxX: 800, maxY: 600 };
  }

  const positions = nodes.filter(n => n.position).map(n => n.position);
  if (positions.length === 0) {
    return { minX: 0, minY: 0, maxX: 800, maxY: 600 };
  }

  return {
    minX: Math.min(...positions.map(p => p.x)),
    minY: Math.min(...positions.map(p => p.y)),
    maxX: Math.max(...positions.map(p => p.x)),
    maxY: Math.max(...positions.map(p => p.y)),
  };
}

/**
 * Calculate positions for new nodes that don't overlap with existing nodes
 * Places new nodes to the right of existing nodes with proper spacing
 *
 * @param {Array} newNodes - Array of new nodes to position
 * @param {Array} existingNodes - Array of existing nodes with positions
 * @param {Array} edges - Array of edges (for considering connections)
 * @returns {Array} New nodes with calculated positions
 */
export function positionNewNodes(newNodes, existingNodes, edges = []) {
  if (newNodes.length === 0) return newNodes;

  const existingPositions = existingNodes
    .filter(n => n.position)
    .map(n => n.position);

  // Get bounding box of existing nodes
  const bbox = getBoundingBox(existingNodes);

  // If no existing nodes, use standard layout
  if (existingPositions.length === 0) {
    return applyLayout(newNodes, edges);
  }

  // Create a map of existing nodes for connection lookups
  const existingNodeMap = new Map(existingNodes.map(n => [n.id, n]));

  const positionedNodes = [];

  for (const node of newNodes) {
    // Find edges connecting this node to existing nodes
    const connectedEdges = edges.filter(
      e => (e.source === node.id || e.target === node.id) &&
           (existingNodeMap.has(e.source) || existingNodeMap.has(e.target))
    );

    let position;

    if (connectedEdges.length > 0) {
      // Position near connected nodes
      const connectedPositions = connectedEdges
        .map(e => {
          const connectedId = e.source === node.id ? e.target : e.source;
          const connectedNode = existingNodeMap.get(connectedId);
          return connectedNode?.position;
        })
        .filter(Boolean);

      if (connectedPositions.length > 0) {
        // Average position of connected nodes + offset
        const avgX = connectedPositions.reduce((sum, p) => sum + p.x, 0) / connectedPositions.length;
        const avgY = connectedPositions.reduce((sum, p) => sum + p.y, 0) / connectedPositions.length;

        // Add offset to avoid exact overlap, distribute around the connected node
        const angle = (positionedNodes.length * 1.2) + Math.PI / 4;
        const offset = 280;
        position = {
          x: avgX + offset * Math.cos(angle),
          y: avgY + offset * Math.sin(angle),
        };
      }
    }

    if (!position) {
      // Fall back to positioning to the right of existing nodes
      position = {
        x: bbox.maxX + 300,
        y: bbox.minY + (positionedNodes.length * (NODE_HEIGHT + 80)),
      };
    }

    // Check for overlaps and adjust using spiral pattern
    const allPositions = [
      ...existingPositions,
      ...positionedNodes.filter(n => n.position).map(n => n.position),
    ];

    let { x, y } = position;
    let attempts = 0;
    while (attempts < 15) {
      const hasOverlap = allPositions.some(pos => nodesOverlap({ x, y }, pos, 220));
      if (!hasOverlap) break;

      // Spiral outward to find free space
      const angle = attempts * 0.8;
      const dist = 180 + attempts * 60;
      x = position.x + dist * Math.cos(angle);
      y = position.y + dist * Math.sin(angle);
      attempts++;
    }

    positionedNodes.push({
      ...node,
      position: { x, y },
    });
  }

  return positionedNodes;
}
