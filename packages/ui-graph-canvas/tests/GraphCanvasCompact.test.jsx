import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act, cleanup, fireEvent } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

// Captures what GraphCanvas hands to ReactFlow (the nodes it renders, the
// fitViewOptions it configures), the camera calls the compact pill makes, and
// the node-state updaters, so the assertions below check behaviour rather than
// markup alone.
//
// useNodesState is deliberately a pure pass-through, matching the other
// GraphCanvas suites: the canvas has effects that write node state on every
// render, so a mock backed by real state never settles.
const hoisted = vi.hoisted(() => ({
  flowProps: null,
  currentNodes: [],
  selectionOnChange: null,
  setNodes: null,
  fitView: null,
  zoomIn: null,
  zoomOut: null,
}));

vi.mock('reactflow', () => {
  const MockReactFlow = (props) => {
    hoisted.flowProps = props;
    hoisted.currentNodes = props.nodes || [];
    return (
      <div data-testid="react-flow">
        {props.nodes?.map((node) => (
          <div key={node.id} className="react-flow__node" data-id={node.id} />
        ))}
        {props.children}
      </div>
    );
  };

  return {
    __esModule: true,
    default: MockReactFlow,
    ReactFlow: MockReactFlow,
    ReactFlowProvider: ({ children }) => <div>{children}</div>,
    useNodesState: (initialNodes) => [initialNodes || [], hoisted.setNodes, vi.fn()],
    useEdgesState: (initialEdges) => [initialEdges || [], vi.fn(), vi.fn()],
    addEdge: (_params, edges) => edges,
    useReactFlow: () => ({
      getNodes: () => hoisted.currentNodes,
      getEdges: () => [],
      setNodes: vi.fn(),
      setEdges: vi.fn(),
      screenToFlowPosition: ({ x, y }) => ({ x, y }),
      setCenter: vi.fn(),
      getViewport: () => ({ x: 0, y: 0, zoom: 1 }),
      fitView: hoisted.fitView,
      zoomIn: hoisted.zoomIn,
      zoomOut: hoisted.zoomOut,
    }),
    useOnSelectionChange: ({ onChange }) => {
      hoisted.selectionOnChange = onChange;
    },
    Background: () => <div data-testid="background" />,
    Controls: () => <div data-testid="controls" />,
    MiniMap: () => <div data-testid="minimap" />,
    SelectionMode: { Partial: 'partial' },
    Handle: () => <div />,
    Position: { Top: 'top', Bottom: 'bottom', Left: 'left', Right: 'right' },
    MarkerType: { ArrowClosed: 'arrowclosed', Arrow: 'arrow' },
  };
});

// node-1 is the hub: node-2 is a direct neighbour, node-3 is two hops away and
// node-4 has no edge at all. Focusing node-1 must keep exactly node-1 and node-2.
const nodes = [
  { id: 'node-1', name: 'Hub', type: 'Actor' },
  { id: 'node-2', name: 'Neighbour', type: 'Initiative' },
  { id: 'node-3', name: 'Two hops', type: 'Initiative' },
  { id: 'node-4', name: 'Unconnected', type: 'Actor' },
];
const edges = [
  { id: 'edge-1', source: 'node-1', target: 'node-2', type: 'RELATES_TO' },
  { id: 'edge-2', source: 'node-2', target: 'node-3', type: 'RELATES_TO' },
];

const renderedNodeIds = () =>
  Array.from(document.querySelectorAll('.react-flow__node'))
    .map((el) => el.dataset.id)
    .sort();

const selectNodes = (selected) =>
  act(() => {
    hoisted.selectionOnChange({ nodes: selected, edges: [] });
  });

const focusButton = () => screen.getByRole('button', { name: 'Focus on selected node' });
const exitFocusButton = () => screen.getByRole('button', { name: 'Back to whole graph' });
const click = (button) => act(() => fireEvent.click(button));

/** Runs the queued animation frame the deferred refits are scheduled on. */
const flushFrame = async () => {
  await act(async () => {
    await new Promise((resolve) => requestAnimationFrame(resolve));
  });
};

beforeEach(() => {
  hoisted.setNodes = vi.fn();
  hoisted.fitView = vi.fn();
  hoisted.zoomIn = vi.fn();
  hoisted.zoomOut = vi.fn();
  // jsdom has no matchMedia; compactMode 'auto' has to degrade to non-compact
  // rather than throw. The 'auto' test installs its own stub.
  delete window.matchMedia;
});

afterEach(() => {
  cleanup();
});

describe('compact canvas chrome', () => {
  it('replaces the ReactFlow control cluster with a touch-sized pill', () => {
    render(<GraphCanvas nodes={nodes} edges={edges} compactMode="on" />);

    expect(screen.queryByTestId('controls')).not.toBeInTheDocument();
    expect(document.querySelector('.graph-compact-controls')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Zoom in' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Zoom out' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Fit whole graph' })).toBeInTheDocument();
  });

  it('suppresses the minimap even when the persisted setting has it on', () => {
    render(<GraphCanvas nodes={nodes} edges={edges} compactMode="on" showMinimap />);

    expect(screen.queryByTestId('minimap')).not.toBeInTheDocument();
  });

  it('drives the camera from the pill', () => {
    render(<GraphCanvas nodes={nodes} edges={edges} compactMode="on" />);

    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }));
    fireEvent.click(screen.getByRole('button', { name: 'Zoom out' }));
    expect(hoisted.zoomIn).toHaveBeenCalled();
    expect(hoisted.zoomOut).toHaveBeenCalled();

    hoisted.fitView.mockClear();
    fireEvent.click(screen.getByRole('button', { name: 'Fit whole graph' }));
    expect(hoisted.fitView).toHaveBeenCalledWith(expect.objectContaining({ padding: 0.05 }));
  });

  it('fits with tighter padding than the desktop canvas', () => {
    render(<GraphCanvas nodes={nodes} edges={edges} compactMode="on" />);
    expect(hoisted.flowProps.fitViewOptions.padding).toBe(0.05);
  });

  it('takes its labels from props so the host can translate them', () => {
    render(
      <GraphCanvas
        nodes={nodes}
        edges={edges}
        compactMode="on"
        compactZoomInLabel="Zooma in"
        compactFitViewLabel="Visa hela grafen"
        focusViewLabel="Fokusera på vald nod"
      />
    );

    expect(screen.getByRole('button', { name: 'Zooma in' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Visa hela grafen' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Fokusera på vald nod' })).toBeInTheDocument();
  });

  it('follows the viewport when compactMode is left on auto', () => {
    window.matchMedia = vi.fn().mockImplementation((query) => ({
      matches: query === '(max-width: 768px)',
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }));

    render(<GraphCanvas nodes={nodes} edges={edges} />);

    expect(document.querySelector('.graph-compact-controls')).toBeInTheDocument();
    expect(screen.queryByTestId('controls')).not.toBeInTheDocument();
  });
});

describe('desktop canvas defaults are unchanged', () => {
  it('keeps the ReactFlow controls, the minimap setting and the original padding', () => {
    render(<GraphCanvas nodes={nodes} edges={edges} compactMode="off" showMinimap />);

    expect(screen.getByTestId('controls')).toBeInTheDocument();
    expect(screen.getByTestId('minimap')).toBeInTheDocument();
    expect(document.querySelector('.graph-compact-controls')).not.toBeInTheDocument();
    expect(hoisted.flowProps.fitViewOptions.padding).toBe(0.2);
  });

  it('offers no focus control at all, so nothing about desktop selection changes', () => {
    render(<GraphCanvas nodes={nodes} edges={edges} compactMode="off" />);
    selectNodes([{ id: 'node-1', type: 'custom' }]);

    expect(
      screen.queryByRole('button', { name: 'Focus on selected node' })
    ).not.toBeInTheDocument();
    expect(renderedNodeIds()).toHaveLength(4);
  });
});

describe('mobile focus view', () => {
  it('is reachable from a selected node without a context menu, and reversible', () => {
    render(<GraphCanvas nodes={nodes} edges={edges} compactMode="on" />);
    expect(renderedNodeIds()).toHaveLength(4);

    selectNodes([{ id: 'node-1', type: 'custom' }]);
    click(focusButton());

    expect(renderedNodeIds()).toEqual(['node-1', 'node-2']);

    click(exitFocusButton());

    expect(renderedNodeIds()).toEqual(['node-1', 'node-2', 'node-3', 'node-4']);
  });

  it('lays the focused neighbours out around the root instead of leaving them stacked', () => {
    const withHubNeighbours = [
      ...nodes,
      { id: 'node-5', name: 'Second neighbour', type: 'Actor' },
      { id: 'node-6', name: 'Third neighbour', type: 'Actor' },
    ];
    const withHubEdges = [
      ...edges,
      { id: 'edge-3', source: 'node-1', target: 'node-5', type: 'RELATES_TO' },
      { id: 'edge-4', source: 'node-6', target: 'node-1', type: 'RELATES_TO' },
    ];

    render(<GraphCanvas nodes={withHubNeighbours} edges={withHubEdges} compactMode="on" />);
    selectNodes([{ id: 'node-1', type: 'custom' }]);
    click(focusButton());

    expect(renderedNodeIds()).toEqual(['node-1', 'node-2', 'node-5', 'node-6']);

    const byId = new Map(hoisted.flowProps.nodes.map((n) => [n.id, n.position]));
    const positions = [...byId.values()].map((p) => `${Math.round(p.x)},${Math.round(p.y)}`);
    expect(new Set(positions).size).toBe(positions.length);

    // Each neighbour sits the same distance from the root — the ring the ego
    // layout draws, not a coincidence of the generic auto-layout.
    const root = byId.get('node-1');
    const radii = ['node-2', 'node-5', 'node-6'].map((id) =>
      Math.round(Math.hypot(byId.get(id).x - root.x, byId.get(id).y - root.y))
    );
    expect(new Set(radii).size).toBe(1);
    expect(radii[0]).toBeGreaterThan(0);
  });

  it('restores the positions the canvas had before focus was entered', () => {
    render(<GraphCanvas nodes={nodes} edges={edges} compactMode="on" />);
    const before = new Map(hoisted.flowProps.nodes.map((n) => [n.id, { ...n.position }]));

    selectNodes([{ id: 'node-1', type: 'custom' }]);
    click(focusButton());

    // The ego layout really did move things, so the restore below is a
    // meaningful assertion rather than a comparison of two identical layouts.
    const during = new Map(hoisted.flowProps.nodes.map((n) => [n.id, { ...n.position }]));
    expect(during.get('node-1')).not.toEqual(before.get('node-1'));

    // Leaving focus re-bases every position, so the reconciliation updater is
    // handed the focus-era nodes and has to put the originals back.
    hoisted.setNodes.mockClear();
    click(exitFocusButton());

    const focusEraNodes = hoisted.flowProps.nodes.map((n) => ({
      ...n,
      position: during.get(n.id) || n.position,
    }));
    const restored = hoisted.setNodes.mock.calls
      .map(([updater]) => updater)
      .reduce((acc, updater) => updater(acc), focusEraNodes);

    for (const node of restored) {
      expect(node.position).toEqual(before.get(node.id));
    }
    expect(restored.map((n) => n.id).sort()).toEqual(['node-1', 'node-2', 'node-3', 'node-4']);
  });

  it('refits the camera on entering and on leaving focus', async () => {
    render(<GraphCanvas nodes={nodes} edges={edges} compactMode="on" />);
    selectNodes([{ id: 'node-1', type: 'custom' }]);

    hoisted.fitView.mockClear();
    click(focusButton());
    await flushFrame();
    expect(hoisted.fitView).toHaveBeenCalledWith(expect.objectContaining({ padding: 0.05 }));

    hoisted.fitView.mockClear();
    click(exitFocusButton());
    await flushFrame();
    expect(hoisted.fitView).toHaveBeenCalledWith(expect.objectContaining({ padding: 0.05 }));
  });

  it('disables the focus control until exactly one graph node is selected', () => {
    render(<GraphCanvas nodes={nodes} edges={edges} compactMode="on" />);
    expect(focusButton()).toBeDisabled();

    selectNodes([
      { id: 'node-1', type: 'custom' },
      { id: 'node-2', type: 'custom' },
    ]);
    expect(focusButton()).toBeDisabled();

    selectNodes([{ id: 'node-1', type: 'custom' }]);
    expect(focusButton()).toBeEnabled();
  });

  it('does not offer focus for an annotation, which has no graph neighbours', () => {
    render(<GraphCanvas nodes={nodes} edges={edges} compactMode="on" />);

    selectNodes([{ id: 'note-1', type: 'note' }]);

    expect(focusButton()).toBeDisabled();
  });

  it('falls back to the whole graph when the focused node leaves the session', () => {
    const { rerender } = render(<GraphCanvas nodes={nodes} edges={edges} compactMode="on" />);
    selectNodes([{ id: 'node-1', type: 'custom' }]);
    click(focusButton());
    expect(renderedNodeIds()).toEqual(['node-1', 'node-2']);

    const withoutHub = nodes.filter((n) => n.id !== 'node-1');
    rerender(<GraphCanvas nodes={withoutHub} edges={edges} compactMode="on" />);

    expect(renderedNodeIds()).toEqual(['node-2', 'node-3', 'node-4']);
    expect(screen.queryByRole('button', { name: 'Back to whole graph' })).not.toBeInTheDocument();
  });
});
