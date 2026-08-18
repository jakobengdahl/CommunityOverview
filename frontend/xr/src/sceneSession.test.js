import { describe, expect, it, vi } from 'vitest';
import { SceneSession, isValidSessionId } from './sceneSession.js';
import { renderableNodes } from './sceneModel.js';

// Stands in for SessionSyncClient: the tests drive its handlers directly, which
// is exactly what the real client does when the SSE stream delivers an event.
class FakeClient {
  constructor(opts) {
    Object.assign(this, opts);
    this.connected = false;
    this.closed = false;
    this.posted = [];
  }
  connect() {
    this.connected = true;
  }
  close() {
    this.closed = true;
  }
}

function payload(nodes, positions, extra = {}) {
  return {
    id: '1111-2222-3333-4444',
    name: 'Demo',
    state: { node_refs: nodes.map((n) => n.id), positions, hidden_node_ids: [], ...extra },
    resolved: { nodes, edges: [] },
  };
}

function makeSession({ loadSession, loadNodeDetails = vi.fn() } = {}) {
  let client;
  const onChange = vi.fn();
  const session = new SceneSession({
    sessionId: '1111-2222-3333-4444',
    clientId: 'client-a',
    streamUrl: '/api/sessions/1111-2222-3333-4444/stream',
    opsUrl: '/api/sessions/1111-2222-3333-4444/ops',
    loadSession,
    loadNodeDetails,
    onChange,
    createClient: (opts) => {
      client = new FakeClient(opts);
      return client;
    },
  });
  return { session, onChange, client: () => client, loadNodeDetails };
}

const twoNodes = payload(
  [
    { id: 'n1', name: 'Alpha', type: 'Actor' },
    { id: 'n2', name: 'Beta', type: 'Goal' },
  ],
  { n1: { x: 0, y: 0 }, n2: { x: 10, y: 10 } }
);

describe('isValidSessionId', () => {
  it('accepts the four-group and legacy two-group forms and rejects the rest', () => {
    expect(isValidSessionId('1111-2222-3333-4444')).toBe(true);
    expect(isValidSessionId('1111-2222')).toBe(true);
    expect(isValidSessionId(' 1111-2222-3333-4444 ')).toBe(true);
    expect(isValidSessionId('1111-2222-3333')).toBe(false);
    expect(isValidSessionId('abcd-2222-3333-4444')).toBe(false);
    expect(isValidSessionId(null)).toBe(false);
  });
});

describe('SceneSession', () => {
  it('passes the session identity through to the sync client and opens the stream', () => {
    const { session, client } = makeSession({ loadSession: vi.fn().mockResolvedValue(twoNodes) });
    session.connect();
    expect(client().sessionId).toBe('1111-2222-3333-4444');
    expect(client().streamUrl).toBe('/api/sessions/1111-2222-3333-4444/stream');
    expect(client().connected).toBe(true);
    expect(session.getState().sessionId).toBe('1111-2222-3333-4444');
    session.close();
    expect(client().closed).toBe(true);
  });

  it('loads the resolved session when the stream reports ready', async () => {
    const loadSession = vi.fn().mockResolvedValue(twoNodes);
    const { session, client, onChange } = makeSession({ loadSession });
    session.connect();
    await client().handlers.onReady(1);

    expect(loadSession).toHaveBeenCalledWith('1111-2222-3333-4444', { resolve: true });
    const { scene, status } = session.getState();
    expect(status).toBe('connected');
    expect(renderableNodes(scene).map((n) => n.id)).toEqual(['n1', 'n2']);
    expect(onChange).toHaveBeenCalled();
  });

  it('surfaces a failed load instead of showing an empty scene as connected', async () => {
    const loadSession = vi.fn().mockRejectedValue(new Error('session not found'));
    const { session, client } = makeSession({ loadSession });
    session.connect();
    await client().handlers.onReady(1);

    expect(session.getState().status).toBe('error');
    expect(session.getState().error).toBe('session not found');
  });

  it('reduces a remote op batch onto the loaded scene', async () => {
    const { session, client } = makeSession({
      loadSession: vi.fn().mockResolvedValue(twoNodes),
      loadNodeDetails: vi
        .fn()
        .mockResolvedValue({ node: { id: 'n3', name: 'Gamma', type: 'Risk' } }),
    });
    session.connect();
    await client().handlers.onReady(1);

    client().handlers.onRemoteOps([
      { op: 'nodes_added', node_ids: ['n3'] },
      { op: 'node_moved', node_id: 'n3', position: { x: 20, y: 20 } },
      { op: 'nodes_hidden', node_ids: ['n1'] },
    ]);
    await vi.waitFor(() => expect(session.getState().scene.nodes.n3.hydrated).toBe(true));

    const rendered = renderableNodes(session.getState().scene);
    expect(rendered.map((n) => n.id)).toEqual(['n2', 'n3']);
    expect(rendered.find((n) => n.id === 'n3').name).toBe('Gamma');
  });

  it('hydrates each newly added node exactly once, even across later op batches', async () => {
    const loadNodeDetails = vi
      .fn()
      .mockImplementation((id) => Promise.resolve({ node: { id, name: id.toUpperCase() } }));
    const { session, client } = makeSession({
      loadSession: vi.fn().mockResolvedValue(twoNodes),
      loadNodeDetails,
    });
    session.connect();
    await client().handlers.onReady(1);

    client().handlers.onRemoteOps([{ op: 'nodes_added', node_ids: ['n3'] }]);
    await vi.waitFor(() => expect(loadNodeDetails).toHaveBeenCalledTimes(1));
    client().handlers.onRemoteOps([{ op: 'node_moved', node_id: 'n3', position: { x: 1, y: 1 } }]);
    await vi.waitFor(() =>
      expect(renderableNodes(session.getState().scene).map((n) => n.id)).toContain('n3')
    );
    expect(loadNodeDetails).toHaveBeenCalledTimes(1);
  });

  it('does not re-request a node whose details the graph no longer has', async () => {
    const loadNodeDetails = vi.fn().mockRejectedValue(new Error('404'));
    const { session, client } = makeSession({
      loadSession: vi.fn().mockResolvedValue(twoNodes),
      loadNodeDetails,
    });
    session.connect();
    await client().handlers.onReady(1);

    client().handlers.onRemoteOps([{ op: 'nodes_added', node_ids: ['ghost'] }]);
    await vi.waitFor(() => expect(loadNodeDetails).toHaveBeenCalledTimes(1));
    client().handlers.onRemoteOps([{ op: 'nodes_hidden', node_ids: ['n1'] }]);
    await vi.waitFor(() => expect(session.getState().scene.hiddenNodeIds).toEqual(['n1']));
    expect(loadNodeDetails).toHaveBeenCalledTimes(1);
  });

  it('re-hydrates a node that was removed and later added back', async () => {
    const loadNodeDetails = vi
      .fn()
      .mockImplementation((id) => Promise.resolve({ node: { id, name: 'Gamma' } }));
    const { session, client } = makeSession({
      loadSession: vi.fn().mockResolvedValue(twoNodes),
      loadNodeDetails,
    });
    session.connect();
    await client().handlers.onReady(1);

    client().handlers.onRemoteOps([{ op: 'nodes_added', node_ids: ['n3'] }]);
    await vi.waitFor(() => expect(loadNodeDetails).toHaveBeenCalledTimes(1));
    client().handlers.onRemoteOps([{ op: 'nodes_removed', node_ids: ['n3'] }]);
    client().handlers.onRemoteOps([{ op: 'nodes_added', node_ids: ['n3'] }]);
    await vi.waitFor(() => expect(loadNodeDetails).toHaveBeenCalledTimes(2));
  });

  it('reloads on resync and lets the newer load win over an in-flight older one', async () => {
    let resolveFirst;
    const loadSession = vi
      .fn()
      .mockImplementationOnce(() => new Promise((res) => (resolveFirst = res)))
      .mockResolvedValueOnce(payload([{ id: 'n9', name: 'Nine' }], { n9: { x: 1, y: 1 } }));
    const { session, client } = makeSession({ loadSession });
    session.connect();

    client().handlers.onReady(1); // first load stays pending
    await client().handlers.onResync(); // second load resolves first

    resolveFirst(twoNodes);
    await Promise.resolve();
    await Promise.resolve();

    expect(Object.keys(session.getState().scene.nodes)).toEqual(['n9']);
  });

  it('folds presence, claims and a rename into the scene the renderer reads', async () => {
    const { session, client } = makeSession({ loadSession: vi.fn().mockResolvedValue(twoNodes) });
    session.connect();
    await client().handlers.onReady(1);

    client().handlers.onPresence([{ client_id: 'client-b', display_name: 'Bo', color: '#f00' }]);
    client().handlers.onSelections({
      n1: { clientId: 'client-b', color: '#f00', displayName: 'Bo' },
    });
    client().handlers.onSessionRenamed('Workshop');

    const { scene } = session.getState();
    expect(scene.roster).toHaveLength(1);
    expect(scene.name).toBe('Workshop');
    expect(renderableNodes(scene).find((n) => n.id === 'n1').claim.displayName).toBe('Bo');
  });

  // SessionSyncClient seeds presence from the snapshot *before* it fires
  // onReady/onResync, so this is the order every real connect takes — a scene
  // rebuilt from the REST payload alone would drop both a moment after they
  // arrived, leaving the headset showing no collaborators and no selections.
  it('keeps the roster and claims the snapshot seeded before it fired onReady', async () => {
    const { session, client } = makeSession({ loadSession: vi.fn().mockResolvedValue(twoNodes) });
    session.connect();

    client().handlers.onPresence([{ client_id: 'client-b', display_name: 'Bo', color: '#f00' }]);
    client().handlers.onSelections({
      n1: { clientId: 'client-b', color: '#f00', displayName: 'Bo' },
    });
    await client().handlers.onReady(1);

    const { scene } = session.getState();
    expect(scene.roster).toHaveLength(1);
    expect(renderableNodes(scene).find((n) => n.id === 'n1').claim.displayName).toBe('Bo');
  });

  it('keeps presence across the reload a resync triggers', async () => {
    const { session, client } = makeSession({ loadSession: vi.fn().mockResolvedValue(twoNodes) });
    session.connect();
    await client().handlers.onReady(1);

    client().handlers.onPresence([{ client_id: 'client-b', display_name: 'Bo', color: '#f00' }]);
    client().handlers.onSelections({
      n2: { clientId: 'client-b', color: '#f00', displayName: 'Bo' },
    });
    await client().handlers.onResync();

    const { scene } = session.getState();
    expect(scene.roster).toHaveLength(1);
    expect(renderableNodes(scene).find((n) => n.id === 'n2').claim.displayName).toBe('Bo');
  });

  // The stream endpoint is get-or-create and the server leaves the generator
  // running after the delete notice, so an open stream would let the next
  // auto-reconnect recreate the very session that was just deleted.
  it('stops the stream on a remote session delete', async () => {
    const { session, client } = makeSession({ loadSession: vi.fn().mockResolvedValue(twoNodes) });
    session.connect();
    await client().handlers.onReady(1);
    client().handlers.onSessionDeleted('client-b');
    expect(session.getState().status).toBe('deleted');
    expect(client().closed).toBe(true);
  });

  it('keeps the delete notice when a reload already in flight resolves', async () => {
    let resolveLoad;
    const { session, client } = makeSession({
      loadSession: vi.fn().mockImplementation(() => new Promise((res) => (resolveLoad = res))),
    });
    session.connect();
    client().handlers.onReady(1);
    client().handlers.onSessionDeleted('client-b');
    // The read was served before the delete committed, so it succeeds anyway.
    resolveLoad(twoNodes);
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();

    expect(session.getState().status).toBe('deleted');
  });

  it('replays ops that arrived while the reload was in flight', async () => {
    let resolveLoad;
    const loadSession = vi.fn().mockImplementation(() => new Promise((res) => (resolveLoad = res)));
    const { session, client, loadNodeDetails } = makeSession({
      loadSession,
      loadNodeDetails: vi
        .fn()
        .mockImplementation((id) => Promise.resolve({ node: { id, name: id } })),
    });
    session.connect();
    client().handlers.onReady(1);

    // Delivered on the stream before the REST response lands, and naming a node
    // the response does not carry — so without the replay it is lost for good.
    client().handlers.onRemoteOps([
      { op: 'nodes_added', node_ids: ['n3'] },
      { op: 'node_moved', node_id: 'n3', position: { x: 7, y: 8 } },
      { op: 'nodes_hidden', node_ids: ['n1'] },
    ]);
    resolveLoad(twoNodes);
    await vi.waitFor(() =>
      expect(renderableNodes(session.getState().scene).map((n) => n.id)).toEqual(['n2', 'n3'])
    );
    expect(session.getState().scene.nodes.n3).toMatchObject({ x: 7, y: 8 });
    await vi.waitFor(() => expect(session.getState().scene.nodes.n3.hydrated).toBe(true));
    // The reload must not forget the fetch the op already started, or every
    // node added during the reload window costs two identical REST reads.
    expect(loadNodeDetails).toHaveBeenCalledTimes(1);
  });

  it('does not replay a stale buffer into a later reload', async () => {
    let resolveFirst;
    const loadSession = vi
      .fn()
      .mockImplementationOnce(() => new Promise((res) => (resolveFirst = res)))
      .mockResolvedValueOnce(payload([{ id: 'n9', name: 'Nine' }], { n9: { x: 1, y: 1 } }));
    const { session, client } = makeSession({ loadSession });
    session.connect();

    client().handlers.onReady(1);
    client().handlers.onRemoteOps([{ op: 'nodes_hidden', node_ids: ['n1'] }]);
    await client().handlers.onResync();
    resolveFirst(twoNodes);
    await Promise.resolve();
    await Promise.resolve();

    // The second reload won; the first reload's buffer must not leak into it.
    expect(Object.keys(session.getState().scene.nodes)).toEqual(['n9']);
    expect(session.getState().scene.hiddenNodeIds).toEqual([]);
  });

  it('stops emitting once closed', async () => {
    const { session, client, onChange } = makeSession({
      loadSession: vi.fn().mockResolvedValue(twoNodes),
    });
    session.connect();
    session.close();
    onChange.mockClear();
    await client().handlers.onReady(1);
    expect(onChange).not.toHaveBeenCalled();
  });
});
