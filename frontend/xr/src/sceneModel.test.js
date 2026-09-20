import { describe, expect, it } from 'vitest';
import {
  EMPTY_SCENE,
  applyOp,
  applyOps,
  hydrateNodes,
  pendingNodeIds,
  renderableEdges,
  renderableNodes,
  nodeDetail,
  sceneFromSession,
  withClaims,
  withRoster,
} from './sceneModel.js';

const sessionPayload = {
  id: '1111-2222-3333-4444',
  name: 'Demo',
  state: {
    node_refs: ['n1', 'n2'],
    positions: { n1: { x: 10, y: 20 }, n2: { x: 30, y: 40 } },
    hidden_node_ids: ['n2'],
  },
  resolved: {
    nodes: [
      { id: 'n1', name: 'Alpha', type: 'Actor' },
      {
        id: 'n2',
        name: 'Beta',
        type: 'Initiative',
        metadata: { description: 'A useful summary.' },
      },
    ],
    edges: [{ id: 'e1', source: 'n1', target: 'n2' }],
  },
};

describe('sceneFromSession', () => {
  it('builds positioned, hydrated nodes from the resolved session payload', () => {
    const scene = sceneFromSession(sessionPayload);
    expect(scene.sessionId).toBe('1111-2222-3333-4444');
    expect(scene.name).toBe('Demo');
    expect(scene.nodes.n1).toEqual({
      id: 'n1',
      name: 'Alpha',
      type: 'Actor',
      summary: null,
      metadata: null,
      x: 10,
      y: 20,
      hydrated: true,
    });
    expect(scene.hiddenNodeIds).toEqual(['n2']);
    expect(pendingNodeIds(scene)).toEqual([]);
    expect(scene.edges.e1).toEqual({ id: 'e1', source: 'n1', target: 'n2', type: null });
    expect(scene.hiddenEdgeIds).toEqual([]);
    expect(nodeDetail(scene, 'n2')).toEqual({
      id: 'n2',
      name: 'Beta',
      type: 'Initiative',
      summary: 'A useful summary.',
      hydrated: true,
    });
  });

  it('materialises no node from a stored position whose node no longer resolves', () => {
    // A node deleted from the graph keeps its entry in the session's position
    // map until something rewrites it. Identity comes from `resolved.nodes`
    // alone, so the stale position must not conjure an anonymous node.
    const scene = sceneFromSession({
      ...sessionPayload,
      state: {
        ...sessionPayload.state,
        node_refs: ['n1', 'n2', 'gone'],
        positions: { ...sessionPayload.state.positions, gone: { x: 5, y: 5 } },
      },
    });
    expect(Object.keys(scene.nodes).sort()).toEqual(['n1', 'n2']);
    expect(renderableNodes(scene).map((n) => n.id)).toEqual(['n1']);
  });

  it('keeps a resolved node without a stored position out of the render set', () => {
    const scene = sceneFromSession({
      ...sessionPayload,
      state: { ...sessionPayload.state, positions: { n1: { x: 10, y: 20 } }, hidden_node_ids: [] },
    });
    expect(scene.nodes.n2.x).toBeNull();
    expect(renderableNodes(scene).map((n) => n.id)).toEqual(['n1']);
  });
});

describe('applyOp', () => {
  const base = sceneFromSession({
    ...sessionPayload,
    state: { ...sessionPayload.state, hidden_node_ids: [] },
  });

  it('adds ids from nodes_added as unhydrated, unpositioned nodes', () => {
    const scene = applyOp(base, { op: 'nodes_added', node_ids: ['n3'] });
    expect(scene.nodes.n3).toEqual({
      id: 'n3',
      name: null,
      type: null,
      x: null,
      y: null,
      hydrated: false,
    });
    expect(pendingNodeIds(scene)).toEqual(['n3']);
    // Not renderable until a position op arrives — the originator emits
    // nodes_added and node_moved as separate ops.
    expect(renderableNodes(scene).map((n) => n.id)).toEqual(['n1', 'n2']);
  });

  it('does not reset a node that nodes_added names again', () => {
    const scene = applyOp(base, { op: 'nodes_added', node_ids: ['n1'] });
    expect(scene).toBe(base);
  });

  it('positions an added node from the follow-up node_moved op', () => {
    const scene = applyOps(base, [
      { op: 'nodes_added', node_ids: ['n3'] },
      { op: 'node_moved', node_id: 'n3', position: { x: 5, y: 6 } },
    ]);
    expect(renderableNodes(scene).map((n) => n.id)).toEqual(['n1', 'n2', 'n3']);
    expect(scene.nodes.n3.x).toBe(5);
  });

  it('drops removed nodes and their hide entries', () => {
    const hidden = applyOp(base, { op: 'nodes_hidden', node_ids: ['n1'] });
    const scene = applyOp(hidden, { op: 'nodes_removed', node_ids: ['n1'] });
    expect(scene.nodes.n1).toBeUndefined();
    expect(scene.hiddenNodeIds).toEqual([]);
    // Re-adding the same id must come back visible, not silently hidden.
    const readded = applyOps(scene, [
      { op: 'nodes_added', node_ids: ['n1'] },
      { op: 'node_moved', node_id: 'n1', position: { x: 1, y: 2 } },
    ]);
    expect(renderableNodes(readded).map((n) => n.id)).toEqual(['n1', 'n2']);
  });

  it('moves only the node a node_moved op names', () => {
    const scene = applyOp(base, { op: 'node_moved', node_id: 'n1', position: { x: 99, y: 98 } });
    expect(scene.nodes.n1).toMatchObject({ x: 99, y: 98 });
    expect(scene.nodes.n2).toBe(base.nodes.n2);
  });

  it('ignores a node_moved for an unknown node', () => {
    expect(applyOp(base, { op: 'node_moved', node_id: 'ghost', position: { x: 1, y: 1 } })).toBe(
      base
    );
  });

  it('ignores non-finite coordinates instead of poisoning the transform', () => {
    const scene = applyOp(base, { op: 'node_moved', node_id: 'n1', position: { x: NaN, y: 3 } });
    expect(scene).toBe(base);
    expect(scene.nodes.n1.x).toBe(10);
  });

  it('applies layout_applied to every known node and drops unknown ids', () => {
    const scene = applyOp(base, {
      op: 'layout_applied',
      positions: { n1: { x: 1, y: 2 }, n2: { x: 3, y: 4 }, ghost: { x: 5, y: 6 } },
    });
    expect(scene.nodes.n1).toMatchObject({ x: 1, y: 2 });
    expect(scene.nodes.n2).toMatchObject({ x: 3, y: 4 });
    expect(scene.nodes.ghost).toBeUndefined();
  });

  it('hides and shows nodes without losing their positions', () => {
    const hidden = applyOp(base, { op: 'nodes_hidden', node_ids: ['n1', 'n1'] });
    expect(hidden.hiddenNodeIds).toEqual(['n1']);
    expect(renderableNodes(hidden).map((n) => n.id)).toEqual(['n2']);
    const shown = applyOp(hidden, { op: 'nodes_shown', node_ids: ['n1'] });
    expect(renderableNodes(shown).map((n) => n.id)).toEqual(['n1', 'n2']);
    expect(shown.nodes.n1.x).toBe(10);
  });

  it('tracks a session rename', () => {
    expect(applyOp(base, { op: 'session_renamed', name: 'Renamed' }).name).toBe('Renamed');
  });

  it('adds, hides, shows and removes renderable edges from the existing op vocabulary', () => {
    const added = applyOp(base, {
      op: 'edges_added',
      edges: [{ id: 'e2', source: 'n1', target: 'n2', type: 'RELATES_TO' }],
    });
    expect(renderableEdges(added)).toEqual([
      { id: 'e1', source: 'n1', target: 'n2', type: null },
      { id: 'e2', source: 'n1', target: 'n2', type: 'RELATES_TO' },
    ]);

    const hidden = applyOp(added, { op: 'edges_hidden', edge_ids: ['e1'] });
    expect(renderableEdges(hidden).map((e) => e.id)).toEqual(['e2']);

    const shown = applyOp(hidden, { op: 'edges_shown', edge_ids: ['e1'] });
    expect(renderableEdges(shown).map((e) => e.id)).toEqual(['e1', 'e2']);

    const removed = applyOp(shown, { op: 'edges_removed', edge_ids: ['e2'] });
    expect(removed.edges.e2).toBeUndefined();
    expect(renderableEdges(removed).map((e) => e.id)).toEqual(['e1']);
  });

  it('does not render an edge whose endpoint is hidden or unpositioned', () => {
    const hidden = applyOp(base, { op: 'nodes_hidden', node_ids: ['n1'] });
    expect(renderableEdges(hidden)).toEqual([]);

    const unpositioned = applyOps(base, [
      { op: 'nodes_added', node_ids: ['n3'] },
      { op: 'edges_added', edges: [{ id: 'e3', source: 'n1', target: 'n3' }] },
    ]);
    expect(renderableEdges(unpositioned).map((e) => e.id)).toEqual(['e1']);
  });

  it('removes edges attached to a removed node', () => {
    const scene = applyOp(base, { op: 'nodes_removed', node_ids: ['n1'] });
    expect(scene.edges.e1).toBeUndefined();
    expect(renderableEdges(scene)).toEqual([]);
  });

  it('leaves the scene untouched for ops this client does not render', () => {
    for (const op of [
      { op: 'annotation_created', annotation: { id: 'a1', kind: 'note' } },
      { op: 'group_membership_changed', group_id: 'a1', member_node_ids: ['n1'] },
      undefined,
    ]) {
      expect(applyOp(base, op)).toBe(base);
    }
  });
});

describe('presence and claims', () => {
  const base = sceneFromSession({
    ...sessionPayload,
    state: { ...sessionPayload.state, hidden_node_ids: [] },
  });

  it('annotates a claimed node with the claiming client', () => {
    const scene = withClaims(base, {
      n1: { clientId: 'client-b', color: '#ff0000', displayName: 'Bo' },
    });
    const [n1, n2] = renderableNodes(scene);
    expect(n1.claim).toEqual({ clientId: 'client-b', color: '#ff0000', displayName: 'Bo' });
    expect(n2.claim).toBeNull();
  });

  it('keeps only roster members that carry a client id', () => {
    const scene = withRoster(base, [{ client_id: 'client-b', display_name: 'Bo' }, null, {}]);
    expect(scene.roster).toEqual([{ client_id: 'client-b', display_name: 'Bo' }]);
  });
});

describe('hydrateNodes', () => {
  it('fills name and type for a node the op stream knew only by id', () => {
    const added = applyOp(EMPTY_SCENE, { op: 'nodes_added', node_ids: ['n3'] });
    const scene = hydrateNodes(added, [{ id: 'n3', name: 'Gamma', type: 'Goal' }]);
    expect(scene.nodes.n3).toMatchObject({ name: 'Gamma', type: 'Goal', hydrated: true });
    expect(pendingNodeIds(scene)).toEqual([]);
  });

  it('preserves optional node summary fields for the read-only detail HUD', () => {
    const added = applyOp(EMPTY_SCENE, { op: 'nodes_added', node_ids: ['n3'] });
    const scene = hydrateNodes(added, [
      { id: 'n3', name: 'Gamma', type: 'Goal', description: 'Plain detail text.' },
    ]);
    expect(nodeDetail(scene, 'n3')).toEqual({
      id: 'n3',
      name: 'Gamma',
      type: 'Goal',
      summary: 'Plain detail text.',
      hydrated: true,
    });
  });

  it('ignores details for a node that was removed while the read was in flight', () => {
    const scene = hydrateNodes(EMPTY_SCENE, [{ id: 'gone', name: 'Gone' }]);
    expect(scene).toBe(EMPTY_SCENE);
  });
});
