/**
 * REST API client for GraphService
 *
 * Calls the backend endpoints exposed by app_host
 */

const API_BASE = '/api';

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
    body: JSON.stringify({ nodes, edges }),
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
    body: JSON.stringify({ updates }),
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
    body: JSON.stringify({ node_ids: nodeIds, confirmed }),
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
 * Get relationship type metadata
 * @returns {Promise<{relationship_types: Array}>}
 */
export async function getRelationshipTypes() {
  return apiFetch(`${API_BASE}/meta/relationship-types`);
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
