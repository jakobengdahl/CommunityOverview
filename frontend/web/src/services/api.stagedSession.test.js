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
