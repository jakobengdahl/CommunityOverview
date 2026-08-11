import { describe, it, expect, beforeEach } from 'vitest';
import useGraphStore, { appendNavEntries } from './graphStore';

const AT = '2026-08-11T10:00:00.000Z';

describe('appendNavEntries', () => {
  it('prepends entries newest-first and stamps the shared timestamp', () => {
    const out = appendNavEntries([], [{ id: 'a', name: 'A', type: 'Goal', action: 'added' }], AT);
    expect(out).toEqual([{ id: 'a', name: 'A', type: 'Goal', action: 'added', at: AT }]);
  });

  it('defaults missing name to the id and normalises unknown actions to visited', () => {
    const [row] = appendNavEntries([], [{ id: 'x', action: 'weird' }], AT);
    expect(row).toMatchObject({ id: 'x', name: 'x', type: '', action: 'visited' });
  });

  it('collapses a repeat of the node already at the top instead of duplicating it', () => {
    const first = appendNavEntries([], [{ id: 'a', name: 'A', action: 'visited' }], AT);
    const second = appendNavEntries(first, [{ id: 'a', name: 'A', action: 'visited' }], AT);
    expect(second).toHaveLength(1);
    expect(second[0].action).toBe('visited');
  });

  it('keeps an "added" designation when a later visit collapses onto it', () => {
    const added = appendNavEntries([], [{ id: 'a', name: 'A', action: 'added' }], AT);
    const afterVisit = appendNavEntries(added, [{ id: 'a', name: 'A', action: 'visited' }], AT);
    expect(afterVisit).toHaveLength(1);
    expect(afterVisit[0].action).toBe('added');
  });

  it('records the whole batch newest-last-on-top and preserves per-node type', () => {
    const out = appendNavEntries(
      [],
      [
        { id: 'a', name: 'A', type: 'Goal', action: 'added' },
        { id: 'b', name: 'B', type: 'Actor', action: 'added' },
      ],
      AT
    );
    expect(out.map((r) => r.id)).toEqual(['b', 'a']);
    expect(out.map((r) => r.type)).toEqual(['Actor', 'Goal']);
  });

  it('does not collapse a repeat that is not at the top', () => {
    let trail = appendNavEntries([], [{ id: 'a', name: 'A', action: 'visited' }], AT);
    trail = appendNavEntries(trail, [{ id: 'b', name: 'B', action: 'visited' }], AT);
    trail = appendNavEntries(trail, [{ id: 'a', name: 'A', action: 'visited' }], AT);
    expect(trail.map((r) => r.id)).toEqual(['a', 'b', 'a']);
  });

  it('bounds the trail to the given limit, dropping the oldest', () => {
    const entries = Array.from({ length: 5 }, (_, i) => ({ id: `n${i}`, action: 'added' }));
    let trail = [];
    for (const e of entries) trail = appendNavEntries(trail, [e], AT, 3);
    expect(trail.map((r) => r.id)).toEqual(['n4', 'n3', 'n2']);
  });

  it('ignores malformed entries', () => {
    expect(appendNavEntries([], [null, {}, { name: 'no id' }], AT)).toEqual([]);
  });
});

describe('graphStore navHistory recording', () => {
  beforeEach(() => {
    useGraphStore.setState({ nodes: [], edges: [], navHistory: [], focusNodeId: null });
  });

  it('records an "added" entry for each genuinely new node', () => {
    useGraphStore
      .getState()
      .addNodesToVisualization([{ id: 'a', name: 'Alpha', type: 'Goal' }], []);
    const { navHistory } = useGraphStore.getState();
    expect(navHistory).toHaveLength(1);
    expect(navHistory[0]).toMatchObject({ id: 'a', name: 'Alpha', type: 'Goal', action: 'added' });
  });

  it('records every node of a bulk add, newest-last-on-top', () => {
    useGraphStore.getState().addNodesToVisualization(
      [
        { id: 'a', name: 'Alpha', type: 'Goal' },
        { id: 'b', name: 'Beta', type: 'Actor' },
        { id: 'c', name: 'Gamma', type: 'Risk' },
      ],
      []
    );
    expect(useGraphStore.getState().navHistory.map((r) => r.id)).toEqual(['c', 'b', 'a']);
  });

  it('resolves the domain type from data for a React Flow shaped node', () => {
    useGraphStore
      .getState()
      .addNodesToVisualization(
        [{ id: 'a', type: 'custom', data: { label: 'Alpha', type: 'Goal' } }],
        []
      );
    expect(useGraphStore.getState().navHistory[0]).toMatchObject({
      id: 'a',
      name: 'Alpha',
      type: 'Goal',
    });
  });

  it('keeps the "added" label after a search-style add-then-focus on the same node', () => {
    useGraphStore
      .getState()
      .addNodesToVisualization([{ id: 'a', name: 'Alpha', type: 'Goal' }], []);
    useGraphStore.getState().setFocusNodeId('a');
    const { navHistory } = useGraphStore.getState();
    expect(navHistory).toHaveLength(1);
    expect(navHistory[0].action).toBe('added');
  });

  it('prunes trail entries for nodes dropped by a wholesale updateVisualization', () => {
    useGraphStore.getState().addNodesToVisualization(
      [
        { id: 'a', name: 'Alpha', type: 'Goal' },
        { id: 'b', name: 'Beta', type: 'Actor' },
      ],
      []
    );
    // Replace the visualization with only 'b'; the 'a' trail row must be pruned.
    useGraphStore.getState().updateVisualization([{ id: 'b', name: 'Beta', type: 'Actor' }], []);
    expect(useGraphStore.getState().navHistory.map((r) => r.id)).toEqual(['b']);
  });

  it('prunes a removed node from the trail', () => {
    useGraphStore
      .getState()
      .addNodesToVisualization([{ id: 'a', name: 'Alpha', type: 'Goal' }], []);
    useGraphStore.getState().removeNode('a');
    expect(useGraphStore.getState().navHistory).toHaveLength(0);
  });

  it('does not record an add for a node already in the visualization', () => {
    useGraphStore.setState({ nodes: [{ id: 'a', name: 'Alpha', type: 'Goal' }] });
    useGraphStore
      .getState()
      .addNodesToVisualization([{ id: 'a', name: 'Alpha', type: 'Goal' }], []);
    expect(useGraphStore.getState().navHistory).toHaveLength(0);
  });

  it('records a "visited" entry when navigating to a node, resolving its name/type', () => {
    useGraphStore.setState({ nodes: [{ id: 'a', name: 'Alpha', type: 'Goal' }] });
    useGraphStore.getState().setFocusNodeId('a');
    const { navHistory, focusNodeId } = useGraphStore.getState();
    expect(focusNodeId).toBe('a');
    expect(navHistory[0]).toMatchObject({
      id: 'a',
      name: 'Alpha',
      type: 'Goal',
      action: 'visited',
    });
  });

  it('records a visit when a node detail dialog is opened', () => {
    useGraphStore.setState({ nodes: [{ id: 'a', name: 'Alpha', type: 'Goal' }] });
    useGraphStore.getState().setDetailNode({ id: 'a', data: { label: 'Alpha' } });
    expect(useGraphStore.getState().navHistory[0]).toMatchObject({ id: 'a', action: 'visited' });
  });

  it('does not record anything when focus is cleared via setFocusNodeId(null)', () => {
    useGraphStore.getState().setFocusNodeId(null);
    expect(useGraphStore.getState().navHistory).toHaveLength(0);
  });

  it('clears the trail on clearVisualization and clearNavHistory', () => {
    useGraphStore.setState({
      navHistory: [{ id: 'a', name: 'A', type: '', action: 'added', at: AT }],
    });
    useGraphStore.getState().clearNavHistory();
    expect(useGraphStore.getState().navHistory).toHaveLength(0);

    useGraphStore.setState({
      navHistory: [{ id: 'a', name: 'A', type: '', action: 'added', at: AT }],
    });
    useGraphStore.getState().clearVisualization();
    expect(useGraphStore.getState().navHistory).toHaveLength(0);
  });
});
