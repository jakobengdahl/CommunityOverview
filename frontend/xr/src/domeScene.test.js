import { describe, expect, it } from 'vitest';
import { domeSceneData, nodeColor, selectionDetail } from './domeScene.js';
import { applyOp, sceneFromSession, withClaims } from './sceneModel.js';

const scene = sceneFromSession({
  id: '1111-2222-3333-4444',
  state: {
    positions: {
      n1: { x: 0, y: 0 },
      n2: { x: 100, y: 100 },
    },
    hidden_node_ids: [],
    hidden_edge_ids: [],
  },
  resolved: {
    nodes: [
      { id: 'n1', name: 'Alpha', type: 'Actor', description: 'First node.' },
      { id: 'n2', name: 'Beta', type: 'Goal' },
    ],
    edges: [{ id: 'e1', source: 'n1', target: 'n2', type: 'RELATES_TO' }],
  },
});

describe('nodeColor', () => {
  it('uses claim color first, then known node type color, then neutral fallback', () => {
    expect(nodeColor({ type: 'Actor' })).toBe('#3B82F6');
    expect(nodeColor({ type: 'Unknown' })).toBe('#9CA3AF');
    expect(nodeColor({ type: 'Actor', claim: { color: '#abcdef' } })).toBe('#abcdef');
  });
});

describe('domeSceneData', () => {
  it('derives readable cards and curved edge points from renderable scene data', () => {
    const data = domeSceneData(scene, { eyeHeight: 1.5 });
    expect(data.cards.map((card) => [card.id, card.title, card.subtitle, card.color])).toEqual([
      ['n1', 'Alpha', 'Actor', '#3B82F6'],
      ['n2', 'Beta', 'Goal', '#6366F1'],
    ]);
    expect(data.cards[0].position.y).toBeGreaterThan(1.5);
    expect(data.edges).toHaveLength(1);
    expect(data.edges[0]).toMatchObject({ id: 'e1', source: 'n1', target: 'n2' });
    expect(data.edges[0].points).toHaveLength(11);
    expect(data.edges[0].points[0]).toEqual(data.cards[0].position);
    expect(data.edges[0].points.at(-1)).toEqual(data.cards[1].position);
  });

  it('carries remote claim color into the card derivation', () => {
    const claimed = withClaims(scene, {
      n1: { clientId: 'client-b', color: '#ff0000', displayName: 'Bo' },
    });
    expect(domeSceneData(claimed).cards[0].color).toBe('#ff0000');
  });

  it('drops an edge as soon as one endpoint stops rendering', () => {
    const hidden = applyOp(scene, { op: 'nodes_hidden', node_ids: ['n2'] });
    const data = domeSceneData(hidden);
    expect(data.cards.map((card) => card.id)).toEqual(['n1']);
    expect(data.edges).toEqual([]);
  });
});

describe('selectionDetail', () => {
  it('returns minimal read-only detail for a selected node', () => {
    expect(selectionDetail(scene, 'n1')).toEqual({
      id: 'n1',
      name: 'Alpha',
      type: 'Actor',
      summary: 'First node.',
      hydrated: true,
    });
  });

  it('returns null for no selection or a missing node', () => {
    expect(selectionDetail(scene, null)).toBeNull();
    expect(selectionDetail(scene, 'missing')).toBeNull();
  });
});
