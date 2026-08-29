import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, act, cleanup, screen, fireEvent } from '@testing-library/react';
import { GraphCanvas } from '../src/index';
import { resolveAttachedPosition } from '../src/utils/annotations';

// A live node store so onNodeDragStop's getFlowNodes()/setNodes read and
// write the same array, the same harness GraphCanvasUndo.test.jsx uses to
// exercise real drag wiring end to end.
const store = vi.hoisted(() => ({ nodes: [], edges: [], handlers: {} }));

vi.mock('reactflow', () => {
  const MockReactFlow = (props) => {
    store.handlers = props;
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
    }),
    useOnSelectionChange: () => {},
    Background: () => <div />,
    Controls: () => <div />,
    MiniMap: () => <div />,
    SelectionMode: { Partial: 'partial' },
  };
});

const nodeById = (id) => store.nodes.find((n) => n.id === id);

// task-annotation-render-direct-manipulation: dragging a label/text/icon/
// vote_dot near a node (re)attaches it; dragging one outside every snap zone
// detaches it and keeps the dropped position (docs/ANNOTATION_CONTRACT.md's
// "Attachment and detach behavior"). The follow-while-attached side of this
// (an attached overlay tracking its target's movement) is covered at the
// pure-function level by resolveAttachedPosition in overlaySerialization.test.js,
// the same split the pre-existing arrow-anchor effect uses.
describe('GraphCanvas attachment: drag-to-attach/detach', () => {
  beforeEach(() => {
    store.nodes = [];
    store.edges = [];
    store.handlers = {};
  });
  afterEach(() => cleanup());

  it('attaches a dropped label to the nearest node, storing the drop offset', () => {
    const onAnnotationChange = vi.fn();
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={onAnnotationChange} />);

    const target = {
      id: 'node-1',
      type: 'custom',
      position: { x: 100, y: 100 },
      width: 40,
      height: 40,
    };
    const label = { id: 'label-1', type: 'label', position: { x: 130, y: 90 }, data: {} };
    store.nodes = [target, label];

    act(() => {
      store.handlers.onNodeDragStop?.({}, label, [label]);
    });

    expect(nodeById('label-1').data.attachment).toEqual({
      target_id: 'node-1',
      target_type: 'node',
      offset: { x: 10, y: -30 },
    });
    expect(onAnnotationChange).toHaveBeenCalled();
  });

  it('detaches a dropped label that lands outside every snap zone, keeping its dropped position', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);

    const target = {
      id: 'node-1',
      type: 'custom',
      position: { x: 100, y: 100 },
      width: 0,
      height: 0,
    };
    const label = {
      id: 'label-1',
      type: 'label',
      position: { x: 900, y: 900 },
      data: { attachment: { target_id: 'node-1', target_type: 'node', offset: { x: 0, y: 0 } } },
    };
    store.nodes = [target, label];

    act(() => {
      store.handlers.onNodeDragStop?.({}, label, [label]);
    });

    expect(nodeById('label-1').data.attachment).toBeUndefined();
    expect(nodeById('label-1').position).toEqual({ x: 900, y: 900 });
  });

  it('re-attaches an already-attached overlay to a different, closer target', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);

    const far = {
      id: 'node-far',
      type: 'custom',
      position: { x: -500, y: -500 },
      width: 0,
      height: 0,
    };
    const near = { id: 'node-near', type: 'custom', position: { x: 0, y: 0 }, width: 0, height: 0 };
    const dot = {
      id: 'dot-1',
      type: 'vote_dot',
      position: { x: 5, y: 5 },
      data: {
        attachment: { target_id: 'node-far', target_type: 'node', offset: { x: 505, y: 505 } },
      },
    };
    store.nodes = [far, near, dot];

    act(() => {
      store.handlers.onNodeDragStop?.({}, dot, [dot]);
    });

    expect(nodeById('dot-1').data.attachment).toEqual({
      target_id: 'node-near',
      target_type: 'node',
      offset: { x: 5, y: 5 },
    });
  });

  it('does not attach an attachable overlay that is part of a multi-node drag', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);

    const target = {
      id: 'node-1',
      type: 'custom',
      position: { x: 100, y: 100 },
      width: 0,
      height: 0,
    };
    const label = { id: 'label-1', type: 'label', position: { x: 105, y: 105 }, data: {} };
    const other = { id: 'node-2', type: 'custom', position: { x: 500, y: 500 } };
    store.nodes = [target, label, other];

    act(() => {
      store.handlers.onNodeDragStop?.({}, label, [label, other]);
    });

    expect(nodeById('label-1').data.attachment).toBeUndefined();
  });

  it('leaves an unattached, un-dropped-near-anything overlay alone (no spurious change)', () => {
    const onAnnotationChange = vi.fn();
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={onAnnotationChange} />);

    const label = { id: 'label-1', type: 'label', position: { x: 900, y: 900 }, data: {} };
    store.nodes = [label];

    act(() => {
      store.handlers.onNodeDragStop?.({}, label, [label]);
    });

    expect(nodeById('label-1').data.attachment).toBeUndefined();
    expect(onAnnotationChange).not.toHaveBeenCalled();
  });
});

// task-annotation-render-direct-manipulation / task-annotation-responsive-
// bottom-toolbox's "Nearby object menu" contract entry point
// (docs/ANNOTATION_CONTRACT.md "Human authoring surfaces"): creates a new
// label/icon/vote_dot/text pre-wired to attach to an existing node/annotation
// from that target's own context menu, rather than requiring create-then-
// drag-near. Exercised through the real GraphCanvas node-context-menu render
// tree (only the `reactflow` package itself is mocked above), the same way
// GraphCanvasAnnotationToolbox.test.jsx exercises the toolbox's own creation
// paths.
//
// The target graph node is passed through the real `nodes` PROP (not
// smuggled into `store.nodes` directly, the way the drag-to-attach tests
// above inject their annotation overlays): GraphCanvas's own
// "reconcile internal nodes from the incoming nodes prop" effect treats any
// non-annotation node absent from that prop as stale and drops it on the
// next reconcile, and this mock's `useNodesState` returns a fresh `setNodes`
// closure every render (a dependency of that very effect), so it reconciles
// on every render here — a plain graph node injected only into `store.nodes`
// does not survive the render `onNodeContextMenu` itself triggers.
describe('GraphCanvas attachment: creation via the Nearby object menu', () => {
  beforeEach(() => {
    store.nodes = [];
    store.edges = [];
    store.handlers = {};
  });
  afterEach(() => cleanup());

  const openNodeMenu = (nodeId) => {
    act(() => {
      const node = store.nodes.find((n) => n.id === nodeId);
      store.handlers.onNodeContextMenu?.(
        { preventDefault: () => {}, stopPropagation: () => {}, clientX: 0, clientY: 0 },
        node
      );
    });
  };

  // `_savedPosition` makes reactFlowNodes place this node exactly here
  // instead of running it through the automatic layout; no `width`/`height`
  // is set, so its centre (nodeCenter) is this same point.
  const targetNodeProp = [{ id: 'node-1', type: 'Actor', _savedPosition: { x: 100, y: 100 } }];

  it('creates a vote_dot pre-wired with the exact same attachment shape drag-to-attach produces', () => {
    const onAnnotationChange = vi.fn();
    render(
      <GraphCanvas nodes={targetNodeProp} edges={[]} onAnnotationChange={onAnnotationChange} />
    );

    openNodeMenu('node-1');
    fireEvent.click(screen.getByRole('button', { name: '+ Vote dot' }));

    const created = store.nodes.find((n) => n.type === 'vote_dot');
    expect(created).toBeTruthy();
    // Same `{target_id, target_type, offset}` triple computeDroppedAttachment
    // produces for a post-creation drop — not a second, parallel shape.
    expect(created.data.attachment).toEqual({
      target_id: 'node-1',
      target_type: 'node',
      offset: { x: 36, y: -36 },
    });
    // Placed at the target's centre (100, 100) plus that same offset.
    expect(created.position).toEqual({ x: 136, y: 64 });
    expect(onAnnotationChange).toHaveBeenCalledWith('create');
  });

  it('follows its target when the target moves, via the exact same resolveAttachedPosition the drag-to-attach path uses', () => {
    // The live GraphCanvas "keep an attached overlay glued to its target"
    // effect is a thin wrapper around resolveAttachedPosition, and is already
    // covered end-to-end for the drag-to-attach path in
    // overlaySerialization.test.js's own `resolveAttachedPosition` suite (see
    // this file's earlier "follow-while-attached" comment) — that split is
    // deliberately reused here rather than re-driving a live GraphCanvas
    // re-render: what this test adds is proof that a node THIS creation path
    // built carries an attachment resolveAttachedPosition can actually act
    // on, not a second copy of that effect's own coverage.
    render(<GraphCanvas nodes={targetNodeProp} edges={[]} onAnnotationChange={vi.fn()} />);

    openNodeMenu('node-1');
    fireEvent.click(screen.getByRole('button', { name: '+ Label' }));
    const created = store.nodes.find((n) => n.type === 'label');
    expect(created.position).toEqual({ x: 136, y: 64 });

    // The target moves elsewhere; recompute the label's position from its
    // stored attachment the same way the live follow effect would.
    const movedCenters = new Map([['node-1', { x: 320, y: 320 }]]);
    expect(resolveAttachedPosition(created, movedCenters)).toEqual({ x: 356, y: 284 });
  });

  it('never creates the new annotation in a locked state', () => {
    render(<GraphCanvas nodes={targetNodeProp} edges={[]} onAnnotationChange={vi.fn()} />);

    openNodeMenu('node-1');
    fireEvent.click(screen.getByRole('button', { name: '+ Text' }));

    const created = store.nodes.find((n) => n.type === 'text');
    expect(created.data.locked).toBeFalsy();
  });
});

// dec-annotation-lock-semantics point 1: `locked` freezes ALL geometry
// change, including the follow effects that resolve a binding's geometry —
// not only user-initiated edits. These mount with the bound target already
// far from the overlay's stored geometry, so an unfixed effect (which
// resolves on every `nodes` change, mount included) would visibly move it.
describe('GraphCanvas attachment/anchor: locked annotations freeze geometry', () => {
  beforeEach(() => {
    store.nodes = [];
    store.edges = [];
    store.handlers = {};
  });
  afterEach(() => cleanup());

  it('does not move a locked, attached label when its target node has moved', () => {
    const target = {
      id: 'node-1',
      type: 'custom',
      position: { x: 900, y: 900 },
      width: 40,
      height: 40,
    };
    const label = {
      id: 'label-1',
      type: 'label',
      position: { x: 0, y: 0 },
      data: {
        locked: true,
        attachment: { target_id: 'node-1', target_type: 'node', offset: { x: 0, y: 0 } },
      },
    };
    store.nodes = [target, label];

    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);

    expect(nodeById('label-1').position).toEqual({ x: 0, y: 0 });
  });

  it('does not move a locked, anchored arrow when its endpoint node has moved', () => {
    const target = {
      id: 'node-1',
      type: 'custom',
      position: { x: 900, y: 900 },
      width: 40,
      height: 40,
    };
    const arrow = {
      id: 'arrow-1',
      type: 'arrow',
      position: { x: 0, y: 0 },
      data: { locked: true, startAnchor: 'node-1', dx: 200, dy: 0 },
    };
    store.nodes = [target, arrow];

    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);

    expect(nodeById('arrow-1').position).toEqual({ x: 0, y: 0 });
    expect(nodeById('arrow-1').data.dx).toBe(200);
    expect(nodeById('arrow-1').data.dy).toBe(0);
  });
});
