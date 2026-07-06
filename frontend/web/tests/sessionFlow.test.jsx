import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';

import * as sessionStore from '../src/services/sessionStore';

// GraphCanvas stub: replays the saveViewSignal round-trip that App's session
// snapshot mechanism is multiplexed over, without rendering ReactFlow.
vi.mock('@community-graph/ui-graph-canvas', async () => {
  const { useEffect } = await import('react');
  function GraphCanvas({ nodes = [], edges = [], saveViewSignal = 0, onSaveView }) {
    useEffect(() => {
      if (saveViewSignal > 0 && onSaveView) {
        onSaveView({
          nodes: nodes.map(n => ({ id: n.id, position: { x: 11, y: 22 }, parentId: undefined })),
          edges: edges.map(e => ({ id: e.id, source: e.source, target: e.target, label: e.label })),
          groups: [],
        });
      }
    }, [saveViewSignal, onSaveView]); // eslint-disable-line react-hooks/exhaustive-deps
    return <div data-testid="graph-canvas-stub" />;
  }
  return {
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
          state: { positions: { 'node-b': { x: 5, y: 6 } }, hidden_node_ids: [], hidden_edge_ids: [], annotations: [] },
          resolved: { nodes: [NODE_B], edges: [] },
          roster: [],
        };
      }
      return { id, state: {}, resolved: { nodes: [], edges: [] }, roster: [] };
    }),
    getSchema: vi.fn(async () => ({ node_types: {} })),
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
      this.onmessage?.({ data: JSON.stringify({ type: 'snapshot', seq: 0, session: { state: {} } }) });
    }, 0);
  }
  close() {}
}
FakeEventSource.instances = [];
global.EventSource = FakeEventSource;

// The sync client posts op batches with global fetch; capture them.
global.fetch = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ applied: [], seq: 1 }) }));

import App from '../src/App';
import * as api from '../src/services/api';
import useGraphStore from '../src/store/graphStore';
import { I18nProvider } from '../src/i18n';

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
    try { return JSON.parse(opts.body).ops || []; } catch { return []; }
  });
}

describe('Server-backed session lifecycle', () => {
  beforeEach(() => {
    window.localStorage.clear();
    useGraphStore.getState().clearVisualization();
    FakeEventSource.instances = [];
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
      expect(useGraphStore.getState().nodes.map(n => n.id)).toEqual(['node-b']);
    });

    // The target was loaded resolved from the server, carrying its saved position
    expect(api.getSession).toHaveBeenCalledWith('5555-6666', { resolve: true });
    expect(useGraphStore.getState().nodes[0]._savedPosition).toEqual({ x: 5, y: 6 });
  });
});
