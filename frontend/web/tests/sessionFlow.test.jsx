import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';

import * as sessionStore from '../src/services/sessionStore';

// Latest props of interest as the canvas actually received them, so a test can
// assert on App's wiring rather than on the store alone.
const canvasProps = vi.hoisted(() => ({ baselineEpoch: null }));

// GraphCanvas stub: replays the saveViewSignal round-trip that App's session
// snapshot mechanism is multiplexed over, without rendering ReactFlow.
vi.mock('@community-graph/ui-graph-canvas', async (importOriginal) => {
  const actual = await importOriginal();
  const { useEffect } = await import('react');
  function GraphCanvas({
    nodes = [],
    edges = [],
    saveViewSignal = 0,
    onSaveView,
    canvasBaselineEpoch,
  }) {
    canvasProps.baselineEpoch = canvasBaselineEpoch;
    useEffect(() => {
      if (saveViewSignal > 0 && onSaveView) {
        onSaveView({
          nodes: nodes.map((n) => ({ id: n.id, position: { x: 11, y: 22 }, parentId: undefined })),
          edges: edges.map((e) => ({
            id: e.id,
            source: e.source,
            target: e.target,
            label: e.label,
          })),
          groups: [],
        });
      }
    }, [saveViewSignal, onSaveView]); // eslint-disable-line react-hooks/exhaustive-deps
    return <div data-testid="graph-canvas-stub" />;
  }
  return {
    ...actual,
    GraphCanvas,
    positionNewNodes: (newNodes) => newNodes,
  };
});
vi.mock('@community-graph/ui-graph-canvas/styles', () => ({}));

const NODE_A = { id: 'node-a', type: 'Actor', name: 'Actor A' };
const NODE_B = { id: 'node-b', type: 'Theme', name: 'Theme B' };

vi.mock('../src/services/api', () => {
  let idCounter = 0;
  return {
    generateVisualizationSessionId: vi.fn(() => `1234-000${++idCounter}`),
    getVisualizationStreamUrl: vi.fn(() => 'http://localhost/stream'),
    getSessionStreamUrl: vi.fn((id) => `http://localhost/api/sessions/${id}/stream`),
    getSessionOpsUrl: vi.fn((id) => `http://localhost/api/sessions/${id}/ops`),
    getClientId: vi.fn(() => 'client-test'),
    getDisplayName: vi.fn(() => null),
    listServerSessions: vi.fn(async () => ({ sessions: [] })),
    renameServerSession: vi.fn(async () => ({})),
    deleteServerSession: vi.fn(async () => ({ deleted: true })),
    getSession: vi.fn(async (id, opts) => {
      if (id === '5555-6666' && opts?.resolve) {
        return {
          id,
          state: {
            positions: { 'node-b': { x: 5, y: 6 } },
            hidden_node_ids: [],
            hidden_edge_ids: [],
            annotations: [],
          },
          resolved: { nodes: [NODE_B], edges: [] },
          roster: [],
        };
      }
      return { id, state: {}, resolved: { nodes: [], edges: [] }, roster: [] };
    }),
    getSchema: vi.fn(async () => ({ node_types: {} })),
    getSubtypes: vi.fn(async () => ({ subtypes: {} })),
    getPresentation: vi.fn(async () => ({ title: 'Test' })),
    getGraphStats: vi.fn(async () => ({ total_nodes: 0, total_edges: 0 })),
    getUiCapabilities: vi.fn(async () => ({ llm_available: false })),
    getSavedView: vi.fn(async () => ({ success: false })),
    getNodeDetails: vi.fn(async () => ({ success: false })),
    getRelatedNodes: vi.fn(async () => ({ nodes: [] })),
    getCollectConfig: vi.fn(async () => ({})),
    addNodes: vi.fn(async () => ({ success: true, added_node_ids: [] })),
    updateNode: vi.fn(async () => ({ success: true })),
    deleteNodes: vi.fn(async () => ({ success: true })),
    addEdge: vi.fn(async () => ({ success: true })),
    updateEdge: vi.fn(async () => ({ success: true })),
    deleteEdge: vi.fn(async () => ({ success: true })),
    exportGraph: vi.fn(async () => ({ nodes: [], edges: [] })),
  };
});

// EventSource is not implemented in jsdom. This fake auto-delivers a snapshot so
// the sync client becomes "ready" and flushes queued ops during the test.
class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.onmessage = null;
    this.onerror = null;
    FakeEventSource.instances.push(this);
    setTimeout(() => {
      this.onmessage?.({
        data: JSON.stringify({ type: 'snapshot', seq: 0, session: { state: {} } }),
      });
    }, 0);
  }
  close() {}
}
FakeEventSource.instances = [];
global.EventSource = FakeEventSource;

// The sync client posts op batches with global fetch; capture them.
global.fetch = vi.fn(async () => ({
  ok: true,
  status: 200,
  json: async () => ({ applied: [], seq: 1 }),
}));

import App from '../src/App';
import * as api from '../src/services/api';
import useGraphStore from '../src/store/graphStore';
import { I18nProvider } from '../src/i18n';
import { SessionSyncClient } from '../src/services/sessionSyncClient';

function renderApp() {
  return render(
    <I18nProvider>
      <App />
    </I18nProvider>
  );
}

function opsFrom(fetchMock) {
  // Flatten every op sent across all captured op-batch POSTs.
  return fetchMock.mock.calls.flatMap(([, opts]) => {
    try {
      return JSON.parse(opts.body).ops || [];
    } catch {
      return [];
    }
  });
}

describe('Server-backed session lifecycle', () => {
  beforeEach(() => {
    window.localStorage.clear();
    useGraphStore.getState().clearVisualization();
    FakeEventSource.instances = [];
    // Reset, or a leftover value from the previous test satisfies the "canvas
    // has rendered" barrier below and it stops being a barrier at all.
    canvasProps.baselineEpoch = null;
    vi.clearAllMocks();
  });

  it('toolbar Save View still opens the naming dialog and emits ops to the server', async () => {
    const { container } = renderApp();

    act(() => {
      useGraphStore.getState().updateVisualization([NODE_A], []);
    });

    // The SavedView button is the last toolbar item
    const toolbarButtons = container.querySelectorAll('.floating-toolbar-item');
    fireEvent.click(toolbarButtons[toolbarButtons.length - 1]);

    await waitFor(() => {
      expect(screen.getByText('Save View')).toBeInTheDocument();
    });
    // The shared round-trip persisted the canvas as incremental ops (step 6),
    // materialising the session on its op stream rather than a full-state PUT.
    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalled();
    });
    const ops = opsFrom(global.fetch);
    expect(ops).toContainEqual({ op: 'nodes_added', node_ids: ['node-a'] });
    expect(ops).toContainEqual({ op: 'node_moved', node_id: 'node-a', position: { x: 11, y: 22 } });
  });

  // The canvas discards its position undo/redo history on this counter, so the
  // whole fix hangs on App passing it down. Asserted here — against the real App
  // and the real store — because the canvas-side and store-side tests both pass
  // even with the prop unwired.
  it('a wholesale canvas replacement reaches the canvas as a new baseline epoch; an in-place edit does not', async () => {
    renderApp();
    await waitFor(() => expect(typeof canvasProps.baselineEpoch).toBe('number'));
    const initial = canvasProps.baselineEpoch;

    // An in-place edit of the current contents (edge retype, node edit): the
    // canvas must not be told the baseline moved, or every edit would silently
    // destroy the user's undo history.
    await act(async () => {
      useGraphStore.getState().updateVisualization([NODE_A], []);
    });
    expect(canvasProps.baselineEpoch).toBe(initial);

    // A saved view loaded over the running session: the canvas is emptied and
    // repopulated from the view's own coordinates, so the epoch must advance.
    await act(async () => {
      useGraphStore.getState().clearVisualization();
      useGraphStore.getState().addNodesToVisualization([NODE_A], []);
    });
    expect(canvasProps.baselineEpoch).toBe(initial + 1);
  });

  // Regression (SMALL_FIXES 2026-07-10): if a sync client's connect() throws
  // (e.g. new EventSource on a malformed stream URL), ensureSyncConnected must
  // not let the exception escape the un-guarded auto-save call site, nor leave a
  // half-connected client installed. Here the first save's connect() throws: the
  // failure is contained (persistSessionSnapshot still completes, so the Save
  // View dialog opens) and the next save builds a fresh client that flushes the
  // pending ops to the server.
  it('a sync connect failure is contained and recovers on the next save', async () => {
    const connectSpy = vi
      .spyOn(SessionSyncClient.prototype, 'connect')
      .mockImplementationOnce(() => {
        throw new Error('malformed stream URL');
      });

    const { container } = renderApp();

    act(() => {
      useGraphStore.getState().updateVisualization([NODE_A], []);
    });

    const toolbarButtons = container.querySelectorAll('.floating-toolbar-item');
    const saveButton = toolbarButtons[toolbarButtons.length - 1];

    // First save: connect() throws but is swallowed, so the snapshot round-trip
    // completes and still opens the naming dialog (with the old bug the throw
    // escaped persistSessionSnapshot and this dialog never appeared).
    fireEvent.click(saveButton);
    await waitFor(() => {
      expect(connectSpy).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(screen.getByText('Save View')).toBeInTheDocument();
    });

    // Second save: a fresh client connects, so the pending ops finally reach the
    // server — proving the first failure was not left stuck in syncRef.
    fireEvent.click(saveButton);
    await waitFor(() => {
      expect(connectSpy.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
    await waitFor(() => {
      expect(opsFrom(global.fetch)).toContainEqual({ op: 'nodes_added', node_ids: ['node-a'] });
    });

    connectSpy.mockRestore();
  });

  it('clearing a materialised session syncs the empty state instead of being silently dropped (R4)', async () => {
    const { container } = renderApp();

    act(() => {
      useGraphStore.getState().updateVisualization([NODE_A], []);
    });

    // Materialise the session via an explicit save (same path as the test above).
    const toolbarButtons = container.querySelectorAll('.floating-toolbar-item');
    fireEvent.click(toolbarButtons[toolbarButtons.length - 1]);
    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalled();
    });
    expect(opsFrom(global.fetch)).toContainEqual({ op: 'nodes_added', node_ids: ['node-a'] });

    // Clear the canvas — what a double-Escape, a last-node delete, or an MCP
    // clear_visualization does. Now that the session is materialised (the
    // save above connected its sync client), this empty state must still
    // reach the server via the debounced auto-save (R4/D14) instead of
    // scheduleAutoSave's own emptiness guard silently dropping it.
    act(() => {
      useGraphStore.getState().clearVisualization();
    });

    await waitFor(
      () => {
        expect(opsFrom(global.fetch)).toContainEqual({ op: 'nodes_removed', node_ids: ['node-a'] });
      },
      { timeout: 3000 }
    );
  });

  it('MCP command dedup: same command_id is skipped, a different command_id re-applies (R5)', async () => {
    renderApp();

    // The legacy push stream is the fixed, id-less URL (distinct from the
    // op-stream's `/api/sessions/{id}/stream`).
    const legacySource = await waitFor(() => {
      const found = FakeEventSource.instances.find((es) => es.url === 'http://localhost/stream');
      expect(found).toBeTruthy();
      return found;
    });

    const deliver = (commandId) =>
      act(() => {
        legacySource.onmessage({
          data: JSON.stringify({
            type: 'tool_result',
            result: { action: 'add_to_visualization', nodes: [NODE_A], edges: [] },
            command_id: commandId,
          }),
        });
      });

    deliver('cmd-1');
    await waitFor(() => {
      expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['node-a']);
    });

    act(() => {
      useGraphStore.getState().removeNode('node-a');
    });
    expect(useGraphStore.getState().nodes).toEqual([]);

    // A redelivery with the *same* command_id (the legacy stream and the hub
    // broadcasting the same push during their handover window) must be
    // skipped, not reapplied.
    deliver('cmd-1');
    await new Promise((r) => setTimeout(r, 50));
    expect(useGraphStore.getState().nodes).toEqual([]);

    // A *different* command_id — a later, legitimately repeated command, e.g.
    // an agent re-adding a node a user just removed — must still apply. This
    // is the exact bug found in review: dedup must be keyed by command_id,
    // not by payload content.
    deliver('cmd-2');
    await waitFor(() => {
      expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['node-a']);
    });
  });

  // task fbd32fc9: a reconnect used to reload the canvas wholesale from
  // server truth with no way back for whatever this client edited while
  // disconnected. resyncFromServer now reads the sync client's still-queued
  // (never-delivered) ops before that reload and replays them afterwards, so
  // the local edit survives instead of silently vanishing.
  it('a reconnect resync restores a local edit that never reached the server, and reports the recovery', async () => {
    const pendingOp = { op: 'nodes_added', node_ids: ['node-a'] };
    const getPendingOpsSpy = vi
      .spyOn(SessionSyncClient.prototype, 'getPendingOps')
      .mockReturnValue([pendingOp]);
    api.getNodeDetails.mockImplementation(async (id) =>
      id === 'node-a' ? { node: NODE_A, edges: [] } : { success: false }
    );

    const { container } = renderApp();

    // Materialize a session (and its realtime stream) the same way the other
    // tests do — the actual queued content is irrelevant here since
    // getPendingOps is stubbed above to stand in for "an edit made offline".
    act(() => {
      useGraphStore.getState().updateVisualization([NODE_A], []);
    });
    const toolbarButtons = container.querySelectorAll('.floating-toolbar-item');
    fireEvent.click(toolbarButtons[toolbarButtons.length - 1]);
    await waitFor(() => screen.getByText('Save View'));

    const sessionSource = await waitFor(() => {
      const found = FakeEventSource.instances.find(
        (es) => es.url.includes('/api/sessions/') && es.url.includes('/stream')
      );
      expect(found).toBeTruthy();
      return found;
    });

    // The server has no record of node-a (it never got delivered) — the
    // default getSession mock resolves an empty session. A catch_up with a
    // missed op is what a genuine reconnect after a drop delivers.
    act(() => {
      sessionSource.onmessage({
        data: JSON.stringify({
          type: 'catch_up',
          seq: 5,
          ops: [{ op: 'nodes_hidden', node_ids: [] }],
          roster: [],
          claims: {},
        }),
      });
    });

    // The reload would otherwise have wiped node-a from the canvas along with
    // it — it must come back, and the recovery must be reported to the user.
    await waitFor(() => {
      expect(useGraphStore.getState().nodes.map((n) => n.id)).toContain('node-a');
    });
    await waitFor(() => {
      expect(screen.getByText('Reconnected — restored 1 change(s) made while offline')).toBeInTheDocument();
    });

    getPendingOpsSpy.mockRestore();
  });

  // Review round 1 regression: reading the pending-ops queue *after* awaiting
  // the reload request would race SessionSyncClient's own reconnect flush
  // (armed right after onResync returns) — a flush that fires first can
  // splice those exact ops out of the queue before this read ever sees them,
  // silently reintroducing the data loss the test above guards against. The
  // fix is call *order*: getPendingOps must run before getSession, not after.
  it('captures pending ops before the reload request, not after (reconnect-flush race)', async () => {
    const callOrder = [];
    const getPendingOpsSpy = vi
      .spyOn(SessionSyncClient.prototype, 'getPendingOps')
      .mockImplementation(function pendingOpsSpy() {
        callOrder.push('getPendingOps');
        return [];
      });
    // Once, not permanently: a lasting override would leak past
    // vi.clearAllMocks() (which resets call history, not implementations)
    // into later tests.
    const getSessionMock = api.getSession.getMockImplementation();
    api.getSession.mockImplementationOnce(async (...args) => {
      callOrder.push('getSession');
      return getSessionMock(...args);
    });

    const { container } = renderApp();
    act(() => {
      useGraphStore.getState().updateVisualization([NODE_A], []);
    });
    const toolbarButtons = container.querySelectorAll('.floating-toolbar-item');
    fireEvent.click(toolbarButtons[toolbarButtons.length - 1]);
    await waitFor(() => screen.getByText('Save View'));

    const sessionSource = await waitFor(() => {
      const found = FakeEventSource.instances.find(
        (es) => es.url.includes('/api/sessions/') && es.url.includes('/stream')
      );
      expect(found).toBeTruthy();
      return found;
    });

    act(() => {
      sessionSource.onmessage({
        data: JSON.stringify({
          type: 'catch_up',
          seq: 5,
          ops: [{ op: 'nodes_hidden', node_ids: [] }],
          roster: [],
          claims: {},
        }),
      });
    });

    await waitFor(() => {
      expect(callOrder).toContain('getSession');
    });
    expect(callOrder.indexOf('getPendingOps')).toBeGreaterThanOrEqual(0);
    expect(callOrder.indexOf('getPendingOps')).toBeLessThan(callOrder.indexOf('getSession'));

    getPendingOpsSpy.mockRestore();
  });

  // Review round 2 regression: a flaky connection reconnecting twice before a
  // slow reload settles must not run two overlapping resyncs — that would
  // replay the same pending ops twice and show a duplicate recovery toast.
  it('a second reconnect while a resync is still in flight does not start a second reload', async () => {
    const pendingOp = { op: 'nodes_added', node_ids: ['node-a'] };
    const getPendingOpsSpy = vi
      .spyOn(SessionSyncClient.prototype, 'getPendingOps')
      .mockReturnValue([pendingOp]);
    api.getNodeDetails.mockImplementation(async (id) =>
      id === 'node-a' ? { node: NODE_A, edges: [] } : { success: false }
    );

    let releaseGetSession;
    const getSessionGate = new Promise((resolve) => {
      releaseGetSession = resolve;
    });
    const getSessionCalls = [];
    // Once, not permanently: only one real call is expected (the guard must
    // block the second reconnect's), and a lasting override would leak past
    // vi.clearAllMocks() into later tests.
    api.getSession.mockImplementationOnce(async (id) => {
      getSessionCalls.push(id);
      await getSessionGate;
      return { id, state: {}, resolved: { nodes: [], edges: [] }, roster: [] };
    });

    const { container } = renderApp();
    act(() => {
      useGraphStore.getState().updateVisualization([NODE_A], []);
    });
    const toolbarButtons = container.querySelectorAll('.floating-toolbar-item');
    fireEvent.click(toolbarButtons[toolbarButtons.length - 1]);
    await waitFor(() => screen.getByText('Save View'));

    const sessionSource = await waitFor(() => {
      const found = FakeEventSource.instances.find(
        (es) => es.url.includes('/api/sessions/') && es.url.includes('/stream')
      );
      expect(found).toBeTruthy();
      return found;
    });

    const deliverCatchUp = () =>
      act(() => {
        sessionSource.onmessage({
          data: JSON.stringify({
            type: 'catch_up',
            seq: 5,
            ops: [{ op: 'nodes_hidden', node_ids: [] }],
            roster: [],
            claims: {},
          }),
        });
      });

    deliverCatchUp(); // first reconnect: getSession call #1 starts, gated
    await waitFor(() => expect(getSessionCalls.length).toBe(1));
    deliverCatchUp(); // second reconnect while the first resync is still in flight
    await new Promise((r) => setTimeout(r, 20));
    expect(getSessionCalls.length).toBe(1); // the second resync never called getSession

    await act(async () => {
      releaseGetSession();
      await Promise.resolve();
    });

    // The (single) in-flight resync still completed and recovered the op.
    await waitFor(() => {
      expect(useGraphStore.getState().nodes.map((n) => n.id)).toContain('node-a');
    });
    expect(getSessionCalls.length).toBe(1);

    getPendingOpsSpy.mockRestore();
  });

  // Review round 3 regression: replaying a recovered op onto the canvas
  // (applyRemoteOp) without also folding it into the sync client's own
  // baseline (foldLocalOp) leaves the baseline stale — since this client's
  // own echo for that op never arrives (echoes of one's own ops are always
  // skipped), nothing else would ever fold it in, so every later autosave's
  // diff would treat the recovered content as still-unsent and resend it
  // indefinitely.
  //
  // (The other review-round-3 finding — a hung reload permanently wedging
  // the reentrancy guard — is fixed by a token-guarded setTimeout self-heal
  // matching SessionSyncClient's own already-unit-tested request-timeout
  // precedent; simulating a real ~20s hang end-to-end through the full save
  // dialog flow was judged impractical to do reliably here.)
  it('folds each recovered op into the sync baseline, not just the canvas', async () => {
    const pendingOp = { op: 'nodes_added', node_ids: ['node-a'] };
    const getPendingOpsSpy = vi
      .spyOn(SessionSyncClient.prototype, 'getPendingOps')
      .mockReturnValue([pendingOp]);
    const foldLocalOpSpy = vi.spyOn(SessionSyncClient.prototype, 'foldLocalOp');
    api.getNodeDetails.mockImplementation(async (id) =>
      id === 'node-a' ? { node: NODE_A, edges: [] } : { success: false }
    );

    const { container } = renderApp();
    act(() => {
      useGraphStore.getState().updateVisualization([NODE_A], []);
    });
    const toolbarButtons = container.querySelectorAll('.floating-toolbar-item');
    fireEvent.click(toolbarButtons[toolbarButtons.length - 1]);
    await waitFor(() => screen.getByText('Save View'));

    const sessionSource = await waitFor(() => {
      const found = FakeEventSource.instances.find(
        (es) => es.url.includes('/api/sessions/') && es.url.includes('/stream')
      );
      expect(found).toBeTruthy();
      return found;
    });

    act(() => {
      sessionSource.onmessage({
        data: JSON.stringify({
          type: 'catch_up',
          seq: 5,
          ops: [{ op: 'nodes_hidden', node_ids: [] }],
          roster: [],
          claims: {},
        }),
      });
    });

    await waitFor(() => {
      expect(foldLocalOpSpy).toHaveBeenCalledWith(pendingOp);
    });

    getPendingOpsSpy.mockRestore();
    foldLocalOpSpy.mockRestore();
  });

  // Review round 1 regression: a bare local selection (no offline edits at
  // all) re-queues a selection_claimed op on every reconnect
  // (_readvertiseSelection). applyRemoteOp has no case for it — replaying it
  // is a no-op — so it must not inflate the reported recovery count or claim
  // a recovery happened when nothing the user did was actually restored.
  it('does not report a recovery for a bare reconnect selection re-advertisement', async () => {
    const getPendingOpsSpy = vi
      .spyOn(SessionSyncClient.prototype, 'getPendingOps')
      .mockReturnValue([{ op: 'selection_claimed', element_ids: ['node-a'] }]);

    const { container } = renderApp();
    act(() => {
      useGraphStore.getState().updateVisualization([NODE_A], []);
    });
    const toolbarButtons = container.querySelectorAll('.floating-toolbar-item');
    fireEvent.click(toolbarButtons[toolbarButtons.length - 1]);
    await waitFor(() => screen.getByText('Save View'));

    const sessionSource = await waitFor(() => {
      const found = FakeEventSource.instances.find(
        (es) => es.url.includes('/api/sessions/') && es.url.includes('/stream')
      );
      expect(found).toBeTruthy();
      return found;
    });

    act(() => {
      sessionSource.onmessage({
        data: JSON.stringify({
          type: 'catch_up',
          seq: 5,
          ops: [{ op: 'nodes_hidden', node_ids: [] }],
          roster: [],
          claims: {},
        }),
      });
    });

    // Give the (fire-and-forget) resync a tick to complete.
    await waitFor(() => {
      expect(getPendingOpsSpy).toHaveBeenCalled();
    });
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByText(/Reconnected — restored/)).not.toBeInTheDocument();

    getPendingOpsSpy.mockRestore();
  });

  it('switching session loads the target from the server, carrying its saved position', async () => {
    // Seed a previous session in the recents list so it shows in the drawer
    sessionStore.touchSession('5555-6666');

    renderApp();

    act(() => {
      useGraphStore.getState().updateVisualization([NODE_A], []);
    });

    // Open the drawer and select the seeded session
    fireEvent.click(screen.getByTitle('Menu'));
    fireEvent.click(screen.getByText('5555-6666'));

    await waitFor(() => {
      expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['node-b']);
    });

    // The target was loaded resolved from the server, carrying its saved position
    expect(api.getSession).toHaveBeenCalledWith('5555-6666', { resolve: true });
    expect(useGraphStore.getState().nodes[0]._savedPosition).toEqual({ x: 5, y: 6 });
  });

  // Review round 5 regression: resyncFromServer's replay loop awaits a
  // network call per recovered nodes_added op; the canvas store it writes to
  // is not scoped by session. If the user switches sessions while that await
  // is still pending, a later op in the *old* session's recovered batch must
  // not go on to land on the *new* session's now-loaded canvas.
  it('stops replaying recovered ops once the user switches sessions mid-replay', async () => {
    sessionStore.touchSession('5555-6666');

    let releaseNodeA;
    const nodeAGate = new Promise((resolve) => {
      releaseNodeA = resolve;
    });
    // Two ops in the "offline" batch: the first stalls on its node fetch (so
    // the test can switch sessions while the loop is paused there), the
    // second must never apply once that switch has happened.
    const pendingOps = [
      { op: 'nodes_added', node_ids: ['node-a'] },
      { op: 'nodes_hidden', node_ids: ['ghost-node'] },
    ];
    const getPendingOpsSpy = vi
      .spyOn(SessionSyncClient.prototype, 'getPendingOps')
      .mockReturnValue(pendingOps);
    api.getNodeDetails.mockImplementation(async (id) => {
      if (id !== 'node-a') return { success: false };
      await nodeAGate;
      return { node: NODE_A, edges: [] };
    });

    const { container } = renderApp();
    act(() => {
      useGraphStore.getState().updateVisualization([NODE_A], []);
    });
    const toolbarButtons = container.querySelectorAll('.floating-toolbar-item');
    fireEvent.click(toolbarButtons[toolbarButtons.length - 1]);
    await waitFor(() => screen.getByText('Save View'));

    const sessionSource = await waitFor(() => {
      const found = FakeEventSource.instances.find(
        (es) => es.url.includes('/api/sessions/') && es.url.includes('/stream')
      );
      expect(found).toBeTruthy();
      return found;
    });

    act(() => {
      sessionSource.onmessage({
        data: JSON.stringify({
          type: 'catch_up',
          seq: 5,
          ops: [{ op: 'nodes_hidden', node_ids: [] }],
          roster: [],
          claims: {},
        }),
      });
    });

    // The replay loop is now paused inside applyRemoteOp's fetch for node-a.
    await waitFor(() => expect(api.getNodeDetails).toHaveBeenCalledWith('node-a'));

    // Switch to a different, already-known session while that fetch is
    // still pending.
    fireEvent.click(screen.getByTitle('Menu'));
    fireEvent.click(screen.getByText('5555-6666'));
    await waitFor(() => {
      expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['node-b']);
    });

    // Now let the stalled fetch resolve — the loop must re-check the active
    // session before its next iteration and stop, so neither node-a (from
    // the old session's own recovered op) nor the hidden-node effect of the
    // batch's second op ever reaches session B's canvas.
    await act(async () => {
      releaseNodeA();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['node-b']);
    expect(useGraphStore.getState().hiddenNodeIds || []).not.toContain('ghost-node');

    getPendingOpsSpy.mockRestore();
  });

  it('drawer name-refresh does not overwrite a locally kept name with a null server name (R7)', async () => {
    // A session renamed locally before the server ever materialised it (or
    // simply one the server hasn't got a name for) must keep its local name
    // when the drawer's periodic refresh sees `name: null` from the server.
    sessionStore.touchSession('5555-6666');
    sessionStore.renameSession('5555-6666', 'My local name');
    api.listServerSessions.mockResolvedValueOnce({
      sessions: [{ id: '5555-6666', name: null }],
    });

    renderApp();
    fireEvent.click(screen.getByTitle('Menu'));

    await waitFor(() => {
      expect(api.listServerSessions).toHaveBeenCalled();
    });

    expect(screen.getByText('My local name')).toBeInTheDocument();
    expect(sessionStore.listSessions().find((s) => s.id === '5555-6666').name).toBe(
      'My local name'
    );
  });

  it('a real load failure (non-404) shows an error notice and stays on the current session', async () => {
    // Distinguishes an actual backend/network error from a 404 ("session
    // doesn't exist yet", handled elsewhere as a normal empty session): only
    // the latter should ever clear the canvas.
    sessionStore.touchSession('7777-8888');
    renderApp();

    act(() => {
      useGraphStore.getState().updateVisualization([NODE_A], []);
    });

    const serverError = new Error('Internal Server Error');
    serverError.status = 500;
    api.getSession.mockImplementationOnce(async () => {
      throw serverError;
    });

    fireEvent.click(screen.getByTitle('Menu'));
    fireEvent.click(screen.getByText('7777-8888'));

    await waitFor(() => {
      expect(screen.getByText('Could not load session')).toBeInTheDocument();
    });

    // The failed switch must not have cleared the current canvas or changed session.
    expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['node-a']);
  });

  it('a baseline-seeding failure after a successful load still commits the switch', async () => {
    // The canvas load (applyServerSession) already succeeded by the time the
    // sync baseline is seeded — a failure only in that best-effort step must
    // not make the switch look like it never happened (App.jsx would
    // otherwise report success visually while claiming to still be on the
    // old session).
    sessionStore.touchSession('9999-0000');
    renderApp();

    act(() => {
      useGraphStore.getState().updateVisualization([NODE_A], []);
    });

    const setBaselineSpy = vi
      .spyOn(SessionSyncClient.prototype, 'setBaseline')
      .mockImplementationOnce(() => {
        throw new Error('malformed baseline');
      });

    fireEvent.click(screen.getByTitle('Menu'));
    fireEvent.click(screen.getByText('9999-0000'));

    await waitFor(() => {
      expect(window.location.search).toContain('session=9999-0000');
    });
    // The target session's (empty) canvas was applied — no error notice shown.
    expect(useGraphStore.getState().nodes).toEqual([]);
    expect(screen.queryByText('Could not load session')).not.toBeInTheDocument();

    setBaselineSpy.mockRestore();
  });

  it('malformed session data fails before the canvas is touched (atomic switch)', async () => {
    // annotations must be an array; a non-iterable value breaks the shared
    // annotationsToGroups/annotationsToOverlays transform used both by
    // applyServerSession and by the sync-baseline computation this now runs
    // *before* applyServerSession, precisely so a throw here can't leave the
    // canvas half-mutated with the switch reported as failed.
    sessionStore.touchSession('aaaa-bbbb');
    renderApp();

    act(() => {
      useGraphStore.getState().updateVisualization([NODE_A], []);
    });

    api.getSession.mockImplementationOnce(async (id) => ({
      id,
      state: { annotations: {} },
      resolved: { nodes: [NODE_B], edges: [] },
      roster: [],
    }));

    fireEvent.click(screen.getByTitle('Menu'));
    fireEvent.click(screen.getByText('aaaa-bbbb'));

    await waitFor(() => {
      expect(screen.getByText('Could not load session')).toBeInTheDocument();
    });

    // Failed before mutating anything: still the original canvas and session.
    expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['node-a']);
    expect(window.location.search).not.toContain('session=aaaa-bbbb');
  });

  // Regression: App.jsx's handleNodeCreated (guarded on isCoarsePointer) must
  // schedule setFocusNodeId(createdNode.id) on a later tick than
  // addNodesToVisualization, not call it in the same synchronous update —
  // mirroring the identical two-step ordering FloatingSearch.jsx already uses
  // for a newly-added node. GraphCanvas itself is stubbed out in this file
  // (see the vi.mock above), so this cannot observe the real ReactFlow
  // instance's own render lag; it only proves the two store writes land in
  // separate ticks, which is what the fix's setTimeout(...,100) is for.
  it('schedules centering a touch-created node on a later tick than the node-store update', async () => {
    const originalMatchMedia = window.matchMedia;
    try {
      window.matchMedia = vi.fn((query) => ({
        matches: query === '(pointer: coarse)',
        media: query,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }));
      api.getSchema.mockResolvedValueOnce({
        node_types: { Actor: { category: 'domain', icon: 'PersonFill', color: '#3B82F6' } },
      });
      api.addNodes.mockResolvedValueOnce({ success: true, added_node_ids: ['new-actor-1'] });

      renderApp();

      // Query by the toolbar's own aria-label (not position): the store's
      // `schema` is a shared module singleton that a prior test may have left
      // populated, so the toolbar can render with stale content for a moment
      // before this test's mocked getSchema() resolves and replaces it.
      const actorButton = await screen.findByRole('button', { name: 'Actor' });
      fireEvent.click(actorButton);

      const nameInput = await screen.findByLabelText('Name *');
      fireEvent.change(nameInput, { target: { value: 'Touch Actor' } });
      fireEvent.click(screen.getByRole('button', { name: 'Create Actor' }));

      await waitFor(() => {
        expect(useGraphStore.getState().nodes.map((n) => n.id)).toContain('new-actor-1');
      });
      // The node is in the store already (assertion above), but the camera
      // must not have been pointed at it in that same update.
      expect(useGraphStore.getState().focusNodeId).not.toBe('new-actor-1');

      await waitFor(() => {
        expect(useGraphStore.getState().focusNodeId).toBe('new-actor-1');
      });
    } finally {
      window.matchMedia = originalMatchMedia;
    }
  });
});
