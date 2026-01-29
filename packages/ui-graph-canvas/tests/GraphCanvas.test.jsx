import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

// Mock ReactFlow since it's heavy in jsdom
vi.mock('reactflow', () => {
  const MockReactFlow = ({ children, nodes, edges }) => (
    <div data-testid="react-flow" className="react-flow">
      <div data-testid="nodes">
        {nodes?.map(node => (
          <div key={node.id} data-testid={`node-${node.id}`}>
            {node.data?.label}
          </div>
        ))}
      </div>
      {children}
    </div>
  );

  return {
    default: MockReactFlow,
    ReactFlow: MockReactFlow,
    ReactFlowProvider: ({ children }) => <div>{children}</div>,
    useNodesState: (initialNodes) => [initialNodes || [], vi.fn(), vi.fn()],
    useEdgesState: (initialEdges) => [initialEdges || [], vi.fn(), vi.fn()],
    useReactFlow: () => ({
      fitView: vi.fn(),
      getNodes: () => [],
      getEdges: () => [],
      setNodes: vi.fn(),
      setEdges: vi.fn(),
    }),
    Background: () => <div data-testid="background" />,
    Controls: () => <div data-testid="controls" />,
    MiniMap: () => <div data-testid="minimap" />,
    Panel: ({ children }) => <div data-testid="panel">{children}</div>,
    Handle: ({ type, position }) => <div data-testid={`handle-${type}`} />,
    Position: { Top: 'top', Bottom: 'bottom', Left: 'left', Right: 'right' },
    MarkerType: { ArrowClosed: 'arrowclosed' },
  };
});

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
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders without crashing', () => {
    render(<GraphCanvas nodes={[]} edges={[]} />);
    expect(screen.getByText('No graph to display')).toBeInTheDocument();
  });

  it('displays empty state message when no nodes provided', () => {
    render(<GraphCanvas nodes={[]} edges={[]} />);
    expect(screen.getByText('No graph to display')).toBeInTheDocument();
    expect(screen.getByText(/Search or add nodes/)).toBeInTheDocument();
  });

  it('renders ReactFlow container when nodes provided', () => {
    render(<GraphCanvas nodes={sampleNodes} edges={sampleEdges} />);
    expect(screen.getByTestId('react-flow')).toBeInTheDocument();
  });

  it('shows save view button when onSaveView is provided', () => {
    const onSaveView = vi.fn();
    render(
      <GraphCanvas
        nodes={sampleNodes}
        edges={sampleEdges}
        onSaveView={onSaveView}
      />
    );
    expect(screen.getByText('💾 Save View')).toBeInTheDocument();
  });

  it('does not show save view button when onSaveView is not provided', () => {
    render(<GraphCanvas nodes={sampleNodes} edges={sampleEdges} />);
    expect(screen.queryByText('💾 Save View')).not.toBeInTheDocument();
  });

  it('renders controls container', () => {
    render(<GraphCanvas nodes={sampleNodes} edges={sampleEdges} />);
    // The controls container should exist
    const controls = document.querySelector('.graph-canvas-controls');
    expect(controls).toBeInTheDocument();
  });

  it('renders main container', () => {
    render(<GraphCanvas nodes={sampleNodes} edges={sampleEdges} />);
    // The main container should exist
    const container = document.querySelector('.graph-canvas-container');
    expect(container).toBeInTheDocument();
  });
});

describe('GraphCanvas with many nodes', () => {
  it('shows lazy loading indicator for large graphs', () => {
    // Create 250 nodes to exceed LAZY_LOAD_THRESHOLD (200)
    const manyNodes = Array.from({ length: 250 }, (_, i) => ({
      id: `node-${i}`,
      name: `Node ${i}`,
      type: 'Actor',
      description: `Description ${i}`,
      communities: [],
    }));

    render(<GraphCanvas nodes={manyNodes} edges={[]} />);
    expect(screen.getByText(/Showing \d+ of 250 nodes/)).toBeInTheDocument();
  });

  it('shows load more button for large graphs', () => {
    const manyNodes = Array.from({ length: 250 }, (_, i) => ({
      id: `node-${i}`,
      name: `Node ${i}`,
      type: 'Actor',
      description: `Description ${i}`,
      communities: [],
    }));

    render(<GraphCanvas nodes={manyNodes} edges={[]} />);
    expect(screen.getByText('Load More')).toBeInTheDocument();
  });
});

describe('GraphCanvas callbacks', () => {
  it('accepts onExpand callback', () => {
    const onExpand = vi.fn();
    render(
      <GraphCanvas
        nodes={sampleNodes}
        edges={sampleEdges}
        onExpand={onExpand}
      />
    );
    // Component should render without errors when callback is provided
    expect(screen.getByTestId('react-flow')).toBeInTheDocument();
  });

  it('accepts onEdit callback', () => {
    const onEdit = vi.fn();
    render(
      <GraphCanvas
        nodes={sampleNodes}
        edges={sampleEdges}
        onEdit={onEdit}
      />
    );
    expect(screen.getByTestId('react-flow')).toBeInTheDocument();
  });

  it('accepts onDelete callback', () => {
    const onDelete = vi.fn();
    render(
      <GraphCanvas
        nodes={sampleNodes}
        edges={sampleEdges}
        onDelete={onDelete}
      />
    );
    expect(screen.getByTestId('react-flow')).toBeInTheDocument();
  });

  it('accepts onCreateGroup callback', () => {
    const onCreateGroup = vi.fn();
    render(
      <GraphCanvas
        nodes={sampleNodes}
        edges={sampleEdges}
        onCreateGroup={onCreateGroup}
      />
    );
    expect(screen.getByTestId('react-flow')).toBeInTheDocument();
  });

  it('accepts highlightedNodeIds prop', () => {
    render(
      <GraphCanvas
        nodes={sampleNodes}
        edges={sampleEdges}
        highlightedNodeIds={['node-1']}
      />
    );
    expect(screen.getByTestId('react-flow')).toBeInTheDocument();
  });

  it('accepts hiddenNodeIds prop', () => {
    render(
      <GraphCanvas
        nodes={sampleNodes}
        edges={sampleEdges}
        hiddenNodeIds={['node-1']}
      />
    );
    expect(screen.getByTestId('react-flow')).toBeInTheDocument();
  });
});
