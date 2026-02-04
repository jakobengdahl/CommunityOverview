import { create } from 'zustand';

// Default welcome message (used before presentation is loaded)
const DEFAULT_WELCOME_MESSAGE = {
  role: 'assistant',
  content: `Välkommen till Community Knowledge Graph!

Du kan ställa frågor som:
• "Vilka initiativ relaterar till NIS2?"
• "Visa alla aktörer i eSam-communityt"
• "Finns det några AI-strategiprojekt?"
• "Sök efter myndigheter som jobbar med digital identitet"

Du kan också ladda upp dokument (PDF, Word, text) för att extrahera entiteter.

**OBS:** Hantera inte personuppgifter i denna tjänst.`,
  timestamp: new Date(),
  id: 'welcome',
};

/**
 * Create a welcome message using the presentation config
 */
function createWelcomeMessage(presentation) {
  const intro = presentation?.introduction || DEFAULT_WELCOME_MESSAGE.content;
  return {
    role: 'assistant',
    content: `${intro}

Du kan ställa frågor som:
• "Vilka initiativ relaterar till NIS2?"
• "Visa alla aktörer"
• "Finns det några AI-strategiprojekt?"
• "Sök efter myndigheter som jobbar med digital identitet"

Du kan också ladda upp dokument (PDF, Word, text) för att extrahera entiteter.

**OBS:** Hantera inte personuppgifter i denna tjänst.`,
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
  selectedNodeId: null,
  editingNode: null,
  contextMenu: null,
  clearGroupsFlag: false, // Signal to clear groups in visualization

  // Search state
  searchQuery: '',
  searchResults: null,

  // Chat state (always visible, no toggle)
  chatMessages: [DEFAULT_WELCOME_MESSAGE],

  // Stats
  stats: null,

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

  clearVisualization: () => set({
    nodes: [],
    edges: [],
    highlightedNodeIds: [],
    hiddenNodeIds: [],
  }),

  setHighlightedNodeIds: (ids) => set({ highlightedNodeIds: ids }),

  toggleNodeVisibility: (nodeId) => {
    const { hiddenNodeIds } = get();
    if (hiddenNodeIds.includes(nodeId)) {
      set({ hiddenNodeIds: hiddenNodeIds.filter(id => id !== nodeId) });
    } else {
      set({ hiddenNodeIds: [...hiddenNodeIds, nodeId] });
    }
  },

  setHiddenNodeIds: (ids) => set({ hiddenNodeIds: ids }),

  setSelectedNodeId: (nodeId) => set({ selectedNodeId: nodeId }),

  setSearchQuery: (query) => set({ searchQuery: query }),

  setSearchResults: (results) => set({ searchResults: results }),

  setStats: (stats) => set({ stats }),

  setLoading: (isLoading) => set({ isLoading }),

  setError: (error) => set({ error }),

  // Schema and presentation actions
  setSchema: (schema) => set({ schema }),

  setPresentation: (presentation) => {
    // Update welcome message with new presentation
    const welcomeMessage = createWelcomeMessage(presentation);
    const { chatMessages } = get();

    // Replace the welcome message if it's the first message
    const updatedMessages = chatMessages.length > 0 && chatMessages[0].id === 'welcome'
      ? [welcomeMessage, ...chatMessages.slice(1)]
      : chatMessages;

    set({
      presentation,
      chatMessages: updatedMessages,
      configLoaded: true,
    });
  },

  setConfig: (schema, presentation) => {
    const welcomeMessage = createWelcomeMessage(presentation);
    set({
      schema,
      presentation,
      chatMessages: [welcomeMessage],
      configLoaded: true,
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

  clearChatMessages: () => {
    const { presentation } = get();
    const welcomeMessage = createWelcomeMessage(presentation);
    set({ chatMessages: [welcomeMessage] });
  },

  // Context menu actions
  setContextMenu: (menu) => set({ contextMenu: menu }),
  closeContextMenu: () => set({ contextMenu: null }),

  // Node editing
  setEditingNode: (node) => set({ editingNode: node }),
  closeEditingNode: () => set({ editingNode: null }),

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
