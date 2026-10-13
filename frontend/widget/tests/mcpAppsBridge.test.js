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

function fromHost(data) {
  window.dispatchEvent(new MessageEvent('message', { data, source: host }));
}

async function nextMessage(predicate) {
  await vi.waitFor(() => expect(sent.find(predicate)).toBeDefined());
  return sent.find(predicate);
}

beforeEach(() => {
  vi.resetModules();
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
});
