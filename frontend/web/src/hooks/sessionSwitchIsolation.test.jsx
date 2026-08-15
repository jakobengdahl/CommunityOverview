import { describe, it, expect, beforeEach, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';

import * as api from '../services/api';
import useGraphStore from '../store/graphStore';
import { useSharedSession } from './useSharedSession';

vi.mock('../services/api', () => ({
  getSession: vi.fn(),
}));

const t = (key) => key;

// A session-switch load path (switchToSession, and the ?session= deep link on
// mount) both flow through loadSessionFromServer. Drive it with the *real* store
// so the whole reconcile — canvas swap plus session-scoped UI reset — is
// exercised end to end.
function realDeps() {
  const s = useGraphStore.getState();
  return {
    clearVisualization: s.clearVisualization,
    addNodesToVisualization: s.addNodesToVisualization,
    setHiddenNodeIds: s.setHiddenNodeIds,
    setHiddenEdgeIds: s.setHiddenEdgeIds,
    setPendingGroups: s.setPendingGroups,
    setPendingAnnotations: s.setPendingAnnotations,
    ensureSyncConnected: vi.fn(() => ({ setBaseline: vi.fn(), sessionId: null })),
    syncRef: { current: null },
    resetSessionScopedState: () => useGraphStore.getState().resetSessionScopedState(t, 'en'),
  };
}

const node = (id) => ({ id, type: 'Actor', name: id });
const sessionPayload = (nodes) => ({ state: { annotations: [] }, resolved: { nodes, edges: [] } });

// Simulate the user working inside the currently loaded session: some assistant
// chat, an active expert, an open node-detail dialog, a selected node, and the
// two dialogs that act on graph content — an edge open for editing and a
// pending node-delete confirmation, both addressing this session's canvas.
function workInsideSession(selectedId) {
  const s = useGraphStore.getState();
  s.addChatMessage({ role: 'user', content: `question about ${selectedId}` });
  s.addChatMessage({ role: 'assistant', content: `answer about ${selectedId}` });
  useGraphStore.setState({
    activeExperts: ['expert-1'],
    detailNode: node(selectedId),
  });
  s.setEditingEdge({ id: `edge-in-${selectedId}`, source: selectedId, target: selectedId });
  s.setDeleteDialog({ nodeId: selectedId, nodeName: selectedId, isMultiple: false });
  s.setSelectedNodeId(selectedId);
  s.setSelectedGraphNodes([node(selectedId)]);
}

function expectCleanSession() {
  const s = useGraphStore.getState();
  expect(s.chatMessages).toHaveLength(1);
  expect(s.chatMessages[0].id).toBe('welcome');
  expect(s.activeExperts).toEqual([]);
  expect(s.detailNode).toBeNull();
  expect(s.selectedNodeId).toBeNull();
  expect(s.selectedGraphNodes).toEqual([]);
  // Left open, confirming the edge dialog would PUT the previous session's edge
  // and fan the change out through the sync client the switch has just pointed
  // at the new session; the delete confirmation would drop a node the user is
  // no longer looking at. App's save handlers are guarded on these being set,
  // so closing them here is what stops both.
  expect(s.editingEdge).toBeNull();
  expect(s.deleteDialog).toBeNull();
}

const nodeIds = () =>
  useGraphStore
    .getState()
    .nodes.map((n) => n.id)
    .sort();

describe('session-switch state isolation (real store)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useGraphStore.setState({
      presentation: {},
      nodes: [],
      edges: [],
      chatMessages: [{ id: 'welcome', role: 'assistant', content: 'welcome' }],
      activeExperts: [],
      detailNode: null,
      editingNode: null,
      editingEdge: null,
      deleteDialog: null,
      contextMenu: null,
      selectedNodeId: null,
      selectedGraphNodes: [],
      assistantSessionEpoch: 0,
    });
  });

  it('isolates history and selection across A→B→A with a node shared by both', async () => {
    // A = {x, a1}, B = {x, b1}; x is shared, a1/b1 are disjoint.
    api.getSession
      .mockResolvedValueOnce(sessionPayload([node('x'), node('a1')])) // → A
      .mockResolvedValueOnce(sessionPayload([node('x'), node('b1')])) // → B
      .mockResolvedValueOnce(sessionPayload([node('x'), node('a1')])); // → A again
    const { result } = renderHook(() => useSharedSession(realDeps()));

    await act(async () => {
      await result.current.loadSessionFromServer('sess-a');
    });
    const epochA = useGraphStore.getState().assistantSessionEpoch;
    workInsideSession('x'); // select the shared node and build history

    await act(async () => {
      await result.current.loadSessionFromServer('sess-b');
    });
    // B's canvas replaced A's (a1 gone); the shared node x is present but the
    // selection was still cleared (minimum reconcile), and no A history leaks.
    expect(nodeIds()).toEqual(['b1', 'x']);
    expectCleanSession();
    expect(useGraphStore.getState().assistantSessionEpoch).toBe(epochA + 1);

    workInsideSession('x');
    await act(async () => {
      await result.current.loadSessionFromServer('sess-a');
    });
    expect(nodeIds()).toEqual(['a1', 'x']);
    expectCleanSession();
    expect(useGraphStore.getState().assistantSessionEpoch).toBe(epochA + 2);
  });

  it('clears a selection whose node is absent from the target (disjoint sets)', async () => {
    api.getSession
      .mockResolvedValueOnce(sessionPayload([node('c1')])) // → C
      .mockResolvedValueOnce(sessionPayload([node('d1')])); // → D (disjoint)
    const { result } = renderHook(() => useSharedSession(realDeps()));

    await act(async () => {
      await result.current.loadSessionFromServer('sess-c');
    });
    workInsideSession('c1');

    await act(async () => {
      await result.current.loadSessionFromServer('sess-d');
    });
    expect(nodeIds()).toEqual(['d1']);
    // c1 is not in D, so nothing stale remains selected or shown.
    expectCleanSession();
  });

  it('resets carried-over state when a deep link resolves to an empty session', async () => {
    // Deep link to a not-yet-materialised / missing session → 404 path.
    const err = new Error('not found');
    err.status = 404;
    api.getSession
      .mockResolvedValueOnce(sessionPayload([node('a1')])) // → A (has content)
      .mockRejectedValueOnce(err); // → deep link, empty
    const { result } = renderHook(() => useSharedSession(realDeps()));

    await act(async () => {
      await result.current.loadSessionFromServer('sess-a');
    });
    workInsideSession('a1');

    await act(async () => {
      await result.current.loadSessionFromServer('sess-empty', { eagerConnect: true });
    });
    expect(useGraphStore.getState().nodes).toEqual([]);
    expectCleanSession();
  });
});
