import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent, act, cleanup, screen } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

// Same live node store / reactflow mock as GraphCanvasUndo.test.jsx, extended
// with a capturable useOnSelectionChange so a test can simulate a mixed
// nodes+annotations multi-selection without driving a real marquee gesture.
const store = vi.hoisted(() => ({ nodes: [], edges: [], handlers: {} }));

vi.mock('reactflow', () => {
  const MockReactFlow = (props) => {
    store.handlers = { ...store.handlers, ...props };
    return <div data-testid="react-flow">{props.children}</div>;
  };
  return {
    __esModule: true,
    default: MockReactFlow,
    ReactFlow: MockReactFlow,
    ReactFlowProvider: ({ children }) => <div>{children}</div>,
    useNodesState: () => [
      store.nodes,
      (updater) => {
        store.nodes = typeof updater === 'function' ? updater(store.nodes) : updater;
      },
      vi.fn(),
    ],
    useEdgesState: () => [store.edges, vi.fn(), vi.fn()],
    addEdge: (_params, edges) => edges,
    useReactFlow: () => ({
      getNodes: () => store.nodes,
      getEdges: () => [],
      setNodes: vi.fn(),
      setEdges: vi.fn(),
      screenToFlowPosition: () => ({ x: 0, y: 0 }),
      setCenter: vi.fn(),
      getViewport: () => ({ x: 0, y: 0, zoom: 1 }),
      fitView: vi.fn(),
      zoomIn: vi.fn(),
      zoomOut: vi.fn(),
    }),
    useOnSelectionChange: ({ onChange }) => {
      store.handlers.selectionOnChange = onChange;
    },
    Background: () => <div />,
    Controls: () => <div />,
    MiniMap: () => <div />,
    SelectionMode: { Partial: 'partial' },
  };
});

const nodeById = (id) => store.nodes.find((n) => n.id === id);

// The graph-node reconciliation effect (GraphCanvas.jsx's "Update nodes when
// input changes") drops any 'custom' node absent from the `nodes` prop on
// every re-render whose deps changed (e.g. `unfocusedNodeCount`, which
// changes whenever a graph node is added to the live store below) — annotation
// overlays survive that reconcile as `isManualNode`, but a graph node needs a
// same-id domain entry in `nodes` so its live (directly-injected) position is
// recognised and preserved rather than reconciled away. Mirrors the shape
// GraphCanvasUndo.test.jsx's `inputNodes` uses.
const domainNode = (id) => ({ id, name: id, type: 'Actor', description: '' });
const syntheticMenuEvent = {
  preventDefault: () => {},
  stopPropagation: () => {},
  clientX: 10,
  clientY: 10,
};

const graphNode = (id, x, y) => ({
  id,
  type: 'custom',
  position: { x, y },
  width: 100,
  height: 50,
  data: {},
});
const noteNode = (id, x, y, dataOverrides = {}) => ({
  id,
  type: 'note',
  position: { x, y },
  width: 50,
  height: 20,
  data: dataOverrides,
});
const labelNode = (id, x, y, dataOverrides = {}) => ({
  id,
  type: 'label',
  position: { x, y },
  width: 40,
  height: 16,
  data: dataOverrides,
});

const openMultiMenu = (nodes) => {
  // Two separate acts: onSelectionContextMenu is a useCallback closed over
  // `selectedNodes` state, so it must be re-bound by a render commit after
  // the selection-change state update before it reads the new selection —
  // calling both in one act would read the still-stale pre-selection closure.
  act(() => {
    store.handlers.selectionOnChange({ nodes, edges: [] });
  });
  act(() => {
    store.handlers.onSelectionContextMenu(syntheticMenuEvent);
  });
};

const clickSubmenu = (triggerName, itemName) => {
  fireEvent.click(screen.getByRole('button', { name: triggerName }));
  fireEvent.click(screen.getByRole('button', { name: itemName }));
};

describe('GraphCanvas multi-select Align/Distribute', () => {
  beforeEach(() => {
    store.nodes = [];
    store.edges = [];
    store.handlers = {};
  });
  afterEach(() => cleanup());

  it('aligns eligible graph nodes and annotations together, persisting through both publish paths', () => {
    const onNodePositionChange = vi.fn();
    const onAnnotationChange = vi.fn();
    render(
      <GraphCanvas
        nodes={[domainNode('g1')]}
        edges={[]}
        onNodePositionChange={onNodePositionChange}
        onAnnotationChange={onAnnotationChange}
      />
    );

    const g1 = graphNode('g1', 300, 0);
    const n1 = noteNode('n1', 100, 200);
    store.nodes = [g1, n1];

    openMultiMenu(store.nodes);
    clickSubmenu(/^align$/i, /^align left$/i);

    // n1 was already at the minimum left edge (100); g1 moves to join it.
    expect(nodeById('g1').position).toEqual({ x: 100, y: 0 });
    expect(nodeById('n1').position).toEqual({ x: 100, y: 200 });

    // Graph node publishes through onNodePositionChange, the same path a
    // drag or Organize uses.
    expect(onNodePositionChange).toHaveBeenCalledWith('g1', { x: 100, y: 0 });
    expect(onNodePositionChange).not.toHaveBeenCalledWith('n1', expect.anything());

    // The annotation move publishes through onAnnotationChange('geometry'),
    // the same notifier the attach-follow effects and drag/attach use.
    expect(onAnnotationChange).toHaveBeenCalledWith('geometry');
  });

  it('excludes a locked and a remote-claimed annotation from align, leaving their geometry untouched', () => {
    const onAnnotationChange = vi.fn();
    render(
      <GraphCanvas nodes={[domainNode('g1')]} edges={[]} onAnnotationChange={onAnnotationChange} />
    );

    const g1 = graphNode('g1', 300, 0);
    const n1 = noteNode('n1', 100, 200);
    const locked = noteNode('locked1', 400, 400, { locked: true });
    const remote = noteNode('remote1', 500, 500, { remoteLease: { clientId: 'other' } });
    store.nodes = [g1, n1, locked, remote];

    openMultiMenu(store.nodes);
    clickSubmenu(/^align$/i, /^align left$/i);

    // The two eligible members aligned to each other...
    expect(nodeById('g1').position).toEqual({ x: 100, y: 0 });
    expect(nodeById('n1').position).toEqual({ x: 100, y: 200 });
    // ...but the locked and remote-claimed notes never moved.
    expect(nodeById('locked1').position).toEqual({ x: 400, y: 400 });
    expect(nodeById('remote1').position).toEqual({ x: 500, y: 500 });

    // A remote claim wins the notice over a plain lock (matches
    // deleteSelectedNodes's own priority).
    expect(screen.getByText('Someone else is editing this annotation')).toBeTruthy();
  });

  it('excludes a currently-attached annotation from align so it is not fought by the attachment-follow effect', () => {
    // The attachment points at a target id that is not present on the canvas
    // at all — resolveAttachedPosition/the follow effect then has nothing to
    // re-glue attached1 to (docs: "detaches and keeps its last resolved
    // geometry" when the target is absent) and leaves it exactly where it
    // is. That isolates what this test actually checks — that align's own
    // eligibility filter, not the follow effect being a no-op for some other
    // reason, is what kept attached1 at (600,600).
    render(<GraphCanvas nodes={[domainNode('g1')]} edges={[]} />);

    const g1 = graphNode('g1', 300, 0);
    const n1 = noteNode('n1', 100, 200);
    const attached = labelNode('attached1', 600, 600, {
      attachment: { target_id: 'nonexistent-target', target_type: 'node', offset: { x: 0, y: 0 } },
    });
    store.nodes = [g1, n1, attached];

    openMultiMenu([g1, n1, attached]);
    clickSubmenu(/^align$/i, /^align left$/i);

    expect(nodeById('g1').position).toEqual({ x: 100, y: 0 });
    expect(nodeById('n1').position).toEqual({ x: 100, y: 200 });
    // The attached label was left exactly where it started: align's
    // eligibility filter excluded it from the move set.
    expect(nodeById('attached1').position).toEqual({ x: 600, y: 600 });

    expect(screen.getByText('Attached items follow their target and were left out')).toBeTruthy();
  });

  it('omits the Distribute trigger below 3 eligible members and offers it once a third joins', () => {
    render(<GraphCanvas nodes={[domainNode('g1')]} edges={[]} />);

    const g1 = graphNode('g1', 0, 0);
    const n1 = noteNode('n1', 200, 0);
    store.nodes = [g1, n1];
    openMultiMenu(store.nodes);
    expect(screen.queryByRole('button', { name: /^distribute$/i })).toBeNull();

    const n2 = noteNode('n2', 400, 0);
    store.nodes = [g1, n1, n2];
    openMultiMenu(store.nodes);
    expect(screen.getByRole('button', { name: /^distribute$/i })).toBeTruthy();
  });

  it('distributes three eligible members with equal gaps along the chosen axis', () => {
    render(<GraphCanvas nodes={[domainNode('g1')]} edges={[]} />);

    // x=10, not 0, for the graph node: the reconciliation effect only treats
    // an *existing* node's position as real (worth preserving) when its x is
    // non-zero — an x of exactly 0 reads as "still at its unset default" and
    // would be replaced by a freshly computed layout position instead.
    const g1 = graphNode('g1', 10, 0); // width 100
    const n1 = noteNode('n1', 1000, 0); // width 50
    const n2 = noteNode('n2', 400, 0); // width 50, the one that moves
    store.nodes = [g1, n1, n2];

    openMultiMenu(store.nodes);
    clickSubmenu(/^distribute$/i, /distribute horizontally/i);

    // Outer two (by centre) keep their x; span = (1000+50) - 10 = 1040,
    // total width = 100+50+50 = 200, gap = (1040-200)/2 = 420.
    expect(nodeById('g1').position.x).toBe(10);
    expect(nodeById('n2').position.x).toBe(530); // 10 + 100 + 420
    expect(nodeById('n1').position.x).toBe(1000);
  });
});
