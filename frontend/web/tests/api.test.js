import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

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
      })
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
