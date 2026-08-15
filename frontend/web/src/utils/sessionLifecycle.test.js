import { describe, it, expect, beforeEach, vi } from 'vitest';
import useGraphStore from '../store/graphStore';
import { receiveRemoteSessionDeleted } from './sessionLifecycle';

const t = (key) => key;

// A session the user has actually worked in: assistant conversation, an active
// expert, an open node-detail dialog, an edit dialog, a context menu and a
// selection — all scoped to the node set currently on the canvas.
function seedWorkedInSession() {
  useGraphStore.setState({
    presentation: {},
    nodes: [{ id: 'a1', type: 'Actor', name: 'Node A1' }],
    edges: [],
    chatMessages: [
      { id: 'welcome', role: 'assistant', content: 'welcome' },
      { id: 'm1', role: 'user', content: 'question in the deleted session' },
      { id: 'm2', role: 'assistant', content: 'answer in the deleted session' },
    ],
    activeExperts: ['expert-a'],
    detailNode: { id: 'a1', name: 'Node A1' },
    editingNode: { id: 'a1', name: 'Node A1' },
    contextMenu: { x: 1, y: 2, nodeId: 'a1' },
    selectedNodeId: 'a1',
    selectedGraphNodes: [{ id: 'a1', name: 'Node A1' }],
    assistantSessionEpoch: 0,
  });
}

// Drive the deletion broadcast against the *real* store, so the whole reaction
// is exercised end to end rather than asserted against mocks.
function receiveDelete(deletedBy, overrides = {}) {
  const deps = {
    deletedBy,
    clientId: 'this-browser',
    sessionId: 'sess-deleted',
    generateSessionId: () => 'sess-fresh',
    removeSession: vi.fn(),
    clearVisualization: useGraphStore.getState().clearVisualization,
    resetSessionScopedState: () => useGraphStore.getState().resetSessionScopedState(t, 'en'),
    setSessionId: vi.fn(),
    reflectSessionUrl: vi.fn(),
    ...overrides,
  };
  const dropped = receiveRemoteSessionDeleted(deps);
  return { dropped, ...deps };
}

describe('receiveRemoteSessionDeleted (real store)', () => {
  beforeEach(() => {
    seedWorkedInSession();
  });

  it('carries nothing from the deleted session into the fresh one', () => {
    const { dropped, removeSession, setSessionId, reflectSessionUrl } =
      receiveDelete('another-browser');

    const state = useGraphStore.getState();
    // The reported symptoms: assistant history and an active node that cannot
    // exist in the session the user now finds themselves in.
    expect(state.chatMessages).toHaveLength(1);
    expect(state.chatMessages[0].id).toBe('welcome');
    expect(state.selectedNodeId).toBeNull();
    expect(state.detailNode).toBeNull();
    expect(state.nodes).toEqual([]);
    // An assistant reply still in flight for the deleted session must not land.
    expect(state.assistantSessionEpoch).toBe(1);

    expect(dropped).toBe(true);
    expect(removeSession).toHaveBeenCalledWith('sess-deleted');
    expect(setSessionId).toHaveBeenCalledWith('sess-fresh');
    expect(reflectSessionUrl).toHaveBeenCalledWith('sess-fresh');
  });

  it('ignores the echo of a delete this browser issued itself', () => {
    // The local delete path already moved this client into a fresh session;
    // reacting again would strand it in a second one.
    const { dropped, removeSession, setSessionId } = receiveDelete('this-browser');

    expect(dropped).toBe(false);
    expect(removeSession).not.toHaveBeenCalled();
    expect(setSessionId).not.toHaveBeenCalled();
    expect(useGraphStore.getState().chatMessages).toHaveLength(3);
  });

  it('still drops when the broadcast names no deleter', () => {
    // The server omits deleted_by on the queue-overflow recovery notice
    // (rest_api.py), so an unattributed delete must not be mistaken for our own.
    const { dropped, setSessionId } = receiveDelete(null);

    expect(dropped).toBe(true);
    expect(setSessionId).toHaveBeenCalledWith('sess-fresh');
    expect(useGraphStore.getState().chatMessages).toHaveLength(1);
  });

  it('resets before the new session id is adopted', () => {
    let historyWhenIdAdopted;
    receiveDelete('another-browser', {
      setSessionId: vi.fn(() => {
        historyWhenIdAdopted = useGraphStore.getState().chatMessages;
      }),
    });

    expect(historyWhenIdAdopted).toHaveLength(1);
  });
});
