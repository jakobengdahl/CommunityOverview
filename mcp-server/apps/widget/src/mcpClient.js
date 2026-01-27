/**
 * MCP Client for widget - calls tools via window.openai.callTool
 *
 * This module provides a consistent API for calling MCP tools
 * from within an embedded widget context (e.g., ChatGPT plugins).
 */

/**
 * Check if MCP tools are available
 */
export function isMCPAvailable() {
  return typeof window !== 'undefined' &&
         window.openai &&
         typeof window.openai.callTool === 'function';
}

/**
 * Call an MCP tool
 * @param {string} toolName - Name of the tool to call
 * @param {Object} args - Tool arguments
 * @returns {Promise<Object>} Tool result
 */
export async function callTool(toolName, args = {}) {
  if (!isMCPAvailable()) {
    throw new Error('MCP tools not available. Ensure window.openai.callTool is defined.');
  }

  try {
    const result = await window.openai.callTool(toolName, args);
    return result;
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
    communities: options.communities,
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

export async function getGraphStats(communities = null) {
  return callTool('get_graph_stats', { communities });
}

export async function exportGraph() {
  // Note: This might not be available as an MCP tool
  // Fallback to search with empty query to get all nodes
  return callTool('search_graph', { query: '', limit: 1000 });
}
