import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, cleanup, act } from '@testing-library/react';
import { StrictMode } from 'react';

// App captures URL session parameters at module load, before the first render.
vi.hoisted(() => {
  window.history.replaceState({}, '', '/?session=1111-2222&view=Overview&node=focus-node');
});
const captured = vi.hoisted(() => ({ canvas: null, shell: null }));

vi.mock('../src/components/DesktopShell', () => ({
  default: (props) => {
    captured.shell = props;
    return null;
  },
}));

vi.mock('@community-graph/ui-graph-canvas', async (importOriginal) => {
  const actual = await importOriginal();
  function GraphCanvas(props) {
    captured.canvas = props;
    return <div data-testid="graph-canvas-stub" />;
  }
  return { ...actual, GraphCanvas, positionNewNodes: (newNodes) => newNodes };
});
vi.mock('@community-graph/ui-graph-canvas/styles', () => ({}));

vi.mock('../src/components/AppDialogs', () => ({
  default: function AppDialogsStub() {
    return null;
  },
}));

vi.mock('../src/services/api', () => {
  let idCounter = 0;
  return {
    generateVisualizationSessionId: vi.fn(() => `4321-000${++idCounter}`),
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
    deleteServerSession: vi.fn(async () => ({ deleted: true })),
    getSession: vi.fn(async (id) => ({
      id,
      state: {},
      resolved: { nodes: [], edges: [] },
      roster: [],
    })),
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
    resolveTeleport: vi.fn(async () => ({ success: true, status: 'local' })),
  };
});

class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.onmessage = null;
    this.onerror = null;
  }
  close() {}
}
global.EventSource = FakeEventSource;
global.fetch = vi.fn(async () => ({
  ok: true,
  status: 200,
  json: async () => ({ applied: [], seq: 1 }),
}));

import App from '../src/App';
import * as api from '../src/services/api';
import useGraphStore from '../src/store/graphStore';
import { I18nProvider } from '../src/i18n';
import en from '../src/i18n/en.json';
import { DEFAULT_REQUEST_TIMEOUT_MS } from '../src/services/sessionSyncClient';

const store = () => useGraphStore.getState();
const payload = (id) => ({
  state: {},
  resolved: { nodes: [{ id, type: 'Actor', name: id }], edges: [] },
});
const view = { success: true, nodes: [{ id: 'view-node', type: 'Actor' }], edges: [] };

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

async function renderApp({ strict = false } = {}) {
  const app = (
    <I18nProvider>
      <App />
    </I18nProvider>
  );
  await act(async () => {
    render(strict ? <StrictMode>{app}</StrictMode> : app);
  });
}

describe('App URL session bootstrap', () => {
  let bootstrap;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    window.localStorage.clear();
    window.history.replaceState({}, '', '/?session=1111-2222&view=Overview&node=focus-node');
    store().clearVisualization();
    store().clearFocusNode();
    bootstrap = deferred();
    api.getSession.mockImplementation((id) =>
      id === '1111-2222' ? bootstrap.promise : Promise.resolve(payload('new-session-node'))
    );
    api.getSavedView.mockResolvedValue(view);
    api.getNodeDetails.mockResolvedValue({
      success: true,
      node: { id: 'focus-node', type: 'Actor' },
      edges: [],
    });
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it.each(['success', '404', '500'])(
    'ignores a late bootstrap %s after another session has loaded',
    async (outcome) => {
      api.getNodeDetails.mockReturnValue(new Promise(() => {}));
      await renderApp();
      await act(async () => {
        captured.shell.onSelectSession('3333-4444');
      });
      await act(async () => {
        captured.canvas.onSaveView({ nodes: [], edges: [], groups: [] });
      });
      expect(captured.canvas.sessionKey).toBe('3333-4444');
      act(() => {
        store().addChatMessage({ id: 'new-chat', role: 'user', content: 'keep this' });
        store().setSelectedNodeId('new-session-node');
      });
      const epoch = store().sessionEpoch;
      await act(async () => {
        if (outcome === 'success') bootstrap.resolve(payload('old-session-node'));
        else
          bootstrap.reject(Object.assign(new Error('late failure'), { status: Number(outcome) }));
      });
      expect(store().nodes.map((n) => n.id)).toEqual(['new-session-node']);
      expect(store().sessionEpoch).toBe(epoch);
      expect(store().selectedNodeId).toBe('new-session-node');
      expect(store().chatMessages.some((m) => m.id === 'new-chat')).toBe(true);
      expect(api.getSavedView).not.toHaveBeenCalled();
      expect(screen.queryByText(en.sessions.link_not_found)).toBeNull();
      expect(console.error).not.toHaveBeenCalled();
    }
  );

  it('applies the view and node focus after a successful bootstrap', async () => {
    await renderApp();
    expect(api.getSavedView).not.toHaveBeenCalled();
    await act(async () => {
      bootstrap.resolve(payload('session-node'));
    });
    expect(api.getSavedView).toHaveBeenCalledWith('Overview');
    expect(store().nodes.map((n) => n.id)).toEqual(['view-node', 'focus-node']);
    expect(store().focusNodeId).toBe('focus-node');
  });

  it('releases the original bootstrap waiter during StrictMode cleanup', async () => {
    // Make the saved-view effect start awaiting the first bootstrap before
    // StrictMode cleans it up and creates the second bootstrap promise.
    store().setStats({ total_nodes: 0, total_edges: 0 });
    await renderApp({ strict: true });
    expect(api.getSession).toHaveBeenCalledTimes(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(DEFAULT_REQUEST_TIMEOUT_MS);
    });
    expect(api.getSavedView).toHaveBeenCalledWith('Overview');
    expect(store().nodes.map((n) => n.id)).toEqual(['view-node', 'focus-node']);
    expect(store().focusNodeId).toBe('focus-node');
    await act(async () => {
      bootstrap.resolve(payload('old-session-node'));
    });
    expect(store().nodes.map((n) => n.id)).toEqual(['view-node', 'focus-node']);
    expect(store().focusNodeId).toBe('focus-node');
  });

  it.each(['success', '404', '500'])(
    'bounds a hung bootstrap and ignores its late %s after view and focus',
    async (outcome) => {
      await renderApp();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(DEFAULT_REQUEST_TIMEOUT_MS - 1);
      });
      expect(api.getSavedView).not.toHaveBeenCalled();
      expect(api.getNodeDetails).not.toHaveBeenCalled();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
      });
      expect(api.getSavedView).toHaveBeenCalledWith('Overview');
      expect(store().nodes.map((n) => n.id)).toEqual(['view-node', 'focus-node']);
      expect(store().focusNodeId).toBe('focus-node');
      const epoch = store().sessionEpoch;
      await act(async () => {
        if (outcome === 'success') bootstrap.resolve(payload('old-session-node'));
        else
          bootstrap.reject(Object.assign(new Error('late failure'), { status: Number(outcome) }));
      });
      expect(store().nodes.map((n) => n.id)).toEqual(['view-node', 'focus-node']);
      expect(store().focusNodeId).toBe('focus-node');
      expect(store().sessionEpoch).toBe(epoch);
      expect(screen.queryByText(en.sessions.link_not_found)).toBeNull();
      expect(console.error).not.toHaveBeenCalled();
    }
  );

  it.each(['node move', 'annotation edit'])(
    'preserves a canvas-local %s made during the view fetch',
    async (edit) => {
      const savedView = deferred();
      api.getSavedView.mockReturnValue(savedView.promise);
      await renderApp();
      await act(async () => {
        bootstrap.resolve(payload('session-node'));
      });
      expect(api.getSavedView).toHaveBeenCalledWith('Overview');
      act(() => {
        if (edit === 'node move')
          captured.canvas.onNodePositionChange('session-node', { x: 10, y: 20 });
        else captured.canvas.onAnnotationChange('style');
      });
      await act(async () => {
        savedView.resolve(view);
      });
      expect(store().nodes.map((n) => n.id)).toEqual(['session-node', 'focus-node']);
      expect(store().focusNodeId).toBe('focus-node');
    }
  );
});
