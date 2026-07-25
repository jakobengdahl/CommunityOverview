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
    acyclicer: 'greedy',
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

  const positions = nodes.filter((n) => n.position).map((n) => n.position);
  if (positions.length === 0) {
    return { minX: 0, minY: 0, maxX: 800, maxY: 600 };
  }

  return {
    minX: Math.min(...positions.map((p) => p.x)),
    minY: Math.min(...positions.map((p) => p.y)),
    maxX: Math.max(...positions.map((p) => p.x)),
    maxY: Math.max(...positions.map((p) => p.y)),
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
import { Position } from 'reactflow';

// Helper to get absolute position of a node (handling nested groups)
function getAbsolutePosition(node, nodeInternals) {
  if (!node.parentId || !nodeInternals) {
    return node.positionAbsolute || node.position;
  }

  const parent = nodeInternals.get(node.parentId);
  if (!parent) {
    return node.positionAbsolute || node.position;
  }

  const parentPos = getAbsolutePosition(parent, nodeInternals);
  return {
    x: (node.position.x || 0) + parentPos.x,
    y: (node.position.y || 0) + parentPos.y,
  };
}

// Helper function to get intersection point between line and rectangle
function getNodeIntersection(intersectionNode, targetNode, nodeInternals) {
  const { width: intersectionNodeWidth, height: intersectionNodeHeight } = intersectionNode;

  // Use absolute positions
  const intersectionNodePosition = getAbsolutePosition(intersectionNode, nodeInternals);
  const targetPosition = getAbsolutePosition(targetNode, nodeInternals);

  const w = intersectionNodeWidth / 2;
  const h = intersectionNodeHeight / 2;

  const x2 = intersectionNodePosition.x + w;
  const y2 = intersectionNodePosition.y + h;
  const x1 = targetPosition.x + (targetNode.width || NODE_WIDTH) / 2;
  const y1 = targetPosition.y + (targetNode.height || NODE_HEIGHT) / 2;

  const xx1 = (x1 - x2) / (2 * w) - (y1 - y2) / (2 * h);
  const yy1 = (x1 - x2) / (2 * w) + (y1 - y2) / (2 * h);
  const a = 1 / (Math.abs(xx1) + Math.abs(yy1));
  const xx3 = a * xx1;
  const yy3 = a * yy1;
  const x = w * (xx3 + yy3) + x2;
  const y = h * (-xx3 + yy3) + y2;

  return { x, y };
}

// Helper to determine Handle position based on intersection
function getEdgePosition(node, intersectionPoint, nodeInternals) {
  const absPos = getAbsolutePosition(node, nodeInternals);
  const n = { ...absPos, ...node };
  const nx = Math.round(n.x);
  const ny = Math.round(n.y);
  const px = Math.round(intersectionPoint.x);
  const py = Math.round(intersectionPoint.y);

  if (px <= nx + 1) {
    return Position.Left;
  }
  if (px >= nx + (n.width || NODE_WIDTH) - 1) {
    return Position.Right;
  }
  if (py <= ny + 1) {
    return Position.Top;
  }
  if (py >= ny + (n.height || NODE_HEIGHT) - 1) {
    return Position.Bottom;
  }

  return Position.Top;
}

/**
 * Calculate parameters for floating edge
 */
export function getEdgeParams(source, target, nodeInternals) {
  const sourceIntersectionPoint = getNodeIntersection(source, target, nodeInternals);
  const targetIntersectionPoint = getNodeIntersection(target, source, nodeInternals);

  const sourcePos = getEdgePosition(source, sourceIntersectionPoint, nodeInternals);
  const targetPos = getEdgePosition(target, targetIntersectionPoint, nodeInternals);

  return {
    sx: sourceIntersectionPoint.x,
    sy: sourceIntersectionPoint.y,
    tx: targetIntersectionPoint.x,
    ty: targetIntersectionPoint.y,
    sourcePos,
    targetPos,
  };
}

const GRID_CELL_W = 280;
const GRID_CELL_H = 180;
const PARENT_MIN_HSPAN = 320;
const CHILD_VOFFSET = 240;
const CHILD_HSPACING = 280;
const CHILD_VSPACING = 180;
const CHILD_GRID_THRESHOLD = 6;
const OVERLAP_MIN_DIST = 220;

function getNodeType(node) {
  return node.data?.type || node.type || 'unknown';
}

function computeOrigin(existingNodes, viewportCenter) {
  if (viewportCenter) return viewportCenter;
  const bbox = getBoundingBox(existingNodes);
  if (existingNodes.filter((n) => n.position).length > 0) {
    return {
      x: (bbox.minX + bbox.maxX) / 2,
      y: (bbox.minY + bbox.maxY) / 2,
    };
  }
  return { x: 400, y: 300 };
}

function gridLayoutAt(nodes, center) {
  const cols = Math.ceil(Math.sqrt(nodes.length * 1.2));
  const rows = Math.ceil(nodes.length / cols);
  const startX = center.x - (cols * GRID_CELL_W) / 2;
  const startY = center.y - (rows * GRID_CELL_H) / 2;

  return nodes.map((node, i) => ({
    ...node,
    position: {
      x: startX + (i % cols) * GRID_CELL_W,
      y: startY + Math.floor(i / cols) * GRID_CELL_H,
    },
  }));
}

function hierarchicalLayout(nodes, crossTypeEdges, nodeTypeMap, origin) {
  const sourceTypes = new Set(crossTypeEdges.map((e) => nodeTypeMap.get(e.source)));
  const targetTypes = new Set(crossTypeEdges.map((e) => nodeTypeMap.get(e.target)));

  // Root types: appear as source but not as target in cross-type edges
  const rootTypes = [...sourceTypes].filter((t) => !targetTypes.has(t));
  if (rootTypes.length === 0) return gridLayoutAt(nodes, origin);

  const parentNodes = nodes.filter((n) => rootTypes.includes(nodeTypeMap.get(n.id)));

  // parent id → [child ids]
  const childrenOf = new Map();
  for (const e of crossTypeEdges) {
    if (!childrenOf.has(e.source)) childrenOf.set(e.source, []);
    childrenOf.get(e.source).push(e.target);
  }

  // Compute horizontal span needed per parent
  function parentHSpan(parentNode) {
    const children = [...new Set(childrenOf.get(parentNode.id) || [])];
    if (children.length === 0) return PARENT_MIN_HSPAN;
    if (children.length <= CHILD_GRID_THRESHOLD) {
      return Math.max(children.length * CHILD_HSPACING, PARENT_MIN_HSPAN);
    }
    const cols = Math.ceil(Math.sqrt(children.length));
    return Math.max(cols * CHILD_HSPACING, PARENT_MIN_HSPAN);
  }

  const spans = parentNodes.map(parentHSpan);
  const totalWidth = spans.reduce((a, b) => a + b, 0);
  let curX = origin.x - totalWidth / 2;

  const positioned = [];
  const placedIds = new Set();

  for (let i = 0; i < parentNodes.length; i++) {
    const parent = parentNodes[i];
    const span = spans[i];
    const parentX = curX + span / 2;
    const parentY = origin.y;

    positioned.push({ ...parent, position: { x: parentX, y: parentY } });
    placedIds.add(parent.id);

    // Skip children already placed under a previous parent (multi-parent edges)
    const children = [...new Set(childrenOf.get(parent.id) || [])]
      .map((id) => nodes.find((n) => n.id === id))
      .filter(Boolean)
      .filter((child) => !placedIds.has(child.id));

    if (children.length > 0) {
      const cols =
        children.length <= CHILD_GRID_THRESHOLD
          ? children.length
          : Math.ceil(Math.sqrt(children.length));
      const childStartX = parentX - ((cols - 1) * CHILD_HSPACING) / 2;

      children.forEach((child, j) => {
        positioned.push({
          ...child,
          position: {
            x: childStartX + (j % cols) * CHILD_HSPACING,
            y: parentY + CHILD_VOFFSET + Math.floor(j / cols) * CHILD_VSPACING,
          },
        });
        placedIds.add(child.id);
      });
    }

    curX += span;
  }

  // Any nodes not yet placed (orphans from non-root, non-child types)
  const orphans = nodes.filter((n) => !placedIds.has(n.id));
  if (orphans.length > 0) {
    const orphanOrigin = { x: origin.x, y: origin.y + CHILD_VOFFSET * 3 };
    positioned.push(...gridLayoutAt(orphans, orphanOrigin));
  }

  return positioned;
}

function resolveOverlaps(positioned, existingPositions) {
  const result = [];
  for (const node of positioned) {
    const allOccupied = [...existingPositions, ...result.map((n) => n.position)];
    let { x, y } = node.position;
    for (let attempt = 0; attempt < 20; attempt++) {
      if (!allOccupied.some((pos) => nodesOverlap({ x, y }, pos, OVERLAP_MIN_DIST))) break;
      const angle = attempt * 0.7;
      const dist = 180 + attempt * 55;
      x = node.position.x + dist * Math.cos(angle);
      y = node.position.y + dist * Math.sin(angle);
    }
    result.push({ ...node, position: { x, y } });
  }
  return result;
}

function layoutBatch(nodes, edges, origin) {
  const nodeTypeMap = new Map(nodes.map((n) => [n.id, getNodeType(n)]));
  const nodeIds = new Set(nodes.map((n) => n.id));
  const internalEdges = edges.filter((e) => nodeIds.has(e.source) && nodeIds.has(e.target));
  const crossTypeEdges = internalEdges.filter(
    (e) => nodeTypeMap.get(e.source) !== nodeTypeMap.get(e.target)
  );
  const uniqueTypes = new Set(nodeTypeMap.values());

  if (uniqueTypes.size >= 2 && crossTypeEdges.length > 0) {
    return hierarchicalLayout(nodes, crossTypeEdges, nodeTypeMap, origin);
  }
  return gridLayoutAt(nodes, origin);
}

/**
 * Position new nodes intelligently on the canvas.
 *
 * Strategy (in priority order):
 *   1. If new nodes span multiple types with cross-type edges → hierarchical layout
 *   2. If many nodes or single type → grid layout centered near viewport
 *   3. Small additions connected to existing nodes → near connected nodes
 *
 * @param {Array} newNodes - Nodes to position
 * @param {Array} existingNodes - Already-placed nodes
 * @param {Array} edges - All edges (new + existing)
 * @param {Object} options
 * @param {Object} [options.viewportCenter] - Flow-coordinate center of the visible viewport {x, y}
 */
export function positionNewNodes(newNodes, existingNodes, edges = [], options = {}) {
  const { viewportCenter = null } = options;
  if (newNodes.length === 0) return newNodes;

  const existingPositions = existingNodes.filter((n) => n.position).map((n) => n.position);
  const origin = computeOrigin(existingNodes, viewportCenter);

  // No existing nodes: run full batch layout
  if (existingPositions.length === 0) {
    const positioned = layoutBatch(newNodes, edges, origin);
    return resolveOverlaps(positioned, []);
  }

  // Analyse the incoming batch
  const newNodeIds = new Set(newNodes.map((n) => n.id));
  const nodeTypeMap = new Map(newNodes.map((n) => [n.id, getNodeType(n)]));
  const uniqueTypes = new Set(nodeTypeMap.values());
  const internalEdges = edges.filter((e) => newNodeIds.has(e.source) && newNodeIds.has(e.target));
  const crossTypeEdges = internalEdges.filter(
    (e) => nodeTypeMap.get(e.source) !== nodeTypeMap.get(e.target)
  );

  // Use batch layout when there are multiple types with relationships or many nodes
  const isBatch = (uniqueTypes.size >= 2 && crossTypeEdges.length > 0) || newNodes.length > 5;
  if (isBatch) {
    const positioned = layoutBatch(newNodes, edges, origin);
    return resolveOverlaps(positioned, existingPositions);
  }

  // Small addition: try to place near connected existing nodes
  const existingNodeMap = new Map(existingNodes.map((n) => [n.id, n]));
  const positionedNodes = [];

  for (const node of newNodes) {
    const connectedEdges = edges.filter(
      (e) =>
        (e.source === node.id || e.target === node.id) &&
        (existingNodeMap.has(e.source) || existingNodeMap.has(e.target))
    );

    let position;

    if (connectedEdges.length > 0) {
      const connectedPositions = connectedEdges
        .map((e) => {
          const connectedId = e.source === node.id ? e.target : e.source;
          return existingNodeMap.get(connectedId)?.position;
        })
        .filter(Boolean);

      if (connectedPositions.length > 0) {
        const avgX = connectedPositions.reduce((s, p) => s + p.x, 0) / connectedPositions.length;
        const avgY = connectedPositions.reduce((s, p) => s + p.y, 0) / connectedPositions.length;
        const angle = positionedNodes.length * 1.2 + Math.PI / 4;
        position = {
          x: avgX + 280 * Math.cos(angle),
          y: avgY + 280 * Math.sin(angle),
        };
      }
    }

    if (!position) {
      // Fall back to near viewport center / origin
      const col = positionedNodes.length % 3;
      const row = Math.floor(positionedNodes.length / 3);
      position = {
        x: origin.x + (col - 1) * GRID_CELL_W,
        y: origin.y + row * GRID_CELL_H,
      };
    }

    const allOccupied = [...existingPositions, ...positionedNodes.map((n) => n.position)];
    let { x, y } = position;
    for (let attempt = 0; attempt < 15; attempt++) {
      if (!allOccupied.some((pos) => nodesOverlap({ x, y }, pos, OVERLAP_MIN_DIST))) break;
      const angle = attempt * 0.8;
      const dist = 180 + attempt * 60;
      x = position.x + dist * Math.cos(angle);
      y = position.y + dist * Math.sin(angle);
    }

    positionedNodes.push({ ...node, position: { x, y } });
  }

  return positionedNodes;
}
