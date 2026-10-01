import { describe, it, expect, beforeEach, vi } from 'vitest';

import useGraphStore from '../src/store/graphStore';
import {
  captureCanvasScope,
  connectNodes,
  deleteEdgeEverywhere,
  expandNode,
  loadSavedViewNode,
  persistNewNodes,
  persistNodeUpdates,
  setEdgeType,
} from '../src/utils/sessionScopedGraphEdits';

const t = (key) => key;

// Drive the real store actions: a switch bumps sessionEpoch, a clear bumps only
// canvasBaselineEpoch, and the guards differ exactly on that difference.
const switchSession = () => {
  useGraphStore.getState().clearVisualization();
  useGraphStore.getState().resetSessionScopedState(t, 'en');
};
const clearCanvas = () => useGraphStore.getState().clearVisualization();
// A switch whose store reset lands on its own, without the canvas reload that
// normally precedes it — so a guard keyed on canvasBaselineEpoch alone fails.
const bumpSessionOnly = () => useGraphStore.getState().resetSessionScopedState(t, 'en');
// A reconnect resync reloads the same session wholesale: clearVisualization and
// then the server's contents, which bumps canvasBaselineEpoch but puts the same
// nodes straight back.
const resync = () => {
  const { nodes, edges } = useGraphStore.getState();
  useGraphStore.getState().clearVisualization();
  useGraphStore.getState().addNodesToVisualization(nodes, edges);
};

const node = (id, extra = {}) => ({ id, type: 'Actor', name: id, ...extra });
const edge = (id, source = 'a', target = 'b') => ({ id, source, target, type: 'RELATES_TO' });

function deferred(value) {
  let release;
  let fail;
  const promise = new Promise((resolve, reject) => {
    release = resolve;
    fail = reject;
  });
  return { promise, release: () => release(value), reject: (error) => fail(error) };
}

const store = () => useGraphStore.getState();
const nodeIds = () => store().nodes.map((n) => n.id);

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(console, 'error').mockImplementation(() => {});
  useGraphStore.setState({
    nodes: [node('a'), node('b')],
    edges: [edge('e1')],
    sessionEpoch: 0,
    canvasBaselineEpoch: 0,
    editingNode: null,
    editingEdge: null,
    deleteDialog: null,
    navHistory: [],
  });
});

describe('captureCanvasScope', () => {
  it('reports a switch as both a session change and a canvas replace', () => {
    const scope = captureCanvasScope();
    useGraphStore.getState().resetSessionScopedState(t, 'en');
    expect(scope.sessionChanged()).toBe(true);
    expect(scope.canvasReplaced()).toBe(true);
  });

  it('reports a clear as a canvas replace but not a session change', () => {
    const scope = captureCanvasScope();
    clearCanvas();
    expect(scope.sessionChanged()).toBe(false);
    expect(scope.canvasReplaced()).toBe(true);
  });

  it('reports neither for an in-place edit', () => {
    const scope = captureCanvasScope();
    store().updateEdgeData('e1', { type: 'OWNS' });
    store().addNodesToVisualization([node('c')], []);
    expect(scope.sessionChanged()).toBe(false);
    expect(scope.canvasReplaced()).toBe(false);
  });
});

describe('persistNodeUpdates (handleNodeUpdate and the save* update branches)', () => {
  function run(interleave, entries = [{ id: 'a', updates: { name: 'A2' } }]) {
    const s = store();
    const call = deferred({});
    const updateNode = vi.fn(() => call.promise);
    const onApplied = vi.fn();
    const inFlight = persistNodeUpdates({
      entries,
      updateNode,
      updateVisualization: s.updateVisualization,
      onApplied,
    });
    interleave?.();
    call.release();
    return { inFlight, updateNode, onApplied };
  }

  it('patches the node on the canvas and runs onApplied when nothing changed', async () => {
    const { inFlight, onApplied } = run();
    expect(await inFlight).toBe(true);
    expect(store().nodes.find((n) => n.id === 'a').name).toBe('A2');
    expect(store().edges.map((e) => e.id)).toEqual(['e1']);
    expect(onApplied).toHaveBeenCalledTimes(1);
  });

  // The node's headline case: the write-back used to replace the new session's
  // whole canvas with the previous session's nodes and edges.
  it('leaves the new session canvas alone when the session switches mid-await', async () => {
    const { inFlight, updateNode, onApplied } = run(() => {
      switchSession();
      store().addNodesToVisualization([node('x')], [edge('ex', 'x', 'x')]);
    });
    expect(await inFlight).toBe(false);
    expect(updateNode).toHaveBeenCalledWith('a', { name: 'A2' });
    expect(nodeIds()).toEqual(['x']);
    expect(store().edges.map((e) => e.id)).toEqual(['ex']);
    expect(onApplied).not.toHaveBeenCalled();
  });

  // sessionEpoch alone misses this: a clear does not bump it, and the stale
  // write-back would restore everything the user just cleared.
  it('does not restore a canvas the user cleared mid-await', async () => {
    const { inFlight, onApplied } = run(clearCanvas);
    await inFlight;
    expect(store().nodes).toEqual([]);
    expect(store().edges).toEqual([]);
    expect(onApplied).not.toHaveBeenCalled();
  });

  it('persists every entry in order and patches each (agent + subscription)', async () => {
    const first = deferred({});
    const second = deferred({});
    const updateNode = vi.fn((id) => (id === 'a' ? first.promise : second.promise));
    const inFlight = persistNodeUpdates({
      entries: [
        { id: 'a', updates: { name: 'agent2' } },
        { id: 'b', updates: { name: 'sub2' } },
      ],
      updateNode,
      updateVisualization: store().updateVisualization,
    });
    expect(updateNode).toHaveBeenCalledTimes(1);
    first.release();
    await Promise.resolve();
    await Promise.resolve();
    expect(updateNode).toHaveBeenCalledTimes(2);
    expect(updateNode).toHaveBeenLastCalledWith('b', { name: 'sub2' });
    second.release();
    expect(await inFlight).toBe(true);
    expect(store().nodes.map((n) => n.name)).toEqual(['agent2', 'sub2']);
  });

  it('drops the patch when only the session epoch moved mid-await', async () => {
    const { inFlight, onApplied } = run(() => {
      bumpSessionOnly();
      store().addNodesToVisualization([node('x')], []);
    });
    expect(await inFlight).toBe(false);
    expect(store().nodes.find((n) => n.id === 'a').name).toBe('a');
    expect(onApplied).not.toHaveBeenCalled();
  });

  // Patching the canvas as it is when the reply lands, not a copy from before
  // the await, is what keeps concurrent in-place changes and resync reloads.
  it('keeps changes made to the canvas during the await', async () => {
    const { inFlight } = run(() => {
      store().addNodesToVisualization([node('c')], [edge('e2', 'a', 'c')]);
    });
    expect(await inFlight).toBe(true);
    expect(nodeIds()).toEqual(['a', 'b', 'c']);
    expect(store().edges.map((e) => e.id)).toEqual(['e1', 'e2']);
    expect(store().nodes.find((n) => n.id === 'a').name).toBe('A2');
  });

  it('still patches the node after a resync reload, without closing a dialog', async () => {
    const { inFlight, onApplied } = run(resync);
    expect(await inFlight).toBe(true);
    expect(store().nodes.find((n) => n.id === 'a').name).toBe('A2');
    expect(onApplied).not.toHaveBeenCalled();
  });

  it('runs onApplied for a node that is not on the canvas', async () => {
    const updateVisualization = vi.fn();
    const onApplied = vi.fn();
    const applied = await persistNodeUpdates({
      entries: [{ id: 'elsewhere', updates: { name: 'Z' } }],
      updateNode: vi.fn().mockResolvedValue({}),
      updateVisualization,
      onApplied,
    });
    expect(applied).toBe(true);
    expect(updateVisualization).not.toHaveBeenCalled();
    expect(onApplied).toHaveBeenCalledTimes(1);
  });

  it('propagates an API failure without touching the canvas', async () => {
    const s = store();
    const before = s.nodes;
    const updateVisualization = vi.fn();
    await expect(
      persistNodeUpdates({
        entries: [{ id: 'a', updates: { name: 'A2' } }],
        updateNode: vi.fn().mockRejectedValue(new Error('boom')),
        updateVisualization,
      })
    ).rejects.toThrow('boom');
    expect(updateVisualization).not.toHaveBeenCalled();
    expect(store().nodes).toBe(before);
  });
});

describe('persistNewNodes (the save* create branches)', () => {
  function run(interleave) {
    const call = deferred({ added_node_ids: ['new1'] });
    const addNodes = vi.fn(() => call.promise);
    const inFlight = persistNewNodes({
      nodes: [node('tmp')],
      addNodes,
      addNodesToVisualization: store().addNodesToVisualization,
      toCanvas: (result) => ({ nodes: [node(result.added_node_ids[0])] }),
    });
    interleave?.();
    call.release();
    return { inFlight, addNodes };
  }

  it('draws the created node when nothing changed', async () => {
    const { inFlight } = run();
    expect(await inFlight).toBe(true);
    expect(nodeIds()).toContain('new1');
  });

  it('does not draw the created node into a session switched to mid-await', async () => {
    const { inFlight, addNodes } = run(switchSession);
    expect(await inFlight).toBe(false);
    expect(addNodes).toHaveBeenCalled();
    expect(nodeIds()).not.toContain('new1');
  });

  // Additive, so a clear does not make it wrong: the user still asked to see it.
  it('still draws the created node after a clear in the same session', async () => {
    const { inFlight } = run(clearCanvas);
    expect(await inFlight).toBe(true);
    expect(nodeIds()).toEqual(['new1']);
  });

  it('creates and draws the edges it is given', async () => {
    const addNodes = vi.fn().mockResolvedValue({ added_node_ids: ['n1'], added_edge_ids: ['k1'] });
    const applied = await persistNewNodes({
      nodes: [node('tmp')],
      edges: [edge('tmp-e', 'tmp', 'a')],
      addNodes,
      addNodesToVisualization: store().addNodesToVisualization,
      toCanvas: (result) => ({
        nodes: [node(result.added_node_ids[0])],
        edges: [edge(result.added_edge_ids[0], 'n1', 'a')],
      }),
    });
    expect(applied).toBe(true);
    expect(addNodes).toHaveBeenCalledWith([node('tmp')], [edge('tmp-e', 'tmp', 'a')]);
    expect(store().edges.map((e) => e.id)).toContain('k1');
  });

  it('does not draw after only the session epoch moved', async () => {
    const { inFlight } = run(bumpSessionOnly);
    expect(await inFlight).toBe(false);
    expect(nodeIds()).not.toContain('new1');
  });

  it('draws nothing when the result carries no ids', async () => {
    const add = vi.fn();
    const applied = await persistNewNodes({
      nodes: [node('tmp')],
      addNodes: vi.fn().mockResolvedValue({}),
      addNodesToVisualization: add,
      toCanvas: () => null,
    });
    expect(applied).toBe(false);
    expect(add).not.toHaveBeenCalled();
  });
});

describe('expandNode (handleExpand)', () => {
  function run(
    interleave,
    related = { nodes: [node('a'), node('c')], edges: [edge('e2', 'a', 'c')] }
  ) {
    const call = deferred(related);
    const showNotification = vi.fn();
    const getRelatedNodes = vi.fn(() => call.promise);
    const inFlight = expandNode({
      nodeId: 'a',
      getRelatedNodes,
      addNodesToVisualization: store().addNodesToVisualization,
      showNotification,
    });
    interleave?.();
    call.release();
    return { inFlight, showNotification, getRelatedNodes };
  }

  it('adds the neighbours when nothing changed', async () => {
    const { inFlight, showNotification, getRelatedNodes } = run();
    expect(await inFlight).toBe(true);
    expect(getRelatedNodes).toHaveBeenCalledWith('a', { depth: 1 });
    expect(nodeIds()).toContain('c');
    expect(store().edges.map((e) => e.id)).toContain('e2');
    expect(showNotification).toHaveBeenCalledWith('success', 'Added 1 new node');
  });

  it('reports when every neighbour is already in view', async () => {
    const { inFlight, showNotification } = run(null, { nodes: [node('b')], edges: [] });
    expect(await inFlight).toBe(true);
    expect(showNotification).toHaveBeenCalledWith('info', 'All related nodes already in view');
  });

  it('reports when there are no neighbours', async () => {
    const { inFlight, showNotification } = run(null, { nodes: [] });
    expect(await inFlight).toBe(true);
    expect(showNotification).toHaveBeenCalledWith('info', 'No related nodes found');
  });

  it('reports a failed fetch', async () => {
    const showNotification = vi.fn();
    await expandNode({
      nodeId: 'a',
      getRelatedNodes: vi.fn().mockRejectedValue(new Error('down')),
      addNodesToVisualization: vi.fn(),
      showNotification,
    });
    expect(showNotification).toHaveBeenCalledWith('error', 'Could not expand node');
  });

  it('drops the result when only the session epoch moved', async () => {
    const { inFlight } = run(bumpSessionOnly);
    expect(await inFlight).toBe(false);
    expect(nodeIds()).not.toContain('c');
  });

  it('drops the result when the anchor is removed in place mid-await', async () => {
    const { inFlight } = run(() => store().removeNode('a'));
    expect(await inFlight).toBe(false);
    expect(nodeIds()).toEqual(['b']);
  });

  it('still expands after a resync reload that kept the anchor', async () => {
    const { inFlight } = run(resync);
    expect(await inFlight).toBe(true);
    expect(nodeIds()).toContain('c');
  });

  it('drops the result when the session switches mid-await', async () => {
    const { inFlight, showNotification } = run(switchSession);
    expect(await inFlight).toBe(false);
    expect(store().nodes).toEqual([]);
    expect(showNotification).not.toHaveBeenCalled();
  });

  it('drops the result when the canvas is cleared mid-await', async () => {
    const { inFlight, showNotification } = run(clearCanvas);
    expect(await inFlight).toBe(false);
    expect(store().nodes).toEqual([]);
    expect(showNotification).not.toHaveBeenCalled();
  });
});

describe('connectNodes (handleConnect)', () => {
  function run(interleave, result = { success: true, edge: edge('e9') }) {
    const call = deferred(result);
    const syncRef = { current: { sendEdgesAdded: vi.fn() } };
    const showNotification = vi.fn();
    const addEdge = vi.fn(() => call.promise);
    const inFlight = connectNodes({
      source: 'a',
      target: 'b',
      addEdge,
      addNodesToVisualization: store().addNodesToVisualization,
      syncRef,
      showNotification,
    });
    interleave?.();
    call.release();
    return { inFlight, syncRef, showNotification, addEdge };
  }

  it('draws and fans out the edge when nothing changed', async () => {
    const { inFlight, syncRef, addEdge } = run();
    expect(await inFlight).toBe(true);
    expect(addEdge).toHaveBeenCalledWith('a', 'b');
    expect(store().edges.map((e) => e.id)).toContain('e9');
    expect(syncRef.current.sendEdgesAdded).toHaveBeenCalledWith([edge('e9')]);
  });

  it('neither draws nor fans out when the session switches mid-await, but reports the edge', async () => {
    const { inFlight, syncRef, showNotification } = run(() => {
      switchSession();
      store().addNodesToVisualization([node('a'), node('b')], []);
    });
    expect(await inFlight).toBe(false);
    expect(store().edges).toEqual([]);
    expect(syncRef.current.sendEdgesAdded).not.toHaveBeenCalled();
    expect(showNotification).toHaveBeenCalledWith('success', 'Connection created');
  });

  it('neither draws nor fans out when only the session epoch moved', async () => {
    const { inFlight, syncRef } = run(bumpSessionOnly);
    expect(await inFlight).toBe(false);
    expect(store().edges.map((e) => e.id)).toEqual(['e1']);
    expect(syncRef.current.sendEdgesAdded).not.toHaveBeenCalled();
  });

  it('neither draws nor fans out when the canvas is cleared mid-await, but reports the edge', async () => {
    const { inFlight, syncRef, showNotification } = run(clearCanvas);
    expect(await inFlight).toBe(false);
    expect(store().edges).toEqual([]);
    expect(syncRef.current.sendEdgesAdded).not.toHaveBeenCalled();
    expect(showNotification).toHaveBeenCalledWith('success', 'Connection created');
  });

  it.each(['a', 'b'])(
    'neither draws nor fans out when endpoint %s is removed in place mid-await',
    async (id) => {
      const { inFlight, syncRef, showNotification } = run(() => store().removeNode(id));
      expect(await inFlight).toBe(false);
      expect(store().edges.map((e) => e.id)).not.toContain('e9');
      expect(syncRef.current.sendEdgesAdded).not.toHaveBeenCalled();
      expect(showNotification).toHaveBeenCalledWith('success', 'Connection created');
    }
  );

  it('still draws and fans out after a resync reload that kept both endpoints', async () => {
    const { inFlight, syncRef } = run(resync);
    expect(await inFlight).toBe(true);
    expect(store().edges.map((e) => e.id)).toContain('e9');
    expect(syncRef.current.sendEdgesAdded).toHaveBeenCalledWith([edge('e9')]);
  });

  it('reports a failed request', async () => {
    const showNotification = vi.fn();
    await connectNodes({
      source: 'a',
      target: 'b',
      addEdge: vi.fn().mockRejectedValue(new Error('down')),
      addNodesToVisualization: vi.fn(),
      syncRef: { current: null },
      showNotification,
    });
    expect(showNotification).toHaveBeenCalledWith('error', 'Could not create connection');
  });

  it('still reports a failed create after a switch', async () => {
    const { inFlight, showNotification } = run(switchSession, { success: false });
    expect(await inFlight).toBe(false);
    expect(showNotification).toHaveBeenCalledWith('error', 'Could not create connection');
  });
});

describe('deleteEdgeEverywhere (handleDeleteEdge)', () => {
  function run(interleave) {
    const call = deferred({ success: true });
    const syncRef = { current: { sendEdgesRemoved: vi.fn() } };
    const showNotification = vi.fn();
    const removeEdge = vi.fn(store().removeEdge);
    const inFlight = deleteEdgeEverywhere({
      edgeId: 'e1',
      deleteEdge: vi.fn(() => call.promise),
      removeEdge,
      syncRef,
      showNotification,
    });
    interleave?.();
    call.release();
    return { inFlight, syncRef, showNotification, removeEdge };
  }

  it('removes and fans out the delete when nothing changed', async () => {
    const { inFlight, syncRef, showNotification } = run();
    expect(await inFlight).toBe(true);
    expect(store().edges).toEqual([]);
    expect(syncRef.current.sendEdgesRemoved).toHaveBeenCalledWith(['e1']);
    expect(showNotification).toHaveBeenCalledWith('success', 'Edge deleted');
  });

  it('touches neither the new canvas nor its collaborators after a switch, but reports the delete', async () => {
    const { inFlight, syncRef, showNotification, removeEdge } = run(() => {
      switchSession();
      store().addNodesToVisualization([node('a'), node('b')], [edge('e1')]);
    });
    expect(await inFlight).toBe(false);
    expect(removeEdge).not.toHaveBeenCalled();
    expect(store().edges.map((e) => e.id)).toEqual(['e1']);
    expect(syncRef.current.sendEdgesRemoved).not.toHaveBeenCalled();
    expect(showNotification).toHaveBeenCalledWith('success', 'Edge deleted');
  });

  it('touches nothing when only the session epoch moved', async () => {
    const { inFlight, syncRef, removeEdge } = run(bumpSessionOnly);
    expect(await inFlight).toBe(false);
    expect(removeEdge).not.toHaveBeenCalled();
    expect(syncRef.current.sendEdgesRemoved).not.toHaveBeenCalled();
  });

  it.each([
    ['a non-success reply', () => Promise.resolve({ success: false })],
    ['a failed request', () => Promise.reject(new Error('down'))],
  ])('treats %s as a failure', async (_label, reply) => {
    const syncRef = { current: { sendEdgesRemoved: vi.fn() } };
    const showNotification = vi.fn();
    const removeEdge = vi.fn();
    const applied = await deleteEdgeEverywhere({
      edgeId: 'e1',
      deleteEdge: vi.fn(reply),
      removeEdge,
      syncRef,
      showNotification,
    });
    expect(applied).toBe(false);
    expect(removeEdge).not.toHaveBeenCalled();
    expect(syncRef.current.sendEdgesRemoved).not.toHaveBeenCalled();
    expect(showNotification).toHaveBeenCalledWith('error', 'Could not delete edge');
  });

  // In-place and same session: a clear is not a reason to drop it.
  it('still fans out the delete after a clear in the same session', async () => {
    const { inFlight, syncRef } = run(clearCanvas);
    expect(await inFlight).toBe(true);
    expect(syncRef.current.sendEdgesRemoved).toHaveBeenCalledWith(['e1']);
  });
});

describe('setEdgeType (handleSetEdgeType)', () => {
  function run(interleave, type = 'OWNS') {
    const call = deferred({});
    const syncRef = { current: { sendEdgesUpdated: vi.fn() } };
    const showNotification = vi.fn();
    const updateEdge = vi.fn(() => call.promise);
    const updateEdgeData = vi.fn(store().updateEdgeData);
    const inFlight = setEdgeType({
      edgeId: 'e1',
      type,
      updateEdge,
      updateEdgeData,
      syncRef,
      showNotification,
    });
    interleave?.();
    call.release();
    return { inFlight, syncRef, showNotification, updateEdge, updateEdgeData };
  }

  it('retypes and fans out when nothing changed', async () => {
    const { inFlight, syncRef, updateEdge } = run();
    expect(await inFlight).toBe(true);
    expect(updateEdge).toHaveBeenCalledWith('e1', { type: 'OWNS' });
    expect(store().edges[0].type).toBe('OWNS');
    expect(syncRef.current.sendEdgesUpdated).toHaveBeenCalledWith([{ id: 'e1', type: 'OWNS' }]);
  });

  it('resets an empty type to RELATES_TO', async () => {
    store().updateEdgeData('e1', { type: 'OWNS' });
    const { inFlight, updateEdge, syncRef } = run(null, '');
    expect(await inFlight).toBe(true);
    expect(updateEdge).toHaveBeenCalledWith('e1', { type: null });
    expect(store().edges[0].type).toBe('RELATES_TO');
    expect(syncRef.current.sendEdgesUpdated).toHaveBeenCalledWith([
      { id: 'e1', type: 'RELATES_TO' },
    ]);
  });

  it('touches neither the new canvas nor its collaborators after a switch, but reports the edit', async () => {
    const { inFlight, syncRef, showNotification, updateEdgeData } = run(() => {
      switchSession();
      store().addNodesToVisualization([node('a'), node('b')], [edge('e1')]);
    });
    expect(await inFlight).toBe(false);
    expect(updateEdgeData).not.toHaveBeenCalled();
    expect(store().edges[0].type).toBe('RELATES_TO');
    expect(syncRef.current.sendEdgesUpdated).not.toHaveBeenCalled();
    expect(showNotification).toHaveBeenCalledWith('success', 'Connection type updated');
  });

  it('touches nothing when only the session epoch moved', async () => {
    const { inFlight, syncRef, updateEdgeData } = run(bumpSessionOnly);
    expect(await inFlight).toBe(false);
    expect(updateEdgeData).not.toHaveBeenCalled();
    expect(syncRef.current.sendEdgesUpdated).not.toHaveBeenCalled();
  });

  it('reports a failed request without editing the canvas', async () => {
    const updateEdgeData = vi.fn();
    const showNotification = vi.fn();
    const applied = await setEdgeType({
      edgeId: 'e1',
      type: 'OWNS',
      updateEdge: vi.fn().mockRejectedValue(new Error('down')),
      updateEdgeData,
      syncRef: { current: { sendEdgesUpdated: vi.fn() } },
      showNotification,
    });
    expect(applied).toBe(false);
    expect(updateEdgeData).not.toHaveBeenCalled();
    expect(showNotification).toHaveBeenCalledWith('error', 'Could not update connection');
  });

  it('still fans out the edit after a clear in the same session', async () => {
    const { inFlight, syncRef } = run(clearCanvas);
    expect(await inFlight).toBe(true);
    expect(syncRef.current.sendEdgesUpdated).toHaveBeenCalled();
  });
});

describe('loadSavedViewNode (saved-view double-click load)', () => {
  const view = {
    type: 'SavedView',
    name: 'My view',
    metadata: {
      node_ids: ['v1', 'v2'],
      positions: { v1: { x: 1, y: 2 } },
      groups: [{ id: 'g1', label: 'Team', position: { x: 0, y: 0 } }],
      parentIds: { v1: 'g1' },
      annotations: [{ id: 'note-1', kind: 'note', position: { x: 1, y: 2 }, text: 'hi' }],
    },
  };

  beforeEach(() => {
    useGraphStore.setState({ pendingGroups: null, pendingAnnotations: null });
  });

  function run(interleave, nodeData = view) {
    const calls = {};
    const getNodeDetails = vi.fn((id) => {
      calls[id] = deferred({ success: true, node: node(id), edges: [edge('ve', 'v1', 'v2')] });
      return calls[id].promise;
    });
    const showNotification = vi.fn();
    const s = store();
    const inFlight = loadSavedViewNode({
      nodeData,
      getNodeDetails,
      clearVisualization: s.clearVisualization,
      addNodesToVisualization: s.addNodesToVisualization,
      setPendingGroups: s.setPendingGroups,
      setPendingAnnotations: s.setPendingAnnotations,
      showNotification,
    });
    interleave?.();
    Object.values(calls).forEach((c) => c.release());
    return { inFlight, showNotification };
  }

  it('replaces the canvas with the saved view when nothing changed', async () => {
    const { inFlight, showNotification } = run();
    expect(await inFlight).toBe(true);
    expect(nodeIds()).toEqual(['v1', 'v2']);
    expect(store().nodes[0]._savedPosition).toEqual({ x: 1, y: 2 });
    expect(store().edges.map((e) => e.id)).toEqual(['ve']);
    expect(store().pendingGroups.groups.map((g) => g.id)).toEqual(['g1']);
    expect(store().pendingAnnotations.map((a) => a.id)).toEqual(['note-1']);
    expect(showNotification).toHaveBeenCalledWith('info', 'Loaded saved view: My view');
  });

  it('prefers the edges saved with the view, filtered by edge_ids otherwise', async () => {
    const saved = { ...view, metadata: { ...view.metadata, edges: [edge('kept', 'v1', 'v2')] } };
    const first = run(null, saved);
    expect(await first.inFlight).toBe(true);
    expect(store().edges.map((e) => e.id)).toEqual(['kept']);

    const filtered = { ...view, metadata: { ...view.metadata, edge_ids: ['other'] } };
    const second = run(null, filtered);
    expect(await second.inFlight).toBe(true);
    expect(store().edges).toEqual([]);
  });

  it('reports a failed load', async () => {
    const showNotification = vi.fn();
    const applied = await loadSavedViewNode({
      nodeData: { ...view, metadata: { ...view.metadata, annotations: 'not-an-array' } },
      getNodeDetails: vi.fn(),
      clearVisualization: vi.fn(),
      addNodesToVisualization: vi.fn(),
      setPendingGroups: vi.fn(),
      setPendingAnnotations: vi.fn(),
      showNotification,
    });
    expect(applied).toBe(false);
    expect(showNotification).toHaveBeenCalledWith('error', 'Could not load saved view');
  });

  it('does not load into a session switched to mid-await', async () => {
    const { inFlight, showNotification } = run(() => {
      switchSession();
      store().addNodesToVisualization([node('x')], []);
    });
    expect(await inFlight).toBe(false);
    expect(nodeIds()).toEqual(['x']);
    expect(store().pendingGroups).toBeNull();
    expect(store().pendingAnnotations).toBeNull();
    expect(showNotification).not.toHaveBeenCalled();
  });

  it('does not load after only the session epoch moved', async () => {
    const { inFlight } = run(bumpSessionOnly);
    expect(await inFlight).toBe(false);
    expect(store().nodes).toEqual([]);
    expect(store().pendingGroups).toBeNull();
  });

  // The load's own clear happens before the await, so it must not count as a
  // replace — but a later one (another view, the clear action) supersedes it.
  it('is superseded by a later wholesale replace in the same session', async () => {
    const { inFlight } = run(() => {
      clearCanvas();
      store().addNodesToVisualization([node('y')], []);
    });
    expect(await inFlight).toBe(false);
    expect(nodeIds()).toEqual(['y']);
    expect(store().pendingGroups).toBeNull();
    expect(store().pendingAnnotations).toBeNull();
  });
});
