/**
 * REST API client for GraphService
 *
 * Calls the backend endpoints exposed by app_host
 */

import {
  SESSION_ID_HEADER,
  directSessionNames,
  mayWriteGraph,
  needsSessionHeader,
  pathBelowRoot,
} from './stagedSessionHeader';

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
// Staged-session binding
// ============================================================

// The session this browser is working in, and the sessions it knows to be
// staged. While the active session is one of them, every request
// needsSessionHeader() picks names it, so graph writes stage and graph reads
// compose (docs/SESSION_OVERLAY_CONTRACT.md §16.1). Keeping the two apart means
// switching sessions unbinds at once: a request can never carry the previous
// session's id into the next one. Only a session the server reports as staged
// is ever marked: a session that exists only in the browser is always direct
// (§5.1), and naming one the server does not hold would be refused.
let _activeSessionId = null;
const _stagedSessions = new Set();
// Sessions whose write mode has not been read from the server yet (a deep
// link before its session loads, or whose load failed). A graph write made
// from one is refused here rather than sent without the header: if the
// session is staged, that write would land in the graph.
const _modeUnknown = new Set();
// Where the backend can stage, a session the server holds may be switched to
// staged by another participant at any moment. Naming every such session on
// the routes that stage or compose lets the server decide each write by the
// mode it holds then, instead of this browser's possibly stale knowledge; in a
// direct session the header changes nothing there.
let _stagingAvailable = false;
const _serverSessions = new Set();

/** @param {boolean} available - the backend reports staged_sessions */
export function setStagingAvailable(available) {
  _stagingAvailable = !!available;
}

/**
 * Record that the server holds a session (its payload was read, it was
 * created, or its stream connected), so naming it cannot be refused as
 * session_not_found.
 * @param {string} sessionId
 */
export function setSessionOnServer(sessionId) {
  if (sessionId) _serverSessions.add(sessionId);
}

/**
 * Set the session graph requests are made from.
 * @param {string|null} sessionId
 */
export function setActiveSessionForRequests(sessionId) {
  _activeSessionId = sessionId || null;
}

/**
 * Record what the server says a session's write mode is. Staged is final
 * (§5.2), so only a delete ever unmarks one.
 * @param {string} sessionId
 * @param {boolean} staged
 */
// Messages this module raises itself, shown wherever an error's message is
// shown: translated by the app (setMessageTranslator), English until then.
let _translate = null;

/** @param {(key: string) => string} translator - i18n `t` */
export function setMessageTranslator(translator) {
  _translate = translator || null;
}

function translate(key, english) {
  const text = _translate?.(key);
  return text && text !== key ? text : english;
}

const _stagedListeners = new Set();

/**
 * Be told whenever a session is newly marked staged here, so a mark made below
 * the UI (e.g. on a re-read before an approval) shows at once.
 * @param {(sessionId: string) => void} listener
 * @returns {() => void} unsubscribe
 */
export function subscribeStagedMarks(listener) {
  _stagedListeners.add(listener);
  return () => _stagedListeners.delete(listener);
}

export function setSessionStaged(sessionId, staged) {
  if (!sessionId) return;
  _modeUnknown.delete(sessionId);
  if (staged) {
    const isNew = !_stagedSessions.has(sessionId);
    _stagedSessions.add(sessionId);
    if (isNew) _stagedListeners.forEach((listener) => listener(sessionId));
  } else {
    _stagedSessions.delete(sessionId);
    _serverSessions.delete(sessionId);
  }
}

/**
 * Hold graph writes from a session until the server has said how it writes.
 * setSessionStaged (with either answer) releases them.
 * @param {string} sessionId
 */
export function setSessionModeUnknown(sessionId) {
  if (sessionId && !_stagedSessions.has(sessionId)) _modeUnknown.add(sessionId);
}

/**
 * The server says the session is direct (or holds no such session, which a
 * browser-only session is): release writes held by setSessionModeUnknown.
 * Never turns a staged session direct.
 * @param {string} sessionId
 */
export function setSessionModeDirect(sessionId) {
  _modeUnknown.delete(sessionId);
}

/**
 * Whether content the session stream hands over may be staged content for
 * this viewer: the active session is staged, or its mode is not known yet.
 * Such content is re-read through composed, authorized reads, never shown as
 * delivered.
 * @returns {boolean}
 */
export function isActiveSessionPossiblyStaged() {
  return (
    !!_activeSessionId &&
    (_stagedSessions.has(_activeSessionId) || _modeUnknown.has(_activeSessionId))
  );
}

/** @returns {string|null} the active session's id when it is staged */
export function getStagedSessionBinding() {
  return _activeSessionId && _stagedSessions.has(_activeSessionId) ? _activeSessionId : null;
}

function sessionHeaders(url, method) {
  const path = pathBelowRoot(url, getPathRoot());
  const staged = getStagedSessionBinding();
  if (staged) {
    return needsSessionHeader(method, path) ? { [SESSION_ID_HEADER]: staged } : {};
  }
  if (
    _stagingAvailable &&
    _activeSessionId &&
    _serverSessions.has(_activeSessionId) &&
    directSessionNames(method, path)
  ) {
    return { [SESSION_ID_HEADER]: _activeSessionId };
  }
  return {};
}

// ============================================================
// API Client
// ============================================================

/**
 * Build the Error a failed response is thrown as. Most endpoints answer
 * {"error": "..."}; FastAPI's HTTPException answers {"detail": "..."}, and the
 * session and staged-change routes answer {"detail": {"error": code,
 * "message": ..., ...data}}. The message is the human-readable reason; `code`
 * and `data` carry a structured refusal so callers can act on it.
 */
async function responseError(response) {
  const errorData = await response.json().catch(() => ({}));
  const detail = errorData.detail;
  const structured = detail && typeof detail === 'object' && !Array.isArray(detail) ? detail : null;
  const message = structured
    ? structured.message || structured.error
    : errorData.error || (typeof detail === 'string' ? detail : null);
  const error = new Error(message || `HTTP error: ${response.status}`);
  error.status = response.status;
  error.code = structured?.error || (typeof errorData.error === 'string' ? errorData.error : null);
  error.data = structured || errorData;
  return error;
}

// A request that may write the graph from a session whose write mode is not
// known yet is held: the session may be staged.
function refuseWhileModeUnknown(url, method) {
  if (
    _activeSessionId &&
    _modeUnknown.has(_activeSessionId) &&
    mayWriteGraph(method, pathBelowRoot(url, getPathRoot()))
  ) {
    const error = new Error(
      translate(
        'staged.write_held',
        "This session's write mode is not known yet, so nothing is written until it has loaded."
      )
    );
    error.code = 'session_mode_unknown';
    throw error;
  }
}

/**
 * Generic fetch helper with error handling
 */
async function apiFetch(url, options = {}) {
  refuseWhileModeUnknown(url, options.method);
  const response = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...sessionHeaders(url, options.method),
      ...options.headers,
    },
  });

  if (!response.ok) {
    throw await responseError(response);
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
 * Resolve the canonical route from a node to the graph that owns it.
 *
 * The server decides, from the caller's graph access, whether a route may be
 * handed back at all; the response's `status` is one of `ok`, `local`,
 * `permission_denied`, `graph_unavailable` or `unknown_node`.
 *
 * @param {string} nodeId - Local or federated node ID
 * @param {Object} options - `sessionId` and `searchQuery` to carry along
 * @returns {Promise<Object>} Resolved teleport target
 */
export async function resolveTeleport(nodeId, options = {}) {
  return apiFetch(`${API_BASE}/federation/teleport`, {
    method: 'POST',
    body: JSON.stringify({
      node_id: nodeId,
      session_id: options.sessionId,
      search_query: options.searchQuery,
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

// ============================================================
// Node attachments (ADR 0008)
// ============================================================

function attachmentsUrl(nodeId) {
  return `${API_BASE}/nodes/${encodeURIComponent(nodeId)}/attachments`;
}

/**
 * The attachment routes answer errors as {detail: {error, message}}, which
 * apiFetch would flatten to "[object Object]"; keep the code so the UI can
 * map it to a translated message.
 */
async function attachmentFetch(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = body && typeof body.detail === 'object' && body.detail ? body.detail : {};
    const error = new Error(
      detail.message || (typeof body.detail === 'string' ? body.detail : '') || ''
    );
    error.status = response.status;
    error.code = detail.error || '';
    throw error;
  }
  return response.json();
}

/**
 * List a node's attachments.
 * @returns {Promise<{attachments: Array, limits: Object, uploads_allowed: boolean}>}
 */
export async function listNodeAttachments(nodeId) {
  return attachmentFetch(attachmentsUrl(nodeId));
}

/**
 * Attach one file to a node. The body is the raw bytes, not multipart.
 * @param {string} nodeId
 * @param {File} file
 * @returns {Promise<Object>} the stored attachment record
 */
export async function uploadNodeAttachment(nodeId, file) {
  return attachmentFetch(`${attachmentsUrl(nodeId)}?filename=${encodeURIComponent(file.name)}`, {
    method: 'POST',
    headers: { 'Content-Type': file.type || 'application/octet-stream' },
    body: file,
  });
}

/** The download route; the server always serves it as an attachment. */
export function getNodeAttachmentUrl(nodeId, attachmentId) {
  return `${attachmentsUrl(nodeId)}/${encodeURIComponent(attachmentId)}`;
}

export async function deleteNodeAttachment(nodeId, attachmentId) {
  return attachmentFetch(getNodeAttachmentUrl(nodeId, attachmentId), { method: 'DELETE' });
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

  // Analysis runs the assistant, which can write: held and named like any
  // other write (no JSON content type: the body is a form).
  refuseWhileModeUnknown(endpoint, 'POST');
  const response = await fetch(endpoint, {
    method: 'POST',
    headers: sessionHeaders(endpoint, 'POST'),
    body: formData,
  });

  if (!response.ok) {
    // A structured refusal (a staged session's) keeps its code and message.
    throw await responseError(response);
  }

  return response.json();
}

/**
 * Approve or reject an agent's held proposal. Through apiFetch, so in a staged
 * session an approval names the session: the backend then applies it there,
 * or refuses one made elsewhere, instead of writing it to the graph.
 * @param {string} proposalId
 * @param {'approve'|'reject'} action
 */
export async function decideAgentProposal(proposalId, action) {
  if (
    action === 'approve' &&
    _stagingAvailable &&
    _activeSessionId &&
    _serverSessions.has(_activeSessionId) &&
    !_stagedSessions.has(_activeSessionId)
  ) {
    // A direct session leaves approval to the proposal's own session, but it
    // may have been switched to staged by someone else a moment ago: read its
    // mode first, so an approval from a now-staged session names it and one
    // made elsewhere is refused rather than written to the graph.
    const id = _activeSessionId;
    let now;
    try {
      now = await getSession(id);
    } catch (error) {
      // Unread, the session may be staged: refuse rather than approve
      // unbound. A 404 means the server holds no such session (it is then
      // browser-only, so direct).
      if (error?.status !== 404) throw error;
      now = null;
    }
    // About the session that was read, and only while it is still the one
    // approving: another session's mode is never inferred from it.
    if (now?.write_mode === 'staged' && _activeSessionId === id) setSessionStaged(id, true);
    if (_activeSessionId !== id) {
      const error = new Error(
        translate(
          'staged.approve_session_changed',
          'The session changed while approving; approve again.'
        )
      );
      error.code = 'session_changed';
      throw error;
    }
  }
  return apiFetch(`${getPathRoot()}/agents/proposals/${encodeURIComponent(proposalId)}/${action}`, {
    method: 'POST',
  });
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
  // Rejection sampling, not a bare `% 10000`: a Uint16 spans 65536 values, so
  // folding the whole range would draw 0000-5535 six times per cycle and
  // 5536-9999 only five, biasing every group. 0-59999 is an exact six-fold
  // cover of the 10000 outcomes, so discarding the tail keeps each group
  // uniform and the address space at its full ~10^16. This id is the
  // capability that guards a shared session (design D7), so the bias is worth
  // the extra draws.
  const groups = [];
  while (groups.length < 4) {
    for (const n of crypto.getRandomValues(new Uint16Array(4))) {
      if (n >= 60000) continue;
      groups.push(String(n % 10000).padStart(4, '0'));
      if (groups.length === 4) break;
    }
  }
  return groups.join('-');
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
 * @param {{writeMode?: 'direct'|'staged'}} [options] - omitted, the session
 *   takes the graph default. A staged session is always created this way, on
 *   the server, before anything acts in it (contract §5.1).
 * @returns {Promise<Object>} session payload (meta + state + roster), with
 *   `write_mode` and `write_mode_source`
 */
export async function createSession(name = null, { writeMode } = {}) {
  const body = { name: name || null };
  if (writeMode) body.write_mode = writeMode;
  return apiFetch(SESSIONS_BASE(), {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

/**
 * Switch a session from direct to staged (contract §5.2). Irreversible: the
 * server refuses staged -> direct with `write_mode_irreversible`.
 * @param {string} sessionId
 * @param {'staged'} writeMode
 * @param {string} [clientId]
 * @returns {Promise<Object>} the session payload
 */
export async function setSessionWriteMode(sessionId, writeMode, clientId) {
  return apiFetch(`${SESSIONS_BASE()}/${encodeURIComponent(sessionId)}`, {
    method: 'PATCH',
    body: JSON.stringify({ write_mode: writeMode, client_id: clientId || null }),
  });
}

/**
 * The diff of a staged session's changes (contract §10), as far as the
 * caller's view covers them.
 * @param {string} sessionId
 * @returns {Promise<{items: Array, count: number, write_mode: string}>}
 */
export async function getStagedChanges(sessionId) {
  return apiFetch(`${SESSIONS_BASE()}/${encodeURIComponent(sessionId)}/staged`);
}

/**
 * Merge staged changes into the graph as one unit (contract §11). A thrown
 * error carries `code` (merge_blocked, merge_raced, merge_incomplete,
 * invalid_merge_request, ...) and `data` (e.g. `items`, `missing`).
 * @param {string} sessionId
 * @param {{merge_id: string, entries: Array, resolutions?: Array}} request
 * @returns {Promise<Object>}
 */
export async function mergeStagedChanges(sessionId, request) {
  return apiFetch(`${SESSIONS_BASE()}/${encodeURIComponent(sessionId)}/staged/merge`, {
    method: 'POST',
    body: JSON.stringify(request),
  });
}

/**
 * Drop staged changes; the graph does not change (contract §12).
 * @param {string} sessionId
 * @param {Array<{kind: string, entity_id: string}>} entries
 * @returns {Promise<{removed_entries: Array}>}
 */
export async function discardStagedChanges(sessionId, entries) {
  return apiFetch(`${SESSIONS_BASE()}/${encodeURIComponent(sessionId)}/staged/discard`, {
    method: 'POST',
    body: JSON.stringify({ entries }),
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
 * the broadcast so connected clients can show "deleted by <name>". A staged
 * session holding staged changes is refused (`layer_not_empty`) unless
 * `discardStaged` is set (contract §13).
 * @param {string} sessionId
 * @param {string} [clientId]
 * @param {{discardStaged?: boolean}} [options]
 * @returns {Promise<{deleted: boolean, id: string}>}
 */
export async function deleteServerSession(sessionId, clientId, { discardStaged = false } = {}) {
  const params = new URLSearchParams();
  if (clientId) params.set('client_id', clientId);
  if (discardStaged) params.set('discard_staged', 'true');
  const suffix = params.toString() ? `?${params.toString()}` : '';
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

/**
 * Ingest a human-pasted/uploaded image as a session `image` annotation.
 *
 * A plain POST, not a sessionSyncClient op: the server validates, optimizes
 * and embeds the image (the same pipeline the MCP `create_image_annotation`
 * tool uses — backend/service/rest_api.py's `ingest_session_image`), so the
 * result cannot be diffed client-side the way a note or shape can. The
 * response here is informational only — the annotation the server actually
 * stored arrives over this browser's own SSE subscription, attributed to a
 * dedicated server client id rather than `clientId`, specifically so that
 * echo is not dropped as a self-authored op (see the endpoint's docstring).
 *
 * @param {string} sessionId
 * @param {Object} image
 * @param {number} image.x
 * @param {number} image.y
 * @param {string} [image.imageData] - a data: URL or bare base64 string
 * @param {string} [image.imageUrl] - an http(s) URL, fetched once server-side
 * @param {number} [image.w]
 * @param {number} [image.h]
 * @param {number} [image.rotation]
 * @param {string} [image.alt]
 * @param {Object} [image.style]
 * @param {number} [image.z]
 * @param {boolean} [image.locked]
 * @param {string} [image.annotationId]
 * @param {number} [image.expectedRevision]
 * @returns {Promise<{annotation: Object, revision: number}>}
 */
export async function ingestSessionImage(sessionId, image) {
  const response = await fetch(
    `${SESSIONS_BASE()}/${encodeURIComponent(sessionId)}/annotations/image`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        client_id: getClientId(),
        x: image.x,
        y: image.y,
        image_data: image.imageData || null,
        image_url: image.imageUrl || null,
        w: image.w ?? null,
        h: image.h ?? null,
        rotation: image.rotation ?? null,
        alt: image.alt || null,
        style: image.style || null,
        z: image.z ?? null,
        locked: !!image.locked,
        annotation_id: image.annotationId || null,
        expected_revision: image.expectedRevision ?? null,
      }),
    }
  );
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(errorData.detail || `Image ingest failed: ${response.status}`);
  }
  return response.json();
}

/**
 * Recent per-session annotation/canvas activity, newest first (backend PR #423:
 * persisted, actor-scoped, bounded to 500 records / 7 days per session).
 * @param {string} sessionId
 * @param {{actor?: string, limit?: number}} [options]
 * @returns {Promise<{session_id: string, activity: Array}>}
 */
export async function getSessionActivity(sessionId, { actor, limit } = {}) {
  const params = new URLSearchParams();
  if (actor) params.set('actor', actor);
  if (limit) params.set('limit', String(limit));
  const suffix = params.toString() ? `?${params.toString()}` : '';
  return apiFetch(`${SESSIONS_BASE()}/${encodeURIComponent(sessionId)}/activity${suffix}`);
}

/**
 * Undo the requesting actor's latest eligible session activity record.
 * Only the actor's own most recent not-yet-undone action is eligible; a
 * thrown error's `status` is 404 (nothing to undo / session gone), 409 (the
 * affected state changed since; another client holds a live selection claim on
 * the annotation — retry; or the session is mid-write — retry), or 429 (rate
 * limited) — see backend/core/session_manager.py's undo_last_action. A fourth
 * 409 (a mismatched `expected_revision`) exists at the endpoint but not here,
 * since this call never sends that field. The three it can reach are told
 * apart by message in sessionActivity.js's classifyUndoError, since only the
 * first is permanent.
 * @param {string} sessionId
 * @param {string} clientId
 * @returns {Promise<{undone_activity_id: string, undone_op: string, applied: Object, revision: number}>}
 */
export async function undoSessionAction(sessionId, clientId) {
  return apiFetch(`${SESSIONS_BASE()}/${encodeURIComponent(sessionId)}/undo`, {
    method: 'POST',
    body: JSON.stringify({ client_id: clientId }),
  });
}

/**
 * Summon every client connected to the session to a model-space region
 * ("Bring everyone here"). Ephemeral: nothing is stored, and only clients
 * connected right now move. The server names the summoner by this client's
 * presence entry. Throws with `status` 400 (invalid bounds), 403, 404 or 429.
 * @param {string} sessionId
 * @param {string} clientId
 * @param {{x: number, y: number, width: number, height: number}} bounds
 * @returns {Promise<{bounds: Object, connected_clients: number}>}
 */
export async function summonSessionViewport(sessionId, clientId, bounds) {
  return apiFetch(`${SESSIONS_BASE()}/${encodeURIComponent(sessionId)}/summon`, {
    method: 'POST',
    body: JSON.stringify({ client_id: clientId, bounds }),
  });
}
