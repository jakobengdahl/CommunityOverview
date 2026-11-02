import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';

import * as api from './api';
import { SESSION_ID_HEADER, needsSessionHeader } from './stagedSessionHeader';

// docs/SESSION_OVERLAY_CONTRACT.md §16.1: in a staged session a graph write
// that leaves the session header out lands in the main graph. These pin which
// requests carry it, and that it never follows the user into another session.

function mockFetch(status = 200, body = {}) {
  const calls = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url, options = {}) => {
      calls.push({ url, options });
      return {
        ok: status >= 200 && status < 300,
        status,
        json: async () => body,
      };
    })
  );
  return calls;
}

function headerOf(call) {
  return call.options.headers?.[SESSION_ID_HEADER];
}

beforeEach(() => {
  api.setActiveSessionForRequests(null);
});

afterEach(() => {
  api.setSessionStaged('1111-2222-3333-4444', false);
  api.setSessionStaged('5555-6666-7777-8888', false);
  api.setStagingAvailable(false);
  api.setActiveSessionForRequests(null);
  vi.unstubAllGlobals();
});

describe('needsSessionHeader', () => {
  it('names the session on every route the backend stages or composes on', () => {
    expect(needsSessionHeader('POST', '/api/search')).toBe(true);
    expect(needsSessionHeader('GET', '/api/nodes/abc')).toBe(true);
    expect(needsSessionHeader('POST', '/api/nodes')).toBe(true);
    expect(needsSessionHeader('PATCH', '/api/nodes/abc')).toBe(true);
    expect(needsSessionHeader('DELETE', '/api/nodes')).toBe(true);
    expect(needsSessionHeader('POST', '/api/edges')).toBe(true);
    expect(needsSessionHeader('DELETE', '/api/edges/e1')).toBe(true);
    expect(needsSessionHeader('POST', '/ui/chat')).toBe(true);
    expect(needsSessionHeader('POST', '/execute_tool')).toBe(true);
  });

  it('names it on a graph write the backend cannot stage, so that write is refused, not applied', () => {
    expect(needsSessionHeader('POST', '/api/views/save')).toBe(true);
    // Upload with analysis runs the assistant, which can write; extraction cannot.
    expect(needsSessionHeader('POST', '/ui/upload')).toBe(true);
    expect(needsSessionHeader('POST', '/ui/upload/extract')).toBe(false);
    expect(needsSessionHeader('DELETE', '/api/anything/else')).toBe(true);
  });

  it('leaves out session routes, plain reads and POSTs that never change the graph', () => {
    expect(needsSessionHeader('POST', '/api/sessions')).toBe(false);
    expect(needsSessionHeader('POST', '/api/sessions/1111-2222/staged/merge')).toBe(false);
    expect(needsSessionHeader('GET', '/api/schema')).toBe(false);
    expect(needsSessionHeader('GET', '/api/history')).toBe(false);
    expect(needsSessionHeader('POST', '/api/federation/teleport')).toBe(false);
    expect(needsSessionHeader('POST', '/ui/propose-nodes')).toBe(false);
  });
});

describe('staged-session binding in api.js', () => {
  it('stages a write made while the active session is staged', async () => {
    const calls = mockFetch();
    api.setActiveSessionForRequests('1111-2222-3333-4444');
    api.setSessionStaged('1111-2222-3333-4444', true);
    await api.updateNode('n1', { name: 'x' });
    await api.searchGraph('q');
    expect(headerOf(calls[0])).toBe('1111-2222-3333-4444');
    expect(headerOf(calls[1])).toBe('1111-2222-3333-4444');
  });

  it('sends no header from a direct session', async () => {
    const calls = mockFetch();
    api.setActiveSessionForRequests('1111-2222-3333-4444');
    await api.updateNode('n1', { name: 'x' });
    expect(headerOf(calls[0])).toBeUndefined();
  });

  it('stops naming a staged session the moment another one is active', async () => {
    const calls = mockFetch();
    api.setActiveSessionForRequests('1111-2222-3333-4444');
    api.setSessionStaged('1111-2222-3333-4444', true);
    api.setActiveSessionForRequests('9999-8888-7777-6666');
    await api.updateNode('n1', { name: 'x' });
    expect(headerOf(calls[0])).toBeUndefined();
  });

  it('never puts the header on a session route, which names its session in the path', async () => {
    const calls = mockFetch(200, { items: [] });
    api.setActiveSessionForRequests('1111-2222-3333-4444');
    api.setSessionStaged('1111-2222-3333-4444', true);
    await api.createSession(null, { writeMode: 'staged' });
    await api.getStagedChanges('1111-2222-3333-4444');
    expect(calls.map(headerOf)).toEqual([undefined, undefined]);
    expect(JSON.parse(calls[0].options.body)).toEqual({ name: null, write_mode: 'staged' });
  });

  it('deletes a staged session with its staged changes only when asked to', async () => {
    const calls = mockFetch(200, { deleted: true });
    await api.deleteServerSession('1111-2222', 'c1');
    await api.deleteServerSession('1111-2222', 'c1', { discardStaged: true });
    expect(calls[0].url).not.toContain('discard_staged');
    expect(calls[1].url).toContain('discard_staged=true');
  });
});

describe('a session whose write mode is not known yet', () => {
  it('writes nothing until the server has said how it writes', async () => {
    const calls = mockFetch();
    api.setActiveSessionForRequests('5555-6666-7777-8888');
    api.setSessionModeUnknown('5555-6666-7777-8888');
    const error = await api.updateNode('n1', { name: 'x' }).catch((e) => e);
    expect(error.code).toBe('session_mode_unknown');
    expect(calls).toHaveLength(0);
    // Reads still go: they change nothing.
    await api.searchGraph('q');
    expect(calls).toHaveLength(1);
  });

  it('releases writes once the server says direct, and stages them once it says staged', async () => {
    const calls = mockFetch();
    api.setActiveSessionForRequests('5555-6666-7777-8888');
    api.setSessionModeUnknown('5555-6666-7777-8888');
    api.setSessionModeDirect('5555-6666-7777-8888');
    await api.updateNode('n1', { name: 'x' });
    expect(headerOf(calls[0])).toBeUndefined();
    api.setSessionModeUnknown('1111-2222-3333-4444');
    api.setActiveSessionForRequests('1111-2222-3333-4444');
    api.setSessionStaged('1111-2222-3333-4444', true);
    await api.updateNode('n1', { name: 'y' });
    expect(headerOf(calls[1])).toBe('1111-2222-3333-4444');
  });
});

describe('a direct session the server holds, where staging exists', () => {
  it('names the session on staging routes, so a switch by someone else is honoured at once', async () => {
    const calls = mockFetch();
    api.setStagingAvailable(true);
    api.setActiveSessionForRequests('5555-6666-7777-8888');
    api.setSessionOnServer('5555-6666-7777-8888');
    await api.updateNode('n1', { name: 'x' });
    await api.saveView('v');
    expect(headerOf(calls[0])).toBe('5555-6666-7777-8888');
    // A route that neither stages nor composes keeps working in a direct session.
    expect(headerOf(calls[1])).toBeUndefined();
  });

  it('leaves an agent proposal approval to the session it was made in', async () => {
    const calls = mockFetch(200, { write_mode: 'direct' });
    api.setStagingAvailable(true);
    api.setActiveSessionForRequests('5555-6666-7777-8888');
    api.setSessionOnServer('5555-6666-7777-8888');
    await api.decideAgentProposal('p1', 'approve');
    const approve = calls.find((c) => c.url.endsWith('/approve'));
    expect(headerOf(approve)).toBeUndefined();
  });

  it('refuses to approve when the session mode cannot be read', async () => {
    const calls = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url, options = {}) => {
        calls.push({ url, options });
        return { ok: false, status: 503, json: async () => ({ detail: 'down' }) };
      })
    );
    api.setStagingAvailable(true);
    api.setActiveSessionForRequests('5555-6666-7777-8888');
    api.setSessionOnServer('5555-6666-7777-8888');
    const error = await api.decideAgentProposal('p1', 'approve').catch((e) => e);
    expect(error.status).toBe(503);
    expect(calls.some((c) => c.url.endsWith('/approve'))).toBe(false);
  });

  it('marks no other session from a read made for the one that was left', async () => {
    let answer;
    const calls = [];
    vi.stubGlobal(
      'fetch',
      vi.fn((url, options = {}) => {
        calls.push({ url, options });
        return new Promise((resolve) => {
          answer = () =>
            resolve({ ok: true, status: 200, json: async () => ({ write_mode: 'staged' }) });
        });
      })
    );
    api.setStagingAvailable(true);
    api.setActiveSessionForRequests('5555-6666-7777-8888');
    api.setSessionOnServer('5555-6666-7777-8888');
    const pending = api.decideAgentProposal('p1', 'approve').catch((e) => e);
    api.setActiveSessionForRequests('9999-0000-1111-2222');
    answer();
    const error = await pending;
    expect(error.code).toBe('session_changed');
    expect(api.getStagedSessionBinding()).toBeNull();
    expect(calls.some((c) => c.url.endsWith('/approve'))).toBe(false);
  });

  it('names a session switched to staged elsewhere before approving from it', async () => {
    const calls = mockFetch(200, { write_mode: 'staged' });
    api.setStagingAvailable(true);
    api.setActiveSessionForRequests('5555-6666-7777-8888');
    api.setSessionOnServer('5555-6666-7777-8888');
    await api.decideAgentProposal('p1', 'approve');
    const approve = calls.find((c) => c.url.endsWith('/approve'));
    expect(headerOf(approve)).toBe('5555-6666-7777-8888');
  });

  it('names no session the server does not hold, nor any where staging is off', async () => {
    const calls = mockFetch();
    api.setActiveSessionForRequests('5555-6666-7777-8888');
    api.setSessionOnServer('5555-6666-7777-8888');
    await api.updateNode('n1', { name: 'x' });
    api.setStagingAvailable(true);
    api.setActiveSessionForRequests('9999-0000-1111-2222');
    await api.updateNode('n1', { name: 'y' });
    expect(calls.map(headerOf)).toEqual([undefined, undefined]);
  });
});

describe('mode-unknown hold, more cases', () => {
  it('holds a chat message too, since the assistant can write', async () => {
    const calls = mockFetch();
    api.setActiveSessionForRequests('5555-6666-7777-8888');
    api.setSessionModeUnknown('5555-6666-7777-8888');
    const error = await api.sendChatMessage([{ role: 'user', content: 'hi' }]).catch((e) => e);
    expect(error.code).toBe('session_mode_unknown');
    expect(calls).toHaveLength(0);
    api.setSessionModeDirect('5555-6666-7777-8888');
  });

  it('never turns a staged session back into an unknown one', async () => {
    const calls = mockFetch();
    api.setActiveSessionForRequests('1111-2222-3333-4444');
    api.setSessionStaged('1111-2222-3333-4444', true);
    api.setSessionModeUnknown('1111-2222-3333-4444');
    await api.updateNode('n1', { name: 'x' });
    expect(headerOf(calls[0])).toBe('1111-2222-3333-4444');
  });
});

describe('file upload', () => {
  it('names a staged session on an upload with analysis, and holds it while the mode is unknown', async () => {
    const calls = mockFetch();
    const file = new File(['x'], 'a.txt');
    api.setActiveSessionForRequests('1111-2222-3333-4444');
    api.setSessionStaged('1111-2222-3333-4444', true);
    await api.uploadFile(file, true);
    expect(headerOf(calls[0])).toBe('1111-2222-3333-4444');
    // The session header joins the write marker; the form sets its own type.
    expect(calls[0].options.headers['X-Requested-With']).toBe('XMLHttpRequest');
    expect(calls[0].options.headers['Content-Type']).toBeUndefined();
    expect(calls[0].options.body).toBeInstanceOf(FormData);
    // Extraction only changes nothing: no header.
    await api.uploadFile(file, false);
    expect(headerOf(calls[1])).toBeUndefined();
    api.setActiveSessionForRequests('5555-6666-7777-8888');
    api.setSessionModeUnknown('5555-6666-7777-8888');
    const error = await api.uploadFile(file, true).catch((e) => e);
    expect(error.code).toBe('session_mode_unknown');
    expect(calls).toHaveLength(2);
    api.setSessionModeDirect('5555-6666-7777-8888');
  });

  it('keeps a structured refusal\'s code and message instead of "[object Object]"', async () => {
    mockFetch(409, {
      detail: { error: 'write_not_stageable', message: 'Cannot stage this upload.' },
    });
    const error = await api.uploadFile(new File(['x'], 'a.txt'), true).catch((e) => e);
    expect(error.message).toBe('Cannot stage this upload.');
    expect(error.code).toBe('write_not_stageable');
    expect(error.status).toBe(409);
  });

  it('keeps a plain string detail as the message', async () => {
    mockFetch(415, { detail: 'Unsupported file type' });
    const error = await api.uploadFile(new File(['x'], 'a.txt'), false).catch((e) => e);
    expect(error.message).toBe('Unsupported file type');
    expect(error.statusOnly).toBe(false);
  });

  it('marks an answer with no reason as status-only, for the UI to word', async () => {
    mockFetch(502, {});
    const error = await api.uploadFile(new File(['x'], 'a.txt'), false).catch((e) => e);
    expect(error.status).toBe(502);
    expect(error.statusOnly).toBe(true);
  });
});

describe('possibly staged', () => {
  it('is true for a staged or not-yet-known session, false for a direct one or none', () => {
    expect(api.isActiveSessionPossiblyStaged()).toBe(false);
    api.setActiveSessionForRequests('5555-6666-7777-8888');
    expect(api.isActiveSessionPossiblyStaged()).toBe(false);
    api.setSessionModeUnknown('5555-6666-7777-8888');
    expect(api.isActiveSessionPossiblyStaged()).toBe(true);
    api.setSessionStaged('5555-6666-7777-8888', true);
    expect(api.isActiveSessionPossiblyStaged()).toBe(true);
  });

  it('approves an agent proposal with the staged session named, and rejects without', async () => {
    const calls = mockFetch();
    api.setActiveSessionForRequests('1111-2222-3333-4444');
    api.setSessionStaged('1111-2222-3333-4444', true);
    await api.decideAgentProposal('p1', 'approve');
    await api.decideAgentProposal('p1', 'reject');
    expect(calls.map(headerOf)).toEqual(['1111-2222-3333-4444', undefined]);
  });
});

describe('staged marks', () => {
  it('tells subscribers once, when a session is first marked staged', () => {
    const seen = [];
    const unsubscribe = api.subscribeStagedMarks((id) => seen.push(id));
    api.setSessionStaged('1111-2222-3333-4444', true);
    api.setSessionStaged('1111-2222-3333-4444', true);
    unsubscribe();
    expect(seen).toEqual(['1111-2222-3333-4444']);
  });

  it('raises its own messages in the UI language', async () => {
    mockFetch();
    api.setMessageTranslator((key) => `sv:${key}`);
    api.setActiveSessionForRequests('5555-6666-7777-8888');
    api.setSessionModeUnknown('5555-6666-7777-8888');
    const error = await api.updateNode('n1', { name: 'x' }).catch((e) => e);
    api.setMessageTranslator(null);
    expect(error.message).toBe('sv:staged.write_held');
  });
});

describe('structured refusals', () => {
  it('carries the code and data of a staged-change refusal', async () => {
    mockFetch(409, {
      detail: { error: 'merge_blocked', message: 'blocked', items: [{ entity_id: 'n1' }] },
    });
    const error = await api
      .mergeStagedChanges('1111-2222', { merge_id: 'm', entries: [] })
      .catch((e) => e);
    expect(error.status).toBe(409);
    expect(error.code).toBe('merge_blocked');
    expect(error.message).toBe('blocked');
    expect(error.data.items).toEqual([{ entity_id: 'n1' }]);
  });

  it('keeps a plain string detail as the message', async () => {
    mockFetch(404, { detail: 'session not found' });
    const error = await api.getSession('1111-2222').catch((e) => e);
    expect(error.message).toBe('session not found');
    expect(error.status).toBe(404);
  });
});
