import { create } from 'zustand';

const FEDERATION_DEPTH_STORAGE_KEY = 'federation_depth';
const SHOW_MINIMAP_STORAGE_KEY = 'show_minimap';

function loadInitialShowMinimap() {
  try {
    const stored = window?.localStorage?.getItem(SHOW_MINIMAP_STORAGE_KEY);
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

  const description = language === 'sv'
    ? (policy.description_sv || policy.description_en)
    : (policy.description_en || policy.description_sv);

  if (!description) return '';

  const label = t ? t('welcome.language_policy_label') : (language === 'sv' ? 'Språkpolicy' : 'Language policy');
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

    const exampleLines = Array.isArray(examples)
      ? examples.map(e => `• "${e}"`).join('\n')
      : '';

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
  selectedNodeId: null,
  selectedGraphNodes: [], // Nodes selected in the graph canvas (full node data)
  editingNode: null,
  detailNode: null, // Node to show in detail dialog (double-click)
  contextMenu: null,
  clearGroupsFlag: false, // Signal to clear groups in visualization
  focusNodeId: null, // Node ID to zoom/pan to
  pendingGroups: null, // Groups to restore from a saved view
  chatPanelOpen: true, // Chat panel expanded vs minimized
  showMinimap: loadInitialShowMinimap(), // Minimap visibility (persisted)

  // Search state
  searchQuery: '',
  searchResults: null,
  federationDepth: loadInitialFederationDepth(),

  // Chat state
  chatMessages: [DEFAULT_WELCOME_MESSAGE],

  // Expert agents
  availableExperts: [],   // All expert agents from config
  activeExperts: [],      // Currently active expert agent IDs

  // Interactive guide state
  guide: {
    isActive: false,
    activeGuide: null,
    currentStepIndex: 0,
    userInputs: {},
    isExecutingAction: false,
  },

  // Guide-driven UI control (set by guide actions, consumed by components)
  guideChatInput: null,    // { text, animated, auto_send } — fills chat textarea
  guideSearchInput: null,  // { text, animated } — fills search bar

  // Stats
  stats: null,

  // LLM availability (null = not yet fetched, true/false = known)
  llmAvailable: null,

  // Loading states
  isLoading: false,
  configLoaded: false,
  error: null,

  // Actions
  setNodes: (nodes) => set({ nodes }),
  setEdges: (edges) => set({ edges }),

  updateVisualization: (nodes, edges, highlightIds = []) => {
    // Ensure uniqueness for nodes and edges
    const uniqueNodes = Array.from(new Map(nodes.map(n => [n.id, n])).values());
    const uniqueEdges = Array.from(new Map(edges.map(e => [e.id, e])).values());

    set({
      nodes: uniqueNodes,
      edges: uniqueEdges,
      highlightedNodeIds: highlightIds,
      clearGroupsFlag: true, // Signal to clear groups
    });
    // Reset flag after a short delay
    setTimeout(() => set({ clearGroupsFlag: false }), 100);
  },

  addNodesToVisualization: (newNodes, newEdges = []) => {
    const { nodes, edges } = get();

    // Create maps from existing items for uniqueness check
    // We use a Map to ensure that if a node comes in that already exists,
    // we assume the existing one is fine (or we could update it, but here we just dedup).
    const nodeMap = new Map(nodes.map(n => [n.id, n]));
    const edgeMap = new Map(edges.map(e => [e.id, e]));

    // Add new items to the map (this handles duplicates within newNodes too)
    newNodes.forEach(node => {
      if (!nodeMap.has(node.id)) {
        nodeMap.set(node.id, node);
      }
    });

    newEdges.forEach(edge => {
      if (!edgeMap.has(edge.id)) {
        edgeMap.set(edge.id, edge);
      }
    });

    // Calculate which IDs are actually new for highlighting
    const existingNodeIds = new Set(nodes.map(n => n.id));
    const actuallyNewNodeIds = newNodes
      .filter(n => !existingNodeIds.has(n.id))
      .map(n => n.id);

    set({
      nodes: Array.from(nodeMap.values()),
      edges: Array.from(edgeMap.values()),
      highlightedNodeIds: actuallyNewNodeIds,
    });
  },

  removeEdge: (edgeId) => {
    const { edges } = get();
    set({ edges: edges.filter(edge => edge.id !== edgeId) });
  },

  clearVisualization: () => set({
    nodes: [],
    edges: [],
    highlightedNodeIds: [],
    hiddenNodeIds: [],
    hiddenEdgeIds: [],
    nodeMarks: {},
    pendingGroups: null,
  }),

  setPendingGroups: (groups) => set({ pendingGroups: groups }),

  setHighlightedNodeIds: (ids) => set({ highlightedNodeIds: ids }),

  setNodeMarks: (marks) => {
    const marksMap = {};
    (marks || []).forEach(m => { marksMap[m.node_id] = { color: m.color, label: m.label || '' }; });
    set({ nodeMarks: marksMap });
  },

  clearNodeMarks: () => set({ nodeMarks: {} }),

  toggleNodeVisibility: (nodeId) => {
    const { hiddenNodeIds } = get();
    if (hiddenNodeIds.includes(nodeId)) {
      set({ hiddenNodeIds: hiddenNodeIds.filter(id => id !== nodeId) });
    } else {
      set({ hiddenNodeIds: [...hiddenNodeIds, nodeId] });
    }
  },

  setHiddenNodeIds: (ids) => set({ hiddenNodeIds: ids }),

  toggleEdgeVisibility: (edgeId) => {
    const { hiddenEdgeIds } = get();
    if (hiddenEdgeIds.includes(edgeId)) {
      set({ hiddenEdgeIds: hiddenEdgeIds.filter(id => id !== edgeId) });
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

  setStats: (stats) => set({ stats }),

  setLlmAvailable: (available) => set({ llmAvailable: available }),

  setLoading: (isLoading) => set({ isLoading }),

  setError: (error) => set({ error }),

  // Schema and presentation actions
  setSchema: (schema) => set({ schema }),

  setPresentation: (presentation, t, language) => {
    // Update welcome message with new presentation
    const welcomeMessage = createWelcomeMessage(presentation, t, language);
    const { chatMessages } = get();

    // Replace the welcome message if it's the first message
    const updatedMessages = chatMessages.length > 0 && chatMessages[0].id === 'welcome'
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
    set({
      schema,
      presentation,
      chatMessages: [welcomeMessage],
      configLoaded: true,
      availableExperts: presentation?.expert_agents || [],
    });
  },

  // Get node color from schema/presentation
  getNodeColor: (nodeType) => {
    const { presentation, schema } = get();

    // Check presentation colors first
    if (presentation?.colors?.[nodeType]) {
      return presentation.colors[nodeType];
    }

    // Fall back to schema-defined color
    if (schema?.node_types?.[nodeType]?.color) {
      return schema.node_types[nodeType].color;
    }

    // Default gray
    return '#9CA3AF';
  },

  // Get node type config
  getNodeTypeConfig: (nodeType) => {
    const { schema } = get();
    return schema?.node_types?.[nodeType] || null;
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

  // Clear highlights after a delay
  clearHighlights: () => {
    setTimeout(() => set({ highlightedNodeIds: [] }), 3000);
  },

  // Chat actions
  addChatMessage: (message) => {
    const { chatMessages } = get();
    set({ chatMessages: [...chatMessages, { ...message, id: message.id || Date.now() }] });
  },

  clearChatMessages: (t) => {
    const { presentation } = get();
    const welcomeMessage = createWelcomeMessage(presentation, t);
    set({ chatMessages: [welcomeMessage] });
  },

  // Context menu actions
  setContextMenu: (menu) => set({ contextMenu: menu }),
  closeContextMenu: () => set({ contextMenu: null }),

  // Node editing
  setEditingNode: (node) => set({ editingNode: node }),
  closeEditingNode: () => set({ editingNode: null }),

  // Node detail view (double-click)
  setDetailNode: (node) => set({ detailNode: node }),
  closeDetailNode: () => set({ detailNode: null }),

  // Graph canvas selection
  setSelectedGraphNodes: (nodes) => set({ selectedGraphNodes: nodes }),
  clearSelectedGraphNodes: () => set({ selectedGraphNodes: [] }),

  // Focus node actions
  setFocusNodeId: (nodeId) => set({ focusNodeId: nodeId }),
  clearFocusNode: () => set({ focusNodeId: null }),

  // Expert agent actions
  setAvailableExperts: (experts) => set({ availableExperts: experts }),

  toggleExpertAgent: (agentId, language) => {
    const { activeExperts, availableExperts, addChatMessage } = get();
    const agent = availableExperts.find(a => a.id === agentId);
    if (!agent) return;

    const isActive = activeExperts.includes(agentId);
    if (isActive) {
      // Remove expert
      set({ activeExperts: activeExperts.filter(id => id !== agentId) });
      const agentName = language === 'sv' ? agent.name : (agent.name_en || agent.name);
      const leaveMsg = language === 'sv'
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
      const agentName = language === 'sv' ? agent.name : (agent.name_en || agent.name);
      const introText = language === 'sv' ? agent.intro_sv : (agent.intro_en || agent.intro_sv);
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

  // Chat panel actions
  toggleChatPanel: () => set(state => ({ chatPanelOpen: !state.chatPanelOpen })),
  setChatPanelOpen: (open) => set({ chatPanelOpen: open }),

  // Guide actions
  startGuide: (guideDefinition) => set({
    guide: {
      isActive: true,
      activeGuide: guideDefinition,
      currentStepIndex: 0,
      userInputs: {},
      isExecutingAction: false,
    },
  }),

  stopGuide: () => set({
    guide: {
      isActive: false,
      activeGuide: null,
      currentStepIndex: 0,
      userInputs: {},
      isExecutingAction: false,
    },
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

  // Delete node from visualization
  removeNode: (nodeId) => {
    const { nodes, edges } = get();
    set({
      nodes: nodes.filter(n => n.id !== nodeId),
      edges: edges.filter(e => e.source !== nodeId && e.target !== nodeId),
    });
  },
}));

export default useGraphStore;
