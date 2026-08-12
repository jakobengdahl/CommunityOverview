import { describe, it, expect, beforeEach } from 'vitest';

import * as sessionStore from '../src/services/sessionStore';
import en from '../src/i18n/en.json';
import sv from '../src/i18n/sv.json';

describe('sessionStore (recents index)', () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it('validates session id format', () => {
    expect(sessionStore.isValidSessionId('1234-5678')).toBe(true); // legacy
    expect(sessionStore.isValidSessionId('1234-5678-9012-3456')).toBe(true); // new
    expect(sessionStore.isValidSessionId('1234-5678-9012')).toBe(false);
    expect(sessionStore.isValidSessionId('12345678')).toBe(false);
    expect(sessionStore.isValidSessionId('abcd-efgh')).toBe(false);
    expect(sessionStore.isValidSessionId('')).toBe(false);
    expect(sessionStore.isValidSessionId(null)).toBe(false);
  });

  // The connect dialog advertises the session-ID shape to users. Session IDs
  // minted by the backend are the four-group form (DDDD-DDDD-DDDD-DDDD); the
  // two-group legacy form is accepted but never generated. Guard the GUI
  // example against regressing back to the shorter, stale format.
  it.each([
    ['en', en],
    ['sv', sv],
  ])('advertises the current four-group session-ID example (%s)', (_lang, catalog) => {
    const example = catalog.sessions.connect_session_placeholder;
    expect(sessionStore.isValidSessionId(example)).toBe(true);
    expect(example.split('-')).toHaveLength(4);
    expect(catalog.sessions.invalid_session_id).toContain(example);
  });

  it('touchSession registers a session and lists it', () => {
    sessionStore.touchSession('1111-2222');
    expect(sessionStore.hasSession('1111-2222')).toBe(true);
    const sessions = sessionStore.listSessions();
    expect(sessions).toHaveLength(1);
    expect(sessions[0].id).toBe('1111-2222');
    expect(sessions[0].name).toBeNull();
  });

  it('lists sessions most recently updated first', () => {
    sessionStore.touchSession('1111-1111');
    const index = JSON.parse(window.localStorage.getItem('graph_sessions_index'));
    index[0].updatedAt = Date.now() - 10_000;
    window.localStorage.setItem('graph_sessions_index', JSON.stringify(index));

    sessionStore.touchSession('2222-2222');

    const sessions = sessionStore.listSessions();
    expect(sessions.map((s) => s.id)).toEqual(['2222-2222', '1111-1111']);
  });

  it('renames only an existing entry and keeps it on later touches', () => {
    sessionStore.touchSession('1111-2222');
    sessionStore.renameSession('1111-2222', '  My session  ');
    expect(sessionStore.listSessions()[0].name).toBe('My session');

    sessionStore.touchSession('1111-2222');
    expect(sessionStore.listSessions()[0].name).toBe('My session');

    // Renaming an unknown id must not resurrect it
    sessionStore.renameSession('9999-9999', 'ghost');
    expect(sessionStore.hasSession('9999-9999')).toBe(false);
  });

  it('removeSession drops an entry from recents', () => {
    sessionStore.touchSession('1111-2222');
    sessionStore.touchSession('3333-4444');
    sessionStore.removeSession('1111-2222');
    expect(sessionStore.hasSession('1111-2222')).toBe(false);
    expect(sessionStore.hasSession('3333-4444')).toBe(true);
  });

  it('purges legacy snapshot keys but keeps the recents index', () => {
    window.localStorage.setItem('graph_session_snapshot_1111-2222', JSON.stringify({ nodes: [] }));
    window.localStorage.setItem('graph_session_snapshot_3333-4444', JSON.stringify({ nodes: [] }));
    sessionStore.touchSession('1111-2222');

    sessionStore.purgeLegacySnapshots();

    expect(window.localStorage.getItem('graph_session_snapshot_1111-2222')).toBeNull();
    expect(window.localStorage.getItem('graph_session_snapshot_3333-4444')).toBeNull();
    expect(sessionStore.hasSession('1111-2222')).toBe(true);
  });

  it('evicts the oldest entry beyond the recents cap', () => {
    for (let i = 0; i < 51; i++) {
      const id = `${String(i).padStart(4, '0')}-0000`;
      sessionStore.touchSession(id);
      // Pin a strictly increasing timestamp so eviction order is deterministic:
      // the just-touched entry stays newest until the next iteration edits it.
      const index = JSON.parse(window.localStorage.getItem('graph_sessions_index'));
      index.find((e) => e.id === id).updatedAt = 1000 + i;
      window.localStorage.setItem('graph_sessions_index', JSON.stringify(index));
    }

    const sessions = sessionStore.listSessions();
    expect(sessions).toHaveLength(50);
    expect(sessions.some((s) => s.id === '0000-0000')).toBe(false);
    expect(sessions.some((s) => s.id === '0050-0000')).toBe(true);
  });
});
