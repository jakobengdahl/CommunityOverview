/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, beforeEach } from 'vitest';
import useGraphStore from './graphStore';

describe('graphStore', () => {
  beforeEach(() => {
    // Reset store to initial state before each test
    useGraphStore.setState({
      nodes: [],
      edges: [],
      highlightedNodeIds: [],
      hiddenNodeIds: [],
      clearGroupsFlag: false
    });
  });

  describe('addNodesToVisualization', () => {
    it('should add new nodes to an empty visualization', () => {
      const newNodes = [
        { id: 'node1', name: 'Node 1', type: 'Actor' },
        { id: 'node2', name: 'Node 2', type: 'Initiative' }
      ];

      const newEdges = [
        { id: 'edge1', source: 'node1', target: 'node2', type: 'RELATES_TO' }
      ];

      useGraphStore.getState().addNodesToVisualization(newNodes, newEdges);

      const state = useGraphStore.getState();

      expect(state.nodes).toHaveLength(2);
      expect(state.edges).toHaveLength(1);
      expect(state.highlightedNodeIds).toEqual(['node1', 'node2']);
      expect(state.clearGroupsFlag).toBe(true);
    });

    it('should merge new nodes with existing nodes without duplicates', () => {
      // Set up existing nodes
      useGraphStore.setState({
        nodes: [
          { id: 'existing1', name: 'Existing 1', type: 'Actor' },
          { id: 'existing2', name: 'Existing 2', type: 'Community' }
        ],
        edges: [
          { id: 'edge0', source: 'existing1', target: 'existing2', type: 'BELONGS_TO' }
        ]
      });

      const newNodes = [
        { id: 'new1', name: 'New 1', type: 'Initiative' },
        { id: 'new2', name: 'New 2', type: 'Resource' }
      ];

      const newEdges = [
        { id: 'edge1', source: 'new1', target: 'existing1', type: 'RELATES_TO' }
      ];

      useGraphStore.getState().addNodesToVisualization(newNodes, newEdges);

      const state = useGraphStore.getState();

      // Should have all 4 nodes (2 existing + 2 new)
      expect(state.nodes).toHaveLength(4);

      // Should have both edges
      expect(state.edges).toHaveLength(2);

      // Only new nodes should be highlighted
      expect(state.highlightedNodeIds).toEqual(['new1', 'new2']);

      // Verify all node IDs are present
      const nodeIds = state.nodes.map(n => n.id);
      expect(nodeIds).toContain('existing1');
      expect(nodeIds).toContain('existing2');
      expect(nodeIds).toContain('new1');
      expect(nodeIds).toContain('new2');
    });

    it('should not add duplicate nodes', () => {
      // Set up existing nodes
      useGraphStore.setState({
        nodes: [
          { id: 'node1', name: 'Node 1', type: 'Actor' }
        ],
        edges: []
      });

      const newNodes = [
        { id: 'node1', name: 'Node 1 Updated', type: 'Actor' }, // Same ID as existing
        { id: 'node2', name: 'Node 2', type: 'Initiative' }
      ];

      useGraphStore.getState().addNodesToVisualization(newNodes, []);

      const state = useGraphStore.getState();

      // Should only have 2 nodes (1 existing + 1 truly new)
      expect(state.nodes).toHaveLength(2);

      // The existing node should NOT be updated (we don't replace)
      const node1 = state.nodes.find(n => n.id === 'node1');
      expect(node1.name).toBe('Node 1'); // Original name preserved
    });

    it('should not add duplicate edges', () => {
      // Set up existing edges
      useGraphStore.setState({
        nodes: [
          { id: 'node1', name: 'Node 1', type: 'Actor' },
          { id: 'node2', name: 'Node 2', type: 'Initiative' }
        ],
        edges: [
          { id: 'edge1', source: 'node1', target: 'node2', type: 'RELATES_TO' }
        ]
      });

      const newEdges = [
        { id: 'edge1', source: 'node1', target: 'node2', type: 'RELATES_TO' }, // Same ID
        { id: 'edge2', source: 'node2', target: 'node1', type: 'BELONGS_TO' }
      ];

      useGraphStore.getState().addNodesToVisualization([], newEdges);

      const state = useGraphStore.getState();

      // Should only have 2 edges (1 existing + 1 truly new)
      expect(state.edges).toHaveLength(2);
    });

    it('should include edges connecting new and existing nodes', () => {
      // Set up existing nodes
      useGraphStore.setState({
        nodes: [
          { id: 'existing1', name: 'Existing 1', type: 'Actor' }
        ],
        edges: []
      });

      const newNodes = [
        { id: 'new1', name: 'New 1', type: 'Initiative' }
      ];

      const newEdges = [
        { id: 'edge1', source: 'new1', target: 'existing1', type: 'BELONGS_TO' }
      ];

      useGraphStore.getState().addNodesToVisualization(newNodes, newEdges);

      const state = useGraphStore.getState();

      // Should have the edge connecting new and existing
      expect(state.edges).toHaveLength(1);
      expect(state.edges[0].source).toBe('new1');
      expect(state.edges[0].target).toBe('existing1');
    });

    it('should set clearGroupsFlag to true', () => {
      const newNodes = [{ id: 'node1', name: 'Node 1', type: 'Actor' }];

      useGraphStore.getState().addNodesToVisualization(newNodes, []);

      const state = useGraphStore.getState();

      expect(state.clearGroupsFlag).toBe(true);
    });

    it('should reset clearGroupsFlag after timeout', (done) => {
      const newNodes = [{ id: 'node1', name: 'Node 1', type: 'Actor' }];

      useGraphStore.getState().addNodesToVisualization(newNodes, []);

      // Wait for timeout to complete
      setTimeout(() => {
        const state = useGraphStore.getState();
        expect(state.clearGroupsFlag).toBe(false);
        done();
      }, 150);
    });

    it('should handle empty arrays gracefully', () => {
      useGraphStore.setState({
        nodes: [{ id: 'existing1', name: 'Existing 1', type: 'Actor' }],
        edges: []
      });

      useGraphStore.getState().addNodesToVisualization([], []);

      const state = useGraphStore.getState();

      // Existing nodes should remain
      expect(state.nodes).toHaveLength(1);
      expect(state.highlightedNodeIds).toEqual([]);
    });
  });

  describe('updateVisualization', () => {
    it('should replace entire visualization', () => {
      // Set up existing data
      useGraphStore.setState({
        nodes: [{ id: 'old1', name: 'Old 1', type: 'Actor' }],
        edges: [{ id: 'oldEdge1', source: 'old1', target: 'old2', type: 'RELATES_TO' }]
      });

      const newNodes = [
        { id: 'new1', name: 'New 1', type: 'Actor' },
        { id: 'new2', name: 'New 2', type: 'Initiative' }
      ];

      const newEdges = [
        { id: 'edge1', source: 'new1', target: 'new2', type: 'BELONGS_TO' }
      ];

      useGraphStore.getState().updateVisualization(newNodes, newEdges, ['new1']);

      const state = useGraphStore.getState();

      // Should completely replace (not merge)
      expect(state.nodes).toHaveLength(2);
      expect(state.edges).toHaveLength(1);
      expect(state.nodes.find(n => n.id === 'old1')).toBeUndefined();
      expect(state.highlightedNodeIds).toEqual(['new1']);
    });

    it('should handle visualization load with positions', () => {
      const nodes = [
        { id: 'node1', name: 'Node 1', type: 'Actor', position: { x: 100, y: 100 } },
        { id: 'node2', name: 'Node 2', type: 'Initiative', position: { x: 200, y: 200 } }
      ];

      useGraphStore.getState().updateVisualization(nodes, [], []);

      const state = useGraphStore.getState();

      expect(state.nodes[0].position).toEqual({ x: 100, y: 100 });
      expect(state.nodes[1].position).toEqual({ x: 200, y: 200 });
    });
  });

  describe('updateNodePositions', () => {
    it('should update positions of existing nodes', () => {
      useGraphStore.setState({
        nodes: [
          { id: 'node1', name: 'Node 1', type: 'Actor', position: { x: 0, y: 0 } },
          { id: 'node2', name: 'Node 2', type: 'Initiative', position: { x: 0, y: 0 } }
        ]
      });

      const positionUpdates = [
        { id: 'node1', position: { x: 100, y: 150 } },
        { id: 'node2', position: { x: 300, y: 250 } }
      ];

      useGraphStore.getState().updateNodePositions(positionUpdates);

      const state = useGraphStore.getState();

      expect(state.nodes[0].position).toEqual({ x: 100, y: 150 });
      expect(state.nodes[1].position).toEqual({ x: 300, y: 250 });
    });

    it('should preserve nodes without position updates', () => {
      useGraphStore.setState({
        nodes: [
          { id: 'node1', name: 'Node 1', type: 'Actor', position: { x: 50, y: 50 } },
          { id: 'node2', name: 'Node 2', type: 'Initiative', position: { x: 100, y: 100 } }
        ]
      });

      const positionUpdates = [
        { id: 'node1', position: { x: 200, y: 200 } }
        // node2 not included in updates
      ];

      useGraphStore.getState().updateNodePositions(positionUpdates);

      const state = useGraphStore.getState();

      expect(state.nodes[0].position).toEqual({ x: 200, y: 200 });
      expect(state.nodes[1].position).toEqual({ x: 100, y: 100 }); // Unchanged
    });
  });

  describe('hidden nodes', () => {
    it('should set hidden node IDs', () => {
      useGraphStore.getState().setHiddenNodeIds(['node1', 'node2']);

      const state = useGraphStore.getState();

      expect(state.hiddenNodeIds).toEqual(['node1', 'node2']);
    });

    it('should toggle node visibility', () => {
      useGraphStore.setState({ hiddenNodeIds: ['node1'] });

      // Hide node2
      useGraphStore.getState().toggleNodeVisibility('node2');

      let state = useGraphStore.getState();
      expect(state.hiddenNodeIds).toContain('node2');
      expect(state.hiddenNodeIds).toContain('node1');

      // Show node1
      useGraphStore.getState().toggleNodeVisibility('node1');

      state = useGraphStore.getState();
      expect(state.hiddenNodeIds).not.toContain('node1');
      expect(state.hiddenNodeIds).toContain('node2');
    });
  });
});
