import { describe, it, expect, beforeEach, vi } from 'vitest';
import useGraphStore from '../store/graphStore';
import { dropIntoFreshSession } from './sessionLifecycle';

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

// Run the helper against the real store actions, so the whole drop is exercised
// end to end rather than asserted against mocks.
function drop(freshId, { setSessionId = vi.fn(), reflectSessionUrl = vi.fn() } = {}) {
  const s = useGraphStore.getState();
  dropIntoFreshSession({
    freshId,
    clearVisualization: s.clearVisualization,
    resetSessionScopedState: () => useGraphStore.getState().resetSessionScopedState(t, 'en'),
    setSessionId,
    reflectSessionUrl,
  });
  return { setSessionId, reflectSessionUrl };
}

describe('dropIntoFreshSession (real store)', () => {
  beforeEach(() => {
    seedWorkedInSession();
  });

  it('leaves no assistant history from the session that was left behind', () => {
    drop('sess-fresh');

    const { chatMessages } = useGraphStore.getState();
    expect(chatMessages).toHaveLength(1);
    expect(chatMessages[0].id).toBe('welcome');
    expect(chatMessages.some((m) => String(m.content).includes('deleted session'))).toBe(false);
  });

  it('clears the active node and every node-scoped overlay, and empties the canvas', () => {
    drop('sess-fresh');

    const state = useGraphStore.getState();
    expect(state.nodes).toEqual([]);
    expect(state.selectedNodeId).toBeNull();
    expect(state.selectedGraphNodes).toEqual([]);
    expect(state.detailNode).toBeNull();
    expect(state.editingNode).toBeNull();
    expect(state.contextMenu).toBeNull();
    expect(state.activeExperts).toEqual([]);
  });

  it('bumps the assistant epoch so a reply still in flight cannot land in the new session', () => {
    drop('sess-fresh');

    expect(useGraphStore.getState().assistantSessionEpoch).toBe(1);
  });

  it('resets before the new session id is adopted', () => {
    let historyWhenIdAdopted;
    const setSessionId = vi.fn(() => {
      historyWhenIdAdopted = useGraphStore.getState().chatMessages;
    });

    const { reflectSessionUrl } = drop('sess-fresh', { setSessionId });

    expect(historyWhenIdAdopted).toHaveLength(1);
    expect(setSessionId).toHaveBeenCalledWith('sess-fresh');
    expect(reflectSessionUrl).toHaveBeenCalledWith('sess-fresh');
  });
});
