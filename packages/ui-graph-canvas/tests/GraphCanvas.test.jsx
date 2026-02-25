import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

vi.mock('reactflow', () => {
  const MockReactFlow = ({ children, nodes }) => (
    <div data-testid="react-flow" className="react-flow">
      <div data-testid="nodes">
        {nodes?.map((node) => (
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
      screenToFlowPosition: () => ({ x: 0, y: 0 }),
      setCenter: vi.fn(),
    }),
    useOnSelectionChange: vi.fn(),
    Background: () => <div data-testid="background" />,
    Controls: () => <div data-testid="controls" />,
    MiniMap: () => <div data-testid="minimap" />,
    SelectionMode: { Partial: 'partial' },
    Handle: ({ type }) => <div data-testid={`handle-${type}`} />,
    Position: { Top: 'top', Bottom: 'bottom', Left: 'left', Right: 'right' },
    MarkerType: { ArrowClosed: 'arrowclosed' },
  };
});

const sampleNodes = [
  { id: 'node-1', name: 'Node 1', type: 'Actor', description: 'a' },
  { id: 'node-2', name: 'Node 2', type: 'Initiative', description: 'b' },
];

const sampleEdges = [
  { id: 'edge-1', source: 'node-1', target: 'node-2', type: 'RELATES_TO' },
];

describe('GraphCanvas', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders graph container and react-flow', () => {
    render(<GraphCanvas nodes={sampleNodes} edges={sampleEdges} />);
    expect(screen.getByTestId('react-flow')).toBeInTheDocument();
    expect(document.querySelector('.graph-canvas-container')).toBeInTheDocument();
  });

  it('shows depth selector when multiple levels exist', () => {
    render(
      <GraphCanvas
        nodes={sampleNodes}
        edges={sampleEdges}
        federationDepth={1}
        federationDepthLevels={[1, 3, 5]}
      />
    );

    expect(screen.getByLabelText('Federated search depth selector')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '1' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '3' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '5' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '2' })).not.toBeInTheDocument();
  });

  it('hides depth selector when only one level exists', () => {
    render(
      <GraphCanvas
        nodes={sampleNodes}
        edges={sampleEdges}
        federationDepth={1}
        federationDepthLevels={[1]}
      />
    );

    expect(screen.queryByLabelText('Federated search depth selector')).not.toBeInTheDocument();
  });

  it('calls onFederationDepthChange when level is clicked', () => {
    const onFederationDepthChange = vi.fn();

    render(
      <GraphCanvas
        nodes={sampleNodes}
        edges={sampleEdges}
        federationDepth={1}
        federationDepthLevels={[1, 3]}
        onFederationDepthChange={onFederationDepthChange}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: '3' }));
    expect(onFederationDepthChange).toHaveBeenCalledWith(3);
  });
});
