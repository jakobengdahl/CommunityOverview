import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act, cleanup, fireEvent } from '@testing-library/react';
import { GraphCanvas } from '../src/index';
import { LAZY_LOAD_THRESHOLD, INITIAL_LOAD_COUNT } from '../src/utils/constants';

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
  liveNodes: null,
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
    if (hoisted.liveNodes === null) hoisted.liveNodes = props.nodes || [];
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
      // getNodes() is ReactFlow's live store, which is NOT the same as what the
      // canvas last rendered: it carries drags, group parenting and annotation
      // overlays. A test seeds hoisted.liveNodes to model that; otherwise it
      // tracks the rendered nodes.
      getNodes: () => hoisted.liveNodes ?? hoisted.currentNodes,
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
  hoisted.liveNodes = null;
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

  // The reconciliation half of focus (what actually lands in node state) is only
  // observable through the setNodes updaters, since useNodesState is mocked as a
  // pass-through. Folding them over a seeded previous state is how these tests
  // reach it.
  const foldSetNodes = (prevNodes) =>
    hoisted.setNodes.mock.calls.map(([updater]) => updater).reduce((acc, u) => u(acc), prevNodes);

  it('restores the exact canvas — positions, group membership and annotations', () => {
    // Live flow state that the incoming memo cannot reproduce on its own: nodes
    // dragged away from their computed layout, one of them inside a group, plus
    // a note overlay that exists only on the canvas. Without this the "restore"
    // assertion would compare two identical layouts and pass either way.
    const liveNodes = [
      { id: 'node-1', type: 'custom', position: { x: 11, y: 12 }, data: {} },
      { id: 'node-2', type: 'custom', position: { x: 21, y: 22 }, parentId: 'group-1', data: {} },
      { id: 'node-3', type: 'custom', position: { x: 31, y: 32 }, data: {} },
      { id: 'node-4', type: 'custom', position: { x: 41, y: 42 }, data: {} },
      { id: 'group-1', type: 'group', position: { x: 5, y: 5 }, data: {} },
      { id: 'note-1', type: 'note', position: { x: 900, y: 900 }, data: {} },
    ];
    render(<GraphCanvas nodes={nodes} edges={edges} compactMode="on" />);
    hoisted.liveNodes = liveNodes;
    selectNodes([{ id: 'node-1', type: 'custom' }]);

    hoisted.setNodes.mockClear();
    click(focusButton());
    const focused = foldSetNodes(liveNodes);

    // Entering: only the ego graph, at ring coordinates, detached from the group
    // so an absolute position is not read as parent-relative.
    expect(focused.map((n) => n.id).sort()).toEqual(['node-1', 'node-2']);
    expect(focused.find((n) => n.id === 'node-2').parentId).toBeUndefined();
    expect(focused.find((n) => n.id === 'node-1').position).not.toEqual({ x: 11, y: 12 });

    hoisted.liveNodes = focused;
    hoisted.setNodes.mockClear();
    click(exitFocusButton());
    const restored = foldSetNodes(focused);

    const byId = new Map(restored.map((n) => [n.id, n]));
    expect([...byId.keys()].sort()).toEqual([
      'group-1',
      'node-1',
      'node-2',
      'node-3',
      'node-4',
      'note-1',
    ]);
    for (const live of liveNodes) {
      expect(byId.get(live.id).position).toEqual(live.position);
    }
    // Group membership survives the round trip — the defect this asserts against
    // is silent detachment, which the autosave would then persist.
    expect(byId.get('node-2').parentId).toBe('group-1');
  });

  it('refuses to save while focused, so the ego layout cannot reach the server', () => {
    const onSaveView = vi.fn();
    const { rerender } = render(
      <GraphCanvas
        nodes={nodes}
        edges={edges}
        compactMode="on"
        onSaveView={onSaveView}
        saveViewSignal={0}
      />
    );

    // Every persistence path in the host — debounced autosave and the explicit
    // toolbar button alike — arrives as this signal.
    onSaveView.mockClear();
    rerender(
      <GraphCanvas
        nodes={nodes}
        edges={edges}
        compactMode="on"
        onSaveView={onSaveView}
        saveViewSignal={1}
      />
    );
    expect(onSaveView).toHaveBeenCalled();

    selectNodes([{ id: 'node-1', type: 'custom' }]);
    click(focusButton());

    onSaveView.mockClear();
    rerender(
      <GraphCanvas
        nodes={nodes}
        edges={edges}
        compactMode="on"
        onSaveView={onSaveView}
        saveViewSignal={2}
      />
    );
    expect(onSaveView).not.toHaveBeenCalled();
    expect(screen.getByText('Leave the focus view before saving')).toBeInTheDocument();

    click(exitFocusButton());
    rerender(
      <GraphCanvas
        nodes={nodes}
        edges={edges}
        compactMode="on"
        onSaveView={onSaveView}
        saveViewSignal={3}
      />
    );
    expect(onSaveView).toHaveBeenCalled();
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

    // ...and it stays gone. A remembered root would re-enter focus by itself the
    // moment that node came back — from an undone delete, a session switched
    // away and back, or an assistant re-adding it.
    rerender(<GraphCanvas nodes={nodes} edges={edges} compactMode="on" />);
    expect(renderedNodeIds()).toEqual(['node-1', 'node-2', 'node-3', 'node-4']);
  });

  it('leaves the lazy-load progress alone, so a large graph comes back whole', () => {
    // Above LAZY_LOAD_THRESHOLD the canvas renders a slice, and the slice size
    // must not be recomputed from the focus set — a focus round trip that reset
    // "Load More" progress would not be the reversible lens it claims to be.
    const many = Array.from({ length: LAZY_LOAD_THRESHOLD + 50 }, (_, i) => ({
      id: `bulk-${i}`,
      name: `Bulk ${i}`,
      type: 'Actor',
    }));
    const bulkEdges = [{ id: 'bulk-edge', source: 'bulk-0', target: 'bulk-1', type: 'RELATES_TO' }];

    render(<GraphCanvas nodes={many} edges={bulkEdges} compactMode="on" />);
    expect(hoisted.flowProps.nodes.length).toBeLessThan(many.length);

    // Advance past the initial slice, otherwise "progress preserved" and
    // "progress reset" produce the same number and the assertion proves nothing.
    click(screen.getByRole('button', { name: 'Load More' }));
    const loadedBefore = hoisted.flowProps.nodes.length;
    expect(loadedBefore).toBeGreaterThan(INITIAL_LOAD_COUNT);

    selectNodes([{ id: 'bulk-0', type: 'custom' }]);
    click(focusButton());
    expect(renderedNodeIds()).toEqual(['bulk-0', 'bulk-1']);

    click(exitFocusButton());
    expect(hoisted.flowProps.nodes.length).toBe(loadedBefore);
  });
});
