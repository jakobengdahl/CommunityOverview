import { getNodeColor } from '../../../packages/ui-graph-canvas/src/utils/nodeColors.js';
import { domePosition, layoutBounds } from './domeLayout.js';
import { nodeDetail, renderableEdges, renderableNodes } from './sceneModel.js';

export const XR_NODE_BUDGET = Object.freeze({
  maxNodes: 120,
  maxDetailedNodes: 48,
});

export function nodeColor(node) {
  if (node?.claim?.color) return node.claim.color;
  return getNodeColor(node?.type);
}

export function formatNodeTitle(node) {
  return node?.name || node?.id || 'Untitled node';
}

export function formatNodeSubtitle(node) {
  return node?.type || 'Unknown type';
}

export function cardLabel(node) {
  return {
    title: formatNodeTitle(node),
    subtitle: formatNodeSubtitle(node),
    footer: node?.id || '',
  };
}

function withEyeHeight(point, eyeHeight) {
  return { x: point.x, y: point.y + eyeHeight, z: point.z };
}

function curvePoints(a, b, lift = 0.18) {
  const mid = {
    x: (a.x + b.x) / 2,
    y: (a.y + b.y) / 2 + lift,
    z: (a.z + b.z) / 2,
  };
  const points = [];
  for (let i = 0; i <= 10; i += 1) {
    const t = i / 10;
    const inv = 1 - t;
    points.push({
      x: inv * inv * a.x + 2 * inv * t * mid.x + t * t * b.x,
      y: inv * inv * a.y + 2 * inv * t * mid.y + t * t * b.y,
      z: inv * inv * a.z + 2 * inv * t * mid.z + t * t * b.z,
    });
  }
  return points;
}

function clampCount(value, fallback) {
  return Number.isFinite(value) && value >= 0 ? Math.floor(value) : fallback;
}

function prioritizeSelected(nodes, selectedNodeId, maxNodes) {
  if (!selectedNodeId || maxNodes <= 0 || nodes.length <= maxNodes) return nodes.slice(0, maxNodes);
  const selectedIndex = nodes.findIndex((node) => node.id === selectedNodeId);
  if (selectedIndex < 0 || selectedIndex < maxNodes) return nodes.slice(0, maxNodes);
  return [...nodes.slice(0, maxNodes - 1), nodes[selectedIndex]];
}

function positionedNode(node, bounds, eyeHeight, domeOptions) {
  const position = withEyeHeight(domePosition(node.x, node.y, bounds, domeOptions), eyeHeight);
  const label = cardLabel(node);
  return {
    ...node,
    position,
    color: nodeColor(node),
    ...label,
  };
}

export function domeSceneData(
  scene,
  {
    eyeHeight = 0,
    selectedNodeId = null,
    maxNodes = XR_NODE_BUDGET.maxNodes,
    maxDetailedNodes = XR_NODE_BUDGET.maxDetailedNodes,
    ...domeOptions
  } = {}
) {
  const renderable = renderableNodes(scene);
  const nodeLimit = Math.min(clampCount(maxNodes, XR_NODE_BUDGET.maxNodes), renderable.length);
  const detailLimit = Math.min(
    clampCount(maxDetailedNodes, XR_NODE_BUDGET.maxDetailedNodes),
    nodeLimit
  );
  const visibleNodes = prioritizeSelected(renderable, selectedNodeId, nodeLimit);
  const bounds = layoutBounds(renderable);
  const positioned = visibleNodes.map((node) =>
    positionedNode(node, bounds, eyeHeight, domeOptions)
  );
  const cards = positioned.slice(0, detailLimit);
  const markers = positioned.slice(detailLimit);
  const visibleById = new Map(positioned.map((node) => [node.id, node]));
  const edges = renderableEdges(scene).flatMap((edge) => {
    const source = visibleById.get(edge.source);
    const target = visibleById.get(edge.target);
    if (!source || !target) return [];
    return [
      {
        ...edge,
        points: curvePoints(source.position, target.position),
      },
    ];
  });
  return {
    cards,
    markers,
    edges,
    budget: {
      totalNodes: renderable.length,
      visibleNodes: positioned.length,
      detailedNodes: cards.length,
      markerNodes: markers.length,
      hiddenByBudget: Math.max(0, renderable.length - positioned.length),
    },
  };
}

export function selectionDetail(scene, selectedNodeId) {
  if (!selectedNodeId) return null;
  return nodeDetail(scene, selectedNodeId);
}
