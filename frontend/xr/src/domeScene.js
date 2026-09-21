import { getNodeColor } from '../../../packages/ui-graph-canvas/src/utils/nodeColors.js';
import { domePosition, layoutBounds } from './domeLayout.js';
import { nodeDetail, renderableEdges, renderableNodes } from './sceneModel.js';

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

export function domeSceneData(scene, { eyeHeight = 0, ...domeOptions } = {}) {
  const nodes = renderableNodes(scene);
  const bounds = layoutBounds(nodes);
  const cards = nodes.map((node) => {
    const position = withEyeHeight(domePosition(node.x, node.y, bounds, domeOptions), eyeHeight);
    const label = cardLabel(node);
    return {
      ...node,
      position,
      color: nodeColor(node),
      ...label,
    };
  });
  const cardsById = new Map(cards.map((node) => [node.id, node]));
  const edges = renderableEdges(scene).map((edge) => {
    const source = cardsById.get(edge.source);
    const target = cardsById.get(edge.target);
    return {
      ...edge,
      points: curvePoints(source.position, target.position),
    };
  });
  return { cards, edges };
}

export function selectionDetail(scene, selectedNodeId) {
  if (!selectedNodeId) return null;
  return nodeDetail(scene, selectedNodeId);
}
