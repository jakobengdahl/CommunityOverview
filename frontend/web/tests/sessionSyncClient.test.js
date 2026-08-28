import { describe, it, expect, beforeEach, vi } from 'vitest';
import {
  computeOps,
  normalizeMirror,
  applyOpToMirror,
  SessionSyncClient,
} from '../src/services/sessionSyncClient';

// ── Fakes ────────────────────────────────────────────────────────────────
class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.onmessage = null;
    this.onerror = null;
    this.closed = false;
    this.readyState = 1; // OPEN — stream connected successfully
    FakeEventSource.instances.push(this);
  }
  emit(obj) {
    this.onmessage?.({ data: JSON.stringify(obj) });
  }
  // terminal=false simulates a transient drop of an open stream (native reconnect);
  // terminal=true simulates a handshake-level failure (e.g. 429, readyState CLOSED).
  error(terminal = false) {
    if (terminal) this.readyState = 2;
    this.onerror?.();
  }
  close() {
    this.closed = true;
    this.readyState = 2;
  }
}
FakeEventSource.instances = [];

function makeFetch(responses = []) {
  const calls = [];
  const queue = [...responses];
  const fn = vi.fn(async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    const next = queue.shift();
    if (next instanceof Error) throw next;
    return next || { ok: true, status: 200, json: async () => ({ applied: [], seq: 1 }) };
  });
  fn.calls = calls;
  return fn;
}

const flush = () => new Promise((r) => setTimeout(r, 0));

function makeClient(overrides = {}) {
  const handlers = {};
  const fetchImpl = overrides.fetchImpl || makeFetch();
  const client = new SessionSyncClient({
    sessionId: '1234-5678',
    clientId: 'client-me',
    streamUrl: '/api/sessions/1234-5678/stream',
    opsUrl: '/api/sessions/1234-5678/ops',
    handlers,
    flushIntervalMs: 1,
    fetchImpl,
    EventSourceImpl: FakeEventSource,
    ...overrides,
  });
  return { client, fetchImpl, handlers };
}

beforeEach(() => {
  FakeEventSource.instances = [];
});

// ── computeOps ───────────────────────────────────────────────────────────
describe('computeOps', () => {
  it('emits nodes_added and nodes_removed as set ops', () => {
    const ops = computeOps({ node_refs: ['a', 'b'] }, { node_refs: ['b', 'c'] });
    expect(ops).toContainEqual({ op: 'nodes_added', node_ids: ['c'] });
    expect(ops).toContainEqual({ op: 'nodes_removed', node_ids: ['a'] });
  });

  it('emits node_moved only for positions that actually changed', () => {
    const prev = { node_refs: ['a', 'b'], positions: { a: { x: 0, y: 0 }, b: { x: 5, y: 5 } } };
    const next = { node_refs: ['a', 'b'], positions: { a: { x: 0, y: 0.2 }, b: { x: 50, y: 5 } } };
    const ops = computeOps(prev, next);
    // 'a' moved less than epsilon → ignored; 'b' moved → one op
    expect(ops.filter((o) => o.op === 'node_moved')).toEqual([
      { op: 'node_moved', node_id: 'b', position: { x: 50, y: 5 } },
    ]);
  });

  it('collapses a bulk position change into a single layout_applied op', () => {
    const node_refs = Array.from({ length: 25 }, (_, i) => `n${i}`);
    const positions = {};
    node_refs.forEach((id, i) => {
      positions[id] = { x: i, y: i };
    });
    const ops = computeOps({ node_refs, positions: {} }, { node_refs, positions });
    const layout = ops.filter((o) => o.op === 'layout_applied');
    expect(layout).toHaveLength(1);
    expect(Object.keys(layout[0].positions)).toHaveLength(25);
    expect(ops.some((o) => o.op === 'node_moved')).toBe(false);
  });

  it('emits hidden / shown set ops for nodes and edges', () => {
    const ops = computeOps(
      { hidden_node_ids: ['a'], hidden_edge_ids: ['e1'] },
      { hidden_node_ids: ['b'], hidden_edge_ids: [] }
    );
    expect(ops).toContainEqual({ op: 'nodes_hidden', node_ids: ['b'] });
    expect(ops).toContainEqual({ op: 'nodes_shown', node_ids: ['a'] });
    expect(ops).toContainEqual({ op: 'edges_shown', edge_ids: ['e1'] });
  });

  it('creates, updates and deletes annotations by id', () => {
    const prev = {
      annotations: [
        { id: 'n1', kind: 'note', text: 'hi' },
        { id: 'n2', kind: 'label', text: 'x' },
      ],
    };
    const next = {
      annotations: [
        { id: 'n1', kind: 'note', text: 'bye' },
        { id: 'n3', kind: 'note', text: 'new' },
      ],
    };
    const ops = computeOps(prev, next);
    expect(ops).toContainEqual({
      op: 'annotation_updated',
      annotation: { id: 'n1', kind: 'note', text: 'bye' },
    });
    expect(ops).toContainEqual({
      op: 'annotation_created',
      annotation: { id: 'n3', kind: 'note', text: 'new' },
    });
    expect(ops).toContainEqual({ op: 'annotation_deleted', annotation_id: 'n2' });
  });

  it('routes group membership changes through group_membership_changed', () => {
    const prev = { annotations: [{ id: 'g1', kind: 'group', label: 'G', member_node_ids: ['a'] }] };
    const next = {
      annotations: [{ id: 'g1', kind: 'group', label: 'G', member_node_ids: ['a', 'b'] }],
    };
    const ops = computeOps(prev, next);
    expect(ops).toContainEqual({
      op: 'group_membership_changed',
      group_id: 'g1',
      member_node_ids: ['a', 'b'],
    });
    expect(ops.some((o) => o.op === 'annotation_updated')).toBe(false);
  });

  it('emits annotation_updated for a group label change without a phantom membership op', () => {
    const prev = {
      annotations: [{ id: 'g1', kind: 'group', label: 'Old', member_node_ids: ['a'] }],
    };
    const next = {
      annotations: [{ id: 'g1', kind: 'group', label: 'New', member_node_ids: ['a'] }],
    };
    const ops = computeOps(prev, next);
    expect(ops.some((o) => o.op === 'group_membership_changed')).toBe(false);
    expect(ops).toContainEqual({
      op: 'annotation_updated',
      annotation: { id: 'g1', kind: 'group', label: 'New', member_node_ids: ['a'] },
    });
  });

  it('ignores updated_at churn on annotations', () => {
    const prev = { annotations: [{ id: 'n1', kind: 'note', text: 'hi', updated_at: '2020' }] };
    const next = { annotations: [{ id: 'n1', kind: 'note', text: 'hi', updated_at: '2021' }] };
    expect(computeOps(prev, next)).toEqual([]);
  });

  it('normalizeMirror dedupes and drops malformed positions', () => {
    const m = normalizeMirror({
      node_refs: ['a', 'a', 'b'],
      positions: { a: { x: 1, y: 2 }, bad: { x: 'z' } },
    });
    expect(m.node_refs).toEqual(['a', 'b']);
    expect(m.positions).toEqual({ a: { x: 1, y: 2 } });
  });
});

// ── applyOpToMirror ─────────────────────────────────────────────────────────
describe('applyOpToMirror', () => {
  it('folds set ops and moves into the mirror', () => {
    let m = { node_refs: ['a'] };
    m = applyOpToMirror(m, { op: 'nodes_added', node_ids: ['b'] });
    m = applyOpToMirror(m, { op: 'node_moved', node_id: 'b', position: { x: 3, y: 4 } });
    m = applyOpToMirror(m, { op: 'nodes_hidden', node_ids: ['a'] });
    expect(m.node_refs).toEqual(['a', 'b']);
    expect(m.positions.b).toEqual({ x: 3, y: 4 });
    expect(m.hidden_node_ids).toEqual(['a']);
  });

  it('removing a node also drops its position, hidden flag and group membership', () => {
    const m0 = {
      node_refs: ['a', 'b'],
      positions: { a: { x: 1, y: 1 } },
      hidden_node_ids: ['a'],
      annotations: [{ id: 'g', kind: 'group', member_node_ids: ['a', 'b'] }],
    };
    const m = applyOpToMirror(m0, { op: 'nodes_removed', node_ids: ['a'] });
    expect(m.node_refs).toEqual(['b']);
    expect(m.positions).toEqual({});
    expect(m.hidden_node_ids).toEqual([]);
    expect(m.annotations[0].member_node_ids).toEqual(['b']);
  });

  it('upserts and deletes annotations and updates group membership', () => {
    let m = { annotations: [] };
    m = applyOpToMirror(m, {
      op: 'annotation_created',
      annotation: { id: 'n1', kind: 'note', text: 'x' },
    });
    m = applyOpToMirror(m, {
      op: 'annotation_updated',
      annotation: { id: 'n1', kind: 'note', text: 'y' },
    });
    expect(m.annotations).toEqual([{ id: 'n1', kind: 'note', text: 'y' }]);
    m = applyOpToMirror(m, {
      op: 'annotation_created',
      annotation: { id: 'g', kind: 'group', member_node_ids: [] },
    });
    m = applyOpToMirror(m, {
      op: 'group_membership_changed',
      group_id: 'g',
      member_node_ids: ['a'],
    });
    expect(m.annotations.find((a) => a.id === 'g').member_node_ids).toEqual(['a']);
    m = applyOpToMirror(m, { op: 'annotation_deleted', annotation_id: 'n1' });
    expect(m.annotations.map((a) => a.id)).toEqual(['g']);
  });
});

// ── SessionSyncClient transport ────────────────────────────────────────────
describe('SessionSyncClient', () => {
  it('opens the stream with client_id and buffers ops until the first snapshot', async () => {
    const { client, fetchImpl } = makeClient();
    client.connect();
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0].url).toContain('client_id=client-me');

    client.syncState({ node_refs: ['a'] });
    await flush();
    expect(fetchImpl.calls).toHaveLength(0); // not ready yet

    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    await flush();
    expect(fetchImpl.calls).toHaveLength(1);
    expect(fetchImpl.calls[0].body.ops).toContainEqual({ op: 'nodes_added', node_ids: ['a'] });
    expect(fetchImpl.calls[0].body.client_id).toBe('client-me');
  });

  // task fbd32fc9: the reconnect path reads this list before reloading the
  // canvas from the server, so it can replay whatever never reached the
  // server instead of silently discarding it.
  it('getPendingOps reports queued-but-unsent ops and clears once they flush', async () => {
    const { client } = makeClient();
    client.connect();
    // Not ready yet (no snapshot delivered) — same "offline" condition as the
    // buffering test above, so the op sits in the queue rather than flushing.
    client.syncState({ node_refs: ['a'] });
    expect(client.getPendingOps()).toContainEqual({ op: 'nodes_added', node_ids: ['a'] });

    // Returns a copy: mutating it must not reach into the live queue.
    const snapshot = client.getPendingOps();
    snapshot.push({ op: 'nodes_added', node_ids: ['bogus'] });
    expect(client.getPendingOps()).not.toContainEqual({ op: 'nodes_added', node_ids: ['bogus'] });

    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    await flush();
    expect(client.getPendingOps()).toEqual([]);
  });

  // Review round 5 (PR #496 / task fbd32fc9): _takeBatch() splices an op out
  // of _queue *before* its POST resolves, so a connection can drop while that
  // batch is mid-flight — narrower than "never sent at all", but the same
  // vanishing-edit risk if getPendingOps only looked at _queue.
  it('getPendingOps also reports an op whose POST is still in flight, not just queued ones', async () => {
    let releasePost;
    const postGate = new Promise((resolve) => {
      releasePost = resolve;
    });
    const fetchImpl = vi.fn(async () => {
      await postGate;
      return { ok: true, status: 200, json: async () => ({ applied: [], seq: 1 }) };
    });
    const { client } = makeClient({ fetchImpl });
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    await flush();

    client.syncState({ node_refs: ['a'] });
    await flush(); // lets the 1ms-debounced flush start the (gated) POST

    expect(fetchImpl).toHaveBeenCalledTimes(1);
    // Spliced out of _queue already (the POST is in flight) — still reported.
    expect(client.getPendingOps()).toContainEqual({ op: 'nodes_added', node_ids: ['a'] });

    releasePost();
    await flush();
    expect(client.getPendingOps()).toEqual([]);
  });

  it('calls onReady on the first snapshot and onResync on a later one', async () => {
    const onReady = vi.fn();
    const onResync = vi.fn();
    const { client } = makeClient({ handlers: { onReady, onResync } });
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({ type: 'snapshot', seq: 3, session: { state: {} } });
    expect(onReady).toHaveBeenCalledWith(3);
    expect(onResync).not.toHaveBeenCalled();
    es.emit({ type: 'snapshot', seq: 5, session: { state: {} } });
    expect(onResync).toHaveBeenCalledTimes(1);
    expect(client.seq).toBe(5);
  });

  it('forwards command events (MCP pushes broadcast via the hub, design R5)', async () => {
    const onCommand = vi.fn();
    const { client } = makeClient({ handlers: { onCommand } });
    client.connect();
    const es = FakeEventSource.instances[0];
    const command = {
      type: 'tool_result',
      tool: 'search_graph',
      result: { action: 'add_to_visualization' },
    };
    es.emit({ type: 'command', command });
    expect(onCommand).toHaveBeenCalledWith(command);
  });

  it('forwards remote ops but suppresses echoes of its own client', async () => {
    const onRemoteOps = vi.fn();
    const { client } = makeClient({ handlers: { onRemoteOps } });
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({ type: 'op', client_id: 'client-me', op: { op: 'node_moved', node_id: 'a' }, seq: 1 });
    expect(onRemoteOps).not.toHaveBeenCalled();
    es.emit({
      type: 'op',
      client_id: 'client-other',
      op: { op: 'node_moved', node_id: 'a' },
      seq: 2,
    });
    expect(onRemoteOps).toHaveBeenCalledWith([{ op: 'node_moved', node_id: 'a' }], {
      clientId: 'client-other',
    });
    expect(client.seq).toBe(2);
  });

  // Regression for the image-ingest echo-attribution fix: the browser that
  // pasted an image is the same browser that made the underlying HTTP POST
  // (backend/service/rest_api.py's ingest_session_image), but the server
  // broadcasts that op under a dedicated marker client id
  // (_HUMAN_IMAGE_INGEST_CLIENT_ID), not the pasting browser's own client_id —
  // specifically so this echo is NOT treated as a self-authored op and
  // dropped by the guard just above. Confirms the initiating client still
  // receives and applies the server's actual embedded/optimized result over
  // its own SSE subscription, rather than needing a bespoke response-body
  // round-trip.
  it('applies an image-ingest op even though the receiving client is the one that "sent" it, because the op carries a dedicated server client id', async () => {
    const onRemoteOps = vi.fn();
    const { client } = makeClient({ clientId: 'client-me', handlers: { onRemoteOps } });
    client.connect();
    const es = FakeEventSource.instances[0];
    const imageOp = {
      op: 'annotation_created',
      annotation: { id: 'img-1', type: 'image', image: { url: 'data:image/webp;base64,AA==' } },
    };
    // The pasting browser is 'client-me' — the same id whose fetch() reached
    // the ingest endpoint — but the broadcast is attributed to the server's
    // marker, not to 'client-me'.
    es.emit({ type: 'op', client_id: 'human-image-ingest', op: imageOp, seq: 1 });
    expect(onRemoteOps).toHaveBeenCalledWith([imageOp], { clientId: 'human-image-ingest' });
  });

  // foldLocalOp is App.jsx's other half of the image-ingest fix: applied
  // directly, ahead of the SSE echo above, so the sync baseline reflects the
  // new annotation immediately (see foldLocalOp's own docstring for why a
  // stale baseline would re-emit a redundant create). These cover the flip
  // side — that the *echo*, when it does arrive afterwards, does not then
  // re-fold the same (by-then possibly stale) creation-time content over a
  // baseline a local edit already advanced.
  describe('foldLocalOp', () => {
    it('advances the baseline immediately, so re-syncing the same state emits nothing', async () => {
      const { client, fetchImpl } = makeClient();
      client.connect();
      FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
      const annotation = { id: 'img-1', type: 'image', position: { x: 1, y: 1 } };
      client.foldLocalOp({ op: 'annotation_created', annotation });

      client.syncState({ annotations: [annotation] });
      await flush();
      expect(fetchImpl.calls).toHaveLength(0);
    });

    it('marks the annotation so the following echo does not re-fold stale content over a newer local edit', async () => {
      const { client, fetchImpl } = makeClient();
      client.connect();
      const es = FakeEventSource.instances[0];
      es.emit({ type: 'snapshot', seq: 0, session: { state: {} } });
      const created = { id: 'img-1', type: 'image', position: { x: 1, y: 1 } };
      client.foldLocalOp({ op: 'annotation_created', annotation: created });

      // The user drags the just-created image before the echo arrives; the
      // normal edit-sync path advances the baseline to the new position.
      const moved = { id: 'img-1', type: 'image', position: { x: 99, y: 99 } };
      client.syncState({ annotations: [moved] });
      await flush();
      fetchImpl.calls.length = 0; // clear the move's own POST for a clean count below

      // The confirming echo now arrives, carrying the stale, pre-drag
      // position — folding it over the baseline again would revert the move.
      es.emit({
        type: 'op',
        client_id: 'human-image-ingest',
        op: { op: 'annotation_created', annotation: created },
        seq: 1,
      });

      // If the echo had wrongly re-folded the stale position, the baseline
      // would now disagree with the still-current (moved) canvas state and
      // this sync would emit a redundant correction. It must not.
      client.syncState({ annotations: [moved] });
      await flush();
      expect(fetchImpl.calls).toHaveLength(0);
    });

    it('only guards the one echo immediately following a local fold — a later, unrelated update for the same id folds normally', async () => {
      const { client, fetchImpl } = makeClient();
      client.connect();
      const es = FakeEventSource.instances[0];
      es.emit({ type: 'snapshot', seq: 0, session: { state: {} } });
      const created = { id: 'img-1', type: 'image', position: { x: 1, y: 1 } };
      client.foldLocalOp({ op: 'annotation_created', annotation: created });
      // The one echo this guards against:
      es.emit({
        type: 'op',
        client_id: 'human-image-ingest',
        op: { op: 'annotation_created', annotation: created },
        seq: 1,
      });

      // A genuine later edit from a different collaborator must still fold —
      // the guard is one-shot, not a permanent block on this annotation id.
      const editedByOther = { id: 'img-1', type: 'image', position: { x: 50, y: 50 } };
      es.emit({
        type: 'op',
        client_id: 'client-other',
        op: { op: 'annotation_updated', annotation: editedByOther },
        seq: 2,
      });

      // Re-syncing the *original* (pre-edit) content is now a real change
      // from the baseline's point of view (which the collaborator's edit
      // should have advanced to `editedByOther`), so it must emit a
      // correcting op — proving the baseline picked up their update rather
      // than the guard having swallowed it.
      client.syncState({ annotations: [created] });
      await flush();
      expect(fetchImpl.calls).toHaveLength(1);
      expect(fetchImpl.calls[0].body.ops).toContainEqual({
        op: 'annotation_updated',
        annotation: created,
      });
    });
  });

  // task fbd32fc9 review round 7: resyncFromServer (App.jsx) folds recovered
  // ops that will flush under this client's own id — unlike foldLocalOp's
  // documented caller (image ingest, broadcast under a shared client id), so
  // foldLocalOp's echo-skip marker would never be consumed and could wrongly
  // swallow a *different* collaborator's later genuine edit to the same
  // annotation. foldOpIntoBaseline is the marker-free variant for that case.
  describe('foldOpIntoBaseline', () => {
    it('advances the baseline the same way foldLocalOp does', async () => {
      const { client, fetchImpl } = makeClient();
      client.connect();
      FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
      const annotation = { id: 'note-1', type: 'note', text: 'hi' };
      client.foldOpIntoBaseline({ op: 'annotation_created', annotation });

      client.syncState({ annotations: [annotation] });
      await flush();
      expect(fetchImpl.calls).toHaveLength(0); // baseline already matches; no redundant op
    });

    it('does not poison a later genuine remote edit to the same annotation, unlike foldLocalOp would', async () => {
      const { client, fetchImpl } = makeClient();
      client.connect();
      const es = FakeEventSource.instances[0];
      es.emit({ type: 'snapshot', seq: 0, session: { state: {} } });
      const created = { id: 'note-1', type: 'note', text: 'hi' };
      client.foldOpIntoBaseline({ op: 'annotation_created', annotation: created });

      // A different collaborator edits the same annotation. If this had used
      // foldLocalOp instead, its marker would still be set (nothing had
      // consumed it — this client's own echo for `created` was never even
      // sent here), and this echo's fold would be wrongly skipped.
      const editedByOther = { id: 'note-1', type: 'note', text: 'edited by someone else' };
      es.emit({
        type: 'op',
        client_id: 'client-other',
        op: { op: 'annotation_updated', annotation: editedByOther },
        seq: 1,
      });

      // Re-syncing the *original* (pre-their-edit) content is now a real
      // change from the baseline's point of view — proving the baseline
      // picked up their update rather than a stale marker having swallowed it.
      client.syncState({ annotations: [created] });
      await flush();
      expect(fetchImpl.calls).toHaveLength(1);
      expect(fetchImpl.calls[0].body.ops).toContainEqual({
        op: 'annotation_updated',
        annotation: created,
      });
    });
  });

  describe('whenReady', () => {
    it('resolves immediately once the client is already ready', async () => {
      const { client } = makeClient();
      client.connect();
      FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
      await expect(client.whenReady()).resolves.toBeUndefined();
    });

    it('resolves once the first snapshot arrives for a not-yet-ready client', async () => {
      const { client } = makeClient();
      client.connect();
      let resolved = false;
      const p = client.whenReady().then(() => {
        resolved = true;
      });
      expect(resolved).toBe(false);
      FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
      await p;
      expect(resolved).toBe(true);
    });

    it('resolves on a reconnect catch_up as well as a fresh snapshot', async () => {
      const { client } = makeClient();
      client.connect();
      let resolved = false;
      const p = client.whenReady().then(() => {
        resolved = true;
      });
      FakeEventSource.instances[0].emit({ type: 'catch_up', seq: 4, ops: [] });
      await p;
      expect(resolved).toBe(true);
    });

    it('resolves every pending waiter, not just the first', async () => {
      const { client } = makeClient();
      client.connect();
      const results = [];
      const p1 = client.whenReady().then(() => results.push(1));
      const p2 = client.whenReady().then(() => results.push(2));
      FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
      await Promise.all([p1, p2]);
      expect(results.sort()).toEqual([1, 2]);
    });

    // Regression: without this, a caller awaiting whenReady() when the
    // session is switched away before the first snapshot ever arrives (e.g.
    // App.jsx's handleImageIngest, mid-paste) would hang forever, keeping
    // this whole client — and its closed EventSource — reachable and alive.
    it('resolves pending waiters on close(), never leaving one hanging', async () => {
      const { client } = makeClient();
      client.connect();
      let resolved = false;
      const p = client.whenReady().then(() => {
        resolved = true;
      });
      client.close();
      await p;
      expect(resolved).toBe(true);
    });
  });

  it('sendEdgesAdded enqueues and POSTs an edges_added op carrying the edges', async () => {
    const { client, fetchImpl } = makeClient();
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    const edge = { id: 'e1', source: 'a', target: 'b', type: 'RELATES_TO' };
    client.sendEdgesAdded([edge]);
    await flush();
    const posted = fetchImpl.calls.at(-1).body.ops;
    expect(posted).toContainEqual({ op: 'edges_added', edges: [edge] });
  });

  it('sendEdgesAdded ignores an empty or id-less edge list', () => {
    const { client, fetchImpl } = makeClient();
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    client.sendEdgesAdded([]);
    client.sendEdgesAdded([{ source: 'a', target: 'b' }]);
    expect(fetchImpl.calls).toHaveLength(0);
  });

  it('forwards a remote edges_added op to the host and never folds it into the mirror', async () => {
    const onRemoteOps = vi.fn();
    const { client, fetchImpl } = makeClient({ handlers: { onRemoteOps } });
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({ type: 'snapshot', seq: 0, session: { state: { node_refs: ['a', 'b'] } } });
    const op = { op: 'edges_added', edges: [{ id: 'e1', source: 'a', target: 'b' }] };
    es.emit({ type: 'op', client_id: 'client-other', op, seq: 1 });
    expect(onRemoteOps).toHaveBeenCalledWith([op], { clientId: 'client-other' });
    // Edges are graph-derived, not mirror state: re-syncing the same node set
    // must emit nothing (the edge op left no residue that could echo back out).
    client.setBaseline({ node_refs: ['a', 'b'] });
    client.syncState({ node_refs: ['a', 'b'] });
    await flush();
    expect(fetchImpl.calls).toHaveLength(0);
  });

  it('applyOpToMirror leaves the mirror unchanged for an edges_added op', () => {
    const before = normalizeMirror({ node_refs: ['a', 'b'], hidden_edge_ids: ['x'] });
    const after = applyOpToMirror(before, {
      op: 'edges_added',
      edges: [{ id: 'e1', source: 'a', target: 'b' }],
    });
    expect(after).toEqual(before);
  });

  it('sendEdgesRemoved enqueues and POSTs an edges_removed op carrying the ids', async () => {
    const { client, fetchImpl } = makeClient();
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    client.sendEdgesRemoved(['e1', 'e2']);
    await flush();
    const posted = fetchImpl.calls.at(-1).body.ops;
    expect(posted).toContainEqual({ op: 'edges_removed', edge_ids: ['e1', 'e2'] });
  });

  it('sendEdgesRemoved ignores an empty or non-string id list', () => {
    const { client, fetchImpl } = makeClient();
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    client.sendEdgesRemoved([]);
    client.sendEdgesRemoved([null, 5]);
    expect(fetchImpl.calls).toHaveLength(0);
  });

  it('sendEdgesUpdated enqueues and POSTs an edges_updated op carrying the edges', async () => {
    const { client, fetchImpl } = makeClient();
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    const edge = { id: 'e1', type: 'DEPENDS_ON' };
    client.sendEdgesUpdated([edge]);
    await flush();
    const posted = fetchImpl.calls.at(-1).body.ops;
    expect(posted).toContainEqual({ op: 'edges_updated', edges: [edge] });
  });

  it('sendEdgesUpdated ignores an empty or id-less edge list', () => {
    const { client, fetchImpl } = makeClient();
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    client.sendEdgesUpdated([]);
    client.sendEdgesUpdated([{ type: 'DEPENDS_ON' }]);
    expect(fetchImpl.calls).toHaveLength(0);
  });

  it('forwards remote edges_removed / edges_updated ops without folding them into the mirror', async () => {
    const onRemoteOps = vi.fn();
    const { client, fetchImpl } = makeClient({ handlers: { onRemoteOps } });
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({ type: 'snapshot', seq: 0, session: { state: { node_refs: ['a', 'b'] } } });
    const removed = { op: 'edges_removed', edge_ids: ['e1'] };
    const updated = { op: 'edges_updated', edges: [{ id: 'e1', type: 'DEPENDS_ON' }] };
    es.emit({ type: 'op', client_id: 'client-other', op: removed, seq: 1 });
    es.emit({ type: 'op', client_id: 'client-other', op: updated, seq: 2 });
    expect(onRemoteOps).toHaveBeenCalledWith([removed], { clientId: 'client-other' });
    expect(onRemoteOps).toHaveBeenCalledWith([updated], { clientId: 'client-other' });
    // Edges are graph-derived, not mirror state: re-syncing the same node set
    // must emit nothing (neither op left residue that could echo back out).
    client.setBaseline({ node_refs: ['a', 'b'] });
    client.syncState({ node_refs: ['a', 'b'] });
    await flush();
    expect(fetchImpl.calls).toHaveLength(0);
  });

  it('applyOpToMirror leaves the mirror unchanged for edges_removed / edges_updated ops', () => {
    const before = normalizeMirror({ node_refs: ['a', 'b'], hidden_edge_ids: ['x'] });
    expect(applyOpToMirror(before, { op: 'edges_removed', edge_ids: ['e1'] })).toEqual(before);
    expect(
      applyOpToMirror(before, { op: 'edges_updated', edges: [{ id: 'e1', type: 'DEPENDS_ON' }] })
    ).toEqual(before);
  });

  it('delivers an agent layout_applied op with its animation hint intact', () => {
    // The canvas animation seam (contract §9-§10) depends on the animation hint
    // and the originating client id surviving delivery: the frontend routes an
    // op carrying `animation` to the tweening channel and shows an "agent is
    // arranging" indicator for the reserved mcp-agent client.
    const onRemoteOps = vi.fn();
    const { client } = makeClient({ handlers: { onRemoteOps } });
    client.connect();
    const op = {
      op: 'layout_applied',
      positions: { a: { x: 10, y: 20 } },
      animation: { animate: true, duration_ms: 400, easing: 'ease-in-out' },
      seq: 3,
    };
    FakeEventSource.instances[0].emit({ type: 'op', client_id: 'mcp-agent', op, seq: 3 });
    expect(onRemoteOps).toHaveBeenCalledWith([op], { clientId: 'mcp-agent' });
    expect(onRemoteOps.mock.calls[0][0][0].animation).toEqual({
      animate: true,
      duration_ms: 400,
      easing: 'ease-in-out',
    });
  });

  it('advances seq from the ops POST response', async () => {
    const fetchImpl = makeFetch([
      { ok: true, status: 200, json: async () => ({ applied: [], seq: 42 }) },
    ]);
    const { client } = makeClient({ fetchImpl });
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    client.syncState({ node_refs: ['a'] });
    await flush();
    expect(client.seq).toBe(42);
  });

  it('setBaseline suppresses ops for state already on the server', async () => {
    const { client, fetchImpl } = makeClient();
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    client.setBaseline({ node_refs: ['a', 'b'] });
    client.syncState({ node_refs: ['a', 'b'] });
    await flush();
    expect(fetchImpl.calls).toHaveLength(0);
  });

  it('requeues ops on a 500 and drops them on a 400', async () => {
    const fetchImpl = makeFetch([
      { ok: false, status: 500 },
      { ok: true, status: 200, json: async () => ({ seq: 1 }) },
    ]);
    const { client } = makeClient({ fetchImpl });
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    client.syncState({ node_refs: ['a'] });
    await new Promise((r) => setTimeout(r, 30)); // let retry fire
    expect(fetchImpl.calls.length).toBeGreaterThanOrEqual(2);
    expect(fetchImpl.calls[1].body.ops).toContainEqual({ op: 'nodes_added', node_ids: ['a'] });

    const dropFetch = makeFetch([{ ok: false, status: 400 }]);
    const onDropped = vi.fn();
    const { client: c2 } = makeClient({ fetchImpl: dropFetch, handlers: { onDropped } });
    c2.connect();
    FakeEventSource.instances[1].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    c2.syncState({ node_refs: ['x'] });
    await new Promise((r) => setTimeout(r, 30));
    expect(onDropped).toHaveBeenCalled();
    expect(dropFetch.calls).toHaveLength(1); // not retried
  });

  // Review round 9 (PR #496 / task fbd32fc9): onDropped is invoked
  // synchronously from inside _flush(), before _flush's own `finally` gets a
  // chance to strip the batch out of _inFlightOps. A caller that reads
  // getPendingOps() from within its onDropped handler (App.jsx's
  // resyncFromServer, triggered by onDropped to converge the canvas back to
  // server truth) must see the dropped op already gone — otherwise the very
  // resync meant to drop it would instead resurrect it.
  it('a dropped op is already gone from getPendingOps by the time onDropped fires', async () => {
    const dropFetch = makeFetch([{ ok: false, status: 400 }]);
    let pendingDuringDrop = null;
    const onDropped = vi.fn(() => {
      pendingDuringDrop = client.getPendingOps();
    });
    const { client } = makeClient({ fetchImpl: dropFetch, handlers: { onDropped } });
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    client.syncState({ node_refs: ['x'] });
    await new Promise((r) => setTimeout(r, 30));

    expect(onDropped).toHaveBeenCalled();
    expect(pendingDuringDrop).toEqual([]);
  });

  it('does not permanently wedge outbound delivery when a POST /ops never settles', async () => {
    // Regression for the shared-session "moves silently stop persisting over
    // time" data-loss bug: a single hung POST (a half-open request held by a
    // proxy) used to leave `_flushing` stuck true forever, so every later op —
    // moves, node adds, everything — silently never reached the server, the
    // batch in flight was lost, and a reload showed none of it stored. The
    // request timeout must abort the stuck POST so delivery resumes.
    const bodies = [];
    let hang = true;
    const fetchImpl = vi.fn((url, opts) => {
      bodies.push(JSON.parse(opts.body));
      if (hang) {
        hang = false;
        return new Promise(() => {}); // first POST never settles
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({ applied: [], seq: bodies.length }),
      });
    });
    const { client } = makeClient({ fetchImpl, requestTimeoutMs: 20 });
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });

    // First move → its POST hangs.
    client.syncState({ node_refs: ['n0'], positions: { n0: { x: 1, y: 1 } } });
    await new Promise((r) => setTimeout(r, 10));
    // A later move made while the first request is still hung must still get out.
    client.syncState({ node_refs: ['n0'], positions: { n0: { x: 2, y: 2 } } });
    await new Promise((r) => setTimeout(r, 150)); // past the timeout + retry backoff

    expect(fetchImpl.mock.calls.length).toBeGreaterThan(1); // not wedged
    const allOps = bodies.flatMap((b) => b.ops || []);
    // Neither move is lost: the hung batch's op is requeued, the later one sent.
    expect(allOps).toContainEqual({ op: 'node_moved', node_id: 'n0', position: { x: 1, y: 1 } });
    expect(allOps).toContainEqual({ op: 'node_moved', node_id: 'n0', position: { x: 2, y: 2 } });
  });

  it('releases the flush guard and retries after a hung ops request times out', async () => {
    // The heart of the wedge fix: a request that never settles must not leave
    // `_flushing` stuck true (which permanently blocks every later flush). After
    // the timeout it is released and the op is retried on a fresh request.
    const fetchImpl = vi.fn(() => new Promise(() => {})); // every POST hangs
    const { client } = makeClient({ fetchImpl, requestTimeoutMs: 20 });
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    client.syncState({ node_refs: ['a'] });
    await new Promise((r) => setTimeout(r, 120));
    // Each hung POST times out and releases `_flushing`, so the client keeps
    // reattempting instead of freezing on the first hung request forever. If the
    // guard were never released, exactly one attempt would ever be made.
    expect(fetchImpl.mock.calls.length).toBeGreaterThan(1);
  });

  it('folds a remote op into the baseline so re-syncing the same state emits nothing (echo-safe)', async () => {
    const onRemoteOps = vi.fn();
    const { client, fetchImpl } = makeClient({ handlers: { onRemoteOps } });
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    client.setBaseline({ node_refs: ['a'] });
    // Another client adds node 'b'; the host applies it locally.
    es.emit({
      type: 'op',
      client_id: 'client-other',
      op: { op: 'nodes_added', node_ids: ['b'] },
      seq: 1,
    });
    expect(onRemoteOps).toHaveBeenCalledTimes(1);
    // The host's resulting snapshot now contains 'b' — but it must not echo back.
    client.syncState({ node_refs: ['a', 'b'] });
    await flush();
    expect(fetchImpl.calls).toHaveLength(0);
  });

  it('exposes the baseline position a node was moved to (for placing async-added nodes)', () => {
    const { client } = makeClient();
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    es.emit({ type: 'op', client_id: 'other', op: { op: 'nodes_added', node_ids: ['a'] }, seq: 1 });
    es.emit({
      type: 'op',
      client_id: 'other',
      op: { op: 'node_moved', node_id: 'a', position: { x: 7, y: 8 } },
      seq: 2,
    });
    expect(client.baselinePosition('a')).toEqual({ x: 7, y: 8 });
    expect(client.baselinePosition('missing')).toBeNull();
  });

  it('isolates a poison-pill op: a rejected multi-op batch resends singly, dropping only the bad op', async () => {
    const onDropped = vi.fn();
    // The server rejects any batch containing the annotation op; valid ops pass.
    const fetchImpl = vi.fn(async (url, opts) => {
      const ops = JSON.parse(opts.body).ops;
      if (ops.some((o) => o.op === 'annotation_created')) return { ok: false, status: 400 };
      return { ok: true, status: 200, json: async () => ({ seq: 1 }) };
    });
    fetchImpl.calls = () => fetchImpl.mock.calls.map(([, o]) => JSON.parse(o.body).ops);
    const { client } = makeClient({ fetchImpl, handlers: { onDropped } });
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    client.syncState({ node_refs: ['a'], annotations: [{ id: 'n1', kind: 'note', text: 'x' }] });
    await new Promise((r) => setTimeout(r, 60));
    // The valid nodes_added was delivered in its own batch; the annotation was dropped.
    const sent = fetchImpl.calls();
    expect(sent.some((ops) => ops.length === 1 && ops[0].op === 'nodes_added')).toBe(true);
    expect(onDropped).toHaveBeenCalled();
    expect(onDropped.mock.calls.at(-1)[0][0].op).toBe('annotation_created');
  });

  it('teardown flush drains every queued op in force-single mode (close() loses none)', async () => {
    // First POST (the multi-op batch) is terminally rejected, flipping the client
    // into force-single recovery with the whole batch requeued.
    const fetchImpl = makeFetch([{ ok: false, status: 400 }]);
    // Long debounce so no automatic re-flush races the explicit teardown flush.
    const { client } = makeClient({ fetchImpl, flushIntervalMs: 60_000 });
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    client.syncState({ node_refs: ['a'], hidden_node_ids: ['h'] }); // 2 ops in one batch
    await client.flush();
    expect(fetchImpl.calls).toHaveLength(1); // rejected batch, both ops requeued

    client.flush();
    client.close();
    await new Promise((r) => setTimeout(r, 10));
    // Both requeued ops left as their own single-op batches despite close().
    const singles = fetchImpl.calls.slice(1).map((c) => c.body.ops);
    expect(singles).toEqual([
      [{ op: 'nodes_added', node_ids: ['a'] }],
      [{ op: 'nodes_hidden', node_ids: ['h'] }],
    ]);
  });

  it('teardown flush drains the remainder even while a force-single op is in flight', async () => {
    let release;
    const gate = new Promise((r) => {
      release = r;
    });
    let call = 0;
    // Call 1: the multi-op batch, terminally rejected (enters force-single).
    // Call 2: the debounced single-op resend, held in flight across teardown.
    const fetchImpl = vi.fn(async (url, opts) => {
      fetchImpl.sent.push(JSON.parse(opts.body).ops);
      call += 1;
      if (call === 1) return { ok: false, status: 400 };
      if (call === 2) await gate;
      return { ok: true, status: 200, json: async () => ({ seq: 1 }) };
    });
    fetchImpl.sent = [];
    const { client } = makeClient({ fetchImpl, flushIntervalMs: 1 });
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });
    client.syncState({ node_refs: ['a'], hidden_node_ids: ['h'], hidden_edge_ids: ['e'] }); // 3 ops
    await client.flush(); // batch rejected → force-single, all 3 requeued
    await new Promise((r) => setTimeout(r, 10)); // debounce resends op 1, which now hangs
    expect(fetchImpl.sent).toHaveLength(2);

    client.flush();
    client.close();
    await new Promise((r) => setTimeout(r, 10));
    release();
    expect(fetchImpl.sent.slice(2)).toEqual([
      [{ op: 'nodes_hidden', node_ids: ['h'] }],
      [{ op: 'edges_hidden', edge_ids: ['e'] }],
    ]);
  });

  it('teardown flush sends queued ops even if the stream never became ready', async () => {
    // A client can be swapped out (session switch) before its stream ever
    // delivers a first snapshot. flush() must not drop ops queued in that
    // window — only the debounced auto-flush should wait for _ready.
    const { client, fetchImpl } = makeClient({ flushIntervalMs: 60_000 });
    client.connect();
    client.syncState({ node_refs: ['a'] });
    await flush();
    expect(fetchImpl.calls).toHaveLength(0); // never-ready: auto-flush stays quiet

    await client.flush();
    expect(fetchImpl.calls).toHaveLength(1);
    expect(fetchImpl.calls[0].body.ops).toContainEqual({ op: 'nodes_added', node_ids: ['a'] });
  });

  it('dispatches session lifecycle events to handlers', () => {
    const onSessionRenamed = vi.fn();
    const onSessionDeleted = vi.fn();
    const { client } = makeClient({ handlers: { onSessionRenamed, onSessionDeleted } });
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({ type: 'session_renamed', name: 'Team map' });
    es.emit({ type: 'session_deleted', deleted_by: 'client-other' });
    expect(onSessionRenamed).toHaveBeenCalledWith('Team map');
    expect(onSessionDeleted).toHaveBeenCalledWith('client-other');
  });

  it('passes since_seq once a seq is known and stops after close', async () => {
    const { client, fetchImpl } = makeClient();
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 7, session: { state: {} } });
    client.close();
    client.syncState({ node_refs: ['a'] });
    await flush();
    expect(fetchImpl.calls).toHaveLength(0);
    expect(FakeEventSource.instances[0].closed).toBe(true);
  });

  it('schedules a backoff reconnect when the handshake is rejected (readyState CLOSED)', () => {
    vi.useFakeTimers();
    try {
      const { client } = makeClient();
      client.connect();
      expect(FakeEventSource.instances).toHaveLength(1);

      // Simulate a 429 handshake failure: EventSource moves to readyState CLOSED.
      FakeEventSource.instances[0].error(true);

      // The dead source must be torn down immediately.
      expect(client.connected).toBe(false);
      expect(FakeEventSource.instances[0].closed).toBe(true);

      // No new connection yet — reconnect is on a backoff timer.
      expect(FakeEventSource.instances).toHaveLength(1);

      // After the backoff fires a fresh EventSource is opened.
      vi.advanceTimersByTime(5000);
      expect(FakeEventSource.instances).toHaveLength(2);
      expect(client.connected).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it('does not schedule a backoff reconnect for transient errors on an open stream', () => {
    vi.useFakeTimers();
    try {
      const { client } = makeClient();
      client.connect();
      const es = FakeEventSource.instances[0];
      es.emit({ type: 'snapshot', seq: 0, session: { state: {} } });

      // Transient drop on already-open stream — native reconnect handles it.
      es.error(false);

      // Source is still registered (browser is reconnecting natively).
      expect(client.connected).toBe(true);
      // No new EventSource is created by our code.
      vi.advanceTimersByTime(5000);
      expect(FakeEventSource.instances).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('cancels the scheduled backoff reconnect when close() is called', () => {
    vi.useFakeTimers();
    try {
      const { client } = makeClient();
      client.connect();
      FakeEventSource.instances[0].error(true); // schedules reconnect
      client.close();

      // Reconnect timer was cancelled; no new connection fires.
      vi.advanceTimersByTime(5000);
      expect(FakeEventSource.instances).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('proactively chunks an oversized queue against the server batch caps (R9)', async () => {
    const fetchImpl = makeFetch([
      { ok: true, status: 200, json: async () => ({ seq: 500 }) },
      { ok: true, status: 200, json: async () => ({ seq: 600 }) },
    ]);
    const { client } = makeClient({ fetchImpl });
    client.connect();
    FakeEventSource.instances[0].emit({ type: 'snapshot', seq: 0, session: { state: {} } });

    // 600 distinct annotation_created ops — one op per annotation id — well
    // over the server's 500-op-per-batch cap but nowhere near the byte cap.
    const annotations = Array.from({ length: 600 }, (_, i) => ({
      id: `ann-${i}`,
      kind: 'note',
      text: 'x',
      position: { x: 0, y: 0 },
    }));
    client.syncState({ annotations });

    // Let the debounced flush chain run to completion (each flush's `finally`
    // reschedules the next one while the queue is non-empty).
    await new Promise((r) => setTimeout(r, 50));

    expect(fetchImpl.calls).toHaveLength(2);
    expect(fetchImpl.calls[0].body.ops).toHaveLength(500);
    expect(fetchImpl.calls[1].body.ops).toHaveLength(100);
  });

  it('ignores a duplicate or stale sequenced op event (R15)', () => {
    const onRemoteOps = vi.fn();
    const { client } = makeClient({ handlers: { onRemoteOps } });
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({ type: 'snapshot', seq: 5, session: { state: {} } });

    es.emit({
      type: 'op',
      client_id: 'client-other',
      op: { op: 'nodes_added', node_ids: ['a'] },
      seq: 6,
    });
    expect(onRemoteOps).toHaveBeenCalledTimes(1);
    expect(client.seq).toBe(6);

    // A duplicate delivery of the same seq (e.g. queued before catch-up and
    // replayed again by catch-up) must not be re-applied.
    es.emit({
      type: 'op',
      client_id: 'client-other',
      op: { op: 'nodes_added', node_ids: ['a'] },
      seq: 6,
    });
    expect(onRemoteOps).toHaveBeenCalledTimes(1);
    expect(client.seq).toBe(6);

    // A stale seq (older than what's already applied) is ignored too.
    es.emit({
      type: 'op',
      client_id: 'client-other',
      op: { op: 'nodes_added', node_ids: ['b'] },
      seq: 4,
    });
    expect(onRemoteOps).toHaveBeenCalledTimes(1);
    expect(client.seq).toBe(6);
  });

  it('reconnect since_seq reflects applied stream events, not the POST-inflated _seq', async () => {
    // Scenario: our own ops POST response advances _seq to 11, but a concurrent op
    // at seq 10 from another client hasn't arrived on the SSE stream yet.
    // If a disconnect/reconnect happens in that gap and uses _seq (11) as since_seq,
    // the server's ops_since(11) returns nothing — the missing seq-10 op is silently
    // dropped forever. since_seq must use _appliedSeq (highest seq actually delivered
    // from the stream) so the server's catch_up includes seq 10.
    const fetchImpl = makeFetch([
      { ok: true, status: 200, json: async () => ({ applied: [], seq: 11 }) },
    ]);
    const { client } = makeClient({ fetchImpl });
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({ type: 'snapshot', seq: 9, session: { state: {} } });

    // Our own op is assigned seq 11 by the server — _seq advances to 11 via POST
    // response, but _appliedSeq stays at 9 (no stream event for 10 or 11 yet).
    client.syncState({ node_refs: ['mine'] });
    await flush();
    expect(client.seq).toBe(11); // _seq = 11 (POST-inflated)

    // Disconnect: terminal error tears down the source and nulls _source.
    es.error(true);
    expect(client.connected).toBe(false);

    // Reconnect (bypassing the backoff timer — we fire connect() directly as the
    // timer callback would).
    client.connect();

    expect(FakeEventSource.instances).toHaveLength(2);
    const reconnectUrl = new URL(FakeEventSource.instances[1].url, 'http://x');
    // Must use _appliedSeq (9), not _seq (11), so the server replays seq 10.
    expect(reconnectUrl.searchParams.get('since_seq')).toBe('9');
  });

  it('reconnect since_seq tracks the highest seq delivered by the stream, including remote ops', () => {
    const { client } = makeClient();
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({ type: 'snapshot', seq: 5, session: { state: {} } });

    // A remote op at seq 6 arrives via the stream — _appliedSeq advances to 6.
    es.emit({
      type: 'op',
      client_id: 'client-other',
      op: { op: 'nodes_added', node_ids: ['a'] },
      seq: 6,
    });

    // Disconnect and reconnect.
    es.error(true);
    client.connect();

    expect(FakeEventSource.instances).toHaveLength(2);
    const reconnectUrl = new URL(FakeEventSource.instances[1].url, 'http://x');
    expect(reconnectUrl.searchParams.get('since_seq')).toBe('6');
  });

  it('still applies a concurrent op whose broadcast arrives after our own POST response advanced _seq ahead of it (R15)', async () => {
    // Two clients editing at once: another client's op lands at seq 10 and its
    // broadcast is in flight; our own op is assigned seq 11 by the server, and
    // the POST response for it (a separate HTTP round-trip from the SSE
    // stream) arrives *before* the other client's seq-10 broadcast does. The
    // dedup guard must not mistake "our own _seq is already past 10" for
    // "we've already applied 10" — that op was never delivered to us yet.
    const onRemoteOps = vi.fn();
    const fetchImpl = makeFetch([
      { ok: true, status: 200, json: async () => ({ applied: [], seq: 11 }) },
    ]);
    const { client } = makeClient({ fetchImpl, handlers: { onRemoteOps } });
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({ type: 'snapshot', seq: 9, session: { state: {} } });

    client.syncState({ node_refs: ['mine'] });
    await flush();
    expect(client.seq).toBe(11); // advanced eagerly by the POST response

    // The other client's earlier (lower-seq) op now arrives over the still-open
    // SSE stream — it must still be applied, not dropped as "stale".
    es.emit({
      type: 'op',
      client_id: 'client-other',
      op: { op: 'nodes_added', node_ids: ['theirs'] },
      seq: 10,
    });
    expect(onRemoteOps).toHaveBeenCalledTimes(1);
    expect(onRemoteOps).toHaveBeenCalledWith([{ op: 'nodes_added', node_ids: ['theirs'] }], {
      clientId: 'client-other',
    });
  });
});

// ── Presence + selection claims (design step 7) ────────────────────────────
describe('SessionSyncClient presence + claims', () => {
  const roster = [
    { client_id: 'client-me', display_name: 'Me', color: '#111' },
    { client_id: 'client-other', display_name: 'Ada', color: '#e6194b' },
  ];

  it('seeds roster and claims from a snapshot, excluding own claims', () => {
    const onPresence = vi.fn();
    const onSelections = vi.fn();
    const { client } = makeClient({ handlers: { onPresence, onSelections } });
    client.connect();
    FakeEventSource.instances[0].emit({
      type: 'snapshot',
      seq: 0,
      session: { state: {} },
      roster,
      claims: { 'node-a': 'client-other', 'node-b': 'client-me' },
    });
    expect(onPresence).toHaveBeenLastCalledWith(roster);
    // Own claim on node-b is excluded; the remote claim is resolved to colour+name.
    expect(client.getRemoteSelections()).toEqual({
      'node-a': { clientId: 'client-other', color: '#e6194b', displayName: 'Ada' },
    });
    expect(onSelections).toHaveBeenLastCalledWith(client.getRemoteSelections());
  });

  it('adds and removes roster members on join / leave and drops departed claims', () => {
    const onPresence = vi.fn();
    const onSelections = vi.fn();
    const { client } = makeClient({ handlers: { onPresence, onSelections } });
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({
      type: 'snapshot',
      seq: 0,
      session: { state: {} },
      roster,
      claims: { 'node-a': 'client-other' },
    });
    expect(client.getRemoteSelections()['node-a']).toBeTruthy();

    es.emit({ type: 'presence_left', client_id: 'client-other' });
    expect(client.getRoster().map((m) => m.client_id)).toEqual(['client-me']);
    // The departed member's claim marker is released.
    expect(client.getRemoteSelections()).toEqual({});

    es.emit({
      type: 'presence_joined',
      member: { client_id: 'client-3', display_name: 'Zoe', color: '#3cb44b' },
    });
    expect(client.getRoster().map((m) => m.client_id)).toContain('client-3');
  });

  it('tracks remote claim / release ops but ignores its own echoes', () => {
    const { client } = makeClient();
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({ type: 'snapshot', seq: 0, session: { state: {} }, roster, claims: {} });

    es.emit({
      type: 'op',
      client_id: 'client-other',
      op: { op: 'selection_claimed', element_ids: ['node-a', 'node-b'] },
    });
    expect(Object.keys(client.getRemoteSelections()).sort()).toEqual(['node-a', 'node-b']);

    es.emit({
      type: 'op',
      client_id: 'client-other',
      op: { op: 'selection_released', element_ids: ['node-a'] },
    });
    expect(Object.keys(client.getRemoteSelections())).toEqual(['node-b']);

    // Our own claim echo must not render as a remote marker.
    es.emit({
      type: 'op',
      client_id: 'client-me',
      op: { op: 'selection_claimed', element_ids: ['node-c'] },
    });
    expect(client.getRemoteSelections()['node-c']).toBeUndefined();
  });

  it('setLocalSelection emits claim for added and release for removed ids', async () => {
    const { client, fetchImpl } = makeClient();
    client.connect();
    FakeEventSource.instances[0].emit({
      type: 'snapshot',
      seq: 0,
      session: { state: {} },
      roster,
      claims: {},
    });

    client.setLocalSelection(['node-a', 'node-b']);
    await flush();
    const first = fetchImpl.calls.at(-1).body.ops;
    expect(first).toContainEqual({ op: 'selection_claimed', element_ids: ['node-a', 'node-b'] });

    client.setLocalSelection(['node-b', 'node-c']);
    await flush();
    const ops = fetchImpl.calls.flatMap((c) => c.body.ops);
    expect(ops).toContainEqual({ op: 'selection_released', element_ids: ['node-a'] });
    expect(ops).toContainEqual({ op: 'selection_claimed', element_ids: ['node-c'] });

    // Re-declaring the same selection is a no-op (no new ops).
    const before = fetchImpl.calls.length;
    client.setLocalSelection(['node-b', 'node-c']);
    await flush();
    expect(fetchImpl.calls.length).toBe(before);
  });

  it('re-advertises the local selection on reconnect (server dropped it on disconnect)', async () => {
    const { client, fetchImpl } = makeClient();
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({ type: 'snapshot', seq: 0, session: { state: {} }, roster, claims: {} });
    client.setLocalSelection(['node-a']);
    await flush();

    // Reconnect delivers a catch_up: the local selection is re-advertised so
    // other clients get the marker back.
    es.emit({ type: 'catch_up', seq: 1, ops: [], roster, claims: {} });
    await flush();
    const reclaims = fetchImpl.calls
      .flatMap((c) => c.body.ops)
      .filter((o) => o.op === 'selection_claimed' && o.element_ids.includes('node-a'));
    expect(reclaims.length).toBeGreaterThanOrEqual(2); // initial claim + reconnect re-advertise
  });

  it('does not queue renewal claims while the stream is disconnected', () => {
    vi.useFakeTimers();
    try {
      const { client, fetchImpl } = makeClient();
      client.connect();
      const es = FakeEventSource.instances[0];
      es.emit({ type: 'snapshot', seq: 0, session: { state: {} }, roster, claims: {} });
      client.setLocalSelection(['node-a']);
      vi.advanceTimersByTime(5); // let the initial claim flush
      const afterClaim = fetchImpl.calls.length;
      expect(afterClaim).toBeGreaterThan(0);

      es.error(); // stream drops → not ready
      vi.advanceTimersByTime(60_000); // several renewal intervals pass
      // No new ops were sent while disconnected (renewals are skipped).
      expect(fetchImpl.calls.length).toBe(afterClaim);
    } finally {
      vi.useRealTimers();
    }
  });

  it('expires a remote claim client-side once its TTL passes', () => {
    let now = 1000;
    const { client } = makeClient({ nowFn: () => now });
    client.connect();
    const es = FakeEventSource.instances[0];
    es.emit({ type: 'snapshot', seq: 0, session: { state: {} }, roster, claims: {} });
    es.emit({
      type: 'op',
      client_id: 'client-other',
      op: { op: 'selection_claimed', element_ids: ['node-a'] },
    });
    expect(client.getRemoteSelections()['node-a']).toBeTruthy();
    // Advance beyond the 30 s TTL: the claim no longer renders.
    now += 31_000;
    expect(client.getRemoteSelections()).toEqual({});
  });
});
