// Connects the XR client to a shared session and keeps `sceneModel` current.
//
// This is the whole point of ADR 0003's "reuse the protocol, re-implement only
// the renderer" claim: `SessionSyncClient` is imported from the 2D web client
// *unchanged* — same SSE subscription, same op vocabulary, same presence and
// claim handling — and its handlers are wired to a plain scene reduction
// instead of to a React Flow store. Nothing about the protocol layer knows
// which renderer is downstream.
//
// The cross-workspace import is deliberate and temporary: lifting
// `sessionSyncClient.js` (and the session helpers in `api.js`) into a shared
// package is a follow-up, and doing it before a second consumer existed would
// have been a guess at the seam. This file is that second consumer.
//
// REST access is injected rather than imported so this module stays free of
// `window` and unit-tests in plain Node, like `domeLayout.js` and
// `sceneModel.js`. `App.jsx` supplies the real `api.js` functions.

import { SessionSyncClient } from '../../web/src/services/sessionSyncClient.js';
import {
  EMPTY_SCENE,
  applyOps,
  hydrateNodes,
  pendingNodeIds,
  sceneFromSession,
  withClaims,
  withRoster,
} from './sceneModel.js';

// Same shape the backend accepts (`SESSION_ID_RE` in
// `backend/core/session_store.py`): four groups of four digits, or the legacy
// two-group form. Validated here so a typo in the headset's short-ID field
// fails immediately instead of opening a stream the server 400s.
const SESSION_ID_RE = /^\d{4}-\d{4}(?:-\d{4}-\d{4})?$/;

export function isValidSessionId(id) {
  return typeof id === 'string' && SESSION_ID_RE.test(id.trim());
}

export class SceneSession {
  /**
   * @param {Object} opts
   * @param {string} opts.sessionId          Short session ID typed or created.
   * @param {string} opts.clientId
   * @param {string|null} [opts.displayName]
   * @param {string} opts.streamUrl
   * @param {string} opts.opsUrl
   * @param {Function} opts.loadSession      (id, {resolve}) => Promise<payload>
   * @param {Function} opts.loadNodeDetails  (id) => Promise<{node}>
   * @param {Function} [opts.onChange]       Called with {scene, status, error}.
   * @param {Function} [opts.createClient]   Injectable for tests.
   */
  constructor({
    sessionId,
    clientId,
    displayName = null,
    streamUrl,
    opsUrl,
    loadSession,
    loadNodeDetails,
    onChange = () => {},
    createClient = (opts) => new SessionSyncClient(opts),
  }) {
    this.sessionId = sessionId;
    this._loadSession = loadSession;
    this._loadNodeDetails = loadNodeDetails;
    this._onChange = onChange;
    this._scene = { ...EMPTY_SCENE, sessionId };
    this._status = 'connecting';
    this._error = null;
    this._closed = false;
    // Bumped on every authoritative reload so a slow in-flight load (or node
    // hydration) started before a resync can never overwrite the newer scene.
    this._generation = 0;
    // Node ids already sent to `loadNodeDetails`. A node deleted from the graph
    // between the op and the read never hydrates, so without this the pending
    // set would never drain and every later op batch would re-fetch it.
    this._hydrationAttempted = new Set();
    // Ops delivered while a reload is in flight, replayed on top of the loaded
    // scene. Every op in the reducer's vocabulary is idempotent, so replaying
    // one the reload already reflects is harmless.
    this._reloadBuffer = null;
    // A remote delete is terminal: the session is gone, so nothing may report
    // it connected again (see `_reload`).
    this._deleted = false;

    this._client = createClient({
      sessionId,
      clientId,
      displayName,
      streamUrl,
      opsUrl,
      handlers: {
        onReady: () => this._reload(),
        onResync: () => this._reload(),
        onRemoteOps: (ops) => this._applyOps(ops),
        onPresence: (roster) => this._update(withRoster(this._scene, roster)),
        onSelections: (claims) => this._update(withClaims(this._scene, claims)),
        onSessionRenamed: (name) => this._update({ ...this._scene, name: name ?? null }),
        onSessionDeleted: () => {
          // The server broadcasts the notice but leaves the SSE generator
          // running, and the stream endpoint is get-or-create. Left open, the
          // next auto-reconnect (headset sleep/wake, network change) would
          // recreate the session we were just told is gone and report it
          // connected again, silently replacing the notice. Stop the stream.
          this._deleted = true;
          this._client.close();
          this._setStatus('deleted');
        },
      },
    });
  }

  getState() {
    return {
      sessionId: this.sessionId,
      scene: this._scene,
      status: this._status,
      error: this._error,
    };
  }

  connect() {
    this._client.connect();
  }

  close() {
    this._closed = true;
    this._client.close();
  }

  _emit() {
    if (!this._closed) this._onChange(this.getState());
  }

  _update(scene) {
    this._scene = scene;
    this._emit();
  }

  _setStatus(status, error = null) {
    this._status = status;
    this._error = error;
    this._emit();
  }

  /**
   * Reload the authoritative session state. The op stream's snapshot carries
   * node *references*, not node data, so the 2D client re-reads the resolved
   * session on connect and on every resync — this client does exactly the same.
   */
  async _reload() {
    if (this._deleted) return;
    const generation = ++this._generation;
    this._reloadBuffer = [];
    try {
      const payload = await this._loadSession(this.sessionId, { resolve: true });
      // The delete notice can land while this read is in flight, and the read
      // itself still succeeds when it was served before the delete committed.
      // Without this guard the resolving reload would replace the notice with
      // "connected" for a session that no longer exists.
      if (this._closed || this._deleted || generation !== this._generation) return;
      const buffered = this._reloadBuffer;
      this._reloadBuffer = null;
      // The reload is authoritative and returns every node already hydrated,
      // so previous attempts carry no information into the new scene.
      this._hydrationAttempted = new Set();
      // Presence and claims are ephemeral and stream-owned: the sync client
      // seeds them from the same snapshot that triggered this reload, and it
      // does so *before* firing onReady/onResync. Rebuilding the scene from
      // the REST payload alone would therefore drop the roster and every
      // collaborator's selection the instant after they arrived, until some
      // later join/leave happened to repopulate them.
      const loaded = {
        ...sceneFromSession(payload, { sessionId: this.sessionId }),
        roster: this._scene.roster,
        claims: this._scene.claims,
      };
      // An op that committed after the REST read but reached the stream before
      // the REST response would otherwise be folded into a scene this
      // assignment then discards — and no later op could repair it, since a
      // move for an unknown id is dropped by design.
      this._scene = applyOps(loaded, buffered);
      this._setStatus('connected');
      this._hydratePending();
    } catch (err) {
      if (this._closed || this._deleted || generation !== this._generation) return;
      this._reloadBuffer = null;
      // The stream stays open, so a later resync retries this on its own; the
      // error is surfaced meanwhile rather than leaving an empty dome.
      this._setStatus('error', err?.message || String(err ?? 'load failed'));
    }
  }

  _applyOps(ops) {
    if (this._reloadBuffer) this._reloadBuffer.push(...(ops || []));
    const next = applyOps(this._scene, ops);
    if (next !== this._scene) {
      // Forget attempts for nodes the batch removed, so re-adding the same node
      // later hydrates it again instead of leaving it anonymous forever.
      for (const id of this._hydrationAttempted) {
        if (!next.nodes[id]) this._hydrationAttempted.delete(id);
      }
      this._update(next);
    }
    this._hydratePending();
  }

  /**
   * Fetch name/type for nodes a collaborator added. `nodes_added` carries ids
   * only, so without this the node would render as an anonymous box — the same
   * reason the 2D client resolves node details on that op.
   */
  async _hydratePending() {
    const pending = pendingNodeIds(this._scene).filter((id) => !this._hydrationAttempted.has(id));
    if (!pending.length) return;
    for (const id of pending) this._hydrationAttempted.add(id);
    const generation = this._generation;
    const results = await Promise.all(
      pending.map((id) =>
        Promise.resolve()
          .then(() => this._loadNodeDetails(id))
          // The node may have been deleted from the graph between the op and
          // this read; drop it rather than failing the whole batch.
          .catch(() => null)
      )
    );
    if (this._closed || generation !== this._generation) return;
    const nodes = results.map((r) => r?.node).filter(Boolean);
    if (nodes.length) this._update(hydrateNodes(this._scene, nodes));
  }
}
