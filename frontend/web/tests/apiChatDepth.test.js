// covers: frontend/web/src/services/api.js
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  sendChatMessage,
  sendSimpleChatMessage,
  setActiveSessionForRequests,
  setStagingAvailable,
  setSessionOnServer,
  setSessionStaged,
} from '../src/services/api';

describe('chat API federation depth payloads', () => {
  beforeEach(() => {
    global.fetch.mockReset();
    global.fetch.mockResolvedValue({
      ok: true,
      json: async () => ({ content: 'ok' }),
    });
  });

  it('includes federation_depth in /ui/chat payload when provided', async () => {
    await sendChatMessage([{ role: 'user', content: 'hello' }], null, { federationDepth: 3 });

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [, options] = global.fetch.mock.calls[0];
    const payload = JSON.parse(options.body);

    expect(payload.federation_depth).toBe(3);
    expect(payload.messages).toHaveLength(1);
  });

  it('includes federation_depth in /ui/chat/simple payload when provided', async () => {
    await sendSimpleChatMessage('hello', null, { federationDepth: 2 });

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [, options] = global.fetch.mock.calls[0];
    const payload = JSON.parse(options.body);

    expect(payload.federation_depth).toBe(2);
    expect(payload.message).toBe('hello');
  });
});

describe('chat progress stream', () => {
  afterEach(() => {
    setActiveSessionForRequests(null);
    setStagingAvailable(false);
    setSessionStaged('1111-2222-3333-4444', false);
  });
  it('decodes split UTF-8 frames and returns the final response', async () => {
    setStagingAvailable(true);
    setSessionOnServer('1111-2222-3333-4444');
    setActiveSessionForRequests('1111-2222-3333-4444');
    const step = { kind: 'tool_started', tool: 'sök' };
    const answer = { content: 'answer', toolUsed: 'sök', toolResult: null };
    const bytes = new TextEncoder().encode(
      ': keep-alive\n\n' +
        `data: ${JSON.stringify(step)}\n\n` +
        `data: ${JSON.stringify({ kind: 'chat_finished', response: answer })}\n\n`
    );
    const read = vi.fn();
    for (const byte of bytes)
      read.mockResolvedValueOnce({ value: new Uint8Array([byte]), done: false });
    read.mockResolvedValue({ done: true });
    const cancel = vi.fn();
    const releaseLock = vi.fn();
    global.fetch.mockResolvedValue({
      ok: true,
      body: { getReader: () => ({ read, cancel, releaseLock }) },
    });
    const onProgress = vi.fn();
    expect(await sendChatMessage([], null, { onProgress })).toEqual(answer);
    expect(onProgress.mock.calls).toEqual([[step], [{ kind: 'chat_finished' }]]);
    expect(cancel).toHaveBeenCalledOnce();
    expect(releaseLock).toHaveBeenCalledOnce();
    const [url, options] = global.fetch.mock.calls.at(-1);
    expect(url).toBe('/ui/chat/stream');
    expect(options.headers['X-Requested-With']).toBe('XMLHttpRequest');
    expect(options.headers['x-communityoverview-session-id']).toBe('1111-2222-3333-4444');
  });

  it.each([{ steps: [] }, { steps: [{ kind: 'error' }] }])(
    'rejects streams without a final response (%j)',
    async ({ steps }) => {
      const read = vi
        .fn()
        .mockResolvedValueOnce({
          value: new TextEncoder().encode(
            steps.map((step) => `data: ${JSON.stringify(step)}\n\n`).join('')
          ),
          done: false,
        })
        .mockResolvedValue({ done: true });
      global.fetch.mockResolvedValue({
        ok: true,
        body: { getReader: () => ({ read, cancel: vi.fn(), releaseLock: vi.fn() }) },
      });
      await expect(sendChatMessage([], null, { onProgress: vi.fn() })).rejects.toBeInstanceOf(
        Error
      );
    }
  );
});
