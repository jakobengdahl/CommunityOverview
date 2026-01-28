/**
 * Tests for Widget component
 *
 * Verifies that:
 * - GraphCanvas callbacks call the correct MCP tools
 * - Expand, edit, search operations work correctly
 * - Error handling works
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import Widget from '../src/Widget';
import * as mcp from '../src/mcpClient';

// Mock GraphCanvas component
vi.mock('@community-graph/ui-graph-canvas', () => ({
  GraphCanvas: ({ nodes, edges, onExpand, onEdit, highlightedNodeIds }) => (
    <div data-testid="graph-canvas">
      <div data-testid="node-count">{nodes?.length || 0} nodes</div>
      <div data-testid="edge-count">{edges?.length || 0} edges</div>
      {nodes?.map((node) => (
        <div key={node.id} data-testid={`node-${node.id}`}>
          <span>{node.name}</span>
          <button
            data-testid={`expand-${node.id}`}
            onClick={() => onExpand?.(node.id, node)}
          >
            Expand
          </button>
          <button
            data-testid={`edit-${node.id}`}
            onClick={() => onEdit?.(node.id, node)}
          >
            Edit
          </button>
        </div>
      ))}
    </div>
  ),
}));

// Mock the CSS import
vi.mock('@community-graph/ui-graph-canvas/styles', () => ({}));

// Mock mcp module
vi.mock('../src/mcpClient', () => ({
  isMCPAvailable: vi.fn(),
  searchGraph: vi.fn(),
  getRelatedNodes: vi.fn(),
  getNodeDetails: vi.fn(),
  addNodes: vi.fn(),
  updateNode: vi.fn(),
  deleteNodes: vi.fn(),
}));

describe('Widget', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Default: MCP is available
    mcp.isMCPAvailable.mockReturnValue(true);
  });

  describe('MCP availability', () => {
    it('shows error message when MCP is not available', () => {
      mcp.isMCPAvailable.mockReturnValue(false);
      render(<Widget />);

      expect(screen.getByText('MCP Not Available')).toBeInTheDocument();
      expect(screen.getByText(/window.openai.callTool/)).toBeInTheDocument();
    });

    it('renders graph canvas when MCP is available', () => {
      render(<Widget />);

      expect(screen.queryByText('MCP Not Available')).not.toBeInTheDocument();
      expect(screen.getByTestId('graph-canvas')).toBeInTheDocument();
    });
  });

  describe('Search functionality', () => {
    it('calls searchGraph MCP tool on search', async () => {
      mcp.searchGraph.mockResolvedValue({ nodes: [], edges: [] });

      render(<Widget />);

      const input = screen.getByPlaceholderText('Search graph...');
      const button = screen.getByRole('button', { name: 'Search' });

      fireEvent.change(input, { target: { value: 'test query' } });
      fireEvent.click(button);

      await waitFor(() => {
        expect(mcp.searchGraph).toHaveBeenCalledWith('test query');
      });
    });

    it('displays search results', async () => {
      const mockNodes = [
        { id: 'n1', name: 'Node 1', type: 'Actor' },
        { id: 'n2', name: 'Node 2', type: 'Initiative' },
      ];
      const mockEdges = [{ id: 'e1', source: 'n1', target: 'n2', type: 'IMPLEMENTS' }];

      mcp.searchGraph.mockResolvedValue({ nodes: mockNodes, edges: mockEdges });

      render(<Widget />);

      const input = screen.getByPlaceholderText('Search graph...');
      fireEvent.change(input, { target: { value: 'test' } });
      fireEvent.click(screen.getByRole('button', { name: 'Search' }));

      await waitFor(() => {
        expect(screen.getByTestId('node-count')).toHaveTextContent('2 nodes');
        expect(screen.getByTestId('edge-count')).toHaveTextContent('1 edges');
      });
    });

    it('shows error message on search failure', async () => {
      mcp.searchGraph.mockRejectedValue(new Error('Search failed'));

      render(<Widget />);

      const input = screen.getByPlaceholderText('Search graph...');
      fireEvent.change(input, { target: { value: 'test' } });
      fireEvent.click(screen.getByRole('button', { name: 'Search' }));

      await waitFor(() => {
        expect(screen.getByText('Search failed')).toBeInTheDocument();
      });
    });

    it('does not search with empty query', async () => {
      render(<Widget />);

      // Button should be disabled with empty query
      const button = screen.getByRole('button', { name: 'Search' });
      expect(button).toBeDisabled();

      // Even if we somehow click it
      fireEvent.click(button);

      expect(mcp.searchGraph).not.toHaveBeenCalled();
    });
  });

  describe('Expand callback - get_related_nodes', () => {
    it('calls get_related_nodes when expand button is clicked', async () => {
      const initialNodes = [{ id: 'n1', name: 'Node 1', type: 'Actor' }];
      mcp.searchGraph.mockResolvedValue({ nodes: initialNodes, edges: [] });
      mcp.getRelatedNodes.mockResolvedValue({ nodes: [], edges: [] });

      render(<Widget />);

      // Search to populate graph
      const input = screen.getByPlaceholderText('Search graph...');
      fireEvent.change(input, { target: { value: 'test' } });
      fireEvent.click(screen.getByRole('button', { name: 'Search' }));

      await waitFor(() => {
        expect(screen.getByTestId('node-n1')).toBeInTheDocument();
      });

      // Click expand button
      fireEvent.click(screen.getByTestId('expand-n1'));

      await waitFor(() => {
        expect(mcp.getRelatedNodes).toHaveBeenCalledWith('n1');
      });
    });

    it('adds new nodes from expand to the graph', async () => {
      const initialNodes = [{ id: 'n1', name: 'Node 1', type: 'Actor' }];
      const relatedNodes = [
        { id: 'n2', name: 'Related Node', type: 'Initiative' },
      ];

      mcp.searchGraph.mockResolvedValue({ nodes: initialNodes, edges: [] });
      mcp.getRelatedNodes.mockResolvedValue({
        nodes: [...initialNodes, ...relatedNodes],
        edges: [{ id: 'e1', source: 'n1', target: 'n2', type: 'IMPLEMENTS' }]
      });

      render(<Widget />);

      // Search to populate
      const input = screen.getByPlaceholderText('Search graph...');
      fireEvent.change(input, { target: { value: 'test' } });
      fireEvent.click(screen.getByRole('button', { name: 'Search' }));

      await waitFor(() => {
        expect(screen.getByTestId('node-count')).toHaveTextContent('1 nodes');
      });

      // Expand
      fireEvent.click(screen.getByTestId('expand-n1'));

      await waitFor(() => {
        expect(screen.getByTestId('node-count')).toHaveTextContent('2 nodes');
        expect(screen.getByTestId('edge-count')).toHaveTextContent('1 edges');
      });
    });

    it('does not duplicate nodes on expand', async () => {
      const nodes = [{ id: 'n1', name: 'Node 1', type: 'Actor' }];

      mcp.searchGraph.mockResolvedValue({ nodes, edges: [] });
      // Return same node as already exists
      mcp.getRelatedNodes.mockResolvedValue({ nodes, edges: [] });

      render(<Widget />);

      const input = screen.getByPlaceholderText('Search graph...');
      fireEvent.change(input, { target: { value: 'test' } });
      fireEvent.click(screen.getByRole('button', { name: 'Search' }));

      await waitFor(() => {
        expect(screen.getByTestId('node-count')).toHaveTextContent('1 nodes');
      });

      fireEvent.click(screen.getByTestId('expand-n1'));

      // Should still be 1 node (no duplicates)
      await waitFor(() => {
        expect(screen.getByTestId('node-count')).toHaveTextContent('1 nodes');
      });
    });
  });

  describe('Edit callback', () => {
    it('calls onNodeSelect when edit button is clicked', async () => {
      const onNodeSelect = vi.fn();
      const nodes = [{ id: 'n1', name: 'Node 1', type: 'Actor' }];

      mcp.searchGraph.mockResolvedValue({ nodes, edges: [] });

      render(<Widget onNodeSelect={onNodeSelect} />);

      const input = screen.getByPlaceholderText('Search graph...');
      fireEvent.change(input, { target: { value: 'test' } });
      fireEvent.click(screen.getByRole('button', { name: 'Search' }));

      await waitFor(() => {
        expect(screen.getByTestId('node-n1')).toBeInTheDocument();
      });

      fireEvent.click(screen.getByTestId('edit-n1'));

      expect(onNodeSelect).toHaveBeenCalledWith('n1', nodes[0]);
    });

    it('does not crash when onNodeSelect is not provided', async () => {
      const nodes = [{ id: 'n1', name: 'Node 1', type: 'Actor' }];
      mcp.searchGraph.mockResolvedValue({ nodes, edges: [] });

      render(<Widget />);

      const input = screen.getByPlaceholderText('Search graph...');
      fireEvent.change(input, { target: { value: 'test' } });
      fireEvent.click(screen.getByRole('button', { name: 'Search' }));

      await waitFor(() => {
        expect(screen.getByTestId('node-n1')).toBeInTheDocument();
      });

      // Should not throw
      expect(() => {
        fireEvent.click(screen.getByTestId('edit-n1'));
      }).not.toThrow();
    });
  });

  describe('Initial query', () => {
    it('runs initial query when provided', async () => {
      mcp.searchGraph.mockResolvedValue({ nodes: [], edges: [] });

      render(<Widget initialQuery="initial search" />);

      await waitFor(() => {
        expect(mcp.searchGraph).toHaveBeenCalledWith('initial search');
      });
    });

    it('does not run initial query when MCP is unavailable', async () => {
      mcp.isMCPAvailable.mockReturnValue(false);

      render(<Widget initialQuery="initial search" />);

      // Wait a bit to ensure no call happens
      await new Promise(resolve => setTimeout(resolve, 100));

      expect(mcp.searchGraph).not.toHaveBeenCalled();
    });
  });

  describe('Loading state', () => {
    it('shows loading indicator during search', async () => {
      let resolveSearch;
      mcp.searchGraph.mockImplementation(() => new Promise(resolve => {
        resolveSearch = resolve;
      }));

      render(<Widget />);

      const input = screen.getByPlaceholderText('Search graph...');
      fireEvent.change(input, { target: { value: 'test' } });
      fireEvent.click(screen.getByRole('button', { name: 'Search' }));

      // Loading should be shown
      expect(screen.getByText('Loading...')).toBeInTheDocument();

      // Resolve the search
      await act(async () => {
        resolveSearch({ nodes: [], edges: [] });
      });

      // Loading should be gone
      await waitFor(() => {
        expect(screen.queryByText('Loading...')).not.toBeInTheDocument();
      });
    });

    it('disables search input and button during loading', async () => {
      let resolveSearch;
      mcp.searchGraph.mockImplementation(() => new Promise(resolve => {
        resolveSearch = resolve;
      }));

      render(<Widget />);

      const input = screen.getByPlaceholderText('Search graph...');
      fireEvent.change(input, { target: { value: 'test' } });
      fireEvent.click(screen.getByRole('button', { name: 'Search' }));

      expect(input).toBeDisabled();
      expect(screen.getByRole('button', { name: 'Search' })).toBeDisabled();

      await act(async () => {
        resolveSearch({ nodes: [], edges: [] });
      });

      await waitFor(() => {
        expect(input).not.toBeDisabled();
      });
    });
  });

  describe('Error dismissal', () => {
    it('allows dismissing error messages', async () => {
      mcp.searchGraph.mockRejectedValue(new Error('Test error'));

      render(<Widget />);

      const input = screen.getByPlaceholderText('Search graph...');
      fireEvent.change(input, { target: { value: 'test' } });
      fireEvent.click(screen.getByRole('button', { name: 'Search' }));

      await waitFor(() => {
        expect(screen.getByText('Test error')).toBeInTheDocument();
      });

      // Dismiss the error
      fireEvent.click(screen.getByRole('button', { name: '×' }));

      expect(screen.queryByText('Test error')).not.toBeInTheDocument();
    });
  });
});

describe('MCP Tool Compliance - Widget Callbacks', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mcp.isMCPAvailable.mockReturnValue(true);
  });

  it('expand uses get_related_nodes tool', async () => {
    const nodes = [{ id: 'test-node', name: 'Test', type: 'Actor' }];
    mcp.searchGraph.mockResolvedValue({ nodes, edges: [] });
    mcp.getRelatedNodes.mockResolvedValue({ nodes: [], edges: [] });

    render(<Widget />);

    // Populate graph
    fireEvent.change(screen.getByPlaceholderText('Search graph...'), { target: { value: 'x' } });
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));

    await waitFor(() => screen.getByTestId('expand-test-node'));

    // Expand
    fireEvent.click(screen.getByTestId('expand-test-node'));

    await waitFor(() => {
      // Verify the getRelatedNodes function was called (which internally calls get_related_nodes tool)
      expect(mcp.getRelatedNodes).toHaveBeenCalledTimes(1);
      expect(mcp.getRelatedNodes).toHaveBeenCalledWith('test-node');
    });
  });

  it('search uses search_graph tool', async () => {
    mcp.searchGraph.mockResolvedValue({ nodes: [], edges: [] });

    render(<Widget />);

    fireEvent.change(screen.getByPlaceholderText('Search graph...'), { target: { value: 'my search' } });
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));

    await waitFor(() => {
      expect(mcp.searchGraph).toHaveBeenCalledTimes(1);
      expect(mcp.searchGraph).toHaveBeenCalledWith('my search');
    });
  });
});
