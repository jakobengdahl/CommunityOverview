import { create } from 'zustand';

// Welcome message shown when chat starts
const WELCOME_MESSAGE = {
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
 * Zustand store for graph state management
 */
const useGraphStore = create((set, get) => ({
  // Graph data
  nodes: [],
  edges: [],

  // UI state
  highlightedNodeIds: [],
  hiddenNodeIds: [],
  selectedNodeId: null,
  editingNode: null,
  contextMenu: null,

  // Search state
  searchQuery: '',
  searchResults: null,

  // Chat state (always visible, no toggle)
  chatMessages: [WELCOME_MESSAGE],

  // Stats
  stats: null,

  // Loading states
  isLoading: false,
  error: null,

  // Actions
  setNodes: (nodes) => set({ nodes }),
  setEdges: (edges) => set({ edges }),

  updateVisualization: (nodes, edges, highlightIds = []) => set({
    nodes,
    edges,
    highlightedNodeIds: highlightIds,
  }),

  addNodesToVisualization: (newNodes, newEdges = []) => {
    const { nodes, edges } = get();
    const existingNodeIds = new Set(nodes.map(n => n.id));
    const existingEdgeIds = new Set(edges.map(e => e.id));

    const filteredNodes = newNodes.filter(n => !existingNodeIds.has(n.id));
    const filteredEdges = newEdges.filter(e => !existingEdgeIds.has(e.id));

    set({
      nodes: [...nodes, ...filteredNodes],
      edges: [...edges, ...filteredEdges],
      highlightedNodeIds: filteredNodes.map(n => n.id),
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

  // Clear highlights after a delay
  clearHighlights: () => {
    setTimeout(() => set({ highlightedNodeIds: [] }), 3000);
  },

  // Chat actions
  addChatMessage: (message) => {
    const { chatMessages } = get();
    set({ chatMessages: [...chatMessages, { ...message, id: message.id || Date.now() }] });
  },

  clearChatMessages: () => set({ chatMessages: [WELCOME_MESSAGE] }),

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
