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

vi.mock('../src/services/api', () => {
  let idCounter = 0;
  return {
    generateVisualizationSessionId: vi.fn(() => `1234-000${++idCounter}`),
    getVisualizationStreamUrl: vi.fn(() => 'http://localhost/stream'),
    updateSessionState: vi.fn(async () => ({ ok: true })),
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

// EventSource is not implemented in jsdom
class FakeEventSource {
  constructor() {}
  close() {}
}
global.EventSource = FakeEventSource;

import App from '../src/App';
import useGraphStore from '../src/store/graphStore';
import { I18nProvider } from '../src/i18n';

const NODE_A = { id: 'node-a', type: 'Actor', name: 'Actor A' };
const NODE_B = { id: 'node-b', type: 'Theme', name: 'Theme B' };

function renderApp() {
  return render(
    <I18nProvider>
      <App />
    </I18nProvider>
  );
}

describe('Session snapshot multiplexing over saveViewSignal', () => {
  beforeEach(() => {
    window.localStorage.clear();
    useGraphStore.getState().clearVisualization();
  });

  it('toolbar Save View still opens the naming dialog through the shared round-trip', async () => {
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
    // The round-trip must not leave a queued callback that would misfire later
    expect(sessionStore.listSessions()).toHaveLength(1); // snapshot also persisted
  });

  it('switching session persists the current canvas first, then restores the target', async () => {
    // Seed a previous session that can be selected from the drawer
    sessionStore.saveSnapshot('5555-6666', {
      nodes: [NODE_B],
      edges: [],
      positions: { 'node-b': { x: 5, y: 6 } },
      parentIds: {},
      groups: [],
      hiddenNodeIds: [],
      hiddenEdgeIds: [],
      savedAt: Date.now(),
    });

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

    // The old (auto-generated 1234-000N) session was snapshotted before the swap
    const oldSession = sessionStore.listSessions().find(s => s.id.startsWith('1234-'));
    expect(oldSession).toBeTruthy();
    const oldSnapshot = sessionStore.getSnapshot(oldSession.id);
    expect(oldSnapshot.nodes.map(n => n.id)).toEqual(['node-a']);
    expect(oldSnapshot.positions['node-a']).toEqual({ x: 11, y: 22 });

    // The restored node carries its saved position from the target snapshot
    expect(useGraphStore.getState().nodes[0]._savedPosition).toEqual({ x: 5, y: 6 });
  });
});
