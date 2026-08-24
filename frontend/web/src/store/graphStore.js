import { create } from 'zustand';

const FEDERATION_DEPTH_STORAGE_KEY = 'federation_depth';
const SHOW_MINIMAP_STORAGE_KEY = 'show_minimap';
const NODE_PREVIEW_STORAGE_KEY = 'node_preview_enabled';
const CANVAS_LOCKED_STORAGE_KEY = 'canvas_locked';
const CHAT_PANEL_OPEN_STORAGE_KEY = 'chat_panel_open';
export const AI_ASSISTANT_COLLAPSED_STORAGE_KEY = 'community-graph:ui:ai-assistant-collapsed';

// Returns the visitor's own explicit open/collapsed choice, or null when none
// has been made yet — distinct from loadInitialChatPanelOpen's boolean return
// so setConfig can tell "no override" apart from "explicitly chose open".
function loadStoredChatPanelOpen() {
  try {
    const stored = window?.localStorage?.getItem(CHAT_PANEL_OPEN_STORAGE_KEY);
    if (stored !== null) return stored === 'true';
    const collapsed = window?.localStorage?.getItem(AI_ASSISTANT_COLLAPSED_STORAGE_KEY);
    if (collapsed === 'true') return false;
    if (collapsed === 'false') return true;
  } catch {
    // ignore storage errors and use default
  }
  return null;
}

function persistChatPanelOpen(open) {
  try {
    window?.localStorage?.setItem(CHAT_PANEL_OPEN_STORAGE_KEY, String(open));
    window?.localStorage?.setItem(AI_ASSISTANT_COLLAPSED_STORAGE_KEY, String(!open));
  } catch {
    // ignore storage errors
  }
}

function configuredChatPanelOpen(presentation) {
  const stored = loadStoredChatPanelOpen();
  if (stored !== null) return stored;
  return !(
    presentation?.default_chat_collapsed || presentation?.ui?.ai_assistant?.default_collapsed
  );
}

function loadInitialChatPanelOpen() {
  const stored = loadStoredChatPanelOpen();
  // Falls back to open (the historical hardcoded default) until the backend
  // config default arrives — setConfig reconciles this once it does.
  return stored !== null ? stored : true;
}

function loadInitialShowMinimap() {
  try {
    const stored = window?.localStorage?.getItem(SHOW_MINIMAP_STORAGE_KEY);
    if (stored !== null) return stored === 'true';
  } catch {
    // ignore storage errors and use default
  }
  return false;
}

function loadInitialNodePreview() {
  try {
    const stored = window?.localStorage?.getItem(NODE_PREVIEW_STORAGE_KEY);
    if (stored !== null) return stored === 'true';
  } catch {
    // ignore storage errors and use default
  }
  return true;
}

function loadInitialCanvasLocked() {
  try {
    const stored = window?.localStorage?.getItem(CANVAS_LOCKED_STORAGE_KEY);
    if (stored !== null) return stored === 'true';
  } catch {
    // ignore storage errors and use default
  }
  return false;
}

function loadInitialFederationDepth() {
  try {
    const stored = window?.localStorage?.getItem(FEDERATION_DEPTH_STORAGE_KEY);
    const parsed = Number(stored);
    if (Number.isInteger(parsed) && parsed >= 1) {
      return parsed;
    }
  } catch {
    // ignore storage errors and use default
  }
  return 1;
}

function hasOwn(obj, key) {
  return Object.prototype.hasOwnProperty.call(obj, key);
}

// How many entries the session-scoped node trail keeps. Bounded so a long
// working session cannot grow it without limit; older entries are dropped.
const NAV_HISTORY_LIMIT = 50;

// Append node-trail entries (newest-first) onto an existing trail, returning a
// new bounded array. Pure so the trimming/dedup logic is unit-testable without
// the store. `at` is the shared ISO timestamp for this batch. The trail is a
// most-recently-used list, not a visit log: a node already anywhere in it is
// moved to the top with a refreshed timestamp rather than added a second time.
// Every row is just a jump target, so a second row for the same node offers the
// user no action the first one does not, while still consuming one of the
// bounded slots that an older, distinct node would otherwise hold. On such a
// move an existing 'added' designation is kept even if the incoming event is a
// 'visit', so a node the user added this session still reads as "Added".
export function appendNavEntries(prev, entries, at, limit = NAV_HISTORY_LIMIT) {
  let next = Array.isArray(prev) ? prev : [];
  for (const e of entries || []) {
    if (!e || !e.id) continue;
    const action = e.action === 'added' ? 'added' : 'visited';
    const existing = next.find((r) => r.id === e.id);
    const row = {
      id: e.id,
      name: e.name || e.id,
      type: e.type || '',
      action: existing?.action === 'added' ? 'added' : action,
      at,
    };
    next = [row, ...next.filter((r) => r.id !== e.id)];
  }
  return next.slice(0, limit);
}

// Best-effort domain name/type for a node that may be a raw graph node (name,
// type) or a React Flow node whose domain fields live under data (the wrapper's
// own `type` is the React Flow node kind, e.g. 'custom', so data wins for type).
function navNameType(node, fallbackId) {
  return {
    name: node?.name || node?.data?.name || node?.data?.label || fallbackId,
    type: node?.data?.type || node?.data?.nodeType || node?.type || '',
  };
}

// clearVisualization raises clearGroupsFlag and then lowers it after a short
// delay. A single shared timer means a second call within that window cancels
// the earlier timer instead of letting it lower the flag prematurely on the
// later call, which would make ReactFlow miss the signal. updateVisualization
// deliberately does NOT touch this flag (see its own comment) — only a genuine
// wholesale replace should drop the canvas's groups and annotations.
let clearGroupsResetTimer = null;
function scheduleClearGroupsReset(set) {
  if (clearGroupsResetTimer) clearTimeout(clearGroupsResetTimer);
  clearGroupsResetTimer = setTimeout(() => {
    clearGroupsResetTimer = null;
    set({ clearGroupsFlag: false });
  }, 100);
}

// One auto-clear timer per pulsing node. A repeated pulse on the same node
// replaces its timer so the visual always lasts the full latest duration, and
// the timer only clears the entry if it still carries the seq it was armed for
// (a newer pulse that arrived first must not be cut short by an older timer).
const pulseClearTimers = new Map();
let pulseSeqCounter = 0;
const PULSE_MIN_MS = 200;
const PULSE_MAX_MS = 15000;
const PULSE_DEFAULT_MS = 1500;

// Default welcome message (used before presentation is loaded)
const DEFAULT_WELCOME_MESSAGE = {
  role: 'assistant',
  content: `Welcome to Community Knowledge Graph!

You can ask questions like:
• "What initiatives relate to NIS2?"
• "Show all actors"
• "Are there any AI strategy projects?"
• "What goals exist around digitalization?"
• "Show all AI agents"

You can also upload documents (PDF, Word, text) to extract entities.

**NOTE:** Do not handle personal data in this service.`,
  timestamp: new Date(),
  id: 'welcome',
};

function getLanguagePolicyText(presentation, language = 'en', t) {
  const policy = presentation?.language_policy;
  if (!policy) return '';

  const description =
    language === 'sv'
      ? policy.description_sv || policy.description_en
      : policy.description_en || policy.description_sv;

  if (!description) return '';

  const label = t
    ? t('welcome.language_policy_label')
    : language === 'sv'
      ? 'Språkpolicy'
      : 'Language policy';
  return `**${label}:** ${description}`;
}

/**
 * Create a welcome message using the presentation config and i18n translations.
 * @param {Object} presentation - Presentation config from backend
 * @param {Function} t - Translation function from i18n (optional)
 * @param {string} language - Active UI language (optional)
 */
function createWelcomeMessage(presentation, t, language = 'en') {
  const intro = presentation?.introduction || '';
  const title = presentation?.title || '';
  const languagePolicyText = getLanguagePolicyText(presentation, language, t);

  // If the introduction contains multiple paragraphs, treat it as a complete
  // welcome message and skip appending generic i18n examples/hints.
  if (intro && intro.includes('\n')) {
    return {
      role: 'assistant',
      content: title
        ? `**${title}**\n\n${intro}${languagePolicyText ? `\n\n${languagePolicyText}` : ''}`
        : `${intro}${languagePolicyText ? `\n\n${languagePolicyText}` : ''}`,
      timestamp: new Date(),
      id: 'welcome',
    };
  }

  if (t) {
    const i18nTitle = t('welcome.title');
    const prompt = t('welcome.prompt');
    const examples = t('welcome.examples');
    const uploadHint = t('welcome.upload_hint');
    const privacyNotice = t('welcome.privacy_notice');

    const exampleLines = Array.isArray(examples) ? examples.map((e) => `• "${e}"`).join('\n') : '';

    return {
      role: 'assistant',
      content: `${i18nTitle}\n\n${intro ? intro + '\n\n' : ''}${languagePolicyText ? languagePolicyText + '\n\n' : ''}${prompt}\n${exampleLines}\n\n${uploadHint}\n\n${privacyNotice}`,
      timestamp: new Date(),
      id: 'welcome',
    };
  }

  // Fallback without i18n
  return {
    role: 'assistant',
    content: intro
      ? `${DEFAULT_WELCOME_MESSAGE.content.split('\n')[0]}\n\n${intro}${languagePolicyText ? `\n\n${languagePolicyText}` : ''}\n\n${DEFAULT_WELCOME_MESSAGE.content.split('\n').slice(2).join('\n')}`
      : `${DEFAULT_WELCOME_MESSAGE.content}${languagePolicyText ? `\n\n${languagePolicyText}` : ''}`,
    timestamp: new Date(),
    id: 'welcome',
  };
}

/**
 * Zustand store for graph state management
 */
const useGraphStore = create((set, get) => ({
  // Graph data
  nodes: [],
  edges: [],

  // Schema and presentation config (loaded from backend)
  schema: null,
  presentation: null,

  // UI state
  highlightedNodeIds: [],
  hiddenNodeIds: [],
  hiddenEdgeIds: [],
  nodeMarks: {},
  // Transient per-node pulse state driven by external trigger URLs (design:
  // external pulse-trigger). Maps nodeId -> { style, color, seq }; each entry is
  // auto-cleared after its duration. seq changes on every (re)trigger so the
  // client can restart the animation even when a node is pulsed again mid-play.
  pulsedNodeIds: {},
  selectedNodeId: null,
  selectedGraphNodes: [], // Nodes selected in the graph canvas (full node data)
  editingNode: null,
  detailNode: null, // Node to show in detail dialog (double-click)
  editingEdge: null, // Edge to show in the edge-edit dialog
  deleteDialog: null, // Pending node-delete confirmation ({ nodeId | nodeIds, ... })
  contextMenu: null,
  // Signal to GraphCanvas to drop its manual groups/annotations. Raised only by
  // clearVisualization (a genuine wholesale replace or session clear) — see the
  // comment above updateVisualization for why that action must not raise it.
  clearGroupsFlag: false,
  // Monotonic counter bumped every time the canvas contents are replaced
  // wholesale (clearVisualization): a saved view loaded into the running
  // session, an agent's replace/load, a search that swaps the view, the
  // clear-canvas action. Each of those establishes a new position baseline, so
  // the canvas discards its position undo/redo history — the recorded "before"
  // positions describe a layout that no longer exists, while the node ids they
  // name may well still be on the canvas. Deliberately *not* bumped by
  // updateVisualization: that action is replace-shaped, but its callers are
  // either in-place edits of the current contents (edge retype, node edit, node
  // removal) that undo must survive, or genuine replacements that call
  // clearVisualization on the line before — so they have already bumped here.
  canvasBaselineEpoch: 0,
  focusNodeId: null, // Node ID to zoom/pan to
  // Session-scoped, newest-first trail of nodes added to the visualization or
  // navigated to, so the user can jump back through what happened. Distinct from
  // the backend graph-mutation log (ActivityDrawer's Graph tab) and from the canvas
  // position undo/redo (useCanvasHistory). Cleared on session switch / clear.
  navHistory: [],
  pendingGroups: null, // Groups to restore from a saved view
  pendingAnnotations: null, // Note/label/arrow annotations to restore from a session
  chatPanelOpen: loadInitialChatPanelOpen(), // Chat panel expanded vs minimized (persisted)
  showMinimap: loadInitialShowMinimap(), // Minimap visibility (persisted)
  nodePreviewEnabled: loadInitialNodePreview(), // Hover info popup on/off (persisted)
  canvasLocked: loadInitialCanvasLocked(), // Navigation-menu lock guarding the board (persisted)

  // Search state
  searchQuery: '',
  searchResults: null,
  federationDepth: loadInitialFederationDepth(),

  // Chat state
  chatMessages: [DEFAULT_WELCOME_MESSAGE],

  // Monotonic counter bumped on every visualization-session switch. Any handler
  // that awaits a network call captures it first and drops its post-await
  // effects if it changed (see isStaleSessionEpoch), so work started in one
  // session can never mutate the chat, canvas or sync fan-out of the session
  // the user has since moved to.
  sessionEpoch: 0,

  // Expert agents
  availableExperts: [], // All expert agents from config
  activeExperts: [], // Currently active expert agent IDs

  // Interactive guide state
  guide: {
    isActive: false,
    activeGuide: null,
    currentStepIndex: 0,
    userInputs: {},
    isExecutingAction: false,
  },

  // Guide-driven UI control (set by guide actions, consumed by components)
  guideChatInput: null, // { text, animated, auto_send } — fills chat textarea
  guideSearchInput: null, // { text, animated } — fills search bar

  // Incremented whenever the search box or chat input is focused, so GraphCanvas
  // can close any open context menu (they live outside the canvas's own click-away area)
  closeMenusSignal: 0,

  // Stats
  stats: null,

  // LLM availability (null = not yet fetched, true/false = known)
  llmAvailable: null,

  // Model profiles (see backend/config/model_profiles.py). Empty profiles list
  // means no profiles are configured — the chat UI has nothing to select from
  // and uses the legacy single-provider path implicitly.
  modelProfiles: [],
  defaultModelProfileId: null,
  modelProfileSelectionEnabled: true,
  // The profile the chat UI will send with the next request. Preselected to
  // the default profile once capabilities are fetched.
  selectedModelProfileId: null,

  // Loading states
  isLoading: false,
  configLoaded: false,
  error: null,

  // Actions
  setNodes: (nodes) => set({ nodes }),
  setEdges: (edges) => set({ edges }),

  updateVisualization: (nodes, edges, highlightIds = []) => {
    // Ensure uniqueness for nodes and edges
    const uniqueNodes = Array.from(new Map(nodes.map((n) => [n.id, n])).values());
    const uniqueEdges = Array.from(new Map(edges.map((e) => [e.id, e])).values());

    // A wholesale replace can drop nodes the trail still references; prune those
    // so every trail row stays a valid jump target. New arrivals are not
    // recorded as 'added' here — a full replace is a baseline, not the
    // incremental user additions the trail captures (those come through
    // addNodesToVisualization).
    // Deliberately does not touch clearGroupsFlag: every caller either performs
    // an in-place edit of the current contents (edge retype, node edit, node
    // removal, chat's update_in_visualization) that must leave groups and
    // annotations on the canvas untouched, or is a genuine replace that calls
    // clearVisualization on the line before — which already raises the flag.
    // Raising it unconditionally here used to wipe every group/annotation on an
    // ordinary node edit, and the loss then persisted through autosave.
    const presentIds = new Set(uniqueNodes.map((n) => n.id));
    set((state) => ({
      nodes: uniqueNodes,
      edges: uniqueEdges,
      highlightedNodeIds: highlightIds,
      navHistory: state.navHistory.filter((e) => presentIds.has(e.id)),
    }));
  },

  addNodesToVisualization: (newNodes, newEdges = []) => {
    const { nodes, edges } = get();

    // Create maps from existing items for uniqueness check
    // We use a Map to ensure that if a node comes in that already exists,
    // we assume the existing one is fine (or we could update it, but here we just dedup).
    const nodeMap = new Map(nodes.map((n) => [n.id, n]));
    const edgeMap = new Map(edges.map((e) => [e.id, e]));

    // Add new items to the map (this handles duplicates within newNodes too)
    newNodes.forEach((node) => {
      if (!nodeMap.has(node.id)) {
        nodeMap.set(node.id, node);
      }
    });

    newEdges.forEach((edge) => {
      if (!edgeMap.has(edge.id)) {
        edgeMap.set(edge.id, edge);
      }
    });

    // Calculate which IDs are actually new for highlighting
    const existingNodeIds = new Set(nodes.map((n) => n.id));
    const newlyAdded = newNodes.filter((n) => !existingNodeIds.has(n.id));
    const actuallyNewNodeIds = newlyAdded.map((n) => n.id);

    const addedRows = newlyAdded.map((n) => ({
      id: n.id,
      ...navNameType(n, n.id),
      action: 'added',
    }));

    set({
      nodes: Array.from(nodeMap.values()),
      edges: Array.from(edgeMap.values()),
      highlightedNodeIds: actuallyNewNodeIds,
      navHistory: appendNavEntries(get().navHistory, addedRows, new Date().toISOString()),
    });
  },

  removeEdge: (edgeId) => {
    const { edges } = get();
    set({ edges: edges.filter((edge) => edge.id !== edgeId) });
  },

  // Update fields of a single edge in place without touching nodes or groups.
  updateEdgeData: (edgeId, updates) => {
    const { edges } = get();
    set({ edges: edges.map((edge) => (edge.id === edgeId ? { ...edge, ...updates } : edge)) });
  },

  // Emptying the canvas also closes the overlays that address canvas content.
  // detailNode/editingNode/editingEdge/deleteDialog each point at a node or edge
  // that is gone once this returns, and confirming one of them would still
  // mutate the global graph for something the user can no longer see. Only the
  // esc-esc path is gated on a dialog being open, so an agent driving
  // clear_visualization can pull the canvas out from under an open dialog;
  // closing them here covers every caller at once. contextMenu is reset for
  // parity with resetSessionScopedState only — nothing outside this store reads
  // it today, and the canvas nodes own their own menus as local state.
  clearVisualization: () => {
    pulseClearTimers.forEach((timer) => clearTimeout(timer));
    pulseClearTimers.clear();
    set((state) => ({
      nodes: [],
      edges: [],
      detailNode: null,
      editingNode: null,
      editingEdge: null,
      deleteDialog: null,
      contextMenu: null,
      highlightedNodeIds: [],
      hiddenNodeIds: [],
      hiddenEdgeIds: [],
      nodeMarks: {},
      pulsedNodeIds: {},
      pendingGroups: null,
      pendingAnnotations: null,
      clearGroupsFlag: true,
      selectedGraphNodes: [],
      selectedNodeId: null,
      navHistory: [],
      canvasBaselineEpoch: state.canvasBaselineEpoch + 1,
    }));
    scheduleClearGroupsReset(set);
  },

  setPendingGroups: (groups) => set({ pendingGroups: groups }),

  setPendingAnnotations: (annotations) => set({ pendingAnnotations: annotations }),

  setHighlightedNodeIds: (ids) => set({ highlightedNodeIds: ids }),

  setNodeMarks: (marks) => {
    const marksMap = {};
    (marks || []).forEach((m) => {
      marksMap[m.node_id] = { color: m.color, label: m.label || '' };
    });
    set({ nodeMarks: marksMap });
  },

  clearNodeMarks: () => set({ nodeMarks: {} }),

  // Play a transient visual pulse on a node in response to an external trigger.
  pulseNode: (nodeId, { style = 'glow', color = null, durationMs } = {}) => {
    if (!nodeId) return;
    const seq = ++pulseSeqCounter;
    const duration = Math.min(
      PULSE_MAX_MS,
      Math.max(PULSE_MIN_MS, Number(durationMs) || PULSE_DEFAULT_MS)
    );
    set((state) => ({
      pulsedNodeIds: { ...state.pulsedNodeIds, [nodeId]: { style, color, seq } },
    }));

    const existing = pulseClearTimers.get(nodeId);
    if (existing) clearTimeout(existing);
    const timer = setTimeout(() => {
      pulseClearTimers.delete(nodeId);
      set((state) => {
        // Only clear if this node still shows the pulse we armed the timer for.
        if (state.pulsedNodeIds[nodeId]?.seq !== seq) return {};
        const next = { ...state.pulsedNodeIds };
        delete next[nodeId];
        return { pulsedNodeIds: next };
      });
    }, duration);
    pulseClearTimers.set(nodeId, timer);
  },

  toggleNodeVisibility: (nodeId) => {
    const { hiddenNodeIds } = get();
    if (hiddenNodeIds.includes(nodeId)) {
      set({ hiddenNodeIds: hiddenNodeIds.filter((id) => id !== nodeId) });
    } else {
      set({ hiddenNodeIds: [...hiddenNodeIds, nodeId] });
    }
  },

  setHiddenNodeIds: (ids) => set({ hiddenNodeIds: ids }),

  toggleEdgeVisibility: (edgeId) => {
    const { hiddenEdgeIds } = get();
    if (hiddenEdgeIds.includes(edgeId)) {
      set({ hiddenEdgeIds: hiddenEdgeIds.filter((id) => id !== edgeId) });
    } else {
      set({ hiddenEdgeIds: [...hiddenEdgeIds, edgeId] });
    }
  },

  setHiddenEdgeIds: (ids) => set({ hiddenEdgeIds: ids }),

  setSelectedNodeId: (nodeId) => set({ selectedNodeId: nodeId }),

  setSearchQuery: (query) => set({ searchQuery: query }),

  setSearchResults: (results) => set({ searchResults: results }),
  setFederationDepth: (depth) => {
    const normalized = Number(depth);
    if (!Number.isInteger(normalized) || normalized < 1) return;
    try {
      window?.localStorage?.setItem(FEDERATION_DEPTH_STORAGE_KEY, String(normalized));
    } catch {
      // ignore storage errors
    }
    set({ federationDepth: normalized });
  },

  setShowMinimap: (show) => {
    try {
      window?.localStorage?.setItem(SHOW_MINIMAP_STORAGE_KEY, String(show));
    } catch {
      // ignore storage errors
    }
    set({ showMinimap: show });
  },

  setNodePreviewEnabled: (enabled) => {
    try {
      window?.localStorage?.setItem(NODE_PREVIEW_STORAGE_KEY, String(enabled));
    } catch {
      // ignore storage errors
    }
    set({ nodePreviewEnabled: enabled });
  },

  setCanvasLocked: (locked) => {
    try {
      window?.localStorage?.setItem(CANVAS_LOCKED_STORAGE_KEY, String(locked));
    } catch {
      // ignore storage errors
    }
    set({ canvasLocked: locked });
  },

  setStats: (stats) => set({ stats }),

  setLlmAvailable: (available) => set({ llmAvailable: available }),

  // Apply the /ui/capabilities "model_profiles" payload: stores the enabled
  // profile list and preselects the default profile for the chat UI.
  setModelProfilesCapability: (modelProfilesCapability) => {
    const profiles = modelProfilesCapability?.profiles || [];
    const defaultProfileId = modelProfilesCapability?.default_profile_id ?? null;
    const selectionEnabled = modelProfilesCapability?.selection_enabled ?? true;
    set({
      modelProfiles: profiles,
      defaultModelProfileId: defaultProfileId,
      modelProfileSelectionEnabled: selectionEnabled,
      selectedModelProfileId: defaultProfileId,
    });
  },

  setSelectedModelProfileId: (profileId) => set({ selectedModelProfileId: profileId }),

  setLoading: (isLoading) => set({ isLoading }),

  setError: (error) => set({ error }),

  // Schema and presentation actions
  setSchema: (schema) => set({ schema }),

  setPresentation: (presentation, t, language) => {
    // Update welcome message with new presentation
    const welcomeMessage = createWelcomeMessage(presentation, t, language);
    const { chatMessages } = get();

    // Replace the welcome message if it's the first message
    const updatedMessages =
      chatMessages.length > 0 && chatMessages[0].id === 'welcome'
        ? [welcomeMessage, ...chatMessages.slice(1)]
        : chatMessages;

    set({
      presentation,
      chatMessages: updatedMessages,
      configLoaded: true,
      availableExperts: presentation?.expert_agents || [],
    });
  },

  setConfig: (schema, presentation, t, language) => {
    const welcomeMessage = createWelcomeMessage(presentation, t, language);
    const patch = {
      schema,
      presentation,
      chatMessages: [welcomeMessage],
      configLoaded: true,
      availableExperts: presentation?.expert_agents || [],
    };
    // Apply the backend's configured startup state only when the visitor has
    // not made their own explicit open/collapse choice yet — an explicit
    // choice always takes precedence, matching the default_language pattern.
    if (loadStoredChatPanelOpen() === null) {
      patch.chatPanelOpen = configuredChatPanelOpen(presentation);
    }
    set(patch);
  },

  // Get node color from schema/presentation
  // Both lookup tables are parsed from profile JSON, so they carry Object.prototype:
  // without an own-property guard a node type named toString or constructor would
  // resolve an inherited member and be returned as a color. hasOwnProperty.call
  // rather than Object.hasOwn: the Vite build target includes safari14, which
  // predates ES2022, and nothing in the build polyfills built-in methods.
  getNodeColor: (nodeType) => {
    const { presentation, schema } = get();

    // Check presentation colors first
    const colors = presentation?.colors;
    if (colors && hasOwn(colors, nodeType) && colors[nodeType]) {
      return colors[nodeType];
    }

    // Fall back to schema-defined color
    const nodeTypes = schema?.node_types;
    if (nodeTypes && hasOwn(nodeTypes, nodeType) && nodeTypes[nodeType]?.color) {
      return nodeTypes[nodeType].color;
    }

    // Default gray
    return '#9CA3AF';
  },

  // Get node type config
  getNodeTypeConfig: (nodeType) => {
    const { schema } = get();
    const nodeTypes = schema?.node_types;
    if (!nodeTypes || !hasOwn(nodeTypes, nodeType)) return null;
    return nodeTypes[nodeType] || null;
  },

  // Get all node types
  getNodeTypes: () => {
    const { schema } = get();
    if (!schema?.node_types) return [];
    return Object.entries(schema.node_types).map(([name, config]) => ({
      type: name,
      ...config,
    }));
  },

  // Get all relationship types
  getRelationshipTypes: () => {
    const { schema } = get();
    if (!schema?.relationship_types) return [];
    return Object.entries(schema.relationship_types).map(([name, config]) => ({
      type: name,
      ...config,
    }));
  },

  isRelationshipTypeApplicable: (relationshipType, sourceType, targetType) => {
    const { schema } = get();
    const config = schema?.relationship_types?.[relationshipType];
    // Unconfigured relationship types are allowed for backward compatibility.
    if (!config) return true;
    const sourceTypes = Array.isArray(config.source_types) ? config.source_types : [];
    const targetTypes = Array.isArray(config.target_types) ? config.target_types : [];
    const sourceAllowed =
      sourceTypes.length === 0 || sourceTypes.includes('*') || sourceTypes.includes(sourceType);
    const targetAllowed =
      targetTypes.length === 0 || targetTypes.includes('*') || targetTypes.includes(targetType);
    return sourceAllowed && targetAllowed;
  },

  getRelationshipTypesForNodes: (sourceType, targetType) => {
    const { getRelationshipTypes, isRelationshipTypeApplicable } = get();
    return getRelationshipTypes().filter((rt) =>
      isRelationshipTypeApplicable(rt.type, sourceType, targetType)
    );
  },

  // Clear highlights after a delay
  clearHighlights: () => {
    setTimeout(() => set({ highlightedNodeIds: [] }), 3000);
  },

  // Chat actions
  addChatMessage: (message) => {
    const { chatMessages } = get();
    set({ chatMessages: [...chatMessages, { ...message, id: message.id || Date.now() }] });
  },

  updateChatMessage: (id, patch) => {
    const { chatMessages } = get();
    set({
      chatMessages: chatMessages.map((m) => (m.id === id ? { ...m, ...patch } : m)),
    });
  },

  clearChatMessages: (t) => {
    const { presentation } = get();
    const welcomeMessage = createWelcomeMessage(presentation, t);
    set({ chatMessages: [welcomeMessage] });
  },

  // Reset all session-scoped UI state when switching visualization sessions so
  // nothing leaks across sessions: the assistant conversation, the active expert
  // roster, and the graph-scoped overlays (detail dialog, node and edge edit
  // dialogs, delete confirmation, context menu, selection) all belong to the
  // session that was active when they opened. Confirming a dialog left open
  // across a switch acts on the old session's graph while the sync client
  // already points at the new one, so the edit would be broadcast into a
  // session it does not belong to.
  // Bumping sessionEpoch invalidates every request still in flight from the
  // previous session — the assistant's, and App's edge-update and node-delete
  // handlers — so none of their results can land in the new session.
  resetSessionScopedState: (t, language) => {
    const { presentation } = get();
    const welcomeMessage = createWelcomeMessage(presentation, t, language);
    set((state) => ({
      chatMessages: [welcomeMessage],
      activeExperts: [],
      detailNode: null,
      editingNode: null,
      editingEdge: null,
      deleteDialog: null,
      contextMenu: null,
      selectedNodeId: null,
      selectedGraphNodes: [],
      navHistory: [],
      sessionEpoch: state.sessionEpoch + 1,
    }));
  },

  // Context menu actions
  setContextMenu: (menu) => set({ contextMenu: menu }),
  closeContextMenu: () => set({ contextMenu: null }),

  // Node editing
  setEditingNode: (node) => set({ editingNode: node }),
  closeEditingNode: () => set({ editingNode: null }),

  // Edge editing and the node-delete confirmation. Held here rather than in App
  // so they reset through the same session-switch choke point as the node
  // dialogs above; both address graph content that the new session may not have.
  setEditingEdge: (edge) => set({ editingEdge: edge }),
  setDeleteDialog: (dialog) => set({ deleteDialog: dialog }),

  // Node detail view (double-click). Opening a node's detail counts as visiting
  // it, so it is recorded on the navigable trail.
  setDetailNode: (node) =>
    set((state) => {
      if (!node?.id) return { detailNode: node };
      const stored = state.nodes.find((n) => n.id === node.id);
      const entry = { id: node.id, ...navNameType(stored || node, node.id), action: 'visited' };
      return {
        detailNode: node,
        navHistory: appendNavEntries(state.navHistory, [entry], new Date().toISOString()),
      };
    }),
  closeDetailNode: () => set({ detailNode: null }),

  // Graph canvas selection
  setSelectedGraphNodes: (nodes) => set({ selectedGraphNodes: nodes }),
  clearSelectedGraphNodes: () => set({ selectedGraphNodes: [] }),

  // Focus node actions. setFocusNodeId is the single "navigate/center to a node"
  // choke point (search results, guided steps, and the node-trail panel all use
  // it), so navigating to a node records a visit on the trail. clearFocusNode
  // only lowers the transient signal and is not a navigation, so it records
  // nothing.
  setFocusNodeId: (nodeId) =>
    set((state) => {
      if (!nodeId) return { focusNodeId: nodeId };
      const stored = state.nodes.find((n) => n.id === nodeId);
      const entry = { id: nodeId, ...navNameType(stored, nodeId), action: 'visited' };
      return {
        focusNodeId: nodeId,
        navHistory: appendNavEntries(state.navHistory, [entry], new Date().toISOString()),
      };
    }),
  clearFocusNode: () => set({ focusNodeId: null }),
  clearNavHistory: () => set({ navHistory: [] }),

  // Expert agent actions
  setAvailableExperts: (experts) => set({ availableExperts: experts }),

  toggleExpertAgent: (agentId, language) => {
    const { activeExperts, availableExperts, addChatMessage } = get();
    const agent = availableExperts.find((a) => a.id === agentId);
    if (!agent) return;

    const isActive = activeExperts.includes(agentId);
    if (isActive) {
      // Remove expert
      set({ activeExperts: activeExperts.filter((id) => id !== agentId) });
      const agentName = language === 'sv' ? agent.name : agent.name_en || agent.name;
      const leaveMsg =
        language === 'sv'
          ? `${agentName} har lämnat diskussionen.`
          : `${agentName} has left the discussion.`;
      addChatMessage({
        role: 'expert',
        expertId: agentId,
        expertName: agentName,
        expertColor: agent.color,
        content: leaveMsg,
        timestamp: new Date(),
        isSystemEvent: true,
      });
    } else {
      // Add expert
      set({ activeExperts: [...activeExperts, agentId] });
      const agentName = language === 'sv' ? agent.name : agent.name_en || agent.name;
      const introText = language === 'sv' ? agent.intro_sv : agent.intro_en || agent.intro_sv;
      // Intro message visible to user
      addChatMessage({
        role: 'expert',
        expertId: agentId,
        expertName: agentName,
        expertColor: agent.color,
        content: introText,
        timestamp: new Date(),
      });
      // Notification to main assistant (hidden from rendering but in history)
      addChatMessage({
        role: 'system',
        content: `[Expert agent "${agentName}" (${agent.id}) has joined the discussion. Specialty: ${agent.specialty_en || agent.specialty}. Coordinate with this expert when relevant questions arise.]`,
        timestamp: new Date(),
        expertJoinNotification: true,
      });
    }
  },

  // Chat panel actions.
  // toggleChatPanel is wired to the panel's own header/minimized-bar click —
  // a genuine explicit choice — so it persists and then overrides the
  // configured default on every later visit (see setConfig above and
  // resetChatPanelToDefault below). Transient setters deliberately do NOT
  // persist: GuideOverlay and MobileShell use them to stage/minimize the panel
  // without treating that surface choreography as the visitor expressing a
  // real preference or silently clobbering one they already made.
  toggleChatPanel: () =>
    set((state) => {
      const next = !state.chatPanelOpen;
      persistChatPanelOpen(next);
      return { chatPanelOpen: next };
    }),
  setChatPanelOpen: (open) => {
    persistChatPanelOpen(open);
    set({ chatPanelOpen: open });
  },
  setChatPanelOpenTransient: (open) => set({ chatPanelOpen: open }),
  toggleChatPanelTransient: () => set((state) => ({ chatPanelOpen: !state.chatPanelOpen })),
  // Clears the visitor's stored choice and falls back to the configured
  // application default again.
  resetChatPanelToDefault: () =>
    set((state) => {
      try {
        window?.localStorage?.removeItem(CHAT_PANEL_OPEN_STORAGE_KEY);
        window?.localStorage?.removeItem(AI_ASSISTANT_COLLAPSED_STORAGE_KEY);
      } catch {
        // ignore storage errors
      }
      return { chatPanelOpen: configuredChatPanelOpen(state.presentation) };
    }),

  // Guide actions
  startGuide: (guideDefinition) =>
    set({
      guide: {
        isActive: true,
        activeGuide: guideDefinition,
        currentStepIndex: 0,
        userInputs: {},
        isExecutingAction: false,
      },
    }),

  stopGuide: () =>
    set({
      guide: {
        isActive: false,
        activeGuide: null,
        currentStepIndex: 0,
        userInputs: {},
        isExecutingAction: false,
      },
      guideChatInput: null,
      guideSearchInput: null,
    }),

  advanceGuide: () => {
    set((state) => {
      const { guide } = state;
      if (!guide.isActive || !guide.activeGuide) return state;
      const nextIndex = guide.currentStepIndex + 1;
      const totalSteps = guide.activeGuide.steps?.length || 0;
      if (nextIndex >= totalSteps) {
        return {
          guide: {
            isActive: false,
            activeGuide: null,
            currentStepIndex: 0,
            userInputs: {},
            isExecutingAction: false,
          },
          guideChatInput: null,
          guideSearchInput: null,
        };
      }
      return { guide: { ...guide, currentStepIndex: nextIndex, isExecutingAction: false } };
    });
  },

  setGuideStepInput: (key, value) => {
    set((state) => ({
      guide: { ...state.guide, userInputs: { ...state.guide.userInputs, [key]: value } },
    }));
  },

  setGuideExecutingAction: (isExecuting) => {
    set((state) => ({ guide: { ...state.guide, isExecutingAction: isExecuting } }));
  },

  // Guide-driven UI fill actions
  setGuideChatInput: (payload) => set({ guideChatInput: payload }),
  clearGuideChatInput: () => set({ guideChatInput: null }),
  setGuideSearchInput: (payload) => set({ guideSearchInput: payload }),
  clearGuideSearchInput: () => set({ guideSearchInput: null }),

  requestCloseMenus: () => set((state) => ({ closeMenusSignal: state.closeMenusSignal + 1 })),

  // Delete node from visualization
  removeNode: (nodeId) => {
    const { nodes, edges, navHistory } = get();
    set({
      nodes: nodes.filter((n) => n.id !== nodeId),
      edges: edges.filter((e) => e.source !== nodeId && e.target !== nodeId),
      // Drop the removed node from the trail so its row can't become a dead
      // jump target.
      navHistory: navHistory.filter((e) => e.id !== nodeId),
    });
  },
}));

/**
 * Whether the visualization session has been switched since `epoch` was taken.
 *
 * Capture the epoch before awaiting, then call this before applying anything the
 * await produced. Reads the store imperatively rather than through the hook so
 * the value is the one current at resolution time, not the one closed over when
 * the handler was created.
 *
 * @param {number} epoch  Value of sessionEpoch captured before the await.
 * @returns {boolean}
 */
export const isStaleSessionEpoch = (epoch) => useGraphStore.getState().sessionEpoch !== epoch;

export default useGraphStore;
