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
    const scripted = FakeEventSource.microtaskMessagesByUrl[url.split('?')[0]];
    if (scripted) {
      queueMicrotask(() =>
        scripted.forEach((msg) => this.onmessage?.({ data: JSON.stringify(msg) }))
      );
      return;
    }
    const configuredSeq = FakeEventSource.snapshotSeqByUrl[url.split('?')[0]];
    const deliver = () =>
      this.onmessage?.({
        data: JSON.stringify({ type: 'snapshot', seq: configuredSeq ?? 0, session: { state: {} } }),
      });
    // A configured seq is delivered on a microtask: it lands after the load's
    // setBaseline but before App re-renders with the new session id, the
    // window where App's handlers are still bound to the previous session.
    if (configuredSeq === undefined) setTimeout(deliver, 0);
    else queueMicrotask(deliver);
  }
  close() {}
}
FakeEventSource.instances = [];
FakeEventSource.snapshotSeqByUrl = {};
// Messages delivered in order on one microtask, in place of the default
// snapshot: they land while App's handlers are still bound to the session
// being left (see the configured-seq note above).
FakeEventSource.microtaskMessagesByUrl = {};
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
import { SessionSyncClient, DEFAULT_REQUEST_TIMEOUT_MS } from '../src/services/sessionSyncClient';

// vi.clearAllMocks() keeps a mockImplementation() a test installed, so each
// module default is captured here and put back before every test; otherwise
// a test inherits whatever the test before it left behind (e.g. a
// getNodeDetails that resolves node-a lets a resync replay it).
const defaultMockImplementations = [
  ...Object.values(api).filter((fn) => vi.isMockFunction(fn)),
  global.fetch,
].map((fn) => [fn, fn.getMockImplementation()]);

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
    FakeEventSource.snapshotSeqByUrl = {};
    FakeEventSource.microtaskMessagesByUrl = {};
    // Reset, or a leftover value from the previous test satisfies the "canvas
    // has rendered" barrier below and it stops being a barrier at all.
    canvasProps.baselineEpoch = null;
    vi.clearAllMocks();
    defaultMockImplementations.forEach(([fn, impl]) => {
      fn.mockReset();
      fn.mockImplementation(impl);
    });
    Object.defineProperty(window.navigator, 'onLine', {
      configurable: true,
      value: true,
    });
  });

  it('shows a clear read-only offline state when the network is gone', async () => {
    renderApp();

    act(() => {
      Object.defineProperty(window.navigator, 'onLine', {
        configurable: true,
        value: false,
      });
      window.dispatchEvent(new Event('offline'));
    });

    expect(
      await screen.findByText('Offline — viewing only. Reconnect before editing the graph.')
    ).toBeInTheDocument();
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
      expect(
        screen.getByText('Reconnected — restored 1 change(s) made while offline')
      ).toBeInTheDocument();
    });

    getPendingOpsSpy.mockRestore();
  });

  // Review round 6 regression: an op enqueued *while* the reload request is
  // still in flight is neither reflected in the reload's payload nor in the
  // pre-request pending-ops snapshot (round 1's fix) — a second capture right
  // before the destructive apply must pick it up too.
  it('recovers an op enqueued while the reload request is still in flight', async () => {
    let callCount = 0;
    const lateOp = { op: 'nodes_added', node_ids: ['node-a'] };
    const getPendingOpsSpy = vi
      .spyOn(SessionSyncClient.prototype, 'getPendingOps')
      .mockImplementation(() => {
        callCount += 1;
        // First read (before the request starts): nothing queued yet.
        // Second read (right after it resolves): the op the user made
        // while it was in flight.
        return callCount === 1 ? [] : [lateOp];
      });
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
      expect(useGraphStore.getState().nodes.map((n) => n.id)).toContain('node-a');
    });
    expect(callCount).toBeGreaterThanOrEqual(2);

    getPendingOpsSpy.mockRestore();
  });

  // Review round 10 regression: an op the server terminally rejects (via
  // onDropped) *while* a resync's reload request is still in flight stays
  // visible in the pre-request pending-ops capture (round 1's fix) even
  // though it is gone from a second, post-request capture (round 6's fix) —
  // the union of the two (round 6) would otherwise keep it anyway and
  // resurrect content the server just explicitly refused.
  it('excludes an op the server terminally rejected while the reload was in flight', async () => {
    const originalConnect = SessionSyncClient.prototype.connect;
    let capturedClient = null;
    const connectSpy = vi
      .spyOn(SessionSyncClient.prototype, 'connect')
      .mockImplementation(function capture(...args) {
        capturedClient = this;
        return originalConnect.apply(this, args);
      });

    const droppedOp = { op: 'nodes_hidden', node_ids: ['node-a'] };
    const getPendingOpsSpy = vi
      .spyOn(SessionSyncClient.prototype, 'getPendingOps')
      .mockReturnValue([droppedOp]);

    let releaseGetSession;
    const getSessionGate = new Promise((resolve) => {
      releaseGetSession = resolve;
    });
    api.getSession.mockImplementationOnce(async (id) => {
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
    expect(capturedClient).toBeTruthy();

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
          ops: [{ op: 'nodes_added', node_ids: [] }],
          roster: [],
          claims: {},
        }),
      });
    });

    // The reload's getSession() is now gated in flight. Simulate
    // SessionSyncClient reporting this exact op as terminally dropped in
    // that window — precisely what its own _flush() does synchronously on a
    // 400/413/404/410 for a single-op batch.
    act(() => {
      capturedClient.handlers.onDropped([droppedOp], 400);
    });

    await act(async () => {
      releaseGetSession();
      await Promise.resolve();
      await Promise.resolve();
    });

    // The drop must not have been resurrected onto the canvas.
    await waitFor(() => {
      expect(useGraphStore.getState().hiddenNodeIds || []).not.toContain('node-a');
    });

    connectSpy.mockRestore();
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
    expect(getSessionCalls.length).toBe(1);
    // getSessionCalls only sees the gated first call; a second resync would
    // reach the default mock instead, so count every call for this session.
    const resyncLoads = () =>
      api.getSession.mock.calls.filter(([id]) => id === getSessionCalls[0]).length;
    expect(resyncLoads()).toBe(1); // the second resync never called getSession

    await act(async () => {
      releaseGetSession();
      await Promise.resolve();
    });

    // The (single) in-flight resync still completed and recovered the op.
    await waitFor(() => {
      expect(useGraphStore.getState().nodes.map((n) => n.id)).toContain('node-a');
    });
    expect(resyncLoads()).toBe(1);

    getPendingOpsSpy.mockRestore();
  });

  // Review round 3 regression: replaying a recovered op onto the canvas
  // (applyRemoteOp) without also folding it into the sync client's own
  // baseline leaves the baseline stale — since this client's own echo for
  // that op never arrives (echoes of one's own ops are always skipped),
  // nothing else would ever fold it in, so every later autosave's diff would
  // treat the recovered content as still-unsent and resend it indefinitely.
  //
  // (The other review-round-3 finding — a hung reload permanently wedging
  // the reentrancy guard — is fixed by a token-guarded setTimeout self-heal
  // matching SessionSyncClient's own already-unit-tested request-timeout
  // precedent; simulating a real ~20s hang end-to-end through the full save
  // dialog flow was judged impractical to do reliably here.)
  //
  // foldOpIntoBaseline, not foldLocalOp (review round 7): folding a recovered
  // op with foldLocalOp — which additionally marks the annotation id to skip
  // one *confirming echo* — left a marker nothing would ever consume (an
  // ordinary op's echo is filtered by the "own client id" check before it
  // could reach that marker), which could then wrongly swallow a *different*
  // collaborator's later genuine edit to the same annotation. See
  // foldOpIntoBaseline's docstring in sessionSyncClient.js.
  it('folds each recovered op into the sync baseline, not just the canvas', async () => {
    const pendingOp = { op: 'nodes_added', node_ids: ['node-a'] };
    const getPendingOpsSpy = vi
      .spyOn(SessionSyncClient.prototype, 'getPendingOps')
      .mockReturnValue([pendingOp]);
    const foldOpIntoBaselineSpy = vi.spyOn(SessionSyncClient.prototype, 'foldOpIntoBaseline');
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
      expect(foldOpIntoBaselineSpy).toHaveBeenCalledWith(pendingOp);
    });
    // Not the annotation-echo-marking variant — see the comment above.
    expect(foldLocalOpSpy).not.toHaveBeenCalled();

    getPendingOpsSpy.mockRestore();
    foldOpIntoBaselineSpy.mockRestore();
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

  it('does not report a recovered count for a stale pending add already in the server snapshot', async () => {
    const pendingOp = { op: 'nodes_added', node_ids: ['node-a'] };
    const getPendingOpsSpy = vi
      .spyOn(SessionSyncClient.prototype, 'getPendingOps')
      .mockReturnValue([pendingOp]);

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

    api.getSession.mockImplementationOnce(async (id) => ({
      id,
      state: { node_refs: ['node-a'], positions: {}, annotations: [] },
      resolved: { nodes: [NODE_A], edges: [] },
      roster: [],
    }));

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
      expect(useGraphStore.getState().nodes.map((n) => n.id)).toContain('node-a');
    });
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByText(/Reconnected — restored/)).not.toBeInTheDocument();
    expect(api.getNodeDetails).not.toHaveBeenCalledWith('node-a');

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

  // A resync for the session being left, still waiting on its reload, must
  // not swallow the first-snapshot resync of the session just switched to:
  // the old call bails at its switched-away check, so nothing else would
  // reload the new session and its canvas would stay on the stale load.
  it('a slow resync for the session being left does not block the new session first-snapshot resync', async () => {
    sessionStore.touchSession('5555-6666');
    FakeEventSource.snapshotSeqByUrl['http://localhost/api/sessions/5555-6666/stream'] = 7;
    const NODE_C = { id: 'node-c', type: 'Theme', name: 'Theme C' };

    let releaseOldReload;
    const oldReloadGate = new Promise((resolve) => {
      releaseOldReload = resolve;
    });
    let gateOtherSessions = false;
    let targetLoads = 0;
    const originalGetSession = api.getSession.getMockImplementation();
    api.getSession.mockImplementation(async (id, opts) => {
      if (id === '5555-6666') {
        targetLoads += 1;
        // The load is older (seq 3) than the stream's first snapshot (seq 7);
        // only the resync that snapshot triggers returns node-c.
        const nodes = targetLoads === 1 ? [NODE_B] : [NODE_B, NODE_C];
        return {
          id,
          seq: targetLoads === 1 ? 3 : 7,
          state: { positions: {}, hidden_node_ids: [], hidden_edge_ids: [], annotations: [] },
          resolved: { nodes, edges: [] },
          roster: [],
        };
      }
      if (gateOtherSessions) {
        await oldReloadGate;
        return { id, state: {}, resolved: { nodes: [NODE_A], edges: [] }, roster: [] };
      }
      return originalGetSession(id, opts);
    });

    try {
      const { container } = renderApp();
      act(() => {
        useGraphStore.getState().updateVisualization([NODE_A], []);
      });
      const toolbarButtons = container.querySelectorAll('.floating-toolbar-item');
      fireEvent.click(toolbarButtons[toolbarButtons.length - 1]);
      await waitFor(() => screen.getByText('Save View'));

      const oldSource = await waitFor(() => {
        const found = FakeEventSource.instances.find(
          (es) => es.url.includes('/api/sessions/') && es.url.includes('/stream')
        );
        expect(found).toBeTruthy();
        return found;
      });
      const oldSessionId = oldSource.url.split('/api/sessions/')[1].split('/')[0];

      gateOtherSessions = true;
      act(() => {
        oldSource.onmessage({
          data: JSON.stringify({
            type: 'catch_up',
            seq: 5,
            ops: [{ op: 'nodes_hidden', node_ids: [] }],
            roster: [],
            claims: {},
          }),
        });
      });
      await waitFor(() =>
        expect(api.getSession).toHaveBeenCalledWith(oldSessionId, { resolve: true })
      );

      fireEvent.click(screen.getByTitle('Menu'));
      fireEvent.click(screen.getByText('5555-6666'));

      await waitFor(() => expect(targetLoads).toBe(2));
      await waitFor(() => {
        expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['node-b', 'node-c']);
      });

      // The old reload settling late must not clobber the new session's canvas.
      await act(async () => {
        releaseOldReload();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['node-b', 'node-c']);
    } finally {
      releaseOldReload();
      api.getSession.mockImplementation(originalGetSession);
    }
  });

  // Switching away and back builds a new sync client for the same session
  // id. Its first-snapshot resync must not be swallowed by the resync the
  // previous client for that id left in flight, and that older resync, when
  // it finally settles, must not overwrite the newer reload.
  it('a slow resync from an earlier visit does not block the resync on returning to the session', async () => {
    sessionStore.touchSession('5555-6666');
    sessionStore.touchSession('7777-8888');
    FakeEventSource.snapshotSeqByUrl['http://localhost/api/sessions/7777-8888/stream'] = 9;
    const NODE_C = { id: 'node-c', type: 'Theme', name: 'Theme C' };
    const emptyState = { positions: {}, hidden_node_ids: [], hidden_edge_ids: [], annotations: [] };

    let releaseFirstResync;
    const firstResyncGate = new Promise((resolve) => {
      releaseFirstResync = resolve;
    });
    let returningLoads = 0;
    const originalGetSession = api.getSession.getMockImplementation();
    api.getSession.mockImplementation(async (id, opts) => {
      if (id !== '7777-8888') return originalGetSession(id, opts);
      returningLoads += 1;
      const call = returningLoads;
      // 1: first load (seq 3, older than the stream's seq 9) — 2: the resync
      // that first snapshot triggers, held open — 3: the reload on returning
      // — 4: the returning client's own first-snapshot resync.
      if (call === 2) {
        await firstResyncGate;
        return { id, seq: 9, state: emptyState, resolved: { nodes: [NODE_A], edges: [] } };
      }
      const nodes = call === 4 ? [NODE_A, NODE_C] : [NODE_A];
      return { id, seq: call === 4 ? 9 : 3, state: emptyState, resolved: { nodes, edges: [] } };
    });

    try {
      renderApp();
      fireEvent.click(screen.getByTitle('Menu'));
      fireEvent.click(screen.getByText('7777-8888'));
      await waitFor(() => expect(returningLoads).toBe(2));

      fireEvent.click(screen.getByTitle('Menu'));
      fireEvent.click(screen.getByText('5555-6666'));
      await waitFor(() => {
        expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['node-b']);
      });

      fireEvent.click(screen.getByTitle('Menu'));
      fireEvent.click(screen.getByText('7777-8888'));
      await waitFor(() => expect(returningLoads).toBe(4));
      await waitFor(() => {
        expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['node-a', 'node-c']);
      });

      await act(async () => {
        releaseFirstResync();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['node-a', 'node-c']);
    } finally {
      releaseFirstResync();
      api.getSession.mockImplementation(originalGetSession);
    }
  });

  // Same away-and-back shape, but the reload on returning is already current
  // (seq 9), so the returning client's first snapshot starts no resync of its
  // own and nothing supersedes the first visit's call. Its session id matches
  // the returning client's, so only a client-identity check stops it from
  // applying its older payload over the return load.
  it('a slow resync from an earlier visit does not overwrite a current return load', async () => {
    sessionStore.touchSession('5555-6666');
    sessionStore.touchSession('7777-8888');
    FakeEventSource.snapshotSeqByUrl['http://localhost/api/sessions/7777-8888/stream'] = 9;
    const NODE_C = { id: 'node-c', type: 'Theme', name: 'Theme C' };
    const emptyState = { positions: {}, hidden_node_ids: [], hidden_edge_ids: [], annotations: [] };

    let releaseFirstResync;
    const firstResyncGate = new Promise((resolve) => {
      releaseFirstResync = resolve;
    });
    let returningLoads = 0;
    const originalGetSession = api.getSession.getMockImplementation();
    api.getSession.mockImplementation(async (id, opts) => {
      if (id !== '7777-8888') return originalGetSession(id, opts);
      returningLoads += 1;
      const call = returningLoads;
      // 1: first load (seq 3, older than the stream's seq 9) — 2: the resync
      // that first snapshot triggers, held open — 3: the reload on returning,
      // already at the stream's seq.
      if (call === 1) return { id, seq: 3, state: emptyState, resolved: { nodes: [], edges: [] } };
      if (call === 2) {
        await firstResyncGate;
        return { id, seq: 9, state: emptyState, resolved: { nodes: [NODE_A], edges: [] } };
      }
      return { id, seq: 9, state: emptyState, resolved: { nodes: [NODE_A, NODE_C], edges: [] } };
    });

    try {
      renderApp();
      fireEvent.click(screen.getByTitle('Menu'));
      fireEvent.click(screen.getByText('7777-8888'));
      await waitFor(() => expect(returningLoads).toBe(2));

      fireEvent.click(screen.getByTitle('Menu'));
      fireEvent.click(screen.getByText('5555-6666'));
      await waitFor(() => {
        expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['node-b']);
      });

      fireEvent.click(screen.getByTitle('Menu'));
      fireEvent.click(screen.getByText('7777-8888'));
      await waitFor(() => {
        expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['node-a', 'node-c']);
      });
      // Let the returning client's first snapshot land: it must not start a
      // resync (seq 9 is not above the load's), or this test proves nothing.
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      expect(returningLoads).toBe(3);

      await act(async () => {
        releaseFirstResync();
        await Promise.resolve();
        await Promise.resolve();
      });
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['node-a', 'node-c']);
    } finally {
      releaseFirstResync();
      api.getSession.mockImplementation(originalGetSession);
    }
  });

  // Materialise a session with a live stream, start a resync whose reload is
  // held on `gate`, and return the stream so a test can deliver ops while the
  // reload is in flight.
  async function startResyncHeldOnGate(gate, loadsById) {
    const { container } = renderApp();
    act(() => {
      useGraphStore.getState().updateVisualization([NODE_A], []);
    });
    const toolbarButtons = container.querySelectorAll('.floating-toolbar-item');
    fireEvent.click(toolbarButtons[toolbarButtons.length - 1]);
    await waitFor(() => screen.getByText('Save View'));
    const source = await waitFor(() => {
      const found = FakeEventSource.instances.find(
        (es) => es.url.includes('/api/sessions/') && es.url.includes('/stream')
      );
      expect(found).toBeTruthy();
      return found;
    });
    const sessionId = source.url.split('/api/sessions/')[1].split('/')[0];
    gate.active = true;
    act(() => {
      source.onmessage({
        data: JSON.stringify({
          type: 'catch_up',
          seq: 5,
          ops: [{ op: 'nodes_hidden', node_ids: [] }],
          roster: [],
          claims: {},
        }),
      });
    });
    await waitFor(() => expect(loadsById(sessionId)).toBe(1));
    return source;
  }

  // An op that streams in while the resync's reload is in flight is applied
  // to the canvas and advances the client's applied seq; a reload payload
  // older than that op must not be applied over it, or the op is lost for
  // good (the stream never resends it).
  it('an op streamed in during the resync reload survives: a payload behind the stream is refetched', async () => {
    let releaseReload;
    const reloadGate = new Promise((resolve) => {
      releaseReload = resolve;
    });
    const gate = { active: false };
    const loads = {};
    const originalGetSession = api.getSession.getMockImplementation();
    api.getSession.mockImplementation(async (id, opts) => {
      if (!gate.active) return originalGetSession(id, opts);
      loads[id] = (loads[id] || 0) + 1;
      if (loads[id] === 1) {
        await reloadGate;
        return { id, seq: 5, state: {}, resolved: { nodes: [NODE_A], edges: [] } };
      }
      return { id, seq: 6, state: {}, resolved: { nodes: [NODE_A, NODE_B], edges: [] } };
    });
    api.getNodeDetails.mockImplementation(async (id) =>
      id === 'node-b' ? { node: NODE_B, edges: [] } : { success: false }
    );

    try {
      const source = await startResyncHeldOnGate(gate, (id) => loads[id] || 0);
      act(() => {
        source.onmessage({
          data: JSON.stringify({
            type: 'op',
            seq: 6,
            client_id: 'someone-else',
            op: { op: 'nodes_added', node_ids: ['node-b'] },
          }),
        });
      });
      await waitFor(() => {
        expect(useGraphStore.getState().nodes.map((n) => n.id)).toContain('node-b');
      });

      await act(async () => {
        releaseReload();
      });
      await waitFor(() => expect(Object.values(loads)).toEqual([2]));
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['node-a', 'node-b']);
    } finally {
      releaseReload();
      api.getSession.mockImplementation(originalGetSession);
    }
  });

  // The refetch is bounded: a stream that stays ahead of every payload must
  // not keep the resync fetching forever; it applies the last payload.
  it('a resync refetches a payload behind the stream at most three times in all', async () => {
    let releaseReload;
    const reloadGate = new Promise((resolve) => {
      releaseReload = resolve;
    });
    const gate = { active: false };
    const loads = {};
    const originalGetSession = api.getSession.getMockImplementation();
    api.getSession.mockImplementation(async (id, opts) => {
      if (!gate.active) return originalGetSession(id, opts);
      loads[id] = (loads[id] || 0) + 1;
      if (loads[id] === 1) await reloadGate;
      const nodes = loads[id] >= 3 ? [NODE_B] : [NODE_A];
      return { id, seq: 5, state: {}, resolved: { nodes, edges: [] } };
    });

    try {
      const source = await startResyncHeldOnGate(gate, (id) => loads[id] || 0);
      act(() => {
        source.onmessage({
          data: JSON.stringify({
            type: 'op',
            seq: 6,
            client_id: 'someone-else',
            op: { op: 'nodes_hidden', node_ids: [] },
          }),
        });
      });

      await act(async () => {
        releaseReload();
      });
      await waitFor(() => {
        expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['node-b']);
      });
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      expect(Object.values(loads)).toEqual([3]);
    } finally {
      releaseReload();
      api.getSession.mockImplementation(originalGetSession);
    }
  });

  // During a switch the new client's stream can deliver before App re-renders,
  // while its handlers still name the session being left: a second snapshot
  // on the new client then asks to resync the old id. That call must neither
  // load the old session onto the new canvas nor hold the in-flight marker
  // that the new session's own later resync needs.
  it('a resync naming the session being left neither loads it nor blocks the new session', async () => {
    sessionStore.touchSession('5555-6666');
    FakeEventSource.microtaskMessagesByUrl['http://localhost/api/sessions/5555-6666/stream'] = [
      { type: 'snapshot', seq: 0, session: { state: {} } },
      { type: 'snapshot', seq: 0, session: { state: {} } },
    ];
    const originalGetSession = api.getSession.getMockImplementation();
    let holdOthers = false;
    api.getSession.mockImplementation(async (id, opts) => {
      if (holdOthers && id !== '5555-6666') await new Promise(() => {});
      return originalGetSession(id, opts);
    });

    try {
      renderApp();
      const callsBeforeSwitch = api.getSession.mock.calls.length;
      holdOthers = true;
      fireEvent.click(screen.getByTitle('Menu'));
      fireEvent.click(screen.getByText('5555-6666'));
      await waitFor(() => {
        expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['node-b']);
      });
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      const idsSinceSwitch = () =>
        api.getSession.mock.calls.slice(callsBeforeSwitch).map(([id]) => id);
      expect(idsSinceSwitch()).toEqual(['5555-6666']);
      expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['node-b']);

      const source = FakeEventSource.instances.find((es) =>
        es.url.includes('/api/sessions/5555-6666/stream')
      );
      act(() => {
        source.onmessage({
          data: JSON.stringify({
            type: 'catch_up',
            seq: 5,
            ops: [{ op: 'nodes_hidden', node_ids: [] }],
            roster: [],
            claims: {},
          }),
        });
      });
      await waitFor(() => expect(idsSinceSwitch()).toEqual(['5555-6666', '5555-6666']));
    } finally {
      api.getSession.mockImplementation(originalGetSession);
    }
  });

  // Switching away and back to the same id while a recovered op's node fetch
  // is pending must stop the replay: the returning client is a new one, and
  // the paused call's remaining ops belong to the client it started for.
  it('stops replaying recovered ops after switching away and back to the same session', async () => {
    sessionStore.touchSession('5555-6666');
    sessionStore.touchSession('7777-8888');
    let releaseNodeA;
    const nodeAGate = new Promise((resolve) => {
      releaseNodeA = resolve;
    });
    const pendingOps = [
      { op: 'nodes_added', node_ids: ['node-a'] },
      { op: 'nodes_hidden', node_ids: ['ghost-node'] },
    ];
    const getPendingOpsSpy = vi
      .spyOn(SessionSyncClient.prototype, 'getPendingOps')
      .mockReturnValue(pendingOps);
    const originalGetNodeDetails = api.getNodeDetails.getMockImplementation();
    api.getNodeDetails.mockImplementation(async (id) => {
      if (id !== 'node-a') return { success: false };
      await nodeAGate;
      return { node: NODE_A, edges: [] };
    });

    try {
      renderApp();
      fireEvent.click(screen.getByTitle('Menu'));
      fireEvent.click(screen.getByText('7777-8888'));
      const source = await waitFor(() => {
        const found = FakeEventSource.instances.find((es) =>
          es.url.includes('/api/sessions/7777-8888/stream')
        );
        expect(found).toBeTruthy();
        return found;
      });
      act(() => {
        source.onmessage({
          data: JSON.stringify({
            type: 'catch_up',
            seq: 5,
            ops: [{ op: 'nodes_hidden', node_ids: [] }],
            roster: [],
            claims: {},
          }),
        });
      });
      await waitFor(() => expect(api.getNodeDetails).toHaveBeenCalledWith('node-a'));

      fireEvent.click(screen.getByTitle('Menu'));
      fireEvent.click(screen.getByText('5555-6666'));
      await waitFor(() => {
        expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['node-b']);
      });
      fireEvent.click(screen.getByTitle('Menu'));
      fireEvent.click(screen.getByText('7777-8888'));
      await waitFor(() => {
        expect(useGraphStore.getState().nodes).toEqual([]);
      });
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });

      await act(async () => {
        releaseNodeA();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(useGraphStore.getState().hiddenNodeIds || []).not.toContain('ghost-node');
    } finally {
      releaseNodeA();
      getPendingOpsSpy.mockRestore();
      api.getNodeDetails.mockImplementation(originalGetNodeDetails);
    }
  });

  // The switched-away check runs after every fetch, not only the first: a
  // switch while the refetch of a payload behind the stream is pending must
  // keep that payload off the new session's canvas.
  it('a refetch that settles after a session switch does not load onto the new session', async () => {
    sessionStore.touchSession('5555-6666');
    let releaseReload;
    const reloadGate = new Promise((resolve) => {
      releaseReload = resolve;
    });
    let releaseRefetch;
    const refetchGate = new Promise((resolve) => {
      releaseRefetch = resolve;
    });
    const gate = { active: false };
    const loads = {};
    const originalGetSession = api.getSession.getMockImplementation();
    api.getSession.mockImplementation(async (id, opts) => {
      if (!gate.active || id === '5555-6666') return originalGetSession(id, opts);
      loads[id] = (loads[id] || 0) + 1;
      if (loads[id] === 1) {
        await reloadGate;
        return { id, seq: 5, state: {}, resolved: { nodes: [NODE_A], edges: [] } };
      }
      await refetchGate;
      return { id, seq: 6, state: {}, resolved: { nodes: [NODE_A], edges: [] } };
    });

    try {
      const source = await startResyncHeldOnGate(gate, (id) => loads[id] || 0);
      act(() => {
        source.onmessage({
          data: JSON.stringify({
            type: 'op',
            seq: 6,
            client_id: 'someone-else',
            op: { op: 'nodes_hidden', node_ids: [] },
          }),
        });
      });
      await act(async () => {
        releaseReload();
      });
      await waitFor(() => expect(Object.values(loads)).toEqual([2]));

      fireEvent.click(screen.getByTitle('Menu'));
      fireEvent.click(screen.getByText('5555-6666'));
      await waitFor(() => {
        expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['node-b']);
      });

      await act(async () => {
        releaseRefetch();
        await Promise.resolve();
        await Promise.resolve();
      });
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['node-b']);
    } finally {
      releaseReload();
      releaseRefetch();
      api.getSession.mockImplementation(originalGetSession);
    }
  });

  // The client's seq also advances on this client's own POST responses (the
  // server's global seq after the batch landed), which says nothing about
  // what the canvas has applied from the stream. A payload that is current
  // with appliedSeq must be applied, not refetched, however far seq ran ahead.
  it('a resync payload current with the applied stream is not refetched after an own POST', async () => {
    let releaseReload;
    const reloadGate = new Promise((resolve) => {
      releaseReload = resolve;
    });
    const gate = { active: false };
    const loads = {};
    const originalGetSession = api.getSession.getMockImplementation();
    api.getSession.mockImplementation(async (id, opts) => {
      if (!gate.active) return originalGetSession(id, opts);
      loads[id] = (loads[id] || 0) + 1;
      if (loads[id] === 1) await reloadGate;
      const nodes = loads[id] === 1 ? [NODE_B] : [NODE_A];
      return { id, seq: 5, state: {}, resolved: { nodes, edges: [] } };
    });
    const originalFetch = global.fetch.getMockImplementation();
    global.fetch.mockImplementation(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ applied: [], seq: 50 }),
    }));
    const clients = [];
    const originalConnect = SessionSyncClient.prototype.connect;
    const connectSpy = vi
      .spyOn(SessionSyncClient.prototype, 'connect')
      .mockImplementation(function connect(...args) {
        clients.push(this);
        return originalConnect.apply(this, args);
      });

    try {
      const source = await startResyncHeldOnGate(gate, (id) => loads[id] || 0);
      const client = clients.find((c) => source.url.includes(`/api/sessions/${c.sessionId}/`));
      await act(async () => {
        client.sendOps([{ op: 'nodes_hidden', node_ids: [] }]);
        await client.flush();
      });
      expect(client.seq).toBe(50);
      expect(client.appliedSeq).toBe(5);

      await act(async () => {
        releaseReload();
      });
      await waitFor(() => {
        expect(useGraphStore.getState().nodes.map((n) => n.id)).toContain('node-b');
      });
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      expect(Object.values(loads)).toEqual([1]);
    } finally {
      releaseReload();
      connectSpy.mockRestore();
      global.fetch.mockImplementation(originalFetch);
      api.getSession.mockImplementation(originalGetSession);
    }
  });

  // Holds back every timeout of the request-timeout length (App's resync
  // guard timer among them) so a test can fire them by hand instead of
  // waiting out the real delay. The ops POST timers share that length and are
  // cleared once their request settles; a cleared timer is dropped here too,
  // so only the timers still scheduled are ever fired.
  function holdRequestTimeouts() {
    const held = new Map();
    let nextId = 0;
    const realSetTimeout = globalThis.setTimeout;
    const realClearTimeout = globalThis.clearTimeout;
    const setSpy = vi.spyOn(globalThis, 'setTimeout').mockImplementation((cb, ms, ...args) => {
      if (ms === DEFAULT_REQUEST_TIMEOUT_MS) {
        nextId -= 1;
        held.set(nextId, cb);
        return nextId;
      }
      return realSetTimeout(cb, ms, ...args);
    });
    const clearSpy = vi.spyOn(globalThis, 'clearTimeout').mockImplementation((id) => {
      if (held.has(id)) held.delete(id);
      else realClearTimeout(id);
    });
    const fire = (ids) =>
      ids.forEach((id) => {
        const cb = held.get(id);
        held.delete(id);
        cb?.();
      });
    return {
      pending: () => [...held.keys()],
      fire,
      fireAll: () => fire([...held.keys()]),
      restore: () => {
        setSpy.mockRestore();
        clearSpy.mockRestore();
      },
    };
  }

  const catchUpMessage = () => ({
    data: JSON.stringify({
      type: 'catch_up',
      seq: 5,
      ops: [{ op: 'nodes_hidden', node_ids: [] }],
      roster: [],
      claims: {},
    }),
  });

  // A reload that never settles must not disable reconnect recovery: once
  // the guard timer fires, the next resync runs. The hung call, settling
  // later, must neither apply its outdated payload nor release the marker
  // the newer call now holds.
  it('a hung resync self-heals on its guard timer and cannot disturb the resync after it', async () => {
    const NODE_C = { id: 'node-c', type: 'Theme', name: 'Theme C' };
    const timeouts = holdRequestTimeouts();
    let releaseHung;
    const hungGate = new Promise((resolve) => {
      releaseHung = resolve;
    });
    let releaseNext;
    const nextGate = new Promise((resolve) => {
      releaseNext = resolve;
    });
    const gate = { active: false };
    const loads = {};
    const originalGetSession = api.getSession.getMockImplementation();
    api.getSession.mockImplementation(async (id, opts) => {
      if (!gate.active) return originalGetSession(id, opts);
      loads[id] = (loads[id] || 0) + 1;
      if (loads[id] === 1) {
        await hungGate;
        return { id, seq: 5, state: {}, resolved: { nodes: [NODE_C], edges: [] } };
      }
      await nextGate;
      return { id, seq: 5, state: {}, resolved: { nodes: [NODE_A, NODE_B], edges: [] } };
    });
    const settle = () =>
      act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });

    try {
      const source = await startResyncHeldOnGate(gate, (id) => loads[id] || 0);
      const totalLoads = () => Object.values(loads).reduce((a, b) => a + b, 0);

      act(() => source.onmessage(catchUpMessage()));
      await settle();
      expect(totalLoads()).toBe(1); // still guarded while the first call hangs

      act(() => timeouts.fireAll());
      act(() => source.onmessage(catchUpMessage()));
      await waitFor(() => expect(totalLoads()).toBe(2));

      await act(async () => {
        releaseHung();
      });
      await settle();
      expect(useGraphStore.getState().nodes.map((n) => n.id)).not.toContain('node-c');

      act(() => source.onmessage(catchUpMessage()));
      await settle();
      expect(totalLoads()).toBe(2); // the newer call still owns the marker

      await act(async () => {
        releaseNext();
      });
      await waitFor(() => {
        expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['node-a', 'node-b']);
      });
    } finally {
      releaseHung();
      releaseNext();
      timeouts.restore();
      api.getSession.mockImplementation(originalGetSession);
    }
  });

  // The token check runs after every fetch, not only the first: a call
  // refetching a payload behind the stream can outlive its guard timer, and
  // a newer resync on the same client then owns the reload. The older call's
  // refetch settling afterwards must not apply over it.
  it('a refetch settling after its guard timer fired does not overwrite the newer resync', async () => {
    const NODE_C = { id: 'node-c', type: 'Theme', name: 'Theme C' };
    const timeouts = holdRequestTimeouts();
    let releaseReload;
    const reloadGate = new Promise((resolve) => {
      releaseReload = resolve;
    });
    let releaseRefetch;
    const refetchGate = new Promise((resolve) => {
      releaseRefetch = resolve;
    });
    const gate = { active: false };
    const loads = {};
    const originalGetSession = api.getSession.getMockImplementation();
    api.getSession.mockImplementation(async (id, opts) => {
      if (!gate.active) return originalGetSession(id, opts);
      loads[id] = (loads[id] || 0) + 1;
      if (loads[id] === 1) {
        await reloadGate;
        return { id, seq: 5, state: {}, resolved: { nodes: [NODE_A], edges: [] } };
      }
      if (loads[id] === 2) {
        await refetchGate;
        return { id, seq: 6, state: {}, resolved: { nodes: [NODE_C], edges: [] } };
      }
      return { id, seq: 6, state: {}, resolved: { nodes: [NODE_A, NODE_B], edges: [] } };
    });

    try {
      const source = await startResyncHeldOnGate(gate, (id) => loads[id] || 0);
      const totalLoads = () => Object.values(loads).reduce((a, b) => a + b, 0);
      act(() => {
        source.onmessage({
          data: JSON.stringify({
            type: 'op',
            seq: 6,
            client_id: 'someone-else',
            op: { op: 'nodes_hidden', node_ids: [] },
          }),
        });
      });
      await act(async () => {
        releaseReload();
      });
      await waitFor(() => expect(totalLoads()).toBe(2));

      expect(timeouts.pending()).toHaveLength(1); // the refetching call's guard timer
      act(() => timeouts.fireAll());
      act(() => source.onmessage(catchUpMessage()));
      await waitFor(() => expect(totalLoads()).toBe(3));
      await waitFor(() => {
        expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['node-a', 'node-b']);
      });

      await act(async () => {
        releaseRefetch();
        await Promise.resolve();
        await Promise.resolve();
      });
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['node-a', 'node-b']);
    } finally {
      releaseReload();
      releaseRefetch();
      timeouts.restore();
      api.getSession.mockImplementation(originalGetSession);
    }
  });

  // A resync superseded by the returning client's own resync no longer owns
  // the in-flight marker: neither its guard timer firing nor its reload
  // settling may release the marker the newer call holds.
  it('a superseded resync leaves the newer resync its in-flight marker', async () => {
    sessionStore.touchSession('5555-6666');
    sessionStore.touchSession('7777-8888');
    FakeEventSource.snapshotSeqByUrl['http://localhost/api/sessions/7777-8888/stream'] = 9;
    const NODE_C = { id: 'node-c', type: 'Theme', name: 'Theme C' };
    const emptyState = { positions: {}, hidden_node_ids: [], hidden_edge_ids: [], annotations: [] };
    const timeouts = holdRequestTimeouts();

    let releaseFirstResync;
    const firstResyncGate = new Promise((resolve) => {
      releaseFirstResync = resolve;
    });
    let releaseReturnResync;
    const returnResyncGate = new Promise((resolve) => {
      releaseReturnResync = resolve;
    });
    let returningLoads = 0;
    const originalGetSession = api.getSession.getMockImplementation();
    api.getSession.mockImplementation(async (id, opts) => {
      if (id !== '7777-8888') return originalGetSession(id, opts);
      returningLoads += 1;
      const call = returningLoads;
      // 1: first load (seq 3) — 2: its first-snapshot resync, held — 3: the
      // reload on returning (seq 3) — 4: the returning client's resync, held.
      if (call === 2) {
        await firstResyncGate;
        return { id, seq: 9, state: emptyState, resolved: { nodes: [NODE_A], edges: [] } };
      }
      if (call === 4) {
        await returnResyncGate;
        return { id, seq: 9, state: emptyState, resolved: { nodes: [NODE_A, NODE_C], edges: [] } };
      }
      return { id, seq: call > 4 ? 9 : 3, state: emptyState, resolved: { nodes: [], edges: [] } };
    });

    try {
      renderApp();
      fireEvent.click(screen.getByTitle('Menu'));
      fireEvent.click(screen.getByText('7777-8888'));
      await waitFor(() => expect(returningLoads).toBe(2));
      const firstVisitTimeouts = timeouts.pending();
      expect(firstVisitTimeouts).toHaveLength(1); // the held resync's guard timer

      fireEvent.click(screen.getByTitle('Menu'));
      fireEvent.click(screen.getByText('5555-6666'));
      await waitFor(() => {
        expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['node-b']);
      });
      fireEvent.click(screen.getByTitle('Menu'));
      fireEvent.click(screen.getByText('7777-8888'));
      await waitFor(() => expect(returningLoads).toBe(4));

      act(() => timeouts.fire(firstVisitTimeouts));
      await act(async () => {
        releaseFirstResync();
        await Promise.resolve();
        await Promise.resolve();
      });

      const returningSource = FakeEventSource.instances
        .filter((es) => es.url.includes('/api/sessions/7777-8888/stream'))
        .at(-1);
      act(() => returningSource.onmessage(catchUpMessage()));
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      expect(returningLoads).toBe(4);

      await act(async () => {
        releaseReturnResync();
      });
      await waitFor(() => {
        expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['node-a', 'node-c']);
      });
    } finally {
      releaseFirstResync();
      releaseReturnResync();
      timeouts.restore();
      api.getSession.mockImplementation(originalGetSession);
    }
  });

  // Same client, superseded mid-replay: the guard timer fired while a slow
  // recovered op's node fetch was pending, and a newer resync has run since.
  // The paused call must stop at its next op instead of applying the rest of
  // its now-stale batch over what the newer resync established.
  it('a resync superseded mid-replay on the same client stops replaying', async () => {
    const timeouts = holdRequestTimeouts();
    let releaseNodeA;
    const nodeAGate = new Promise((resolve) => {
      releaseNodeA = resolve;
    });
    let recovering = true;
    const pendingOps = [
      { op: 'nodes_added', node_ids: ['node-a'] },
      { op: 'nodes_hidden', node_ids: ['ghost-node'] },
    ];
    const getPendingOpsSpy = vi
      .spyOn(SessionSyncClient.prototype, 'getPendingOps')
      .mockImplementation(() => (recovering ? pendingOps : []));
    const originalGetNodeDetails = api.getNodeDetails.getMockImplementation();
    api.getNodeDetails.mockImplementation(async (id) => {
      if (id !== 'node-a') return { success: false };
      await nodeAGate;
      return { node: NODE_A, edges: [] };
    });

    try {
      const { container } = renderApp();
      act(() => {
        useGraphStore.getState().updateVisualization([NODE_A], []);
      });
      const toolbarButtons = container.querySelectorAll('.floating-toolbar-item');
      fireEvent.click(toolbarButtons[toolbarButtons.length - 1]);
      await waitFor(() => screen.getByText('Save View'));
      const source = await waitFor(() => {
        const found = FakeEventSource.instances.find(
          (es) => es.url.includes('/api/sessions/') && es.url.includes('/stream')
        );
        expect(found).toBeTruthy();
        return found;
      });
      const sessionId = source.url.split('/api/sessions/')[1].split('/')[0];
      const loads = () => api.getSession.mock.calls.filter(([id]) => id === sessionId).length;
      const loadsBefore = loads();

      act(() => source.onmessage(catchUpMessage()));
      await waitFor(() => expect(api.getNodeDetails).toHaveBeenCalledWith('node-a'));

      recovering = false;
      act(() => timeouts.fireAll());
      act(() => source.onmessage(catchUpMessage()));
      await waitFor(() => expect(loads()).toBe(loadsBefore + 2));
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });

      await act(async () => {
        releaseNodeA();
        await Promise.resolve();
        await Promise.resolve();
      });
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      expect(useGraphStore.getState().hiddenNodeIds || []).not.toContain('ghost-node');
    } finally {
      releaseNodeA();
      timeouts.restore();
      getPendingOpsSpy.mockRestore();
      api.getNodeDetails.mockImplementation(originalGetNodeDetails);
    }
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
