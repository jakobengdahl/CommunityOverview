import { describe, it, expect, beforeEach, vi } from 'vitest';

import * as sessionStore from '../src/services/sessionStore';

function makeSnapshot(nodeCount = 1) {
  return {
    nodes: Array.from({ length: nodeCount }, (_, i) => ({ id: `n${i}`, name: `Node ${i}` })),
    edges: [],
    positions: { n0: { x: 10, y: 20 } },
    parentIds: {},
    groups: [],
    hiddenNodeIds: [],
    hiddenEdgeIds: [],
    savedAt: Date.now(),
  };
}

describe('sessionStore', () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it('validates session id format', () => {
    expect(sessionStore.isValidSessionId('1234-5678')).toBe(true);
    expect(sessionStore.isValidSessionId('12345678')).toBe(false);
    expect(sessionStore.isValidSessionId('abcd-efgh')).toBe(false);
    expect(sessionStore.isValidSessionId('')).toBe(false);
    expect(sessionStore.isValidSessionId(null)).toBe(false);
  });

  it('saves and restores a snapshot', () => {
    const snapshot = makeSnapshot(3);
    sessionStore.saveSnapshot('1111-2222', snapshot);

    const restored = sessionStore.getSnapshot('1111-2222');
    expect(restored.nodes).toHaveLength(3);
    expect(restored.positions.n0).toEqual({ x: 10, y: 20 });

    const sessions = sessionStore.listSessions();
    expect(sessions).toHaveLength(1);
    expect(sessions[0].id).toBe('1111-2222');
    expect(sessions[0].nodeCount).toBe(3);
    expect(sessions[0].name).toBeNull();
  });

  it('lists sessions most recently updated first', () => {
    sessionStore.saveSnapshot('1111-1111', makeSnapshot());
    // Force distinct timestamps
    const index = JSON.parse(window.localStorage.getItem('graph_sessions_index'));
    index[0].updatedAt = Date.now() - 10_000;
    window.localStorage.setItem('graph_sessions_index', JSON.stringify(index));

    sessionStore.saveSnapshot('2222-2222', makeSnapshot());

    const sessions = sessionStore.listSessions();
    expect(sessions.map(s => s.id)).toEqual(['2222-2222', '1111-1111']);
  });

  it('renames a session and keeps the name on later snapshot saves', () => {
    sessionStore.saveSnapshot('1111-2222', makeSnapshot());
    sessionStore.renameSession('1111-2222', '  My session  ');
    expect(sessionStore.listSessions()[0].name).toBe('My session');

    sessionStore.saveSnapshot('1111-2222', makeSnapshot(2));
    expect(sessionStore.listSessions()[0].name).toBe('My session');
    expect(sessionStore.listSessions()[0].nodeCount).toBe(2);
  });

  it('touchSession registers a session without a snapshot', () => {
    sessionStore.touchSession('9999-0000');
    expect(sessionStore.hasSession('9999-0000')).toBe(true);
    expect(sessionStore.getSnapshot('9999-0000')).toBeNull();
  });

  it('evicts other sessions when storage quota is hit, then succeeds', () => {
    sessionStore.saveSnapshot('1111-1111', makeSnapshot());
    const index = JSON.parse(window.localStorage.getItem('graph_sessions_index'));
    index[0].updatedAt = 1000;
    window.localStorage.setItem('graph_sessions_index', JSON.stringify(index));

    // First setItem for the new snapshot throws (quota), retry succeeds
    const original = Storage.prototype.setItem;
    let failures = 1;
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (key, val) {
      if (key.startsWith('graph_session_snapshot_2222') && failures > 0) {
        failures -= 1;
        throw new DOMException('quota', 'QuotaExceededError');
      }
      return original.call(this, key, val);
    });

    const ok = sessionStore.saveSnapshot('2222-2222', makeSnapshot(2));
    spy.mockRestore();

    expect(ok).toBe(true);
    expect(sessionStore.getSnapshot('2222-2222')).not.toBeNull();
    // The older session was evicted to make room
    expect(sessionStore.hasSession('1111-1111')).toBe(false);
    expect(sessionStore.getSnapshot('1111-1111')).toBeNull();
  });

  it('rolls back an existing entry to its previous state when the write keeps failing', () => {
    sessionStore.saveSnapshot('1111-1111', makeSnapshot(1));
    const before = sessionStore.listSessions()[0];

    const original = Storage.prototype.setItem;
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (key, val) {
      if (key.startsWith('graph_session_snapshot_')) {
        throw new DOMException('quota', 'QuotaExceededError');
      }
      return original.call(this, key, val);
    });

    const ok = sessionStore.saveSnapshot('1111-1111', makeSnapshot(9));
    spy.mockRestore();

    expect(ok).toBe(false);
    // The index entry still describes the snapshot that remains in storage
    const sessions = sessionStore.listSessions();
    expect(sessions).toHaveLength(1);
    expect(sessions[0].nodeCount).toBe(before.nodeCount);
    expect(sessionStore.getSnapshot('1111-1111').nodes).toHaveLength(1);
  });

  it('drops the index entry for a new session whose snapshot can never be written', () => {
    const original = Storage.prototype.setItem;
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (key, val) {
      if (key.startsWith('graph_session_snapshot_')) {
        throw new DOMException('quota', 'QuotaExceededError');
      }
      return original.call(this, key, val);
    });

    const ok = sessionStore.saveSnapshot('2222-2222', makeSnapshot(2));
    spy.mockRestore();

    expect(ok).toBe(false);
    // No lying entry advertising a snapshot that isn't in storage
    expect(sessionStore.hasSession('2222-2222')).toBe(false);
    expect(sessionStore.getSnapshot('2222-2222')).toBeNull();
  });

  it('evicts the oldest session beyond the capacity cap', () => {
    for (let i = 0; i < 31; i++) {
      const id = `${String(i).padStart(4, '0')}-0000`;
      sessionStore.saveSnapshot(id, makeSnapshot());
      // Ensure strictly increasing timestamps so eviction order is stable
      const index = JSON.parse(window.localStorage.getItem('graph_sessions_index'));
      index.find(e => e.id === id).updatedAt = 1000 + i;
      window.localStorage.setItem('graph_sessions_index', JSON.stringify(index));
    }

    const sessions = sessionStore.listSessions();
    expect(sessions).toHaveLength(30);
    expect(sessions.some(s => s.id === '0000-0000')).toBe(false);
    expect(sessionStore.getSnapshot('0000-0000')).toBeNull();
    expect(sessions.some(s => s.id === '0030-0000')).toBe(true);
  });
});
