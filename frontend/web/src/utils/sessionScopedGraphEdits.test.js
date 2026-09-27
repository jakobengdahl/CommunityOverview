import { describe, it, expect, beforeEach, vi } from 'vitest';

import useGraphStore from '../store/graphStore';
import {
  agentCreateToCanvas,
  agentUpdateEntries,
  applyEdgeUpdate,
  confirmNodeDelete,
  createDialogNode,
  createdNodesToCanvas,
  openAgentEditor,
} from './sessionScopedGraphEdits';

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
  let fail;
  const promise = new Promise((resolve, reject) => {
    release = resolve;
    fail = reject;
  });
  return { promise, release: () => release(), reject: (error) => fail(error) };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(console, 'error').mockImplementation(() => {});
  useGraphStore.setState({
    nodes: [node('a'), node('b')],
    edges: [edge('e1')],
    sessionEpoch: 0,
    canvasBaselineEpoch: 0,
    editingEdge: null,
    deleteDialog: null,
    navHistory: [],
  });
});

describe('applyEdgeUpdate', () => {
  function harness() {
    const s = useGraphStore.getState();
    const editingEdge = edge('e1');
    useGraphStore.setState({ editingEdge });
    return {
      editingEdge,
      updates: { type: 'OWNS' },
      updateEdgeData: vi.fn(s.updateEdgeData),
      syncRef: { current: { sendEdgesUpdated: vi.fn() } },
      setEditingEdge: vi.fn(s.setEditingEdge),
      showNotification: vi.fn(),
    };
  }

  it('applies the edit and fans it out when the session is unchanged', async () => {
    const h = harness();
    const applied = await applyEdgeUpdate({ ...h, updateEdge: vi.fn().mockResolvedValue({}) });

    expect(applied).toBe(true);
    expect(h.updateEdgeData).toHaveBeenCalledWith('e1', { type: 'OWNS' });
    expect(useGraphStore.getState().edges).toEqual([{ ...edge('e1'), type: 'OWNS' }]);
    expect(h.syncRef.current.sendEdgesUpdated).toHaveBeenCalledWith([{ id: 'e1', type: 'OWNS' }]);
    expect(h.setEditingEdge).toHaveBeenCalledWith(null);
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
    expect(h.updateEdgeData).not.toHaveBeenCalled();
    expect(h.syncRef.current.sendEdgesUpdated).not.toHaveBeenCalled();
    // Nor may it close a dialog: this session's copy is already closed, so the
    // only thing left to hit is one the user opened after switching.
    expect(h.setEditingEdge).not.toHaveBeenCalled();
    // The PUT really did land in the graph, so it is still reported — the same
    // rule confirmNodeDelete follows for its delete.
    expect(h.showNotification).toHaveBeenCalledWith('success', 'Edge updated');
  });

  it('does not resurrect a cleared canvas when the edge update resolves', async () => {
    const h = harness();
    const call = deferred();
    const updateEdge = vi.fn(() => call.promise);

    const inFlight = applyEdgeUpdate({ ...h, updateEdge });
    expect(updateEdge).toHaveBeenCalled();

    useGraphStore.getState().clearVisualization();
    call.release();
    const applied = await inFlight;

    expect(applied).toBe(false);
    expect(h.updateEdgeData).not.toHaveBeenCalled();
    expect(h.syncRef.current.sendEdgesUpdated).not.toHaveBeenCalled();
    expect(h.setEditingEdge).not.toHaveBeenCalled();
    expect(useGraphStore.getState().nodes).toEqual([]);
    expect(useGraphStore.getState().edges).toEqual([]);
    expect(h.showNotification).toHaveBeenCalledWith('success', 'Edge updated');
  });

  // The session-scoped work runs inside the same try as the PUT, so announcing
  // success before it would let a throw there contradict itself: "Edge updated"
  // followed by "Could not update edge" for an edit that actually landed.
  it('does not claim success before the session-scoped work has run', async () => {
    const h = harness();
    h.updateEdgeData = vi.fn(() => {
      throw new Error('canvas blew up');
    });

    const applied = await applyEdgeUpdate({ ...h, updateEdge: vi.fn().mockResolvedValue({}) });

    expect(applied).toBe(false);
    expect(h.showNotification).toHaveBeenCalledTimes(1);
    expect(h.showNotification).toHaveBeenCalledWith('error', 'Could not update edge');
  });

  // The regression: the edit used to write back the nodes and edges it was
  // handed before the await, reverting anything that changed in between.
  it('keeps changes made to the canvas during the await', async () => {
    const h = harness();
    const call = deferred();
    const updateEdge = vi.fn(() => call.promise);

    const inFlight = applyEdgeUpdate({ ...h, updateEdge });
    useGraphStore.getState().addNodesToVisualization([node('c')], [edge('e2')]);
    call.release();
    const applied = await inFlight;

    expect(applied).toBe(true);
    const { nodes, edges } = useGraphStore.getState();
    expect(nodes.map((n) => n.id)).toContain('c');
    expect(edges.find((e) => e.id === 'e2')).toBeTruthy();
    expect(edges.find((e) => e.id === 'e1').type).toBe('OWNS');
  });

  // A reconnect resync reloads the same session wholesale: it bumps
  // canvasBaselineEpoch but puts the edge straight back, so the edit stands.
  // The resync's clear already closed the dialog, so nothing is closed here.
  it('still applies and fans out the edit after a resync reload that kept the edge', async () => {
    const h = harness();
    const call = deferred();
    const updateEdge = vi.fn(() => call.promise);

    const inFlight = applyEdgeUpdate({ ...h, updateEdge });
    const { nodes, edges } = useGraphStore.getState();
    useGraphStore.getState().clearVisualization();
    useGraphStore.getState().addNodesToVisualization(nodes, edges);
    const reopened = { ...edge('e1'), label: 'opened after the resync' };
    useGraphStore.setState({ editingEdge: reopened });
    call.release();
    const applied = await inFlight;

    expect(applied).toBe(true);
    expect(useGraphStore.getState().edges.find((e) => e.id === 'e1').type).toBe('OWNS');
    expect(h.syncRef.current.sendEdgesUpdated).toHaveBeenCalledWith([{ id: 'e1', type: 'OWNS' }]);
    expect(h.setEditingEdge).not.toHaveBeenCalled();
    expect(useGraphStore.getState().editingEdge).toBe(reopened);
    expect(h.showNotification).toHaveBeenCalledWith('success', 'Edge updated');
  });

  it('neither patches nor fans out an edge removed in place mid-await', async () => {
    const h = harness();
    const call = deferred();
    const updateEdge = vi.fn(() => call.promise);

    const inFlight = applyEdgeUpdate({ ...h, updateEdge });
    useGraphStore.getState().removeEdge('e1');
    call.release();
    const applied = await inFlight;

    expect(applied).toBe(false);
    expect(useGraphStore.getState().edges).toEqual([]);
    expect(h.syncRef.current.sendEdgesUpdated).not.toHaveBeenCalled();
    // removeEdge leaves the dialog open and the dialog does not close itself,
    // so the save must still close it.
    expect(h.setEditingEdge).toHaveBeenCalledWith(null);
    expect(useGraphStore.getState().editingEdge).toBeNull();
    expect(h.showNotification).toHaveBeenCalledWith('success', 'Edge updated');
  });

  it('drops the edit when only the session epoch moved', async () => {
    const h = harness();
    const call = deferred();
    const updateEdge = vi.fn(() => call.promise);

    const inFlight = applyEdgeUpdate({ ...h, updateEdge });
    switchSession();
    // The switch reset leaves the canvas as it was; only the epoch moved.
    useGraphStore.setState({ nodes: [node('a'), node('b')], edges: [edge('e1')] });
    call.release();
    const applied = await inFlight;

    expect(applied).toBe(false);
    expect(useGraphStore.getState().edges).toEqual([edge('e1')]);
    expect(h.syncRef.current.sendEdgesUpdated).not.toHaveBeenCalled();
    expect(h.setEditingEdge).not.toHaveBeenCalled();
  });

  it('reports a failed PUT without touching the canvas', async () => {
    const h = harness();
    const applied = await applyEdgeUpdate({
      ...h,
      updateEdge: vi.fn().mockRejectedValue(new Error('boom')),
    });

    expect(applied).toBe(false);
    expect(h.updateEdgeData).not.toHaveBeenCalled();
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
    expect(h.setDeleteDialog).toHaveBeenCalledWith(null);
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
    // Closing the confirmation is session-scoped too: this session's copy is
    // already closed, so a late close could only dismiss a fresh confirmation
    // the user opened after switching.
    expect(h.setDeleteDialog).not.toHaveBeenCalled();
    // The delete really did happen in the graph, so it is still reported.
    expect(h.showNotification).toHaveBeenCalledWith('success', 'Node deleted');
  });

  // The close sits in a finally, so it has to respect the guard on the failure
  // path too — otherwise a failed delete still dismisses whatever confirmation
  // the user has open in the session they moved to.
  it('leaves the confirmation alone when the delete fails after a switch', async () => {
    const h = harness(single);
    const call = deferred();
    const deleteNodes = vi.fn(() => call.promise);

    const inFlight = confirmNodeDelete({ ...h, deleteNodes });
    switchSession();
    call.reject(new Error('boom'));
    const applied = await inFlight;

    expect(applied).toBe(false);
    expect(h.setDeleteDialog).not.toHaveBeenCalled();
    expect(h.removeNode).not.toHaveBeenCalled();
    expect(h.showNotification).toHaveBeenCalledWith('error', 'Could not delete node(s)');
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

describe('openAgentEditor (handleEdit, Agent branch)', () => {
  const agent = { id: 'ag', type: 'Agent', name: 'ag', metadata: { subscription_id: 'sub' } };
  const subscription = { id: 'sub', type: 'EventSubscription', name: 'sub' };

  it('opens the editor with the fetched subscription when nothing changed', async () => {
    const getNodeDetails = vi.fn().mockResolvedValue({ success: true, node: subscription });
    const openEditor = vi.fn();
    const opened = await openAgentEditor({
      agent,
      getNodeDetails,
      openEditor,
      showNotification: vi.fn(),
    });

    expect(opened).toBe(true);
    expect(getNodeDetails).toHaveBeenCalledWith('sub');
    expect(openEditor).toHaveBeenCalledWith({ agent, subscription });
  });

  it('opens the editor without a subscription when the agent has none', async () => {
    const getNodeDetails = vi.fn();
    const openEditor = vi.fn();
    const bare = { ...agent, metadata: {} };
    await openAgentEditor({ agent: bare, getNodeDetails, openEditor, showNotification: vi.fn() });

    expect(getNodeDetails).not.toHaveBeenCalled();
    expect(openEditor).toHaveBeenCalledWith({ agent: bare, subscription: null });
  });

  it('opens the editor without a subscription the fetch could not find', async () => {
    const openEditor = vi.fn();
    await openAgentEditor({
      agent,
      getNodeDetails: vi.fn().mockResolvedValue({ success: false }),
      openEditor,
      showNotification: vi.fn(),
    });

    expect(openEditor).toHaveBeenCalledWith({ agent, subscription: null });
  });

  // The regression: the editor opened over the session the user had moved to,
  // for a node picked in the previous one.
  it('does not open the editor after a session switch mid-await', async () => {
    const call = deferred();
    const getNodeDetails = vi.fn(() =>
      call.promise.then(() => ({ success: true, node: subscription }))
    );
    const openEditor = vi.fn();
    const showNotification = vi.fn();

    const inFlight = openAgentEditor({ agent, getNodeDetails, openEditor, showNotification });
    switchSession();
    call.release();

    expect(await inFlight).toBe(false);
    expect(openEditor).not.toHaveBeenCalled();
    expect(showNotification).not.toHaveBeenCalled();
  });

  it('reports a failed fetch without opening the editor', async () => {
    const openEditor = vi.fn();
    const showNotification = vi.fn();
    const opened = await openAgentEditor({
      agent,
      getNodeDetails: vi.fn().mockRejectedValue(new Error('boom')),
      openEditor,
      showNotification,
    });

    expect(opened).toBe(false);
    expect(openEditor).not.toHaveBeenCalled();
    expect(showNotification).toHaveBeenCalledWith('error', 'Could not load agent details');
  });

  it('does not report a failed fetch into a session switched to mid-await', async () => {
    const call = deferred();
    const showNotification = vi.fn();
    const inFlight = openAgentEditor({
      agent,
      getNodeDetails: vi.fn(() => call.promise),
      openEditor: vi.fn(),
      showNotification,
    });
    switchSession();
    call.reject(new Error('boom'));

    expect(await inFlight).toBe(false);
    expect(showNotification).not.toHaveBeenCalled();
  });
});

describe('createDialogNode (CreateNodeDialog via handleNodeCreated)', () => {
  const draft = { type: 'Actor', name: 'New actor' };

  function harness(result = { added_node_ids: ['n1'] }) {
    return {
      node: draft,
      addNodes: vi.fn().mockResolvedValue(result),
      addNodesToVisualization: vi.fn(useGraphStore.getState().addNodesToVisualization),
      showNotification: vi.fn(),
      onDrawn: vi.fn(),
    };
  }

  it('creates, draws and reports the node when nothing changed', async () => {
    const h = harness();
    const drawn = await createDialogNode(h);

    expect(drawn).toBe(true);
    expect(h.addNodes).toHaveBeenCalledWith([draft], []);
    expect(h.addNodesToVisualization).toHaveBeenCalledWith([{ ...draft, id: 'n1' }], []);
    expect(useGraphStore.getState().nodes.map((n) => n.id)).toContain('n1');
    expect(h.onDrawn).toHaveBeenCalledWith({ ...draft, id: 'n1' });
    expect(h.showNotification).toHaveBeenCalledWith('success', 'Actor "New actor" created');
  });

  // The regression: the dialog drew the node into whichever session was active
  // when the reply landed.
  it('reports but does not draw or focus the node after a session switch mid-await', async () => {
    const h = harness();
    const call = deferred();
    h.addNodes = vi.fn(() => call.promise.then(() => ({ added_node_ids: ['n1'] })));

    const inFlight = createDialogNode(h);
    switchSession();
    call.release();

    expect(await inFlight).toBe(false);
    expect(h.addNodesToVisualization).not.toHaveBeenCalled();
    expect(h.onDrawn).not.toHaveBeenCalled();
    expect(useGraphStore.getState().nodes.map((n) => n.id)).not.toContain('n1');
    expect(h.showNotification).toHaveBeenCalledWith('success', 'Actor "New actor" created');
  });

  it('still draws the node after a clear in the same session', async () => {
    const h = harness();
    const call = deferred();
    h.addNodes = vi.fn(() => call.promise.then(() => ({ added_node_ids: ['n1'] })));

    const inFlight = createDialogNode(h);
    useGraphStore.getState().clearVisualization();
    call.release();

    expect(await inFlight).toBe(true);
    expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['n1']);
  });

  it('neither draws nor reports when the result carries no ids', async () => {
    const h = harness({});
    expect(await createDialogNode(h)).toBe(false);
    expect(h.addNodesToVisualization).not.toHaveBeenCalled();
    expect(h.showNotification).not.toHaveBeenCalled();
  });

  it('propagates an API failure so the dialog can show it', async () => {
    const h = harness();
    h.addNodes = vi.fn().mockRejectedValue(new Error('boom'));
    await expect(createDialogNode(h)).rejects.toThrow('boom');
    expect(h.addNodesToVisualization).not.toHaveBeenCalled();
  });
});

describe('createdNodesToCanvas (single-node create branches)', () => {
  it('gives each sent node the id the server assigned', () => {
    expect(
      createdNodesToCanvas([{ type: 'Skill', name: 's' }], { added_node_ids: ['s1'] })
    ).toEqual({ nodes: [{ type: 'Skill', name: 's', id: 's1' }] });
  });

  it('returns null when nothing was created', () => {
    expect(createdNodesToCanvas([{ name: 's' }], { added_node_ids: [] })).toBeNull();
    expect(createdNodesToCanvas([{ name: 's' }], {})).toBeNull();
  });
});

describe('agentCreateToCanvas (handleSaveAgent create branch)', () => {
  // Sent in the order the dialog builds them, with placeholder ids the edge
  // refers to; the server replaces every one of them.
  const agentNodes = [
    { id: 'tmp-sub', type: 'EventSubscription', name: 'sub' },
    { id: 'tmp-agent', type: 'Agent', name: 'agent' },
  ];
  const agentEdges = [{ id: 'tmp-edge', source: 'tmp-agent', target: 'tmp-sub', type: 'USES' }];

  it('remaps node ids, edge id and both edge endpoints by node type', () => {
    const drawn = agentCreateToCanvas(agentNodes, agentEdges, {
      added_node_ids: ['sub-1', 'agent-1'],
      added_edge_ids: ['edge-1'],
    });

    expect(drawn.nodes.map((n) => [n.type, n.id])).toEqual([
      ['EventSubscription', 'sub-1'],
      ['Agent', 'agent-1'],
    ]);
    expect(drawn.edges).toEqual([
      { id: 'edge-1', source: 'agent-1', target: 'sub-1', type: 'USES' },
    ]);
  });

  it('keeps the sent edge id when the server returns none', () => {
    const drawn = agentCreateToCanvas(agentNodes, agentEdges, {
      added_node_ids: ['sub-1', 'agent-1'],
    });
    expect(drawn.edges[0].id).toBe('tmp-edge');
  });

  it('returns null when nothing was created', () => {
    expect(agentCreateToCanvas(agentNodes, agentEdges, { added_node_ids: [] })).toBeNull();
  });
});

describe('agentUpdateEntries (handleSaveAgent update branch)', () => {
  it('persists the agent, then its subscription', () => {
    expect(
      agentUpdateEntries({
        agentId: 'ag',
        agentUpdates: { name: 'x' },
        subscriptionId: 'sub',
        subscriptionUpdates: { filters: {} },
      })
    ).toEqual([
      { id: 'ag', updates: { name: 'x' } },
      { id: 'sub', updates: { filters: {} } },
    ]);
  });

  it('persists only the agent when the subscription did not change', () => {
    expect(
      agentUpdateEntries({ agentId: 'ag', agentUpdates: { name: 'x' }, subscriptionId: 'sub' })
    ).toEqual([{ id: 'ag', updates: { name: 'x' } }]);
  });
});
