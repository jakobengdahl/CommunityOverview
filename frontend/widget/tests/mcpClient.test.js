/**
 * Tests for MCP Client module
 *
 * Verifies that:
 * - Each function calls the correct MCP tool name
 * - Arguments match the MCP specification
 * - Error handling works correctly
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  isMCPAvailable,
  callTool,
  searchGraph,
  getNodeDetails,
  getRelatedNodes,
  findSimilarNodes,
  addNodes,
  updateNode,
  deleteNodes,
  getGraphStats,
} from '../src/mcpClient';

describe('mcpClient', () => {
  let mockCallTool;

  beforeEach(() => {
    // Set up mock window.openai.callTool
    mockCallTool = vi.fn().mockResolvedValue({ success: true });
    global.window = {
      openai: {
        callTool: mockCallTool,
      },
    };
  });

  afterEach(() => {
    vi.clearAllMocks();
    delete global.window;
  });

  describe('isMCPAvailable', () => {
    it('returns true when window.openai.callTool exists', () => {
      expect(isMCPAvailable()).toBe(true);
    });

    it('returns false when window.openai is missing', () => {
      global.window = {};
      expect(isMCPAvailable()).toBe(false);
    });

    it('returns false when callTool is not a function', () => {
      global.window.openai = { callTool: 'not a function' };
      expect(isMCPAvailable()).toBe(false);
    });

    it('returns false when window is undefined', () => {
      delete global.window;
      expect(isMCPAvailable()).toBe(false);
    });
  });

  describe('callTool', () => {
    it('calls window.openai.callTool with correct arguments', async () => {
      await callTool('test_tool', { arg1: 'value1' });

      expect(mockCallTool).toHaveBeenCalledWith('test_tool', { arg1: 'value1' });
    });

    it('returns the result from callTool', async () => {
      mockCallTool.mockResolvedValue({ data: 'test result' });

      const result = await callTool('test_tool', {});

      expect(result).toEqual({ data: 'test result' });
    });

    it('throws error when MCP is not available', async () => {
      global.window = {};

      await expect(callTool('test_tool', {})).rejects.toThrow('MCP tools not available');
    });

    it('propagates errors from callTool', async () => {
      mockCallTool.mockRejectedValue(new Error('Tool error'));

      await expect(callTool('test_tool', {})).rejects.toThrow('Tool error');
    });
  });

  describe('searchGraph', () => {
    it('calls search_graph with correct tool name', async () => {
      await searchGraph('test query');

      expect(mockCallTool).toHaveBeenCalledWith('search_graph', expect.any(Object));
    });

    it('passes query and default options', async () => {
      await searchGraph('test query');

      expect(mockCallTool).toHaveBeenCalledWith('search_graph', {
        query: 'test query',
        node_types: undefined,
        communities: undefined,
        limit: 50,
      });
    });

    it('passes custom options', async () => {
      await searchGraph('test', {
        nodeTypes: ['Actor', 'Initiative'],
        communities: ['eSam'],
        limit: 100,
      });

      expect(mockCallTool).toHaveBeenCalledWith('search_graph', {
        query: 'test',
        node_types: ['Actor', 'Initiative'],
        communities: ['eSam'],
        limit: 100,
      });
    });
  });

  describe('getNodeDetails', () => {
    it('calls get_node_details with correct tool name', async () => {
      await getNodeDetails('node-123');

      expect(mockCallTool).toHaveBeenCalledWith('get_node_details', expect.any(Object));
    });

    it('passes node_id correctly', async () => {
      await getNodeDetails('test-node-id');

      expect(mockCallTool).toHaveBeenCalledWith('get_node_details', {
        node_id: 'test-node-id',
      });
    });
  });

  describe('getRelatedNodes', () => {
    it('calls get_related_nodes with correct tool name', async () => {
      await getRelatedNodes('node-123');

      expect(mockCallTool).toHaveBeenCalledWith('get_related_nodes', expect.any(Object));
    });

    it('passes node_id and default depth', async () => {
      await getRelatedNodes('test-node');

      expect(mockCallTool).toHaveBeenCalledWith('get_related_nodes', {
        node_id: 'test-node',
        relationship_types: undefined,
        depth: 1,
      });
    });

    it('passes custom options', async () => {
      await getRelatedNodes('test-node', {
        relationshipTypes: ['BELONGS_TO', 'PART_OF'],
        depth: 3,
      });

      expect(mockCallTool).toHaveBeenCalledWith('get_related_nodes', {
        node_id: 'test-node',
        relationship_types: ['BELONGS_TO', 'PART_OF'],
        depth: 3,
      });
    });
  });

  describe('findSimilarNodes', () => {
    it('calls find_similar_nodes with correct tool name', async () => {
      await findSimilarNodes('Test Name');

      expect(mockCallTool).toHaveBeenCalledWith('find_similar_nodes', expect.any(Object));
    });

    it('passes name and default options', async () => {
      await findSimilarNodes('Organization ABC');

      expect(mockCallTool).toHaveBeenCalledWith('find_similar_nodes', {
        name: 'Organization ABC',
        node_type: undefined,
        threshold: 0.7,
        limit: 5,
      });
    });

    it('passes custom options', async () => {
      await findSimilarNodes('Test', {
        nodeType: 'Actor',
        threshold: 0.9,
        limit: 10,
      });

      expect(mockCallTool).toHaveBeenCalledWith('find_similar_nodes', {
        name: 'Test',
        node_type: 'Actor',
        threshold: 0.9,
        limit: 10,
      });
    });
  });

  describe('addNodes', () => {
    it('calls add_nodes with correct tool name', async () => {
      await addNodes([{ name: 'Test' }], []);

      expect(mockCallTool).toHaveBeenCalledWith('add_nodes', expect.any(Object));
    });

    it('passes nodes and edges correctly', async () => {
      const nodes = [
        { id: 'n1', type: 'Actor', name: 'Node 1' },
        { id: 'n2', type: 'Initiative', name: 'Node 2' },
      ];
      const edges = [
        { source: 'n1', target: 'n2', type: 'BELONGS_TO' },
      ];

      await addNodes(nodes, edges);

      expect(mockCallTool).toHaveBeenCalledWith('add_nodes', {
        nodes,
        edges,
      });
    });

    it('defaults edges to empty array', async () => {
      await addNodes([{ name: 'Test' }]);

      expect(mockCallTool).toHaveBeenCalledWith('add_nodes', {
        nodes: [{ name: 'Test' }],
        edges: [],
      });
    });
  });

  describe('updateNode', () => {
    it('calls update_node with correct tool name', async () => {
      await updateNode('node-123', { name: 'New Name' });

      expect(mockCallTool).toHaveBeenCalledWith('update_node', expect.any(Object));
    });

    it('passes node_id and updates correctly', async () => {
      const updates = {
        name: 'Updated Name',
        description: 'Updated description',
      };

      await updateNode('test-node', updates);

      expect(mockCallTool).toHaveBeenCalledWith('update_node', {
        node_id: 'test-node',
        updates,
      });
    });
  });

  describe('deleteNodes', () => {
    it('calls delete_nodes with correct tool name', async () => {
      await deleteNodes(['node-1', 'node-2']);

      expect(mockCallTool).toHaveBeenCalledWith('delete_nodes', expect.any(Object));
    });

    it('passes node_ids and confirmed flag', async () => {
      await deleteNodes(['n1', 'n2'], true);

      expect(mockCallTool).toHaveBeenCalledWith('delete_nodes', {
        node_ids: ['n1', 'n2'],
        confirmed: true,
      });
    });

    it('defaults confirmed to false', async () => {
      await deleteNodes(['n1']);

      expect(mockCallTool).toHaveBeenCalledWith('delete_nodes', {
        node_ids: ['n1'],
        confirmed: false,
      });
    });
  });

  describe('getGraphStats', () => {
    it('calls get_graph_stats with correct tool name', async () => {
      await getGraphStats();

      expect(mockCallTool).toHaveBeenCalledWith('get_graph_stats', expect.any(Object));
    });

    it('passes communities filter', async () => {
      await getGraphStats(['eSam', 'Myndigheter']);

      expect(mockCallTool).toHaveBeenCalledWith('get_graph_stats', {
        communities: ['eSam', 'Myndigheter'],
      });
    });

    it('defaults communities to null', async () => {
      await getGraphStats();

      expect(mockCallTool).toHaveBeenCalledWith('get_graph_stats', {
        communities: null,
      });
    });
  });
});

describe('MCP Tool Name Compliance', () => {
  let mockCallTool;

  beforeEach(() => {
    mockCallTool = vi.fn().mockResolvedValue({ success: true });
    global.window = {
      openai: {
        callTool: mockCallTool,
      },
    };
  });

  afterEach(() => {
    vi.clearAllMocks();
    delete global.window;
  });

  // These tests verify that our client uses the exact tool names
  // defined in the MCP specification (graph_services/mcp_tools.py)

  const toolTests = [
    { fn: () => searchGraph('test'), expectedTool: 'search_graph' },
    { fn: () => getNodeDetails('id'), expectedTool: 'get_node_details' },
    { fn: () => getRelatedNodes('id'), expectedTool: 'get_related_nodes' },
    { fn: () => findSimilarNodes('name'), expectedTool: 'find_similar_nodes' },
    { fn: () => addNodes([], []), expectedTool: 'add_nodes' },
    { fn: () => updateNode('id', {}), expectedTool: 'update_node' },
    { fn: () => deleteNodes([]), expectedTool: 'delete_nodes' },
    { fn: () => getGraphStats(), expectedTool: 'get_graph_stats' },
  ];

  toolTests.forEach(({ fn, expectedTool }) => {
    it(`${fn.toString().replace(/\(\)\s*=>\s*/, '')} calls "${expectedTool}"`, async () => {
      await fn();
      expect(mockCallTool).toHaveBeenCalledWith(expectedTool, expect.any(Object));
    });
  });
});
