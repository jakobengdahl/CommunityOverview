/**
 * REST API client for GraphService
 *
 * Calls the backend endpoints exposed by app_host
 */

export function getPathRoot() {
  const pathname = window.location.pathname;
  const webIndex = pathname.lastIndexOf('/web/');
  return webIndex !== -1 ? pathname.substring(0, webIndex) : '';
}

const API_BASE = getPathRoot() + '/api';

// ============================================================
// Event Context / Session ID Management
// ============================================================

/**
 * Mint a random lowercase-hex token of `length` characters.
 *
 * Backed by crypto.getRandomValues, like generateVisualizationSessionId below.
 * Math.random is seeded per page and its output is recoverable from earlier
 * draws, so identifiers minted from it are guessable across clients; neither id
 * built on this helper is treated as a capability today, but they are shared
 * with collaborators, so they are minted unpredictably.
 */
function randomToken(length) {
  const bytes = crypto.getRandomValues(new Uint8Array(Math.ceil(length / 2)));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0'))
    .join('')
    .slice(0, length);
}

/**
 * Generate a unique session ID for event tracking.
 * This helps with webhook loop prevention.
 */
function generateSessionId() {
  return 'session-' + Date.now().toString(36) + '-' + randomToken(9);
}

// Session ID for this browser session (persisted in sessionStorage)
let _eventSessionId = null;

/**
 * Get the current session ID, creating one if needed.
 * @returns {string} The session ID
 */
export function getEventSessionId() {
  if (!_eventSessionId) {
    // Try to restore from sessionStorage
    _eventSessionId = sessionStorage.getItem('eventSessionId');
    if (!_eventSessionId) {
      _eventSessionId = generateSessionId();
      sessionStorage.setItem('eventSessionId', _eventSessionId);
    }
  }
  return _eventSessionId;
}

/**
 * Get the event origin identifier for web UI requests.
 * @returns {string} The event origin
 */
export function getEventOrigin() {
  return 'web-ui';
}

// ============================================================
// API Client
// ============================================================

/**
 * Generic fetch helper with error handling
 */
async function apiFetch(url, options = {}) {
  const response = await fetch(url, {
    headers: {
      'Content-Type': 'application/json',
      ...options.headers,
    },
    ...options,
  });

  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    const error = new Error(errorData.error || `HTTP error: ${response.status}`);
    error.status = response.status;
    throw error;
  }

  return response.json();
}

/**
 * Search for nodes in the graph
 * @param {string} query - Search text
 * @param {Object} options - Search options
 * @returns {Promise<{nodes: Array, edges: Array}>}
 */
export async function searchGraph(query, options = {}) {
  return apiFetch(`${API_BASE}/search`, {
    method: 'POST',
    body: JSON.stringify({
      query,
      node_types: options.nodeTypes,
      communities: options.communities,
      limit: options.limit || 50,
      federation_depth: options.federationDepth,
    }),
  });
}

/**
 * Get details for a specific node
 * @param {string} nodeId - Node ID
 * @returns {Promise<{node: Object, edges: Array}>}
 */
export async function getNodeDetails(nodeId) {
  return apiFetch(`${API_BASE}/nodes/${encodeURIComponent(nodeId)}`);
}

/**
 * Get nodes related to a given node
 * @param {string} nodeId - Starting node ID
 * @param {Object} options - Query options
 * @returns {Promise<{nodes: Array, edges: Array}>}
 */
export async function getRelatedNodes(nodeId, options = {}) {
  return apiFetch(`${API_BASE}/nodes/${encodeURIComponent(nodeId)}/related`, {
    method: 'POST',
    body: JSON.stringify({
      relationship_types: options.relationshipTypes,
      depth: options.depth || 1,
    }),
  });
}

/**
 * Find similar nodes by name
 * @param {string} name - Name to search for
 * @param {Object} options - Search options
 * @returns {Promise<{similar_nodes: Array}>}
 */
export async function findSimilarNodes(name, options = {}) {
  return apiFetch(`${API_BASE}/similar`, {
    method: 'POST',
    body: JSON.stringify({
      name,
      node_type: options.nodeType,
      threshold: options.threshold || 0.7,
      limit: options.limit || 5,
    }),
  });
}

/**
 * Add nodes and edges to the graph
 * @param {Array} nodes - Nodes to add
 * @param {Array} edges - Edges to add
 * @returns {Promise<{success: boolean, added_node_ids: Array, added_edge_ids: Array}>}
 */
export async function addNodes(nodes, edges = []) {
  return apiFetch(`${API_BASE}/nodes`, {
    method: 'POST',
    body: JSON.stringify({
      nodes,
      edges,
      event_origin: getEventOrigin(),
      event_session_id: getEventSessionId(),
    }),
  });
}

/**
 * Update an existing node
 * @param {string} nodeId - Node ID
 * @param {Object} updates - Fields to update
 * @returns {Promise<{success: boolean}>}
 */
export async function updateNode(nodeId, updates) {
  return apiFetch(`${API_BASE}/nodes/${encodeURIComponent(nodeId)}`, {
    method: 'PATCH',
    body: JSON.stringify({
      updates,
      event_origin: getEventOrigin(),
      event_session_id: getEventSessionId(),
    }),
  });
}

/**
 * Delete nodes from the graph
 * @param {Array} nodeIds - Node IDs to delete
 * @param {boolean} confirmed - Confirmation flag
 * @returns {Promise<{success: boolean, deleted_count: number}>}
 */
export async function deleteNodes(nodeIds, confirmed = false) {
  return apiFetch(`${API_BASE}/nodes`, {
    method: 'DELETE',
    body: JSON.stringify({
      node_ids: nodeIds,
      confirmed,
      event_origin: getEventOrigin(),
      event_session_id: getEventSessionId(),
    }),
  });
}

/**
 * Add a single edge between existing nodes
 * @param {string} source - Source node ID
 * @param {string} target - Target node ID
 * @param {Object} options - Edge options (type, label)
 * @returns {Promise<{success: boolean, edge: Object}>}
 */
export async function addEdge(source, target, options = {}) {
  return apiFetch(`${API_BASE}/edges`, {
    method: 'POST',
    body: JSON.stringify({
      source,
      target,
      type: options.type || null,
      label: options.label || null,
      event_origin: getEventOrigin(),
      event_session_id: getEventSessionId(),
    }),
  });
}

/**
 * Update an existing edge
 * @param {string} edgeId - Edge ID
 * @param {Object} updates - Fields to update (type, label, metadata)
 * @returns {Promise<{success: boolean, edge: Object}>}
 */
export async function updateEdge(edgeId, updates) {
  return apiFetch(`${API_BASE}/edges/${encodeURIComponent(edgeId)}`, {
    method: 'PATCH',
    body: JSON.stringify({
      updates,
      event_origin: getEventOrigin(),
      event_session_id: getEventSessionId(),
    }),
  });
}

/**
 * Delete a single edge
 * @param {string} edgeId - Edge ID
 * @returns {Promise<{success: boolean}>}
 */
export async function deleteEdge(edgeId) {
  return apiFetch(`${API_BASE}/edges/${encodeURIComponent(edgeId)}`, {
    method: 'DELETE',
  });
}

/**
 * Get recent graph mutation history (newest first).
 * @param {Object} options - { limit, offset }
 * @returns {Promise<{success: boolean, entries: Array, count: number, limit: number, offset: number}>}
 */
export async function getGraphHistory(options = {}) {
  const limit = options.limit ?? 50;
  const offset = options.offset ?? 0;
  return apiFetch(`${API_BASE}/history?limit=${limit}&offset=${offset}`);
}

/**
 * Get mutation history for a single node (newest first).
 * @param {string} nodeId - Node ID
 * @param {Object} options - { limit, offset }
 * @returns {Promise<{success: boolean, node_id: string, entries: Array, count: number}>}
 */
export async function getNodeHistory(nodeId, options = {}) {
  const limit = options.limit ?? 50;
  const offset = options.offset ?? 0;
  return apiFetch(
    `${API_BASE}/nodes/${encodeURIComponent(nodeId)}/history?limit=${limit}&offset=${offset}`
  );
}

/**
 * Get mutation history for a single edge (newest first).
 * @param {string} edgeId - Edge ID
 * @param {Object} options - { limit, offset }
 * @returns {Promise<{success: boolean, edge_id: string, entries: Array, count: number}>}
 */
export async function getEdgeHistory(edgeId, options = {}) {
  const limit = options.limit ?? 50;
  const offset = options.offset ?? 0;
  return apiFetch(
    `${API_BASE}/edges/${encodeURIComponent(edgeId)}/history?limit=${limit}&offset=${offset}`
  );
}

/**
 * Get graph statistics
 * @param {Array} communities - Optional community filter
 * @returns {Promise<{total_nodes: number, total_edges: number, ...}>}
 */
export async function getGraphStats(communities = null) {
  const url = communities
    ? `${API_BASE}/stats?communities=${communities.join(',')}`
    : `${API_BASE}/stats`;
  return apiFetch(url);
}

/**
 * Get node type metadata
 * @returns {Promise<{node_types: Array}>}
 */
export async function getNodeTypes() {
  return apiFetch(`${API_BASE}/meta/node-types`);
}

/**
 * Get existing subtypes grouped by node type
 * @param {string} [nodeType] - Optional filter by node type
 * @returns {Promise<{subtypes: Object}>}
 */
export async function getSubtypes(nodeType) {
  const params = nodeType ? `?node_type=${encodeURIComponent(nodeType)}` : '';
  return apiFetch(`${API_BASE}/meta/subtypes${params}`);
}

/**
 * Get relationship type metadata
 * @returns {Promise<{relationship_types: Array}>}
 */
export async function getRelationshipTypes() {
  return apiFetch(`${API_BASE}/meta/relationship-types`);
}

/**
 * Get the complete schema configuration
 * @returns {Promise<{node_types: Object, relationship_types: Object}>}
 */
export async function getSchema() {
  return apiFetch(`${API_BASE}/schema`);
}

/**
 * Get the presentation configuration
 * @returns {Promise<{title: string, introduction: string, colors: Object, prompt_prefix: string, prompt_suffix: string}>}
 */
export async function getPresentation() {
  return apiFetch(`${API_BASE}/presentation`);
}

/**
 * Export the entire graph
 * @returns {Promise<{nodes: Array, edges: Array}>}
 */
export async function exportGraph() {
  return apiFetch(`${API_BASE}/export`);
}

/**
 * Save a view
 * @param {string} name - View name
 * @returns {Promise<{success: boolean}>}
 */
export async function saveView(name) {
  return apiFetch(`${API_BASE}/views/save`, {
    method: 'POST',
    body: JSON.stringify({ name }),
  });
}

/**
 * Get a saved view
 * @param {string} name - View name
 * @returns {Promise<Object>}
 */
export async function getSavedView(name) {
  return apiFetch(`${API_BASE}/views/${encodeURIComponent(name)}`);
}

/**
 * List all saved views
 * @returns {Promise<{views: Array}>}
 */
export async function listSavedViews() {
  return apiFetch(`${API_BASE}/views`);
}

/**
 * Execute a backend tool directly (for MCP tool compatibility)
 * @param {string} toolName - Tool name
 * @param {Object} args - Tool arguments
 * @returns {Promise<Object>}
 */
export async function executeTool(toolName, args) {
  return apiFetch(getPathRoot() + '/execute_tool', {
    method: 'POST',
    body: JSON.stringify({
      tool_name: toolName,
      arguments: args,
    }),
  });
}

// ============================================================
// UI Backend Chat API (/ui/*)
// ============================================================

const UI_API_BASE = getPathRoot() + '/ui';

/**
 * Send a chat message to the backend
 * @param {Array} messages - Conversation history
 * @param {string} documentContext - Optional document text to include
 * @returns {Promise<{content: string, toolUsed: string|null, toolResult: Object|null}>}
 */
export async function sendChatMessage(messages, documentContext = null, options = {}) {
  const body = { messages };
  if (documentContext) {
    body.document_context = documentContext;
  }
  if (options.federationDepth) {
    body.federation_depth = options.federationDepth;
  }
  if (options.modelProfileId) {
    body.model_profile_id = options.modelProfileId;
  }
  if (options.expertAgentId) {
    body.expert_agent_id = options.expertAgentId;
  }
  if (options.skillsContext) {
    body.skills_context = options.skillsContext;
  }
  if (options.collectionShortName) {
    body.collection_short_name = options.collectionShortName;
  }
  if (Array.isArray(options.visibleNodeIds)) {
    body.visible_node_ids = options.visibleNodeIds;
  }
  if (Array.isArray(options.selectedNodeIds)) {
    body.selected_node_ids = options.selectedNodeIds;
  }
  return apiFetch(`${UI_API_BASE}/chat`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

/**
 * Send a simple chat message (single message, no history)
 * @param {string} message - The message to send
 * @param {string} documentContext - Optional document text
 * @returns {Promise<{content: string, toolUsed: string|null, toolResult: Object|null}>}
 */
export async function sendSimpleChatMessage(message, documentContext = null, options = {}) {
  const body = { message };
  if (documentContext) {
    body.document_context = documentContext;
  }
  if (options.federationDepth) {
    body.federation_depth = options.federationDepth;
  }
  return apiFetch(`${UI_API_BASE}/chat/simple`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

/**
 * Upload a file for analysis
 * @param {File} file - The file to upload
 * @param {boolean} analyze - Whether to analyze with LLM (default: false, just extract text)
 * @returns {Promise<{success: boolean, filename: string, text: string, analysis?: string}>}
 */
export async function uploadFile(file, analyze = false, options = {}) {
  const formData = new FormData();
  formData.append('file', file);
  if (options.modelProfileId) formData.append('model_profile_id', options.modelProfileId);

  const endpoint = analyze ? `${UI_API_BASE}/upload` : `${UI_API_BASE}/upload/extract`;

  const response = await fetch(endpoint, {
    method: 'POST',
    body: formData,
  });

  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(errorData.detail || `Upload failed: ${response.status}`);
  }

  return response.json();
}

/**
 * Get chat service info
 * @returns {Promise<{llm_provider: string, supported_formats: string[]}>}
 */
export async function getChatInfo() {
  return apiFetch(`${UI_API_BASE}/info`);
}

/**
 * Get UI feature capabilities from the backend.
 * Used during startup to decide which features to show.
 * @returns {Promise<{llm_available: boolean, llm_provider: string}>}
 */
export async function getUiCapabilities() {
  return apiFetch(`${UI_API_BASE}/capabilities`);
}

/**
 * Get supported file formats for upload
 * @returns {Promise<{formats: string[]}>}
 */
export async function getSupportedFormats() {
  return apiFetch(`${UI_API_BASE}/supported-formats`);
}

/**
 * Propose nodes from text using LLM analysis
 * @param {string} text - Text to extract nodes from
 * @param {Object} options - Extraction options
 * @returns {Promise<{proposed_nodes: Array, similar_existing: Object, requires_confirmation: boolean}>}
 */
export async function proposeNodesFromText(text, options = {}) {
  return apiFetch(`${UI_API_BASE}/propose-nodes`, {
    method: 'POST',
    body: JSON.stringify({
      text,
      node_type: options.nodeType,
      communities: options.communities,
      model_profile_id: options.modelProfileId,
    }),
  });
}

export async function getCollectConfig(shortName) {
  return apiFetch(`${API_BASE}/collect/${encodeURIComponent(shortName)}`);
}

// ============================================================
// Visualization Session
// ============================================================

/**
 * Generate a cryptographically-random visualization session ID.
 * Format: "DDDD-DDDD-DDDD-DDDD" (four groups of four decimal digits, ~10^16
 * address space) so an unauthenticated caller cannot feasibly enumerate live
 * sessions. The backend still accepts the two-group legacy form for old URLs.
 * @returns {string}
 */
export function generateVisualizationSessionId() {
  const buf = crypto.getRandomValues(new Uint16Array(4));
  return Array.from(buf, (n) => String(n % 10000).padStart(4, '0')).join('-');
}

/**
 * Return the SSE stream URL for a visualization session.
 * Uses the same path-root prefix as all other API calls so sub-path
 * deployments (e.g. /tenant1/web/) work correctly.
 *
 * @param {string} sessionId
 * @returns {string}
 */
export function getVisualizationStreamUrl(sessionId) {
  return `${getPathRoot()}/sessions/${encodeURIComponent(sessionId)}/stream`;
}

/**
 * Mint (or rotate) the pulse-trigger token for a live visualization session and
 * return the absolute trigger URL an external system calls to pulse a node.
 * Re-minting rotates the token, so any previously shared URL stops working.
 *
 * @param {string} sessionId
 * @returns {Promise<{ url: string, token: string }>}
 */
export async function mintPulseTriggerUrl(sessionId) {
  const data = await apiFetch(
    `${getPathRoot()}/sessions/${encodeURIComponent(sessionId)}/trigger-token`,
    { method: 'POST' }
  );
  const base = new URL(`${getPathRoot()}${data.pulse_path}`, window.location.origin);
  base.searchParams.set('token', data.trigger_token);
  return { url: base.toString(), token: data.trigger_token };
}

/**
 * Return the realtime op-protocol SSE stream URL for a shared session
 * (design step 6). Distinct from the legacy MCP-push stream above: this one
 * carries applied ops, presence and claims from the fan-out hub.
 *
 * @param {string} sessionId
 * @returns {string}
 */
export function getSessionStreamUrl(sessionId) {
  return `${SESSIONS_BASE()}/${encodeURIComponent(sessionId)}/stream`;
}

// Stable per-browser client id for shared-session presence and op attribution
// (design 3.4). Kept in localStorage so it survives reloads. 12 hex chars: the
// server keys element claims and rate-limit buckets on this id, so two live
// clients colliding would let one release the other's claims.
const CLIENT_ID_KEY = 'graph_client_id';
let _clientId = null;

export function getClientId() {
  if (_clientId) return _clientId;
  try {
    _clientId = window.localStorage.getItem(CLIENT_ID_KEY);
  } catch {
    _clientId = null;
  }
  if (!_clientId) {
    _clientId = 'client-' + randomToken(12);
    try {
      window.localStorage.setItem(CLIENT_ID_KEY, _clientId);
    } catch {
      // ignore storage errors — a per-tab id is still usable
    }
  }
  return _clientId;
}

// User-editable presence name shown to collaborators in a shared session
// (design 3.4). When unset the server assigns a "Guest-<n>" default.
const DISPLAY_NAME_KEY = 'graph_display_name';

export function getDisplayName() {
  try {
    return window.localStorage.getItem(DISPLAY_NAME_KEY) || null;
  } catch {
    return null;
  }
}

export function setDisplayName(name) {
  const trimmed = (name || '').trim();
  try {
    if (trimmed) window.localStorage.setItem(DISPLAY_NAME_KEY, trimmed);
    else window.localStorage.removeItem(DISPLAY_NAME_KEY);
  } catch {
    // ignore storage errors
  }
}

const SESSIONS_BASE = () => `${API_BASE}/sessions`;

/**
 * Create a new server-side shared session (server-assigned id).
 * @param {string|null} name
 * @returns {Promise<Object>} session payload (meta + state + roster)
 */
export async function createSession(name = null) {
  return apiFetch(SESSIONS_BASE(), {
    method: 'POST',
    body: JSON.stringify({ name: name || null }),
  });
}

/**
 * List server-side session metadata (used to refresh recent-session names).
 * @returns {Promise<{sessions: Array}>}
 */
export async function listServerSessions() {
  return apiFetch(SESSIONS_BASE());
}

/**
 * Get a server-side session. With resolve=true the node references are
 * rehydrated to full node objects (+ edges) for loading onto the canvas.
 * @param {string} sessionId
 * @param {{resolve?: boolean}} options
 * @returns {Promise<Object>}
 */
export async function getSession(sessionId, { resolve = false } = {}) {
  const suffix = resolve ? '?resolve=true' : '';
  return apiFetch(`${SESSIONS_BASE()}/${encodeURIComponent(sessionId)}${suffix}`);
}

/**
 * Rename a server-side session (or clear the name with null/empty).
 * @param {string} sessionId
 * @param {string|null} name
 * @returns {Promise<Object>}
 */
export async function renameServerSession(sessionId, name) {
  return apiFetch(`${SESSIONS_BASE()}/${encodeURIComponent(sessionId)}`, {
    method: 'PATCH',
    body: JSON.stringify({ name: name || null }),
  });
}

/**
 * Delete a server-side session. The optional client id names the deleter in
 * the broadcast so connected clients can show "deleted by <name>".
 * @param {string} sessionId
 * @param {string} [clientId]
 * @returns {Promise<{deleted: boolean, id: string}>}
 */
export async function deleteServerSession(sessionId, clientId) {
  const suffix = clientId ? `?client_id=${encodeURIComponent(clientId)}` : '';
  return apiFetch(`${SESSIONS_BASE()}/${encodeURIComponent(sessionId)}${suffix}`, {
    method: 'DELETE',
  });
}

/**
 * Return the op-batch POST URL for a shared session (design step 6).
 * The sync client posts here directly so it can react to HTTP status codes
 * (429 backoff, 400 drop); hence a URL builder rather than a fetch helper.
 *
 * @param {string} sessionId
 * @returns {string}
 */
export function getSessionOpsUrl(sessionId) {
  return `${SESSIONS_BASE()}/${encodeURIComponent(sessionId)}/ops`;
}
