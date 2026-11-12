// covers: frontend/web/src/App.jsx
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

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

// The api mock the latest App import was given (a fresh App gets fresh mocks).
const apiMocks = vi.hoisted(() => ({ latest: null }));
vi.mock('../src/services/api', () => {
  let idCounter = 0;
  apiMocks.latest = {
    generateVisualizationSessionId: vi.fn(() => `1234-000${++idCounter}`),
    getVisualizationStreamUrl: vi.fn(() => 'http://localhost/stream'),
    getSessionStreamUrl: vi.fn((id) => `http://localhost/api/sessions/${id}/stream`),
    getSessionOpsUrl: vi.fn((id) => `http://localhost/api/sessions/${id}/ops`),
    getClientId: vi.fn(() => 'client-test'),
    setActiveSessionForRequests: vi.fn(),
    setSessionStaged: vi.fn(),
    setSessionModeUnknown: vi.fn(),
    isActiveSessionPossiblyStaged: vi.fn(() => false),
    setSessionModeDirect: vi.fn(),
    setSessionOnServer: vi.fn(),
    setStagingAvailable: vi.fn(),
    subscribeStagedMarks: vi.fn(() => () => {}),
    setMessageTranslator: vi.fn(),
    getStagedSessionBinding: vi.fn(() => null),
    getStagedChanges: vi.fn(async () => ({ items: [], count: 0 })),
    getDisplayName: vi.fn(() => null),
    listServerSessions: vi.fn(async () => ({ sessions: [] })),
    renameServerSession: vi.fn(async () => ({})),
    copySession: vi.fn(),
    deleteServerSession: vi.fn(async () => ({ deleted: true })),
    createSession: vi.fn(async () => ({ id: '9999-0001', write_mode: 'staged' })),
    setSessionWriteMode: vi.fn(async (id) => ({ id, write_mode: 'staged' })),
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
  return apiMocks.latest;
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

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it.each([
    [413, 'Could not copy session: the source exceeds the session size limit.'],
    [403, 'Could not copy session'],
    [undefined, 'Could not copy session'],
  ])(
    'explains a copy refusal with status %s without switching sessions',
    async (status, message) => {
      api.copySession.mockRejectedValueOnce(Object.assign(new Error('server detail'), { status }));
      sessionStore.touchSession('7777-8888');
      renderApp();
      await waitFor(() => expect(api.setActiveSessionForRequests).toHaveBeenCalled());
      const activeSession = api.setActiveSessionForRequests.mock.calls.at(-1)[0];
      const sessionsBefore = sessionStore.listSessions().map(({ id }) => id);
      fireEvent.click(screen.getByTitle('Menu'));
      const label = await screen.findByText('7777-8888');
      let row = label;
      while (!row.querySelector('[aria-label="Session actions"]')) row = row.parentElement;
      fireEvent.click(row.querySelector('[aria-label="Session actions"]'));
      fireEvent.click(screen.getByRole('menuitem', { name: 'Create a copy' }));

      expect(await screen.findByText(message)).toBeInTheDocument();
      expect(api.copySession).toHaveBeenCalledTimes(1);
      expect(api.copySession).toHaveBeenCalledWith('7777-8888');
      expect(api.setActiveSessionForRequests).toHaveBeenLastCalledWith(activeSession);
      expect(sessionStore.listSessions().map(({ id }) => id)).toEqual(sessionsBefore);
      expect(screen.queryByText('server detail')).not.toBeInTheDocument();
    }
  );
});
