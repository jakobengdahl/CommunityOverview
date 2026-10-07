import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';

/**
 * App's half of teleport: the handler it hands the canvas and the shells, and
 * the arrival effect that consumes a route this deployment hands out.
 *
 * Neither had any coverage. Every component test mocks `api.resolveTeleport` or
 * injects a resolver, so the whole inbound effect — the `?view=` gate, the `?q=`
 * prefill, the `?from_graph=` notice — and the prop wiring could be deleted
 * outright with the suite still green. Modelled on appHandlerWiring.test.jsx,
 * in its own file because the arrival cases drive `window.location.search` and
 * call `getSavedView`/`getNodeDetails` during startup.
 */
const captured = vi.hoisted(() => ({ canvas: null }));

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
    setTimeout(
      () =>
        this.onmessage?.({
          data: JSON.stringify({ type: 'snapshot', seq: 0, session: { state: {} } }),
        }),
      0
    );
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

const store = () => useGraphStore.getState();
const REMOTE_NODE = { id: 'remote-1', type: 'Actor', name: 'External Node' };

function setSearch(search) {
  window.history.replaceState({}, '', `/${search}`);
}

async function renderApp() {
  render(
    <I18nProvider>
      <App />
    </I18nProvider>
  );
  await waitFor(() => expect(api.getGraphStats).toHaveBeenCalled());
  await waitFor(() => expect(store().stats).toBeTruthy());
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

describe('App teleport wiring', () => {
  beforeEach(() => {
    window.localStorage.clear();
    store().clearVisualization();
    store().clearFocusNode();
    store().clearGuideSearchInput();
    captured.canvas = null;
    vi.clearAllMocks();
    vi.spyOn(console, 'error').mockImplementation(() => {});
    setSearch('');
  });

  afterEach(() => {
    setSearch('');
    vi.restoreAllMocks();
  });

  describe('the outbound handler reaches the canvas', () => {
    it('hands the canvas a teleport handler', async () => {
      await renderApp();

      expect(typeof captured.canvas.onTeleportToSourceGraph).toBe('function');
    });

    it('gives the canvas the translated menu labels rather than key names', async () => {
      await renderApp();

      const labels = captured.canvas.contextMenuLabels;
      expect(labels.openInSourceGraph).toBe(en.context_menu.open_in_source_graph);
      expect(labels.openInSourceGraphTooltip).toBe(en.federation.teleport_action_tooltip);
      expect(labels.openInSourceGraph).not.toContain('context_menu.');
      expect(labels.openInSourceGraphTooltip).not.toContain('federation.');
    });

    it('resolves through the API with the live session and the given query', async () => {
      await renderApp();

      await act(async () => {
        await captured.canvas.onTeleportToSourceGraph(
          'federated::esam-main::remote-1',
          {},
          {
            searchQuery: 'external',
          }
        );
      });

      expect(api.resolveTeleport).toHaveBeenCalledTimes(1);
      const [nodeId, options] = api.resolveTeleport.mock.calls[0];
      expect(nodeId).toBe('federated::esam-main::remote-1');
      expect(options.searchQuery).toBe('external');
      expect(options.sessionId).toBeTruthy();
    });
  });

  describe('arriving from a teleport', () => {
    it('focuses the node named in the link', async () => {
      api.getNodeDetails.mockResolvedValue({
        success: true,
        node: REMOTE_NODE,
        edges: [],
      });
      setSearch('?node=remote-1');

      await renderApp();

      await waitFor(() => expect(api.getNodeDetails).toHaveBeenCalledWith('remote-1'));
      await waitFor(() => expect(store().focusNodeId).toBe('remote-1'));
      expect(store().nodes.map((n) => n.id)).toContain('remote-1');
    });

    it('puts the carried search text back in the search box', async () => {
      // Asserted on the box rather than the store: FloatingSearch consumes
      // guideSearchInput and clears it immediately, so the store value is
      // transient and what the user sees is the real outcome.
      api.getNodeDetails.mockResolvedValue({ success: true, node: REMOTE_NODE, edges: [] });
      setSearch('?node=remote-1&q=external');

      await renderApp();

      await waitFor(() =>
        expect(screen.getByPlaceholderText(en.floating_search.placeholder).value).toBe('external')
      );
    });

    it('leaves the search box alone when the link carries no search text', async () => {
      api.getNodeDetails.mockResolvedValue({ success: true, node: REMOTE_NODE, edges: [] });
      setSearch('?node=remote-1');

      await renderApp();

      await waitFor(() => expect(store().focusNodeId).toBe('remote-1'));
      expect(screen.getByPlaceholderText(en.floating_search.placeholder).value).toBe('');
      expect(store().guideSearchInput).toBeNull();
    });

    it('says which graph the visitor came from', async () => {
      api.getNodeDetails.mockResolvedValue({ success: true, node: REMOTE_NODE, edges: [] });
      setSearch('?node=remote-1&from_graph=eSam');

      await renderApp();

      await waitFor(() =>
        expect(
          screen.getByText(en.federation.teleport_returned_from.replace('{graph}', 'eSam'))
        ).toBeInTheDocument()
      );
    });

    it('says nothing about a sender the link does not name', async () => {
      api.getNodeDetails.mockResolvedValue({ success: true, node: REMOTE_NODE, edges: [] });
      setSearch('?node=remote-1');

      await renderApp();

      await waitFor(() => expect(store().focusNodeId).toBe('remote-1'));
      expect(screen.queryByText(/Opened from/i)).toBeNull();
    });

    it('does nothing at all without a node parameter', async () => {
      // q alone is not an arrival; nothing should be fetched or prefilled.
      setSearch('?q=external');

      await renderApp();

      expect(api.getNodeDetails).not.toHaveBeenCalled();
      expect(screen.getByPlaceholderText(en.floating_search.placeholder).value).toBe('');
    });

    it('reports a link whose node is not in this graph', async () => {
      api.getNodeDetails.mockResolvedValue({ success: false });
      setSearch('?node=missing');

      await renderApp();

      await waitFor(() => expect(api.getNodeDetails).toHaveBeenCalledWith('missing'));
      expect(store().focusNodeId).toBeNull();
    });
  });

  describe('the saved-view gate', () => {
    it('focuses the node only once the saved-view load has settled', async () => {
      // A gui_url may legitimately carry both parameters, and the saved-view
      // path clears the canvas after its own await — so focusing first would
      // have the node wiped out from under the focus.
      let releaseView;
      api.getSavedView.mockImplementation(() => new Promise((resolve) => (releaseView = resolve)));
      api.getNodeDetails.mockResolvedValue({ success: true, node: REMOTE_NODE, edges: [] });
      setSearch('?view=Overview&node=remote-1');

      await renderApp();

      expect(api.getNodeDetails).not.toHaveBeenCalled();

      await act(async () => {
        releaseView({ success: true, nodes: [], edges: [] });
        await new Promise((resolve) => setTimeout(resolve, 0));
      });

      await waitFor(() => expect(api.getNodeDetails).toHaveBeenCalledWith('remote-1'));
      await waitFor(() => expect(store().focusNodeId).toBe('remote-1'));
    });

    it('still focuses the node when the saved-view load fails', async () => {
      // The gate must not strand the arrival on an error path.
      api.getSavedView.mockRejectedValue(new Error('view blew up'));
      api.getNodeDetails.mockResolvedValue({ success: true, node: REMOTE_NODE, edges: [] });
      setSearch('?view=Overview&node=remote-1');

      await renderApp();

      await waitFor(() => expect(api.getNodeDetails).toHaveBeenCalledWith('remote-1'));
      await waitFor(() => expect(store().focusNodeId).toBe('remote-1'));
    });

    it('still focuses the node when the saved view does not exist', async () => {
      api.getSavedView.mockResolvedValue({ success: false });
      api.getNodeDetails.mockResolvedValue({ success: true, node: REMOTE_NODE, edges: [] });
      setSearch('?view=Missing&node=remote-1');

      await renderApp();

      await waitFor(() => expect(api.getNodeDetails).toHaveBeenCalledWith('remote-1'));
      await waitFor(() => expect(store().focusNodeId).toBe('remote-1'));
    });

    it('does not wait when there is no saved view to wait for', async () => {
      api.getNodeDetails.mockResolvedValue({ success: true, node: REMOTE_NODE, edges: [] });
      setSearch('?node=remote-1');

      await renderApp();

      expect(api.getSavedView).not.toHaveBeenCalled();
      await waitFor(() => expect(store().focusNodeId).toBe('remote-1'));
    });
  });
});
