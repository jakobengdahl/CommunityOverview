/**
 * REST API client for GraphService
 *
 * Calls the backend endpoints exposed by app_host
 */

const API_BASE = '/api';

// ============================================================
// Event Context / Session ID Management
// ============================================================

/**
 * Generate a unique session ID for event tracking.
 * This helps with webhook loop prevention.
 */
function generateSessionId() {
  return 'session-' + Date.now().toString(36) + '-' + Math.random().toString(36).substr(2, 9);
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
    throw new Error(errorData.error || `HTTP error: ${response.status}`);
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
  return apiFetch('/execute_tool', {
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

const UI_API_BASE = '/ui';

/**
 * Send a chat message to the backend
 * @param {Array} messages - Conversation history
 * @param {string} documentContext - Optional document text to include
 * @returns {Promise<{content: string, toolUsed: string|null, toolResult: Object|null}>}
 */
export async function sendChatMessage(messages, documentContext = null) {
  const body = { messages };
  if (documentContext) {
    body.document_context = documentContext;
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
export async function sendSimpleChatMessage(message, documentContext = null) {
  const body = { message };
  if (documentContext) {
    body.document_context = documentContext;
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
export async function uploadFile(file, analyze = false) {
  const formData = new FormData();
  formData.append('file', file);

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
    }),
  });
}
