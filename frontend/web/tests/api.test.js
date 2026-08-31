import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

describe('apiFetch error handling', () => {
  const originalFetch = global.fetch;

  afterEach(() => {
    global.fetch = originalFetch;
    vi.resetModules();
  });

  it('attaches the HTTP status to the thrown error, so callers can distinguish e.g. 404 from other failures', async () => {
    global.fetch = vi.fn(async () => ({
      ok: false,
      status: 404,
      json: async () => ({ detail: 'session not found' }),
    }));
    const { getSession } = await import('../src/services/api.js');

    await expect(getSession('missing-id')).rejects.toMatchObject({ status: 404 });
  });
});

// ingestSessionImage is a plain fetch (like uploadFile), not sessionSyncClient's
// op-batch POST, because the server does a validate/optimize/embed round-trip
// no client-side diff could predict. See backend/service/rest_api.py's
// ingest_session_image and this module's own docstring for why the response
// is informational only — the SessionSyncClient behaviour that makes the
// pasting browser see the real result via SSE is covered separately in
// sessionSyncClient.test.js's "image-ingest echo-attribution" test.
describe('ingestSessionImage', () => {
  const originalFetch = global.fetch;

  afterEach(() => {
    global.fetch = originalFetch;
    vi.resetModules();
  });

  it('posts to the session image-annotation endpoint with the browser client id and given fields', async () => {
    let captured;
    global.fetch = vi.fn(async (url, opts) => {
      captured = { url, opts };
      return {
        ok: true,
        status: 200,
        json: async () => ({ annotation: { id: 'img-1' }, revision: 3 }),
      };
    });
    const { ingestSessionImage, getClientId } = await import('../src/services/api.js');

    const result = await ingestSessionImage('1234-5678', {
      x: 10,
      y: 20,
      imageData: 'data:image/png;base64,AAAA',
    });

    expect(captured.url).toContain('/sessions/1234-5678/annotations/image');
    expect(captured.opts.method).toBe('POST');
    const body = JSON.parse(captured.opts.body);
    expect(body).toMatchObject({
      client_id: getClientId(),
      x: 10,
      y: 20,
      image_data: 'data:image/png;base64,AAAA',
      image_url: null,
    });
    expect(result).toEqual({ annotation: { id: 'img-1' }, revision: 3 });
  });

  it('rejects with the server detail message on failure', async () => {
    global.fetch = vi.fn(async () => ({
      ok: false,
      status: 413,
      json: async () => ({ detail: 'embedding this image would exceed the session limit' }),
    }));
    const { ingestSessionImage } = await import('../src/services/api.js');

    await expect(
      ingestSessionImage('1234-5678', { x: 0, y: 0, imageData: 'data:image/png;base64,AA==' })
    ).rejects.toThrow('embedding this image would exceed the session limit');
  });
});

describe('getEventSessionId', () => {
  let originalSessionStorage;
  let mockSessionStorage;

  beforeEach(() => {
    // Mock sessionStorage
    const store = {};
    mockSessionStorage = {
      getItem: vi.fn((key) => store[key] || null),
      setItem: vi.fn((key, value) => {
        store[key] = value.toString();
      }),
      clear: vi.fn(() => {
        for (let key in store) {
          delete store[key];
        }
      }),
    };

    originalSessionStorage = global.sessionStorage;
    global.sessionStorage = mockSessionStorage;
    vi.resetModules();
  });

  afterEach(() => {
    global.sessionStorage = originalSessionStorage;
    vi.clearAllMocks();
  });

  it('generates a new session ID if none exists in sessionStorage', async () => {
    const { getEventSessionId } = await import('../src/services/api.js');

    const id = getEventSessionId();

    expect(id).toMatch(/^session-[a-z0-9]+-[a-z0-9]+$/);
    expect(global.sessionStorage.getItem).toHaveBeenCalledWith('eventSessionId');
    expect(global.sessionStorage.setItem).toHaveBeenCalledWith('eventSessionId', id);
  });

  it('restores the session ID from sessionStorage if it exists', async () => {
    global.sessionStorage.setItem('eventSessionId', 'session-test-id');
    // Clear mock calls to ensure we only check the ones from getEventSessionId
    global.sessionStorage.getItem.mockClear();
    global.sessionStorage.setItem.mockClear();

    const { getEventSessionId } = await import('../src/services/api.js');

    const id = getEventSessionId();

    expect(id).toBe('session-test-id');
    expect(global.sessionStorage.getItem).toHaveBeenCalledWith('eventSessionId');
    expect(global.sessionStorage.setItem).not.toHaveBeenCalled();
  });

  it('caches the session ID in module state and does not check sessionStorage on subsequent calls', async () => {
    const { getEventSessionId } = await import('../src/services/api.js');

    // First call
    const firstId = getEventSessionId();

    // Clear mocks to check subsequent calls
    global.sessionStorage.getItem.mockClear();
    global.sessionStorage.setItem.mockClear();

    // Second call
    const secondId = getEventSessionId();

    expect(secondId).toBe(firstId);
    expect(global.sessionStorage.getItem).not.toHaveBeenCalled();
    expect(global.sessionStorage.setItem).not.toHaveBeenCalled();
  });
});

// Both identifiers are shared with collaborators — the client id travels in
// presence rosters and in the session_deleted payload — so they are minted from
// crypto.getRandomValues, as generateVisualizationSessionId already is. Math.random
// is seeded per page and recoverable from earlier draws, which is what CodeQL
// reports as insecure randomness.
describe('identifier minting', () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    vi.resetModules();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('mints the client id without Math.random', async () => {
    const randomSpy = vi.spyOn(Math, 'random');
    const { getClientId } = await import('../src/services/api.js');

    expect(getClientId()).toMatch(/^client-[0-9a-f]{12}$/);
    expect(randomSpy).not.toHaveBeenCalled();
  });

  it('mints the event session id without Math.random', async () => {
    const randomSpy = vi.spyOn(Math, 'random');
    const { getEventSessionId } = await import('../src/services/api.js');

    expect(getEventSessionId()).toMatch(/^session-[0-9a-z]+-[0-9a-f]{9}$/);
    expect(randomSpy).not.toHaveBeenCalled();
  });
});
