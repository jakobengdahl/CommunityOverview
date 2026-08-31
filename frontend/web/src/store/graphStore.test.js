import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import useGraphStore from './graphStore';

const t = (key) => key;

// Seed the store fields that a session switch must reset, simulating a session
// that has accumulated assistant history, experts, an open node-detail dialog
// and a selection.
function seedDirtySessionState() {
  useGraphStore.setState({
    presentation: {},
    chatMessages: [
      { id: 'welcome', role: 'assistant', content: 'welcome' },
      { id: 'm1', role: 'user', content: 'hi from session A' },
      { id: 'm2', role: 'assistant', content: 'reply from session A' },
    ],
    activeExperts: ['expert-a'],
    detailNode: { id: 'a1', name: 'Node A1' },
    editingNode: { id: 'a2', name: 'Node A2' },
    editingEdge: { id: 'e1', source: 'a1', target: 'a2', type: 'RELATES_TO' },
    deleteDialog: { nodeId: 'a1', nodeName: 'Node A1', isMultiple: false },
    contextMenu: { x: 1, y: 2, nodeId: 'a1' },
    selectedNodeId: 'a1',
    selectedGraphNodes: [{ id: 'a1', name: 'Node A1' }],
  });
}

describe('graphStore.resetSessionScopedState', () => {
  beforeEach(() => {
    useGraphStore.setState({ sessionEpoch: 0 });
  });

  it('collapses assistant history to a single welcome message', () => {
    seedDirtySessionState();
    useGraphStore.getState().resetSessionScopedState(t, 'en');

    const { chatMessages } = useGraphStore.getState();
    expect(chatMessages).toHaveLength(1);
    expect(chatMessages[0].id).toBe('welcome');
    // No message content from the previous session survives.
    expect(chatMessages.some((m) => String(m.content).includes('session A'))).toBe(false);
  });

  it('clears experts and every node-scoped overlay and selection', () => {
    seedDirtySessionState();
    useGraphStore.getState().resetSessionScopedState(t, 'en');

    const state = useGraphStore.getState();
    expect(state.activeExperts).toEqual([]);
    expect(state.detailNode).toBeNull();
    expect(state.editingNode).toBeNull();
    expect(state.contextMenu).toBeNull();
    expect(state.selectedNodeId).toBeNull();
    expect(state.selectedGraphNodes).toEqual([]);
  });

  // Confirming either of these after a switch acts on the previous session's
  // graph — and the edge dialog's save fans out through the sync client, which
  // by then belongs to the new session. Neither may survive the switch.
  it('closes the edge-edit dialog and the pending node-delete confirmation', () => {
    seedDirtySessionState();
    useGraphStore.getState().resetSessionScopedState(t, 'en');

    const state = useGraphStore.getState();
    expect(state.editingEdge).toBeNull();
    expect(state.deleteDialog).toBeNull();
  });

  it('bumps the assistant epoch on every switch (A→B→A)', () => {
    seedDirtySessionState();
    const start = useGraphStore.getState().sessionEpoch;

    useGraphStore.getState().resetSessionScopedState(t, 'en'); // A → B
    const afterFirst = useGraphStore.getState().sessionEpoch;
    expect(afterFirst).toBe(start + 1);

    // Accumulate history again in B, then switch back to A: history must not
    // carry and the epoch must advance a second time.
    useGraphStore.setState({
      chatMessages: [
        { id: 'welcome', role: 'assistant', content: 'welcome' },
        { id: 'b1', role: 'user', content: 'hi from session B' },
      ],
    });
    useGraphStore.getState().resetSessionScopedState(t, 'en'); // B → A
    expect(useGraphStore.getState().sessionEpoch).toBe(start + 2);
    expect(useGraphStore.getState().chatMessages).toHaveLength(1);
    expect(useGraphStore.getState().chatMessages[0].id).toBe('welcome');
  });
});

// The canvas keys its position undo/redo history on this counter, so what does
// and does not bump it is the whole contract: a wholesale replacement of the
// canvas contents establishes a new position baseline, an in-place edit does not.
describe('graphStore.canvasBaselineEpoch', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useGraphStore.setState({ canvasBaselineEpoch: 0, nodes: [], edges: [] });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('advances on every wholesale canvas replacement', () => {
    useGraphStore.getState().clearVisualization();
    expect(useGraphStore.getState().canvasBaselineEpoch).toBe(1);

    // A saved view reloaded over the top of another one: the second load must be
    // distinguishable from the first, or the canvas keeps a history recorded
    // against the layout in between.
    useGraphStore.getState().clearVisualization();
    expect(useGraphStore.getState().canvasBaselineEpoch).toBe(2);
  });

  it('does not advance on an in-place edit of the current contents', () => {
    // updateVisualization doubles as the setter for ordinary edits (edge retype,
    // node edit, node removal). Bumping here would silently destroy the user's
    // undo history every time they edited a node.
    useGraphStore.getState().updateVisualization([{ id: 'n1' }], []);
    expect(useGraphStore.getState().canvasBaselineEpoch).toBe(0);

    useGraphStore.getState().addNodesToVisualization([{ id: 'n2' }], []);
    expect(useGraphStore.getState().canvasBaselineEpoch).toBe(0);
  });
});

// GraphCanvas drops every manual group/note/label/annotation from the canvas
// while this flag is raised. Regression coverage for a bug where an ordinary
// in-place node edit (handleNodeUpdate and its siblings all route through
// updateVisualization) wiped every overlay off the canvas, and autosave then
// persisted the loss as annotation_deleted ops.
describe('graphStore.clearGroupsFlag', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useGraphStore.setState({ clearGroupsFlag: false, nodes: [], edges: [] });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('is not raised by an in-place edit of the current contents', () => {
    // updateVisualization doubles as the setter for ordinary edits (edge
    // retype, node edit, node removal, chat's update_in_visualization). Only a
    // genuine replace — which calls clearVisualization first — may raise this.
    useGraphStore.getState().updateVisualization([{ id: 'n1' }], []);
    expect(useGraphStore.getState().clearGroupsFlag).toBe(false);

    vi.advanceTimersByTime(200);
    expect(useGraphStore.getState().clearGroupsFlag).toBe(false);
  });

  it('is raised (then lowered) by a wholesale canvas replacement', () => {
    useGraphStore.getState().clearVisualization();
    expect(useGraphStore.getState().clearGroupsFlag).toBe(true);

    vi.advanceTimersByTime(200);
    expect(useGraphStore.getState().clearGroupsFlag).toBe(false);
  });
});

describe('graphStore.clearVisualization', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  // Every caller that empties the canvas without a session change — esc-esc, the
  // toolbar clear, a search replacing the results, the guide, and the MCP-driven
  // clear_visualization — used to leave these open. Only the keyboard path is
  // gated on a dialog being open, so an agent can pull the canvas out from under
  // one; confirming it afterwards mutates the global graph for a node or edge
  // the user can no longer see.
  it('closes the dialogs that address canvas content', () => {
    seedDirtySessionState();
    useGraphStore.setState({ nodes: [{ id: 'a1' }], edges: [{ id: 'e1' }] });

    useGraphStore.getState().clearVisualization();

    const s = useGraphStore.getState();
    expect(s.nodes).toEqual([]);
    expect(s.edges).toEqual([]);
    expect(s.detailNode).toBeNull();
    expect(s.editingNode).toBeNull();
    expect(s.editingEdge).toBeNull();
    expect(s.deleteDialog).toBeNull();
    expect(s.contextMenu).toBeNull();
  });

  // A clear is not a session switch: the assistant conversation and the experts
  // belong to the session, not to the canvas contents, and must survive it.
  it('leaves session-scoped state that is not about canvas content alone', () => {
    seedDirtySessionState();
    const epoch = useGraphStore.getState().sessionEpoch;

    useGraphStore.getState().clearVisualization();

    const s = useGraphStore.getState();
    expect(s.chatMessages).toHaveLength(3);
    expect(s.activeExperts).toEqual(['expert-a']);
    expect(s.sessionEpoch).toBe(epoch);
  });
});

describe('graphStore.pulseNode', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useGraphStore.setState({ pulsedNodeIds: {} });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('sets a pulse entry with style and colour, then auto-clears it', () => {
    useGraphStore.getState().pulseNode('n1', { style: 'grow', color: '#f00', durationMs: 1000 });
    const entry = useGraphStore.getState().pulsedNodeIds.n1;
    expect(entry).toMatchObject({ style: 'grow', color: '#f00' });
    expect(typeof entry.seq).toBe('number');

    vi.advanceTimersByTime(1000);
    expect(useGraphStore.getState().pulsedNodeIds.n1).toBeUndefined();
  });

  it('ignores a call without a node id', () => {
    useGraphStore.getState().pulseNode('', { style: 'glow' });
    expect(useGraphStore.getState().pulsedNodeIds).toEqual({});
  });

  it('clamps the duration into the allowed range', () => {
    useGraphStore.getState().pulseNode('n1', { durationMs: 999999 });
    // Below the 15s ceiling the entry is still present; just past it, it clears.
    vi.advanceTimersByTime(15000);
    expect(useGraphStore.getState().pulsedNodeIds.n1).toBeUndefined();
  });

  it('a repeat pulse bumps the seq and resets the auto-clear window', () => {
    useGraphStore.getState().pulseNode('n1', { durationMs: 1000 });
    const first = useGraphStore.getState().pulsedNodeIds.n1.seq;
    vi.advanceTimersByTime(600);
    useGraphStore.getState().pulseNode('n1', { durationMs: 1000 });
    const second = useGraphStore.getState().pulsedNodeIds.n1.seq;
    expect(second).toBeGreaterThan(first);

    // The first timer must not clear the refreshed pulse.
    vi.advanceTimersByTime(600);
    expect(useGraphStore.getState().pulsedNodeIds.n1).toMatchObject({ seq: second });
    // The refreshed window then elapses.
    vi.advanceTimersByTime(400);
    expect(useGraphStore.getState().pulsedNodeIds.n1).toBeUndefined();
  });
});
