import { describe, it, expect, beforeEach } from 'vitest';
import { sendChatMessage, sendSimpleChatMessage } from '../src/services/api';

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
