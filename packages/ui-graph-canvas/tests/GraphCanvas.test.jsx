import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

const hoisted = vi.hoisted(() => ({ onNodesChange: null, selectionOnChange: null }));

vi.mock('reactflow', () => {
  const MockReactFlow = ({ children, nodes, edges, onEdgeContextMenu, onNodeContextMenu }) => (
    <div data-testid="react-flow" className="react-flow">
      <div data-testid="nodes">
        {nodes?.map((node) => (
          <div
            key={node.id}
            data-testid={`node-${node.id}`}
            onContextMenu={(event) => onNodeContextMenu?.(event, node)}
          >
            {node.data?.label}
          </div>
        ))}
      </div>
      <div data-testid="edges">
        {edges?.map((edge) => (
          <div
            key={edge.id}
            data-testid={`edge-${edge.id}`}
            className="react-flow__edge"
            onContextMenu={(event) => onEdgeContextMenu?.(event, edge)}
          >
            {edge.label || edge.type}
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
    useNodesState: (initialNodes) => {
      if (!hoisted.onNodesChange) hoisted.onNodesChange = vi.fn();
      return [initialNodes || [], vi.fn(), hoisted.onNodesChange];
    },
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
    useOnSelectionChange: ({ onChange }) => {
      hoisted.selectionOnChange = onChange;
    },
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

  it('suppresses text selection while modifier-clicking to multi-select', () => {
    render(<GraphCanvas nodes={sampleNodes} edges={sampleEdges} />);
    const wrapper = screen.getByTestId('react-flow').parentElement;

    expect(document.body.classList.contains('graph-suppress-selection')).toBe(false);

    // Shift-click (multi-select) should suppress text selection on the body.
    fireEvent.mouseDown(wrapper, { button: 0, shiftKey: true });
    expect(document.body.classList.contains('graph-suppress-selection')).toBe(true);

    // Releasing the mouse lifts the suppression.
    fireEvent.mouseUp(document);
    expect(document.body.classList.contains('graph-suppress-selection')).toBe(false);
  });

  it('does not suppress text selection on a plain click', () => {
    render(<GraphCanvas nodes={sampleNodes} edges={sampleEdges} />);
    const wrapper = screen.getByTestId('react-flow').parentElement;

    fireEvent.mouseDown(wrapper, { button: 0 });
    expect(document.body.classList.contains('graph-suppress-selection')).toBe(false);
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

  it('calls onDeleteEdge from edge context menu when delete is clicked', () => {
    const onDeleteEdge = vi.fn();

    render(
      <GraphCanvas
        nodes={sampleNodes}
        edges={sampleEdges}
        onDeleteEdge={onDeleteEdge}
      />
    );

    fireEvent.contextMenu(screen.getByTestId('edge-edge-1'));
    fireEvent.click(screen.getByRole('button', { name: /delete/i }));

    expect(onDeleteEdge).toHaveBeenCalledWith('edge-1');
  });

  it('lets you change an edge relationship type from its context menu', () => {
    const onSetEdgeType = vi.fn();
    const schema = {
      relationship_types: {
        RELATES_TO: { description: 'Relates to (general connection)' },
        BELONGS_TO: { description: 'Belongs to' },
      },
    };

    render(
      <GraphCanvas
        nodes={sampleNodes}
        edges={sampleEdges}
        schema={schema}
        onSetEdgeType={onSetEdgeType}
      />
    );

    fireEvent.contextMenu(screen.getByTestId('edge-edge-1'));

    // General connection is always offered and reflects the RELATES_TO edge.
    expect(screen.getByRole('button', { name: /general connection/i })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /^belongs_to$/i }));

    expect(onSetEdgeType).toHaveBeenCalledWith('edge-1', 'BELONGS_TO');
  });

  it('resets an edge to a general connection from its context menu', () => {
    const onSetEdgeType = vi.fn();
    const schema = { relationship_types: { BELONGS_TO: { description: 'Belongs to' } } };

    render(
      <GraphCanvas
        nodes={sampleNodes}
        edges={[{ id: 'edge-1', source: 'node-1', target: 'node-2', type: 'BELONGS_TO' }]}
        schema={schema}
        onSetEdgeType={onSetEdgeType}
      />
    );

    fireEvent.contextMenu(screen.getByTestId('edge-edge-1'));
    fireEvent.click(screen.getByRole('button', { name: /general connection/i }));

    expect(onSetEdgeType).toHaveBeenCalledWith('edge-1', 'RELATES_TO');
  });

  it('selects every node of the same type from a single node context menu', () => {
    render(<GraphCanvas nodes={sampleNodes} edges={sampleEdges} />);

    fireEvent.contextMenu(screen.getByTestId('node-node-1'));
    fireEvent.click(
      screen.getByRole('button', { name: /select all nodes of the same type/i })
    );

    // node-1 is an Actor, so it is selected while the Initiative node-2 is not.
    expect(hoisted.onNodesChange).toHaveBeenCalledWith(
      expect.arrayContaining([
        { id: 'node-1', type: 'select', selected: true },
        { id: 'node-2', type: 'select', selected: false },
      ])
    );
  });

  it('selects same-type nodes that were not part of the original selection', () => {
    const nodes = [
      { id: 'node-1', name: 'Actor 1', type: 'Actor' },
      { id: 'node-2', name: 'Initiative 1', type: 'Initiative' },
      { id: 'node-3', name: 'Actor 2', type: 'Actor' },
      { id: 'node-4', name: 'Theme 1', type: 'Theme' },
    ];

    render(<GraphCanvas nodes={nodes} edges={[]} />);

    // Right-clicking the one Actor should still select the other Actor (node-3).
    fireEvent.contextMenu(screen.getByTestId('node-node-1'));
    fireEvent.click(
      screen.getByRole('button', { name: /select all nodes of the same type/i })
    );

    expect(hoisted.onNodesChange).toHaveBeenCalledWith(
      expect.arrayContaining([
        { id: 'node-1', type: 'select', selected: true },
        { id: 'node-2', type: 'select', selected: false },
        { id: 'node-3', type: 'select', selected: true },
        { id: 'node-4', type: 'select', selected: false },
      ])
    );
  });

  it('selects the union of all selected types from the multi-node context menu', () => {
    const nodes = [
      { id: 'node-1', name: 'Actor 1', type: 'Actor' },
      { id: 'node-2', name: 'Initiative 1', type: 'Initiative' },
      { id: 'node-3', name: 'Actor 2', type: 'Actor' },
      { id: 'node-4', name: 'Theme 1', type: 'Theme' },
    ];

    render(<GraphCanvas nodes={nodes} edges={[]} />);

    // Simulate a multi-selection spanning two types (Actor + Initiative).
    act(() => {
      hoisted.selectionOnChange({
        nodes: [
          { id: 'node-1', data: { nodeType: 'Actor' } },
          { id: 'node-2', data: { nodeType: 'Initiative' } },
        ],
        edges: [],
      });
    });

    // Right-clicking a node that is part of the multi-selection opens the multi menu.
    fireEvent.contextMenu(screen.getByTestId('node-node-1'));
    fireEvent.click(
      screen.getByRole('button', { name: /select all nodes of the same type/i })
    );

    // Every Actor and Initiative node is selected; the Theme node is not.
    expect(hoisted.onNodesChange).toHaveBeenCalledWith(
      expect.arrayContaining([
        { id: 'node-1', type: 'select', selected: true },
        { id: 'node-2', type: 'select', selected: true },
        { id: 'node-3', type: 'select', selected: true },
        { id: 'node-4', type: 'select', selected: false },
      ])
    );
  });

  it('closes an open context menu when closeMenusSignal increases', () => {
    const { rerender } = render(
      <GraphCanvas nodes={sampleNodes} edges={sampleEdges} closeMenusSignal={0} />
    );

    fireEvent.contextMenu(screen.getByTestId('edge-edge-1'));
    expect(document.querySelector('.edge-context-menu')).toBeInTheDocument();

    rerender(
      <GraphCanvas nodes={sampleNodes} edges={sampleEdges} closeMenusSignal={1} />
    );

    expect(document.querySelector('.edge-context-menu')).not.toBeInTheDocument();
  });
});
