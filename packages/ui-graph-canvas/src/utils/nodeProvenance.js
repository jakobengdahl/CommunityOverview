/**
 * One place that answers "which graph owns this node?" for the whole UI.
 *
 * Federated nodes arrive with provenance stamped on their metadata by the
 * backend's federation cache (`origin_graph_id`, `origin_graph_name`,
 * `origin_node_id`, `is_federated`). A local node carries none of it. The
 * search result list and the canvas both need that distinction to decide
 * whether to offer teleport, and when the two decided it separately they
 * drifted: the result list keyed off `origin_graph_id` while the only other
 * marker in the payload was `is_federated`. Keeping one reader means a node
 * shown in both places offers the same action in both places.
 *
 * `origin_graph_id` is the authoritative field — it is the same one the backend
 * narrows graph visibility on (`access.node_graph_id`) — so an empty or missing
 * value means local, whatever else the metadata says.
 *
 * This lives in the canvas package and is re-exported from its index so the
 * host app's search results and the canvas's context menu read provenance
 * through one function rather than each carrying its own copy of the rule.
 */

/**
 * Metadata for a node that may be a raw API node or a canvas `node.data`.
 *
 * @param {Object} node - API node, or a React Flow node's `data`
 * @returns {Object} The node's metadata, never null
 */
function metadataOf(node) {
  if (!node) return {};
  return node.metadata || node.data?.metadata || {};
}

/**
 * The id of the graph that owns `node`, or '' when this graph owns it.
 *
 * @param {Object} node - API node or canvas node data
 * @returns {string}
 */
export function originGraphId(node) {
  const value = metadataOf(node).origin_graph_id;
  // Coerced the way the backend's access.node_graph_id / teleport._normalize do
  // (`str(value or "").strip()`), so a non-string value classifies the same on
  // both sides of the wire rather than the two readers disagreeing.
  return value ? String(value).trim() : '';
}

/**
 * True when `node` is owned by another graph and can be teleported to.
 *
 * @param {Object} node - API node or canvas node data
 * @returns {boolean}
 */
export function isFederatedNode(node) {
  return originGraphId(node) !== '';
}

/**
 * The display name of the graph that owns `node`, falling back through the
 * stats-provided display names to a caller-supplied label for the local graph.
 *
 * @param {Object} node - API node or canvas node data
 * @param {Object} options - `graphDisplayNames` map and `localLabel`
 * @returns {string}
 */
export function originGraphName(node, { graphDisplayNames = {}, localLabel = '' } = {}) {
  const graphId = originGraphId(node);
  if (!graphId) {
    return graphDisplayNames.local || localLabel;
  }
  return metadataOf(node).origin_graph_name || graphDisplayNames[graphId] || graphId;
}
