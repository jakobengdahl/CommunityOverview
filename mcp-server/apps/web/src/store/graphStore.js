import { create } from 'zustand';

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

  // Search state
  searchQuery: '',
  searchResults: null,

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
}));

export default useGraphStore;
