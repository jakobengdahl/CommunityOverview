import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

// Sample test data
const sampleNodes = [
  {
    id: 'node-1',
    name: 'Test Organization',
    type: 'Actor',
    description: 'A test organization',
    summary: 'Test org summary',
    communities: ['TestCommunity'],
  },
  {
    id: 'node-2',
    name: 'Test Initiative',
    type: 'Initiative',
    description: 'A test initiative',
    summary: 'Test initiative summary',
    communities: ['TestCommunity'],
  },
];

const sampleEdges = [
  {
    id: 'edge-1',
    source: 'node-1',
    target: 'node-2',
    type: 'IMPLEMENTS',
  },
];

describe('GraphCanvas', () => {
  it('renders without crashing', () => {
    render(<GraphCanvas nodes={[]} edges={[]} />);
    expect(screen.getByText('No graph to display')).toBeInTheDocument();
  });

  it('displays empty state message when no nodes provided', () => {
    render(<GraphCanvas nodes={[]} edges={[]} />);
    expect(screen.getByText('No graph to display')).toBeInTheDocument();
    expect(screen.getByText(/Search or add nodes/)).toBeInTheDocument();
  });

  it('renders nodes when provided', async () => {
    render(<GraphCanvas nodes={sampleNodes} edges={sampleEdges} />);

    // Wait for React Flow to initialize
    await waitFor(() => {
      expect(screen.getByText('Test Organization')).toBeInTheDocument();
    });

    expect(screen.getByText('Test Initiative')).toBeInTheDocument();
  });

  it('shows node type labels', async () => {
    render(<GraphCanvas nodes={sampleNodes} edges={sampleEdges} />);

    await waitFor(() => {
      expect(screen.getByText('ACTOR')).toBeInTheDocument();
    });

    expect(screen.getByText('INITIATIVE')).toBeInTheDocument();
  });

  it('shows save view button when onSaveView is provided', async () => {
    const onSaveView = vi.fn();
    render(
      <GraphCanvas
        nodes={sampleNodes}
        edges={sampleEdges}
        onSaveView={onSaveView}
      />
    );

    await waitFor(() => {
      expect(screen.getByText('💾 Save View')).toBeInTheDocument();
    });
  });

  it('does not show save view button when onSaveView is not provided', async () => {
    render(<GraphCanvas nodes={sampleNodes} edges={sampleEdges} />);

    await waitFor(() => {
      expect(screen.getByText('Test Organization')).toBeInTheDocument();
    });

    expect(screen.queryByText('💾 Save View')).not.toBeInTheDocument();
  });

  it('applies highlight class to highlighted nodes', async () => {
    render(
      <GraphCanvas
        nodes={sampleNodes}
        edges={sampleEdges}
        highlightedNodeIds={['node-1']}
      />
    );

    await waitFor(() => {
      const node = screen.getByText('Test Organization').closest('.graph-custom-node');
      expect(node).toHaveClass('highlighted');
    });
  });

  it('hides nodes in hiddenNodeIds', async () => {
    render(
      <GraphCanvas
        nodes={sampleNodes}
        edges={sampleEdges}
        hiddenNodeIds={['node-1']}
      />
    );

    await waitFor(() => {
      expect(screen.getByText('Test Initiative')).toBeInTheDocument();
    });

    expect(screen.queryByText('Test Organization')).not.toBeInTheDocument();
  });

  it('calls onExpand when expand button is clicked', async () => {
    const onExpand = vi.fn();
    render(
      <GraphCanvas
        nodes={sampleNodes}
        edges={sampleEdges}
        onExpand={onExpand}
      />
    );

    await waitFor(() => {
      expect(screen.getByText('Test Organization')).toBeInTheDocument();
    });

    // Hover over the node to show the expand button
    const nodeElement = screen.getByText('Test Organization').closest('.graph-custom-node');
    fireEvent.mouseEnter(nodeElement);

    // Find and click the expand button
    const expandButton = screen.getAllByTitle('Show related nodes')[0];
    fireEvent.click(expandButton);

    expect(onExpand).toHaveBeenCalledWith('node-1', expect.objectContaining({
      id: 'node-1',
      name: 'Test Organization',
    }));
  });

  it('calls onEdit when edit button is clicked', async () => {
    const onEdit = vi.fn();
    render(
      <GraphCanvas
        nodes={sampleNodes}
        edges={sampleEdges}
        onEdit={onEdit}
      />
    );

    await waitFor(() => {
      expect(screen.getByText('Test Organization')).toBeInTheDocument();
    });

    // Hover over the node to show the edit button
    const nodeElement = screen.getByText('Test Organization').closest('.graph-custom-node');
    fireEvent.mouseEnter(nodeElement);

    // Find and click the edit button
    const editButton = screen.getAllByTitle('Edit node')[0];
    fireEvent.click(editButton);

    expect(onEdit).toHaveBeenCalledWith('node-1', expect.objectContaining({
      id: 'node-1',
      name: 'Test Organization',
    }));
  });
});

describe('GraphCanvas with many nodes', () => {
  it('shows lazy loading indicator for large graphs', async () => {
    // Create 250 nodes to exceed LAZY_LOAD_THRESHOLD
    const manyNodes = Array.from({ length: 250 }, (_, i) => ({
      id: `node-${i}`,
      name: `Node ${i}`,
      type: 'Actor',
      description: `Description ${i}`,
      communities: [],
    }));

    render(<GraphCanvas nodes={manyNodes} edges={[]} />);

    await waitFor(() => {
      expect(screen.getByText(/Showing \d+ of 250 nodes/)).toBeInTheDocument();
    });
  });

  it('shows load more button for large graphs', async () => {
    const manyNodes = Array.from({ length: 250 }, (_, i) => ({
      id: `node-${i}`,
      name: `Node ${i}`,
      type: 'Actor',
      description: `Description ${i}`,
      communities: [],
    }));

    render(<GraphCanvas nodes={manyNodes} edges={[]} />);

    await waitFor(() => {
      expect(screen.getByText('Load More')).toBeInTheDocument();
    });
  });
});
