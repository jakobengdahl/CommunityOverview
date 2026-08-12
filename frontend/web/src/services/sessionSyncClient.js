/**
 * Realtime sync client for shared visualization sessions (design step 6).
 *
 * Owns the op-protocol SSE subscription and the upstream op POST channel that
 * together replace step 4's full-state PUT (see
 * `docs/MULTI_USER_SESSIONS_DESIGN.md` §3.3/§3.7). Responsibilities:
 *
 *  - Subscribe to `GET /api/sessions/{id}/stream` and dispatch parsed events
 *    (snapshot, catch_up, applied ops, presence, rename, delete, MCP command)
 *    to handlers that `App.jsx` binds to the Zustand store and canvas signals.
 *  - Derive the minimal op set from a full-state snapshot (`syncState`) by
 *    diffing against the last-synced baseline, batch ops and POST them to
 *    `/ops`. This keeps every mutation path (search, expand, drag, annotations)
 *    converging without wiring an explicit op emitter into each one.
 *  - Stay echo-safe: the baseline mirrors what the server already has, so a
 *    remote op the host re-applies (updating the baseline) never bounces back
 *    out as a fresh local op — no store guard flag needed.
 *  - Reconnect + catch up: the browser `EventSource` auto-reconnects; the client
 *    passes `since_seq` so the server replays missed ops or sends a snapshot,
 *    which the host treats as a resync signal.
 *
 * The module is framework-agnostic (no React, no direct store import): fetch and
 * EventSource are injectable so it unit-tests against fakes.
 */

// Positions are floats from ReactFlow; ignore sub-pixel jitter so idle canvases
// do not emit a stream of no-op node_moved ops.
const POSITION_EPSILON = 0.5;

// Above this many changed positions in one snapshot, collapse them into a single
// layout_applied op instead of one node_moved each — keeps bulk layouts and
// initial materialisation within the server's per-batch op cap.
const LAYOUT_BATCH_THRESHOLD = 20;

// Mirror the server's per-batch caps (design §3.9) so an oversized queue is
// chunked proactively (R9) instead of only after the server rejects a
// too-large batch and the client falls back to one-op-at-a-time recovery.
// Kept comfortably under the server's actual limits (500 ops / 256 KB) to
// leave margin for JSON encoding differences between JSON.stringify and the
// server's json.dumps.
const MAX_OPS_PER_BATCH = 500;
const MAX_BATCH_BYTES = 240 * 1024;

// A single `POST /ops` that never settles (a hung connection through a proxy —
// SSE deployments commonly sit behind Cloud Run / an ingress that can hold a
// half-open request open indefinitely) must not wedge the outbound channel
// forever. Without a bound, `_flush`'s `await this._fetch(...)` never returns,
// the `finally` that clears `_flushing` never runs, and every later flush
// short-circuits on the `_flushing` guard — so all subsequent ops (moves, node
// adds, everything) silently stop reaching the server for the life of the tab.
// A timeout aborts the stuck request so the batch is requeued and retried —
// the same at-least-once retry the pre-existing network-error path already did.
// Resends are safe: set ops and moves are idempotent and annotation_created is
// an upsert-by-id server-side (R3), so a resend after a lost response converges.
const DEFAULT_REQUEST_TIMEOUT_MS = 20_000;

// Selection claims are advisory soft-locks (design 3.5). The server expires a
// claim 30 s after its last renewal; the local client renews well inside that
// window and mirrors the same TTL so a departed collaborator's marker never
// lingers even if its disconnect event is missed.
const CLAIM_TTL_MS = 30_000;
const CLAIM_RENEW_MS = 15_000;

const EMPTY_MIRROR = Object.freeze({
  node_refs: [],
  positions: {},
  hidden_node_ids: [],
  hidden_edge_ids: [],
  annotations: [],
});

function roundPos(p) {
  return { x: Math.round((p?.x || 0) * 100) / 100, y: Math.round((p?.y || 0) * 100) / 100 };
}

function samePos(a, b) {
  return (
    Math.abs((a?.x || 0) - (b?.x || 0)) <= POSITION_EPSILON &&
    Math.abs((a?.y || 0) - (b?.y || 0)) <= POSITION_EPSILON
  );
}

/** Normalise a host state object into the comparable baseline mirror shape. */
export function normalizeMirror(state) {
  const s = state || {};
  const positions = {};
  for (const [id, pos] of Object.entries(s.positions || {})) {
    if (pos && typeof pos.x === 'number' && typeof pos.y === 'number') {
      positions[id] = roundPos(pos);
    }
  }
  return {
    node_refs: Array.from(new Set(s.node_refs || [])),
    positions,
    hidden_node_ids: Array.from(new Set(s.hidden_node_ids || [])),
    hidden_edge_ids: Array.from(new Set(s.hidden_edge_ids || [])),
    annotations: Array.isArray(s.annotations) ? s.annotations : [],
  };
}

function diffAdded(prev, next) {
  const before = new Set(prev);
  return next.filter((id) => !before.has(id));
}

// Stable serialisation for annotation equality (key order can vary between the
// load path and the canvas snapshot path, so JSON.stringify alone is unsafe).
function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') {
    const out = {};
    for (const key of Object.keys(value).sort()) out[key] = canonical(value[key]);
    return out;
  }
  return value;
}

function annotationsEqual(a, b, { ignoreMembers = false } = {}) {
  const strip = (ann) => {
    const copy = { ...ann };
    delete copy.updated_at;
    delete copy.created_by;
    if (ignoreMembers) delete copy.member_node_ids;
    return copy;
  };
  return JSON.stringify(canonical(strip(a))) === JSON.stringify(canonical(strip(b)));
}

/**
 * Compute the ordered op batch that turns `prev` (last synced) into `next`.
 * Pure — exported for unit testing. Order: additions and creates before the
 * changes that depend on them, deletions/removals last.
 */
export function computeOps(prevState, nextState) {
  const prev = normalizeMirror(prevState);
  const next = normalizeMirror(nextState);
  const ops = [];

  const addedNodes = diffAdded(prev.node_refs, next.node_refs);
  const removedNodes = diffAdded(next.node_refs, prev.node_refs);
  if (addedNodes.length) ops.push({ op: 'nodes_added', node_ids: addedNodes });

  // Positions: only for nodes that survive in next. New-with-position and moved
  // both count. Bulk changes collapse into one layout_applied op.
  const movedIds = next.node_refs.filter((id) => {
    const np = next.positions[id];
    if (!np) return false;
    const pp = prev.positions[id];
    return !pp || !samePos(pp, np);
  });
  if (movedIds.length > LAYOUT_BATCH_THRESHOLD) {
    const positions = {};
    movedIds.forEach((id) => {
      positions[id] = next.positions[id];
    });
    ops.push({ op: 'layout_applied', positions });
  } else {
    movedIds.forEach((id) =>
      ops.push({ op: 'node_moved', node_id: id, position: next.positions[id] })
    );
  }

  const hiddenAdded = diffAdded(prev.hidden_node_ids, next.hidden_node_ids);
  const hiddenRemoved = diffAdded(next.hidden_node_ids, prev.hidden_node_ids);
  if (hiddenAdded.length) ops.push({ op: 'nodes_hidden', node_ids: hiddenAdded });
  if (hiddenRemoved.length) ops.push({ op: 'nodes_shown', node_ids: hiddenRemoved });

  const edgeHiddenAdded = diffAdded(prev.hidden_edge_ids, next.hidden_edge_ids);
  const edgeHiddenRemoved = diffAdded(next.hidden_edge_ids, prev.hidden_edge_ids);
  if (edgeHiddenAdded.length) ops.push({ op: 'edges_hidden', edge_ids: edgeHiddenAdded });
  if (edgeHiddenRemoved.length) ops.push({ op: 'edges_shown', edge_ids: edgeHiddenRemoved });

  // Annotations, keyed by id.
  const prevAnn = new Map(prev.annotations.filter((a) => a && a.id).map((a) => [a.id, a]));
  const nextAnn = new Map(next.annotations.filter((a) => a && a.id).map((a) => [a.id, a]));
  for (const [id, ann] of nextAnn) {
    const before = prevAnn.get(id);
    if (!before) {
      ops.push({ op: 'annotation_created', annotation: ann });
      continue;
    }
    // A group's membership has its own op; other fields go through annotation_updated.
    if (ann.kind === 'group') {
      const beforeMembers = before.member_node_ids || [];
      const nextMembers = ann.member_node_ids || [];
      if (JSON.stringify([...beforeMembers].sort()) !== JSON.stringify([...nextMembers].sort())) {
        ops.push({ op: 'group_membership_changed', group_id: id, member_node_ids: nextMembers });
      }
      if (!annotationsEqual(before, ann, { ignoreMembers: true })) {
        ops.push({ op: 'annotation_updated', annotation: ann });
      }
    } else if (!annotationsEqual(before, ann)) {
      ops.push({ op: 'annotation_updated', annotation: ann });
    }
  }
  for (const id of prevAnn.keys()) {
    if (!nextAnn.has(id)) ops.push({ op: 'annotation_deleted', annotation_id: id });
  }

  if (removedNodes.length) ops.push({ op: 'nodes_removed', node_ids: removedNodes });
  return ops;
}

/**
 * Apply a single applied op to the baseline mirror (client-side mirror of the
 * server's state transforms). Keeping the baseline current for *remote* ops is
 * what makes the outgoing diff echo-safe: once a remote change is folded into
 * the baseline, the next local snapshot no longer looks like it added it.
 * Pure — exported for unit testing. Returns a new mirror.
 */
export function applyOpToMirror(mirrorState, op) {
  const m = normalizeMirror(mirrorState);
  const type = op && op.op;
  switch (type) {
    case 'nodes_added':
      m.node_refs = Array.from(new Set([...m.node_refs, ...(op.node_ids || [])]));
      break;
    case 'nodes_removed': {
      const drop = new Set(op.node_ids || []);
      m.node_refs = m.node_refs.filter((id) => !drop.has(id));
      m.positions = Object.fromEntries(Object.entries(m.positions).filter(([id]) => !drop.has(id)));
      m.hidden_node_ids = m.hidden_node_ids.filter((id) => !drop.has(id));
      m.annotations = m.annotations.map((a) =>
        Array.isArray(a.member_node_ids)
          ? { ...a, member_node_ids: a.member_node_ids.filter((id) => !drop.has(id)) }
          : a
      );
      break;
    }
    case 'node_moved':
      if (op.node_id && op.position)
        m.positions = { ...m.positions, [op.node_id]: roundPos(op.position) };
      break;
    case 'layout_applied':
      for (const [id, pos] of Object.entries(op.positions || {})) m.positions[id] = roundPos(pos);
      break;
    case 'nodes_hidden':
      m.hidden_node_ids = Array.from(new Set([...m.hidden_node_ids, ...(op.node_ids || [])]));
      break;
    case 'nodes_shown': {
      const drop = new Set(op.node_ids || []);
      m.hidden_node_ids = m.hidden_node_ids.filter((id) => !drop.has(id));
      break;
    }
    case 'edges_hidden':
      m.hidden_edge_ids = Array.from(new Set([...m.hidden_edge_ids, ...(op.edge_ids || [])]));
      break;
    case 'edges_shown': {
      const drop = new Set(op.edge_ids || []);
      m.hidden_edge_ids = m.hidden_edge_ids.filter((id) => !drop.has(id));
      break;
    }
    case 'annotation_created':
    case 'annotation_updated': {
      const ann = op.annotation;
      if (ann && ann.id) {
        const idx = m.annotations.findIndex((a) => a.id === ann.id);
        if (idx === -1) m.annotations = [...m.annotations, ann];
        else {
          const next = m.annotations.slice();
          next[idx] = { ...next[idx], ...ann };
          m.annotations = next;
        }
      }
      break;
    }
    case 'annotation_deleted':
      m.annotations = m.annotations.filter((a) => a.id !== op.annotation_id);
      break;
    case 'group_membership_changed':
      m.annotations = m.annotations.map((a) =>
        a.id === op.group_id && a.kind === 'group'
          ? { ...a, member_node_ids: [...(op.member_node_ids || [])] }
          : a
      );
      break;
    default:
      break; // session_renamed etc. carry no mirror state
  }
  return m;
}

export class SessionSyncClient {
  /**
   * @param {Object} opts
   * @param {string} opts.sessionId
   * @param {string} opts.clientId
   * @param {string|null} [opts.displayName]
   * @param {string} opts.streamUrl  Full SSE URL (query appended by the client).
   * @param {string} opts.opsUrl     Full POST URL for op batches.
   * @param {Object} [opts.handlers] onReady, onResync, onRemoteOps, onPresence,
   *   onSelections, onPresenceJoined, onPresenceLeft, onSessionRenamed,
   *   onSessionDeleted, onDropped, onCommand.
   * @param {number} [opts.flushIntervalMs]
   * @param {Function} [opts.fetchImpl]
   * @param {Function} [opts.EventSourceImpl]
   * @param {Function} [opts.nowFn] Monotonic clock for claim TTLs (injectable for tests).
   */
  constructor({
    sessionId,
    clientId,
    displayName = null,
    streamUrl,
    opsUrl,
    handlers = {},
    flushIntervalMs = 150,
    requestTimeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
    fetchImpl,
    EventSourceImpl,
    nowFn,
  }) {
    this.sessionId = sessionId;
    this.clientId = clientId;
    this.displayName = displayName;
    this.streamUrl = streamUrl;
    this.opsUrl = opsUrl;
    this.handlers = handlers;
    this.flushIntervalMs = flushIntervalMs;
    // Upper bound on how long a single ops POST may stay in flight before it is
    // aborted and retried; <= 0 disables the bound. See DEFAULT_REQUEST_TIMEOUT_MS.
    this._requestTimeoutMs = requestTimeoutMs;
    this._fetch = fetchImpl || (typeof fetch !== 'undefined' ? fetch.bind(globalThis) : null);
    this._EventSource =
      EventSourceImpl || (typeof EventSource !== 'undefined' ? EventSource : null);
    this._now = nowFn || (() => Date.now());

    this._baseline = EMPTY_MIRROR;
    this._queue = [];
    this._seq = 0;
    // Highest seq actually applied from a stream event (snapshot/catch_up/op),
    // as opposed to `_seq` — which `_flush` also optimistically advances to the
    // POST response's `body.seq`, the server's *global* seq right after our
    // batch landed. That global value can be ahead of what our own SSE stream
    // has delivered yet (a concurrent op from another client that committed
    // just before ours, whose broadcast is still in flight on a different HTTP
    // connection). The R15 duplicate/stale guard must dedupe against what we've
    // actually seen, not that optimistic ceiling, or a legitimately-not-yet-
    // -received op arriving after our own POST response would be silently
    // dropped forever instead of applied late.
    this._appliedSeq = 0;
    this._ready = false; // stream has delivered its first event (session exists)
    this._hadSnapshot = false;
    this._source = null;
    this._flushTimer = null;
    this._retryTimer = null;
    this._reconnectTimer = null;
    this._closed = false;
    this._flushing = false;
    this._forceSingle = false;

    // Presence + selection claims (design 3.4 / 3.5), all ephemeral.
    this._roster = new Map(); // client_id -> member {client_id, display_name, color}
    this._claims = new Map(); // element_id -> { clientId, expiresAt }
    this._localSelection = []; // element ids this client currently claims
    this._renewTimer = null;
    this._pruneTimer = null;
  }

  get seq() {
    return this._seq;
  }
  get connected() {
    return this._source != null;
  }

  /**
   * The position the baseline currently holds for a node, or null. Lets the host
   * place a node that another client added-then-moved at its authoritative spot,
   * even though the two ops arrive as separate async-applied events.
   */
  baselinePosition(nodeId) {
    const p = this._baseline.positions[nodeId];
    return p ? { x: p.x, y: p.y } : null;
  }

  // ── Presence + selection claims (design 3.4 / 3.5) ─────────────────────────

  /** Current roster as an array of members ({client_id, display_name, color}). */
  getRoster() {
    return Array.from(this._roster.values());
  }

  /**
   * Live selection claims held by *other* clients, as
   * ``element_id -> { clientId, color, displayName }``. Own claims are excluded
   * (the local user already sees their own selection natively) and expired
   * claims are skipped, so this is safe to render directly as remote markers.
   */
  getRemoteSelections() {
    const now = this._now();
    const out = {};
    for (const [eid, claim] of this._claims) {
      if (claim.expiresAt <= now || claim.clientId === this.clientId) continue;
      const member = this._roster.get(claim.clientId);
      if (!member) continue;
      out[eid] = {
        clientId: claim.clientId,
        color: member.color,
        displayName: member.display_name,
      };
    }
    return out;
  }

  /**
   * Declare which elements the local user has selected. Diffs against the last
   * declared set: newly selected elements are claimed, deselected ones released,
   * and a renewal timer keeps the active claims alive server-side. No-ops when
   * the selection is unchanged, so it is safe to call on every selection event.
   */
  setLocalSelection(elementIds) {
    const next = Array.from(
      new Set((elementIds || []).filter((id) => typeof id === 'string' && id))
    );
    const nextSet = new Set(next);
    const prevSet = new Set(this._localSelection);
    const added = next.filter((id) => !prevSet.has(id));
    const removed = this._localSelection.filter((id) => !nextSet.has(id));
    this._localSelection = next;
    if (removed.length) this._enqueue([{ op: 'selection_released', element_ids: removed }]);
    if (added.length) this._enqueue([{ op: 'selection_claimed', element_ids: added }]);
    if (next.length) this._startRenewTimer();
    else this._stopRenewTimer();
  }

  _emitPresence() {
    if (this.handlers.onPresence) this.handlers.onPresence(this.getRoster());
  }

  _emitSelections() {
    if (this.handlers.onSelections) this.handlers.onSelections(this.getRemoteSelections());
  }

  _seedPresence(roster, claims) {
    this._roster = new Map(
      (roster || []).filter((m) => m && m.client_id).map((m) => [m.client_id, m])
    );
    const now = this._now();
    this._claims = new Map();
    for (const [eid, clientId] of Object.entries(claims || {})) {
      this._claims.set(eid, { clientId, expiresAt: now + CLAIM_TTL_MS });
    }
    this._emitPresence();
    this._emitSelections();
  }

  _applyClaimOp(clientId, op) {
    const ids = Array.isArray(op.element_ids) ? op.element_ids : [];
    if (!ids.length) return;
    if (op.op === 'selection_claimed') {
      const expiresAt = this._now() + CLAIM_TTL_MS;
      for (const eid of ids) this._claims.set(eid, { clientId, expiresAt });
    } else {
      for (const eid of ids) {
        const held = this._claims.get(eid);
        if (held && held.clientId === clientId) this._claims.delete(eid);
      }
    }
    this._emitSelections();
  }

  _pruneClaims() {
    const now = this._now();
    let changed = false;
    for (const [eid, claim] of this._claims) {
      if (claim.expiresAt <= now) {
        this._claims.delete(eid);
        changed = true;
      }
    }
    if (changed) this._emitSelections();
  }

  _startPruneTimer() {
    if (this._pruneTimer || this._closed) return;
    this._pruneTimer = setInterval(() => this._pruneClaims(), CLAIM_TTL_MS / 3);
  }

  _startRenewTimer() {
    if (this._renewTimer || this._closed) return;
    this._renewTimer = setInterval(() => {
      if (!this._localSelection.length) {
        this._stopRenewTimer();
        return;
      }
      // Skip renewals while the stream is down: the server released this
      // client's claims on disconnect and reseeds on reconnect, so queuing
      // renewal ops that cannot be sent would only grow the backlog. The
      // reconnect handler re-advertises the selection instead.
      if (!this._ready) return;
      this._enqueue([{ op: 'selection_claimed', element_ids: this._localSelection.slice() }]);
    }, CLAIM_RENEW_MS);
  }

  /**
   * Re-claim the current local selection after a reconnect. The server releases
   * a client's claims when its stream drops, so on reconnect other clients have
   * lost this user's selection markers — re-emit them to restore the markers.
   */
  _readvertiseSelection() {
    if (this._localSelection.length) {
      this._enqueue([{ op: 'selection_claimed', element_ids: this._localSelection.slice() }]);
    }
  }

  _stopRenewTimer() {
    if (this._renewTimer) {
      clearInterval(this._renewTimer);
      this._renewTimer = null;
    }
  }

  /** Open the SSE stream. Idempotent. */
  connect() {
    if (this._source || this._closed || !this._EventSource) return;
    this._startPruneTimer();
    const params = new URLSearchParams({ client_id: this.clientId });
    if (this.displayName) params.set('name', this.displayName);
    if (this._appliedSeq > 0) params.set('since_seq', String(this._appliedSeq));
    const url = `${this.streamUrl}?${params.toString()}`;
    const source = new this._EventSource(url);
    source.onmessage = (e) => {
      if (!e || !e.data) return;
      let data;
      try {
        data = JSON.parse(e.data);
      } catch {
        return;
      }
      this._handleEvent(data);
    };
    source.onerror = () => {
      this._ready = false;
      if (source.readyState === 2) {
        // The connection was never opened (e.g. a 429 handshake rejection): the
        // browser closes the EventSource permanently (readyState CLOSED) and will
        // not auto-reconnect. Tear it down and schedule a backoff reconnect.
        try {
          source.close();
        } catch {
          /* ignore */
        }
        this._source = null;
        this._scheduleReconnect();
      }
      // readyState 0 (CONNECTING) means a drop of an already-open stream; the
      // browser is already retrying, so native reconnect will recover — no action.
    };
    this._source = source;
  }

  /** Set the synced baseline without emitting ops (after a load or remote apply). */
  setBaseline(state) {
    this._baseline = normalizeMirror(state);
  }

  /**
   * Diff a full host-state snapshot against the baseline and enqueue the
   * resulting ops. The baseline advances optimistically; transient POST
   * failures are retried so it never claims un-delivered state.
   */
  syncState(state) {
    const next = normalizeMirror(state);
    const ops = computeOps(this._baseline, next);
    this._baseline = next;
    if (ops.length) this._enqueue(ops);
  }

  _enqueue(ops) {
    for (const op of ops) this._queue.push(op);
    this._scheduleFlush();
  }

  /**
   * Force an immediate flush attempt, bypassing the debounce and the `_ready`
   * gate. Used before the client is torn down on session switch so last-moment
   * ops still leave even if the stream never delivered a snapshot (e.g. the
   * user switches away before the session confirms): the POST is initiated
   * synchronously (its `fetch` survives a following `close()`, which only
   * tears down the EventSource, not the in-flight request).
   *
   * In `_forceSingle` recovery mode the regular flush sends one op per call and
   * relies on later flushes for the rest — but teardown gives no later flush, and
   * a following `close()` would drop the remainder. Drain the whole queue here as
   * sequential single-op batches instead (the loop holds its own copy of the
   * queue, so `close()` cannot clear it): a poisoned op still fails alone while
   * the valid ops behind it get their best-effort send. `base_seq` is
   * informational server-side, so responses need no per-op handling. Draining is
   * safe even while a debounced single-op `_flush` is in flight — that op was
   * already spliced out of the queue, and on failure it re-concats into a queue
   * that is inert once `close()` has run (every timer is `_closed`-guarded).
   * @returns {Promise<void>}
   */
  flush() {
    if (this._forceSingle && this._queue.length > 0 && this._fetch) {
      const pending = this._queue.splice(0);
      return (async () => {
        for (const op of pending) {
          try {
            // Bounded like every other ops POST (_postOps) so a hung request on
            // teardown cannot stall this drain loop indefinitely.
            await this._postOps([op]);
          } catch {
            /* best-effort teardown flush */
          }
        }
      })();
    }
    return this._flush({ bypassReady: true });
  }

  _scheduleFlush() {
    if (this._flushTimer || this._closed) return;
    this._flushTimer = setTimeout(() => {
      this._flushTimer = null;
      this._flush();
    }, this.flushIntervalMs);
  }

  _flushSoon() {
    if (this._queue.length) this._scheduleFlush();
  }

  /**
   * Take the next batch to send from the front of the queue. In `_forceSingle`
   * recovery mode, one op at a time (unchanged). Otherwise, chunk proactively
   * against the server's per-batch op-count and byte caps (§3.9, R9): sending
   * everything in one request and only reacting to a `413`/one-at-a-time
   * fallback after the fact wastes a round-trip on an oversized queue (a bulk
   * layout_applied plus a burst of annotation edits, say) and needlessly
   * degrades to single-op sends for the whole backlog.
   */
  _takeBatch() {
    if (this._forceSingle) return this._queue.splice(0, 1);
    const limit = Math.min(this._queue.length, MAX_OPS_PER_BATCH);
    const batch = [];
    let bytes = 0;
    for (let i = 0; i < limit; i++) {
      const opBytes = JSON.stringify(this._queue[i]).length;
      if (batch.length > 0 && bytes + opBytes > MAX_BATCH_BYTES) break;
      batch.push(this._queue[i]);
      bytes += opBytes;
    }
    // A single op over the byte cap still gets attempted alone (the server
    // will 413 it, and the existing terminal-rejection path drops it via
    // onDropped) rather than silently stalling the queue forever.
    if (batch.length === 0 && this._queue.length > 0) batch.push(this._queue[0]);
    this._queue.splice(0, batch.length);
    return batch;
  }

  /**
   * POST one op batch, bounded by `_requestTimeoutMs`. Resolves with the fetch
   * `Response`; rejects on a network error, an abort, or the timeout. The
   * timeout is enforced with a `Promise.race`-style guard rather than relying on
   * the request honouring `AbortController` (so a hung request — or a fake fetch
   * that ignores the signal — can never leave the flush loop awaiting forever;
   * that is the wedge this method exists to prevent). The abort is best-effort
   * on top, to actually cancel a real in-flight request when the runtime
   * supports it.
   */
  _postOps(batch) {
    const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
    const options = {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_id: this.clientId, base_seq: this._seq, ops: batch }),
    };
    if (controller) options.signal = controller.signal;
    const request = this._fetch(this.opsUrl, options);
    if (!(this._requestTimeoutMs > 0)) return request;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (controller) {
          try {
            controller.abort();
          } catch {
            /* ignore */
          }
        }
        reject(new Error('ops request timed out'));
      }, this._requestTimeoutMs);
      Promise.resolve(request).then(
        (resp) => {
          clearTimeout(timer);
          resolve(resp);
        },
        (err) => {
          clearTimeout(timer);
          reject(err);
        }
      );
    });
  }

  async _flush({ bypassReady = false } = {}) {
    if (this._flushing || this._closed) return;
    if ((!this._ready && !bypassReady) || !this._queue.length || !this._fetch) return;
    this._flushing = true;
    // `batch` is taken inside the try so that a throw here (e.g. JSON.stringify
    // choking on a malformed op) still runs the finally that clears `_flushing`
    // — otherwise the outbound channel would wedge exactly as a hung request
    // would. After a rejected multi-op batch, resend one op at a time so a
    // single bad op can't take valid ops down with it (the server applies a
    // batch all-or-nothing). Otherwise chunk the queue against the server's caps.
    let batch = null;
    try {
      batch = this._takeBatch();
      const resp = await this._postOps(batch);
      if (resp && resp.ok) {
        const body = await resp.json().catch(() => ({}));
        if (typeof body.seq === 'number') this._seq = body.seq;
        if (this._forceSingle && this._queue.length === 0) this._forceSingle = false;
      } else if (
        resp &&
        (resp.status === 400 || resp.status === 413 || resp.status === 404 || resp.status === 410)
      ) {
        // Terminal rejection (malformed / too large / session gone). Retrying
        // never succeeds. If this was a multi-op batch, requeue and switch to
        // one-at-a-time so only the offending op is ultimately dropped; a lone
        // rejected op is dropped outright (its effect stays in the baseline, but
        // it is genuinely un-persistable — e.g. a hard annotation-limit hit).
        if (batch.length > 1) {
          this._queue = batch.concat(this._queue);
          this._forceSingle = true;
        } else {
          if (this.handlers.onDropped) this.handlers.onDropped(batch, resp.status);
          if (this._forceSingle && this._queue.length === 0) this._forceSingle = false;
        }
      } else {
        // 429 / 5xx / unknown: requeue and back off.
        this._queue = batch.concat(this._queue);
        this._scheduleRetry();
      }
    } catch {
      // Network error, abort, or the request timeout: requeue and back off so a
      // transient stall (or a hung connection the timeout just cancelled) is
      // retried instead of permanently wedging op delivery. This is the same
      // at-least-once retry the network-error path always did; resends converge
      // (set ops/moves are idempotent, annotation_created upserts by id — R3).
      if (batch && batch.length) this._queue = batch.concat(this._queue);
      this._scheduleRetry();
    } finally {
      this._flushing = false;
      this._flushSoon();
    }
  }

  _scheduleRetry() {
    if (this._retryTimer || this._closed) return;
    this._retryTimer = setTimeout(
      () => {
        this._retryTimer = null;
        this._flush();
      },
      Math.max(500, this.flushIntervalMs * 4)
    );
  }

  _scheduleReconnect() {
    if (this._reconnectTimer || this._closed) return;
    this._reconnectTimer = setTimeout(
      () => {
        this._reconnectTimer = null;
        this.connect();
      },
      Math.max(2000, this.flushIntervalMs * 8)
    );
  }

  _handleEvent(data) {
    switch (data.type) {
      case 'snapshot':
        if (typeof data.seq === 'number') {
          this._seq = data.seq;
          this._appliedSeq = data.seq; // a snapshot brings the baseline fully up to date
        }
        this._ready = true;
        this._seedPresence(data.roster, data.claims);
        if (!this._hadSnapshot) {
          this._hadSnapshot = true;
          if (this.handlers.onReady) this.handlers.onReady(data.seq);
        } else {
          this._readvertiseSelection();
          if (this.handlers.onResync) this.handlers.onResync();
        }
        this._flushSoon();
        break;
      case 'catch_up':
        if (typeof data.seq === 'number') {
          this._seq = data.seq;
          this._appliedSeq = data.seq; // onResync below reloads state fully up to this seq
        }
        this._ready = true;
        this._hadSnapshot = true;
        this._seedPresence(data.roster, data.claims);
        // catch_up only follows a reconnect (since_seq was sent), so always
        // re-advertise the local selection the server dropped on disconnect.
        this._readvertiseSelection();
        if (Array.isArray(data.ops) && data.ops.length && this.handlers.onResync) {
          this.handlers.onResync();
        }
        this._flushSoon();
        break;
      case 'op': {
        const op = data.op || {};
        if (typeof data.seq === 'number') {
          // Duplicate/stale delivery is tolerated but was previously
          // unguarded (R15): events published between the stream's connect
          // (subscribe) and the catch-up computation are delivered twice
          // (once inside the snapshot/catch-up, once as a queued event).
          // Guard against `_appliedSeq` (what this client has actually applied
          // from the stream) rather than `_seq`: `_flush()` also advances
          // `_seq` optimistically to the POST response's `body.seq` — the
          // server's global seq right after our own batch landed, which can
          // already be ahead of a concurrent op whose broadcast is still in
          // flight on the separate SSE connection. Guarding on `_seq` there
          // would silently and permanently drop that op instead of applying
          // it (a touch late) once it arrives.
          if (data.seq <= this._appliedSeq) return;
          this._appliedSeq = data.seq;
          if (data.seq > this._seq) this._seq = data.seq;
        }
        // Claim ops are ephemeral (advisory soft-locks, never sequenced state).
        // Track other clients' claims for presence markers; our own echoes need
        // no tracking since the local user sees their own selection natively.
        if (op.op === 'selection_claimed' || op.op === 'selection_released') {
          if (data.client_id !== this.clientId) this._applyClaimOp(data.client_id, op);
          return;
        }
        if (data.client_id === this.clientId) return; // echo of our own op — baseline already has it
        // Fold the remote change into the baseline before the host applies it,
        // so the resulting local store change does not diff back out as an echo.
        this._baseline = applyOpToMirror(this._baseline, op);
        if (this.handlers.onRemoteOps)
          this.handlers.onRemoteOps([op], { clientId: data.client_id });
        break;
      }
      case 'presence_joined':
        if (data.member && data.member.client_id) {
          this._roster.set(data.member.client_id, data.member);
          this._emitPresence();
        }
        if (this.handlers.onPresenceJoined) this.handlers.onPresenceJoined(data.member);
        break;
      case 'presence_left': {
        this._roster.delete(data.client_id);
        let claimsChanged = false;
        for (const [eid, claim] of this._claims) {
          if (claim.clientId === data.client_id) {
            this._claims.delete(eid);
            claimsChanged = true;
          }
        }
        this._emitPresence();
        if (claimsChanged) this._emitSelections();
        if (this.handlers.onPresenceLeft) this.handlers.onPresenceLeft(data.client_id);
        break;
      }
      case 'session_renamed':
        if (this.handlers.onSessionRenamed) this.handlers.onSessionRenamed(data.name);
        break;
      case 'session_deleted':
        if (this.handlers.onSessionDeleted) this.handlers.onSessionDeleted(data.deleted_by);
        break;
      case 'command':
        // MCP visualization push, broadcast to every subscriber (design §3.8),
        // unlike the legacy single-consumer push stream it superseded (R5).
        if (this.handlers.onCommand) this.handlers.onCommand(data.command);
        break;
      default:
        // 'ping', unknown: ignore.
        break;
    }
  }

  close() {
    this._closed = true;
    if (this._source) {
      try {
        this._source.close();
      } catch {
        /* ignore */
      }
      this._source = null;
    }
    if (this._flushTimer) {
      clearTimeout(this._flushTimer);
      this._flushTimer = null;
    }
    if (this._retryTimer) {
      clearTimeout(this._retryTimer);
      this._retryTimer = null;
    }
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    this._stopRenewTimer();
    if (this._pruneTimer) {
      clearInterval(this._pruneTimer);
      this._pruneTimer = null;
    }
    this._queue = [];
  }
}
