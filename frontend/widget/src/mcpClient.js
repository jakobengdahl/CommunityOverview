/**
 * MCP client for the widget when it runs inside a chat host.
 *
 * Two host bridges are supported:
 * - MCP Apps (extension io.modelcontextprotocol/ui, spec 2026-01-26), used by
 *   Claude and by ChatGPT: JSON-RPC 2.0 over postMessage with the parent frame.
 * - window.openai, the ChatGPT Apps SDK globals, preferred when present since
 *   ChatGPT keeps supporting it alongside MCP Apps.
 */

export const MCP_APPS_PROTOCOL_VERSION = '2026-01-26';
const APP_INFO = { name: 'community-graph-widget', version: '1.0.0' };
// Matches WIDGET_HEIGHT_PX in backend/service/mcp_widget.py.
const WIDGET_HEIGHT_PX = 520;

function hasOpenAIBridge() {
  return !!(
    typeof window !== 'undefined' &&
    window.openai &&
    typeof window.openai.callTool === 'function'
  );
}

function isFramed() {
  return typeof window !== 'undefined' && !!window.parent && window.parent !== window;
}

/**
 * Check if MCP tools are available
 */
export function isMCPAvailable() {
  return hasOpenAIBridge() || isFramed();
}

/**
 * Reduce a CallToolResult to the tool's own payload.
 *
 * FastMCP puts a Dict-returning tool's value under structuredContent.result;
 * a host without structuredContent support leaves only the JSON text block.
 * Anything else (a bare payload) is returned unchanged.
 */
export function unwrapToolResult(raw) {
  if (!raw || typeof raw !== 'object') return raw;
  const textBlock = Array.isArray(raw.content)
    ? raw.content.find((c) => c && c.type === 'text')
    : undefined;
  if (raw.isError) {
    throw new Error(textBlock?.text || 'Tool call failed');
  }
  const sc = raw.structuredContent;
  if (sc && typeof sc === 'object') {
    const keys = Object.keys(sc);
    if (keys.length === 1 && keys[0] === 'result' && sc.result && typeof sc.result === 'object') {
      return sc.result;
    }
    return sc;
  }
  if (textBlock) {
    try {
      return JSON.parse(textBlock.text);
    } catch {
      return raw;
    }
  }
  return raw;
}

// ---- MCP Apps postMessage bridge ----

let nextRequestId = 1;
const pending = new Map();
// One handshake and one listener set per page: a widget that connects twice
// (React StrictMode re-runs effects) must neither re-initialize nor let the
// first connection's disconnect drop the second one's listener.
const toolResultListeners = new Set();
let initialized = null;
let listening = false;

function post(message) {
  // The view's sandbox origin is opaque; the host validates the source frame.
  window.parent.postMessage({ jsonrpc: '2.0', ...message }, '*');
}

function handleMessage(event) {
  if (event.source !== window.parent) return;
  const msg = event.data;
  if (!msg || msg.jsonrpc !== '2.0') return;
  if (msg.id !== undefined && pending.has(msg.id) && !msg.method) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) reject(new Error(msg.error.message || 'Host request failed'));
    else resolve(msg.result);
    return;
  }
  if (msg.method && msg.id === undefined) {
    if (msg.method === 'ui/notifications/tool-result') {
      toolResultListeners.forEach((listener) => listener(msg.params || {}));
    }
    return;
  }
  if (msg.method) {
    // Host -> view requests: the widget holds no state to tear down, and an
    // unanswered request would leave the host waiting.
    if (msg.method === 'ui/resource-teardown' || msg.method === 'ping') {
      post({ id: msg.id, result: {} });
    } else {
      post({ id: msg.id, error: { code: -32601, message: `Method not found: ${msg.method}` } });
    }
  }
}

function listen() {
  if (!listening) {
    window.addEventListener('message', handleMessage);
    listening = true;
  }
}

function request(method, params) {
  listen();
  const id = nextRequestId++;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    post({ id, method, params });
  });
}

/**
 * Connect to the host and receive the result of the tool that opened the widget.
 *
 * @param {{onToolResult?: (payload: Object) => void}} handlers
 * @returns {Promise<() => void>} resolves to a disconnect function
 */
export async function connectHost({ onToolResult } = {}) {
  const deliver = (raw) => {
    if (!onToolResult) return;
    try {
      onToolResult(unwrapToolResult(raw));
    } catch (error) {
      console.error('Tool result could not be read', error);
    }
  };

  if (hasOpenAIBridge()) {
    if (window.openai.toolOutput) deliver({ structuredContent: window.openai.toolOutput });
    const onGlobals = (event) => {
      const toolOutput = event?.detail?.globals?.toolOutput;
      if (toolOutput) deliver({ structuredContent: toolOutput });
    };
    window.addEventListener('openai:set_globals', onGlobals);
    return () => window.removeEventListener('openai:set_globals', onGlobals);
  }

  if (!isFramed()) return () => {};

  toolResultListeners.add(deliver);
  listen();
  if (!initialized) {
    initialized = request('ui/initialize', {
      protocolVersion: MCP_APPS_PROTOCOL_VERSION,
      appInfo: APP_INFO,
      appCapabilities: {},
    }).then(() => {
      post({ method: 'ui/notifications/initialized', params: {} });
      post({
        method: 'ui/notifications/size-changed',
        params: { height: WIDGET_HEIGHT_PX },
      });
    });
    // A failed handshake may be retried by the next connect.
    initialized.catch(() => {
      initialized = null;
    });
  }
  try {
    await initialized;
  } catch (error) {
    toolResultListeners.delete(deliver);
    throw error;
  }
  return () => {
    toolResultListeners.delete(deliver);
  };
}

/**
 * Call an MCP tool through the host and return its payload.
 * @param {string} toolName - Name of the tool to call
 * @param {Object} args - Tool arguments
 * @returns {Promise<Object>} Tool result
 */
export async function callTool(toolName, args = {}) {
  if (!isMCPAvailable()) {
    throw new Error('MCP tools not available: no MCP Apps host or window.openai bridge.');
  }

  try {
    const raw = hasOpenAIBridge()
      ? await window.openai.callTool(toolName, args)
      : await request('tools/call', { name: toolName, arguments: args });
    return unwrapToolResult(raw);
  } catch (error) {
    console.error(`MCP tool call failed: ${toolName}`, error);
    throw error;
  }
}

// Convenience methods for common operations

export async function searchGraph(query, options = {}) {
  return callTool('search_graph', {
    query,
    node_types: options.nodeTypes,
    limit: options.limit || 50,
  });
}

export async function getNodeDetails(nodeId) {
  return callTool('get_node_details', { node_id: nodeId });
}

export async function getRelatedNodes(nodeId, options = {}) {
  return callTool('get_related_nodes', {
    node_id: nodeId,
    relationship_types: options.relationshipTypes,
    depth: options.depth || 1,
  });
}

export async function findSimilarNodes(name, options = {}) {
  return callTool('find_similar_nodes', {
    name,
    node_type: options.nodeType,
    threshold: options.threshold || 0.7,
    limit: options.limit || 5,
  });
}

export async function addNodes(nodes, edges = []) {
  return callTool('add_nodes', { nodes, edges });
}

export async function updateNode(nodeId, updates) {
  return callTool('update_node', { node_id: nodeId, updates });
}

export async function deleteNodes(nodeIds, confirmed = false) {
  return callTool('delete_nodes', { node_ids: nodeIds, confirmed });
}

export async function getGraphStats() {
  return callTool('get_graph_stats', {});
}

export async function exportGraph() {
  // Note: This might not be available as an MCP tool
  // Fallback to search with empty query to get all nodes
  return callTool('search_graph', { query: '', limit: 1000 });
}

/**
 * Get the schema configuration
 * @returns {Promise<{node_types: Object, relationship_types: Object}>}
 */
export async function getSchema() {
  return callTool('get_schema', {});
}

/**
 * Get the presentation configuration
 * @returns {Promise<{title: string, introduction: string, colors: Object}>}
 */
export async function getPresentation() {
  return callTool('get_presentation', {});
}
