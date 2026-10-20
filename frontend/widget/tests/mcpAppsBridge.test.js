/**
 * The MCP Apps (io.modelcontextprotocol/ui, spec 2026-01-26) postMessage
 * bridge as a host sees it: the handshake messages, tool calls proxied as
 * JSON-RPC tools/call, and the tool-result notification reaching the widget.
 *
 * The host is played by an iframe's window installed as window.parent, so
 * MessageEvents carry a real Window as their source.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

let host;
let sent;
let originalParent;
// Each test imports a fresh module instance; drop the window listeners the
// previous instances added so they cannot answer this test's host messages.
let addedListeners;
let addListenerSpy;

function fromHost(data) {
  window.dispatchEvent(new MessageEvent('message', { data, source: host }));
}

async function nextMessage(predicate) {
  await vi.waitFor(() => expect(sent.find(predicate)).toBeDefined());
  return sent.find(predicate);
}

beforeEach(() => {
  vi.resetModules();
  addedListeners = [];
  const originalAdd = window.addEventListener.bind(window);
  addListenerSpy = vi.spyOn(window, 'addEventListener').mockImplementation((type, fn, opts) => {
    addedListeners.push([type, fn, opts]);
    return originalAdd(type, fn, opts);
  });
  const frame = document.createElement('iframe');
  document.body.appendChild(frame);
  host = frame.contentWindow;
  sent = [];
  host.postMessage = vi.fn((message) => sent.push(message));
  originalParent = Object.getOwnPropertyDescriptor(window, 'parent');
  Object.defineProperty(window, 'parent', { value: host, configurable: true });
  delete window.openai;
});

afterEach(() => {
  addListenerSpy.mockRestore();
  addedListeners.forEach(([type, fn, opts]) => window.removeEventListener(type, fn, opts));
  if (originalParent) Object.defineProperty(window, 'parent', originalParent);
  document.body.innerHTML = '';
});

describe('MCP Apps bridge', () => {
  it('is available when framed without window.openai', async () => {
    const { isMCPAvailable } = await import('../src/mcpClient');
    expect(isMCPAvailable()).toBe(true);
  });

  it('performs the ui/initialize handshake and delivers the tool result', async () => {
    const { connectHost, MCP_APPS_PROTOCOL_VERSION } = await import('../src/mcpClient');
    const onToolResult = vi.fn();
    const connected = connectHost({ onToolResult });

    const init = await nextMessage((m) => m.method === 'ui/initialize');
    expect(init.jsonrpc).toBe('2.0');
    expect(init.params).toEqual({
      protocolVersion: MCP_APPS_PROTOCOL_VERSION,
      appInfo: { name: 'community-graph-widget', version: '1.0.0' },
      appCapabilities: {},
    });
    expect(MCP_APPS_PROTOCOL_VERSION).toBe('2026-01-26');

    fromHost({
      jsonrpc: '2.0',
      id: init.id,
      result: {
        protocolVersion: '2026-01-26',
        hostInfo: {},
        hostCapabilities: {},
        hostContext: {},
      },
    });
    await connected;

    const initialized = await nextMessage((m) => m.method === 'ui/notifications/initialized');
    expect(initialized.id).toBeUndefined();
    const size = await nextMessage((m) => m.method === 'ui/notifications/size-changed');
    expect(size.params.height).toBeGreaterThan(0);

    fromHost({
      jsonrpc: '2.0',
      method: 'ui/notifications/tool-result',
      params: {
        content: [{ type: 'text', text: '{}' }],
        structuredContent: { result: { nodes: [{ id: 'a1' }], edges: [] } },
      },
    });
    expect(onToolResult).toHaveBeenCalledWith({ nodes: [{ id: 'a1' }], edges: [] });
  });

  it('ignores messages from frames other than the host', async () => {
    const { connectHost } = await import('../src/mcpClient');
    const onToolResult = vi.fn();
    connectHost({ onToolResult });
    await nextMessage((m) => m.method === 'ui/initialize');

    window.dispatchEvent(
      new MessageEvent('message', {
        data: {
          jsonrpc: '2.0',
          method: 'ui/notifications/tool-result',
          params: { structuredContent: { nodes: [] } },
        },
        source: window,
      })
    );
    expect(onToolResult).not.toHaveBeenCalled();
  });

  it('proxies callTool as a JSON-RPC tools/call and unwraps the result', async () => {
    const { callTool } = await import('../src/mcpClient');
    const pendingCall = callTool('search_graph', { query: 'alpha', limit: 5 });

    const req = await nextMessage((m) => m.method === 'tools/call');
    expect(req.params).toEqual({ name: 'search_graph', arguments: { query: 'alpha', limit: 5 } });

    fromHost({
      jsonrpc: '2.0',
      id: req.id,
      result: { content: [], structuredContent: { result: { nodes: [{ id: 'x' }] } } },
    });
    await expect(pendingCall).resolves.toEqual({ nodes: [{ id: 'x' }] });
  });

  it('rejects callTool when the host answers with a JSON-RPC error', async () => {
    const { callTool } = await import('../src/mcpClient');
    const pendingCall = callTool('search_graph', {});
    const req = await nextMessage((m) => m.method === 'tools/call');
    fromHost({ jsonrpc: '2.0', id: req.id, error: { code: -32000, message: 'denied' } });
    await expect(pendingCall).rejects.toThrow('denied');
  });

  it('answers a host teardown request so the host is not left waiting', async () => {
    const { connectHost } = await import('../src/mcpClient');
    connectHost({});
    await nextMessage((m) => m.method === 'ui/initialize');

    fromHost({ jsonrpc: '2.0', id: 'td-1', method: 'ui/resource-teardown', params: {} });
    const reply = await nextMessage((m) => m.id === 'td-1');
    expect(reply.result).toEqual({});
  });
});

describe('MCP Apps bridge lifecycle', () => {
  const initResult = {
    protocolVersion: '2026-01-26',
    hostInfo: {},
    hostCapabilities: {},
    hostContext: {},
  };

  it('sends initialized only after the host answers ui/initialize', async () => {
    const { connectHost } = await import('../src/mcpClient');
    const connected = connectHost({});
    const init = await nextMessage((m) => m.method === 'ui/initialize');
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(sent.some((m) => m.method === 'ui/notifications/initialized')).toBe(false);

    fromHost({ jsonrpc: '2.0', id: init.id, result: initResult });
    await connected;
    expect(sent.filter((m) => m.method === 'ui/notifications/initialized')).toHaveLength(1);
  });

  // React StrictMode mounts, cleans up and re-mounts the effect before the
  // handshake resolves.
  it('survives a connect, disconnect, connect sequence with one handshake', async () => {
    const { connectHost } = await import('../src/mcpClient');
    const first = vi.fn();
    const second = vi.fn();
    const firstConnect = connectHost({ onToolResult: first });
    const secondConnect = connectHost({ onToolResult: second });

    const init = await nextMessage((m) => m.method === 'ui/initialize');
    fromHost({ jsonrpc: '2.0', id: init.id, result: initResult });
    const disconnectFirst = await firstConnect;
    await secondConnect;
    disconnectFirst();

    fromHost({
      jsonrpc: '2.0',
      method: 'ui/notifications/tool-result',
      params: { structuredContent: { result: { nodes: [{ id: 'n' }] } } },
    });
    expect(sent.filter((m) => m.method === 'ui/initialize')).toHaveLength(1);
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledWith({ nodes: [{ id: 'n' }] });
  });

  it('delivers each tool result to every connected listener until it disconnects', async () => {
    const { connectHost } = await import('../src/mcpClient');
    const a = vi.fn();
    const b = vi.fn();
    const connectA = connectHost({ onToolResult: a });
    const connectB = connectHost({ onToolResult: b });
    const init = await nextMessage((m) => m.method === 'ui/initialize');
    fromHost({ jsonrpc: '2.0', id: init.id, result: initResult });
    const disconnectA = await connectA;
    await connectB;

    const toolResult = (id) =>
      fromHost({
        jsonrpc: '2.0',
        method: 'ui/notifications/tool-result',
        params: { structuredContent: { nodes: [{ id }] } },
      });
    toolResult('one');
    expect(a).toHaveBeenCalledWith({ nodes: [{ id: 'one' }] });
    expect(b).toHaveBeenCalledWith({ nodes: [{ id: 'one' }] });

    disconnectA();
    toolResult('two');
    expect(a).toHaveBeenCalledTimes(1);
    expect(b).toHaveBeenLastCalledWith({ nodes: [{ id: 'two' }] });
  });

  it('drops the listener of a failed handshake', async () => {
    const { connectHost } = await import('../src/mcpClient');
    const onToolResult = vi.fn();
    const connected = connectHost({ onToolResult });
    const init = await nextMessage((m) => m.method === 'ui/initialize');
    fromHost({ jsonrpc: '2.0', id: init.id, error: { code: -32000, message: 'refused' } });
    await expect(connected).rejects.toThrow('refused');

    fromHost({
      jsonrpc: '2.0',
      method: 'ui/notifications/tool-result',
      params: { structuredContent: { nodes: [] } },
    });
    expect(onToolResult).not.toHaveBeenCalled();
  });

  it('retries the handshake on the next connect after a failure', async () => {
    const { connectHost } = await import('../src/mcpClient');
    const first = connectHost({});
    const init1 = await nextMessage((m) => m.method === 'ui/initialize');
    fromHost({ jsonrpc: '2.0', id: init1.id, error: { code: -32000, message: 'busy' } });
    await expect(first).rejects.toThrow('busy');

    const second = connectHost({});
    await vi.waitFor(() =>
      expect(sent.filter((m) => m.method === 'ui/initialize')).toHaveLength(2)
    );
    const init2 = sent.filter((m) => m.method === 'ui/initialize')[1];
    fromHost({ jsonrpc: '2.0', id: init2.id, result: initResult });
    await expect(second).resolves.toBeTypeOf('function');
  });

  it('answers ping and rejects unknown host requests with method-not-found', async () => {
    const { connectHost } = await import('../src/mcpClient');
    connectHost({});
    await nextMessage((m) => m.method === 'ui/initialize');

    fromHost({ jsonrpc: '2.0', id: 'p-1', method: 'ping' });
    fromHost({ jsonrpc: '2.0', id: 'u-1', method: 'ui/unknown-thing', params: {} });

    expect((await nextMessage((m) => m.id === 'p-1')).result).toEqual({});
    expect((await nextMessage((m) => m.id === 'u-1')).error.code).toBe(-32601);
  });
});

describe('unwrapToolResult', () => {
  it('handles every shape a host can hand back', async () => {
    const { unwrapToolResult } = await import('../src/mcpClient');
    expect(unwrapToolResult({ structuredContent: { result: { nodes: [] } } })).toEqual({
      nodes: [],
    });
    expect(unwrapToolResult({ structuredContent: { nodes: [], query: 'q' } })).toEqual({
      nodes: [],
      query: 'q',
    });
    expect(unwrapToolResult({ content: [{ type: 'text', text: '{"nodes":[1]}' }] })).toEqual({
      nodes: [1],
    });
    expect(unwrapToolResult({ nodes: [2] })).toEqual({ nodes: [2] });
    expect(() =>
      unwrapToolResult({ isError: true, content: [{ type: 'text', text: 'bad input' }] })
    ).toThrow('bad input');
  });
});

describe('window.openai bridge', () => {
  it('takes precedence over the postMessage bridge and reads toolOutput', async () => {
    const callToolMock = vi.fn().mockResolvedValue({ structuredContent: { result: { ok: 1 } } });
    window.openai = {
      callTool: callToolMock,
      toolOutput: { result: { nodes: [{ id: 'o1' }], edges: [] } },
    };
    const { connectHost, callTool } = await import('../src/mcpClient');
    const onToolResult = vi.fn();

    await connectHost({ onToolResult });
    expect(onToolResult).toHaveBeenCalledWith({ nodes: [{ id: 'o1' }], edges: [] });

    await expect(callTool('get_node_details', { node_id: 'o1' })).resolves.toEqual({ ok: 1 });
    expect(callToolMock).toHaveBeenCalledWith('get_node_details', { node_id: 'o1' });
    expect(sent).toEqual([]);
    delete window.openai;
  });

  it('delivers a toolOutput that arrives later through openai:set_globals', async () => {
    window.openai = { callTool: vi.fn() };
    const { connectHost } = await import('../src/mcpClient');
    const onToolResult = vi.fn();
    const disconnect = await connectHost({ onToolResult });

    window.dispatchEvent(
      new CustomEvent('openai:set_globals', {
        detail: { globals: { toolOutput: { result: { nodes: [{ id: 'late' }] } } } },
      })
    );
    expect(onToolResult).toHaveBeenCalledWith({ nodes: [{ id: 'late' }] });

    disconnect();
    window.dispatchEvent(
      new CustomEvent('openai:set_globals', {
        detail: { globals: { toolOutput: { nodes: [{ id: 'after' }] } } },
      })
    );
    expect(onToolResult).toHaveBeenCalledTimes(1);
    delete window.openai;
  });
});

describe('MCP Apps bridge message handling', () => {
  it('answers a host request that reuses a pending request id without settling the call', async () => {
    const { callTool } = await import('../src/mcpClient');
    const pendingCall = callTool('search_graph', {});
    const req = await nextMessage((m) => m.method === 'tools/call');
    let settled = false;
    pendingCall.then(
      () => (settled = true),
      () => (settled = true)
    );

    fromHost({ jsonrpc: '2.0', id: req.id, method: 'ping' });
    const reply = await nextMessage((m) => m.id === req.id && m.result !== undefined);
    expect(reply.result).toEqual({});
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(settled).toBe(false);

    fromHost({ jsonrpc: '2.0', id: req.id, result: { structuredContent: { nodes: [] } } });
    await expect(pendingCall).resolves.toEqual({ nodes: [] });
  });

  it('ignores a second response to a request that already settled', async () => {
    const { callTool } = await import('../src/mcpClient');
    const pendingCall = callTool('search_graph', {});
    const req = await nextMessage((m) => m.method === 'tools/call');

    fromHost({ jsonrpc: '2.0', id: req.id, result: { structuredContent: { nodes: [1] } } });
    await expect(pendingCall).resolves.toEqual({ nodes: [1] });
    const sentBefore = sent.length;

    expect(() =>
      fromHost({ jsonrpc: '2.0', id: req.id, error: { code: -32000, message: 'late' } })
    ).not.toThrow();
    expect(sent).toHaveLength(sentBefore);
  });
});

describe('connectHost outside any host', () => {
  it('returns a no-op disconnect and posts nothing on an unframed page without window.openai', async () => {
    Object.defineProperty(window, 'parent', { value: window, configurable: true });
    const { connectHost } = await import('../src/mcpClient');
    const onToolResult = vi.fn();

    const disconnect = await connectHost({ onToolResult });

    expect(disconnect).toBeTypeOf('function');
    expect(() => disconnect()).not.toThrow();
    expect(sent).toEqual([]);
    expect(addedListeners.filter(([type]) => type === 'message')).toEqual([]);
  });
});
