import { describe, it, expect, beforeEach, vi } from 'vitest';

import useGraphStore from '../store/graphStore';
import { applyEdgeUpdate, confirmNodeDelete } from './sessionScopedGraphEdits';

const t = (key) => key;

// Switching sessions is what bumps the epoch, so drive the real store action
// rather than setting the counter by hand — that keeps the test honest about
// which switch path it is simulating.
const switchSession = () => useGraphStore.getState().resetSessionScopedState(t, 'en');

const node = (id) => ({ id, type: 'Actor', name: id });
const edge = (id) => ({ id, source: 'a', target: 'b', type: 'RELATES_TO' });

/**
 * A network call that hands back the lever to resolve it, so a test can switch
 * sessions at the one moment that matters: after the request is in flight and
 * before its result is applied.
 */
function deferred() {
  let release;
  const promise = new Promise((resolve) => {
    release = resolve;
  });
  return { promise, release: () => release() };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(console, 'error').mockImplementation(() => {});
  useGraphStore.setState({
    nodes: [node('a'), node('b')],
    edges: [edge('e1')],
    sessionEpoch: 0,
    editingEdge: null,
    deleteDialog: null,
    navHistory: [],
  });
});

describe('applyEdgeUpdate', () => {
  function harness() {
    const s = useGraphStore.getState();
    return {
      editingEdge: edge('e1'),
      updates: { type: 'OWNS' },
      nodes: s.nodes,
      edges: s.edges,
      updateVisualization: vi.fn(s.updateVisualization),
      syncRef: { current: { sendEdgesUpdated: vi.fn() } },
      setEditingEdge: vi.fn(s.setEditingEdge),
      showNotification: vi.fn(),
    };
  }

  it('applies the edit and fans it out when the session is unchanged', async () => {
    const h = harness();
    const applied = await applyEdgeUpdate({ ...h, updateEdge: vi.fn().mockResolvedValue({}) });

    expect(applied).toBe(true);
    expect(h.updateVisualization).toHaveBeenCalled();
    expect(h.syncRef.current.sendEdgesUpdated).toHaveBeenCalledWith([{ id: 'e1', type: 'OWNS' }]);
    expect(h.showNotification).toHaveBeenCalledWith('success', 'Edge updated');
  });

  // The regression: the dialog guard in App has already passed, and the reset at
  // the switch has already closed the dialog, so neither stops this. Only the
  // epoch captured before the await does.
  it('does not touch the canvas or fan out when the session switches mid-await', async () => {
    const h = harness();
    const call = deferred();
    const updateEdge = vi.fn(() => call.promise);

    const inFlight = applyEdgeUpdate({ ...h, updateEdge });
    expect(updateEdge).toHaveBeenCalled();

    switchSession();
    call.release();
    const applied = await inFlight;

    expect(applied).toBe(false);
    // The canvas of the session the user is now in must be untouched, and the
    // edit must not be broadcast through the sync client the switch repointed.
    expect(h.updateVisualization).not.toHaveBeenCalled();
    expect(h.syncRef.current.sendEdgesUpdated).not.toHaveBeenCalled();
    expect(h.showNotification).not.toHaveBeenCalled();
  });

  it('reports a failed PUT without touching the canvas', async () => {
    const h = harness();
    const applied = await applyEdgeUpdate({
      ...h,
      updateEdge: vi.fn().mockRejectedValue(new Error('boom')),
    });

    expect(applied).toBe(false);
    expect(h.updateVisualization).not.toHaveBeenCalled();
    expect(h.showNotification).toHaveBeenCalledWith('error', 'Could not update edge');
  });
});

describe('confirmNodeDelete', () => {
  function harness(dialog) {
    const s = useGraphStore.getState();
    return {
      deleteDialog: dialog,
      removeNode: vi.fn(s.removeNode),
      setDeleteDialog: vi.fn(s.setDeleteDialog),
      showNotification: vi.fn(),
    };
  }

  const single = { nodeId: 'a', nodeName: 'a', isMultiple: false };
  const multiple = { nodeIds: ['a', 'b'], nodeNames: ['a', 'b'], isMultiple: true };

  it('drops the deleted node from the canvas when the session is unchanged', async () => {
    const h = harness(single);
    const applied = await confirmNodeDelete({
      ...h,
      deleteNodes: vi.fn().mockResolvedValue({}),
    });

    expect(applied).toBe(true);
    expect(h.removeNode).toHaveBeenCalledWith('a');
    expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['b']);
    expect(h.showNotification).toHaveBeenCalledWith('success', 'Node deleted');
  });

  it('deletes every selected node in the multiple case', async () => {
    const h = harness(multiple);
    const deleteNodes = vi.fn().mockResolvedValue({});
    await confirmNodeDelete({ ...h, deleteNodes });

    expect(deleteNodes).toHaveBeenCalledWith(['a', 'b'], true);
    expect(useGraphStore.getState().nodes).toEqual([]);
    expect(h.showNotification).toHaveBeenCalledWith('success', '2 nodes deleted');
  });

  // The regression: the delete itself is global and stands, but the canvas edit
  // that follows it belongs to the session the user has already left.
  it('leaves the new session’s canvas alone when the session switches mid-await', async () => {
    const h = harness(single);
    const call = deferred();
    const deleteNodes = vi.fn(() => call.promise);

    const inFlight = confirmNodeDelete({ ...h, deleteNodes });
    expect(deleteNodes).toHaveBeenCalledWith(['a'], true);

    switchSession();
    // The user is now on a different session, whose canvas happens to hold its
    // own nodes; the in-flight delete must not reach into them.
    useGraphStore.setState({ nodes: [node('x'), node('y')] });
    call.release();
    const applied = await inFlight;

    expect(applied).toBe(false);
    expect(h.removeNode).not.toHaveBeenCalled();
    expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['x', 'y']);
    // The delete really did happen in the graph, so it is still reported.
    expect(h.showNotification).toHaveBeenCalledWith('success', 'Node deleted');
  });

  it('closes the confirmation even when the delete fails', async () => {
    const h = harness(single);
    const applied = await confirmNodeDelete({
      ...h,
      deleteNodes: vi.fn().mockRejectedValue(new Error('boom')),
    });

    expect(applied).toBe(false);
    expect(h.setDeleteDialog).toHaveBeenCalledWith(null);
    expect(useGraphStore.getState().nodes).toHaveLength(2);
    expect(h.showNotification).toHaveBeenCalledWith('error', 'Could not delete node(s)');
  });
});
