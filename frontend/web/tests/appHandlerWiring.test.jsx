import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, waitFor, act } from '@testing-library/react';

// The session-scoped helpers App delegates to are tested on their own in
// appAwaitingHandlerEpochGuards.test.js. What those tests cannot see is App's
// side of each call: which API function, store action and callback it hands
// over. So this file renders App with the canvas and the dialog stack stubbed
// to record the handlers App gives them, invokes those handlers the way the
// real components would, and checks what reached the API and the store.
const captured = vi.hoisted(() => ({ canvas: null, dialogs: null }));

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
  default: function AppDialogsStub(props) {
    captured.dialogs = props;
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

const NODE_A = { id: 'a', type: 'Actor', name: 'A' };
const NODE_B = { id: 'b', type: 'Actor', name: 'B' };
const EDGE_1 = { id: 'e1', source: 'a', target: 'b', type: 'RELATES_TO', label: 'old' };

const store = () => useGraphStore.getState();
const nodeIds = () => store().nodes.map((n) => n.id);
const findNode = (id) => store().nodes.find((n) => n.id === id);
const findEdge = (id) => store().edges.find((e) => e.id === id);

// Handlers are recreated when their dependencies change, so always call the
// ones from App's latest render, as the real canvas and dialogs would.
const canvas = () => captured.canvas;
const dialogs = () => captured.dialogs;

let startupStats = null;

async function renderApp() {
  render(
    <I18nProvider>
      <App />
    </I18nProvider>
  );
  // Let the startup work settle before seeding the canvas, so nothing it
  // does afterwards replaces the seed. The config load applies schema, stats
  // and capabilities in one synchronous block, and the mock returns a fresh
  // object per call, so seeing this render's stats object in the store proves
  // that block ran rather than that App mounted.
  await waitFor(() => expect(api.getGraphStats).toHaveBeenCalled());
  startupStats = await api.getGraphStats.mock.results[0].value;
  await waitFor(() => expect(store().stats).toBe(startupStats));
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
  // The barrier above keys on the first run's stats. A second startup run
  // (a dependency change, or the error path refetching stats) could land after
  // the seed and replace it, so require that startup settled on exactly one.
  expect(api.getGraphStats).toHaveBeenCalledTimes(1);
  expect(store().stats).toBe(startupStats);
  await act(async () => {
    store().updateVisualization([NODE_A, NODE_B], [EDGE_1]);
  });
  expect(nodeIds()).toEqual(['a', 'b']);
}

describe('App handler wiring', () => {
  beforeEach(() => {
    window.localStorage.clear();
    store().clearVisualization();
    store().setEditingNode(null);
    store().setEditingEdge(null);
    captured.canvas = null;
    captured.dialogs = null;
    startupStats = null;
    vi.clearAllMocks();
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(async () => {
    // renderApp's check runs once, right after startup settles; a second
    // startup run that begins later would land mid-test and replace the seed.
    // Only App's config load fetches stats, so give any such late run time to
    // start, then require that the test ended on the one startup run.
    try {
      if (startupStats) {
        await act(async () => {
          await new Promise((resolve) => setTimeout(resolve, 50));
        });
        expect(api.getGraphStats).toHaveBeenCalledTimes(1);
        expect(store().stats).toBe(startupStats);
      }
    } finally {
      vi.restoreAllMocks();
    }
  });

  it('handleEdit opens the agent editor with the subscription fetched from the API', async () => {
    const subscription = { id: 'sub', type: 'EventSubscription', name: 'Sub' };
    await renderApp();
    api.getNodeDetails.mockResolvedValueOnce({ success: true, node: subscription });
    const agent = { id: 'ag', type: 'Agent', name: 'Ag', metadata: { subscription_id: 'sub' } };

    await act(async () => {
      await canvas().onEdit('ag', agent);
    });

    expect(api.getNodeDetails).toHaveBeenCalledWith('sub');
    expect(dialogs().dialogs.showAgentDialog).toBe(true);
    expect(dialogs().dialogs.editingAgentData).toEqual({ agent, subscription });
  });

  it('handleNodeUpdate persists the edit, patches the canvas and closes the node editor', async () => {
    await renderApp();
    await act(async () => {
      await canvas().onEdit('a', NODE_A);
    });
    expect(store().editingNode).toEqual({ id: 'a', data: NODE_A });

    await act(async () => {
      await dialogs().onNodeUpdate('a', { name: 'A2' });
    });

    expect(api.updateNode).toHaveBeenCalledWith('a', { name: 'A2' });
    expect(findNode('a').name).toBe('A2');
    expect(store().editingNode).toBeNull();
  });

  it('handleSaveSubscription closes the subscription editor once the update lands', async () => {
    await renderApp();
    const subscription = { id: 'sub', type: 'EventSubscription', name: 'Sub' };
    await act(async () => {
      await canvas().onEdit('sub', subscription);
    });
    expect(dialogs().dialogs.editingSubscriptionData).toEqual(subscription);

    await act(async () => {
      await dialogs().onSaveSubscription({ id: 'sub', updates: { name: 'Sub2' } });
    });

    expect(api.updateNode).toHaveBeenCalledWith('sub', { name: 'Sub2' });
    expect(dialogs().dialogs.editingSubscriptionData).toBeNull();
  });

  it('handleSaveAgent persists both the agent and its subscription on an update', async () => {
    await renderApp();

    await act(async () => {
      await dialogs().onSaveAgent({
        agentId: 'ag',
        agentUpdates: { name: 'Ag2' },
        subscriptionId: 'sub',
        subscriptionUpdates: { name: 'Sub2' },
      });
    });

    expect(api.updateNode.mock.calls).toEqual([
      ['ag', { name: 'Ag2' }],
      ['sub', { name: 'Sub2' }],
    ]);
  });

  it.each([
    ['onSaveSkill', { type: 'Skill', name: 'New skill' }],
    ['onSaveSubscription', { type: 'EventSubscription', name: 'New sub' }],
    ['onSaveAKC', { type: 'ActiveKnowledgeCollection', name: 'New collection' }],
  ])('%s draws the created node under the id the server assigned', async (handler, data) => {
    await renderApp();
    api.addNodes.mockResolvedValueOnce({ success: true, added_node_ids: ['created-1'] });

    await act(async () => {
      await dialogs()[handler](data);
    });

    expect(api.addNodes).toHaveBeenCalledWith([data], []);
    expect(findNode('created-1')).toMatchObject({ type: data.type, name: data.name });
  });

  it('handleEdgeUpdate persists the edge edit and applies it to the canvas edge', async () => {
    await renderApp();
    await act(async () => {
      canvas().onEditEdge('e1', {});
    });
    expect(store().editingEdge).toMatchObject({ id: 'e1' });

    await act(async () => {
      await dialogs().onEdgeUpdate({ label: 'new' });
    });

    expect(api.updateEdge).toHaveBeenCalledWith('e1', { label: 'new' });
    expect(findEdge('e1').label).toBe('new');
    expect(store().editingEdge).toBeNull();
  });

  it('handleExpand fetches the neighbours of the given node and draws them', async () => {
    await renderApp();
    const neighbour = { id: 'n1', type: 'Actor', name: 'N1' };
    api.getRelatedNodes.mockResolvedValueOnce({
      nodes: [neighbour],
      edges: [{ id: 'en', source: 'b', target: 'n1', type: 'RELATES_TO' }],
    });

    await act(async () => {
      await canvas().onExpand('b');
    });

    expect(api.getRelatedNodes).toHaveBeenCalledWith('b', { depth: 1 });
    expect(nodeIds()).toContain('n1');
    expect(findEdge('en')).toBeTruthy();
  });

  it('handleConnect creates the edge between the dragged endpoints and draws it', async () => {
    await renderApp();
    api.addEdge.mockResolvedValueOnce({
      success: true,
      edge: { id: 'e2', source: 'b', target: 'a', type: 'RELATES_TO' },
    });

    await act(async () => {
      await canvas().onConnect({ source: 'b', target: 'a' });
    });

    expect(api.addEdge).toHaveBeenCalledWith('b', 'a');
    expect(findEdge('e2')).toMatchObject({ source: 'b', target: 'a' });
  });

  it('handleDeleteEdge deletes the given edge in the API and on the canvas', async () => {
    await renderApp();

    await act(async () => {
      await canvas().onDeleteEdge('e1');
    });

    expect(api.deleteEdge).toHaveBeenCalledWith('e1');
    expect(findEdge('e1')).toBeUndefined();
  });

  it('handleSetEdgeType retypes the given edge in the API and on the canvas', async () => {
    await renderApp();

    await act(async () => {
      await canvas().onSetEdgeType('e1', 'OWNS');
    });

    expect(api.updateEdge).toHaveBeenCalledWith('e1', { type: 'OWNS' });
    expect(findEdge('e1').type).toBe('OWNS');
  });

  it('double-clicking a saved view loads its nodes from the API into the canvas', async () => {
    await renderApp();
    // Once-only, so the stub cannot outlive this test whatever the runner's
    // mock-restore semantics or test order.
    const savedViewNode = async (id) => ({
      success: true,
      node: { id, type: 'Actor', name: id.toUpperCase() },
      edges: [],
    });
    api.getNodeDetails.mockImplementationOnce(savedViewNode).mockImplementationOnce(savedViewNode);
    const view = { type: 'SavedView', name: 'View', metadata: { node_ids: ['v1', 'v2'] } };

    await act(async () => {
      await canvas().onNodeDoubleClick('view-1', view);
    });

    expect(api.getNodeDetails).toHaveBeenCalledWith('v1');
    expect(api.getNodeDetails).toHaveBeenCalledWith('v2');
    expect(nodeIds()).toEqual(['v1', 'v2']);
  });
});
