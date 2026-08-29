import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, act, cleanup, screen, fireEvent } from '@testing-library/react';
import { GraphCanvas } from '../src/index';
import { resolveAttachedPosition } from '../src/utils/annotations';

// A live node store so onNodeDragStop's getFlowNodes()/setNodes read and
// write the same array, the same harness GraphCanvasUndo.test.jsx uses to
// exercise real drag wiring end to end.
const store = vi.hoisted(() => ({ nodes: [], edges: [], handlers: {} }));

// Mirrors ANNOTATION_TYPES (utils/annotations.js) — kept as its own literal
// set (rather than importing that module here) so this mock factory, which
// vitest hoists above other imports, never risks a temporal-dead-zone read.
const OVERLAY_NODE_TYPES = new Set([
  'note',
  'label',
  'arrow',
  'text',
  'shape',
  'icon',
  'vote_dot',
  'image',
  'freehand',
  'group',
]);

vi.mock('reactflow', () => {
  // Renders only annotation-kind nodes through the real `nodeTypes` map
  // GraphCanvas builds (GenericAnnotationNode, NoteNode, ArrowNode, …) —
  // exactly like real ReactFlow does — so an annotation's OWN context menu
  // (which every kind opens itself via a local `onContextMenu` handler,
  // rather than through the `onNodeContextMenu` prop this file's earlier
  // tests drive) actually mounts here. Needed for a live
  // annotation-typed-target test: an annotation-kind node early-returns out
  // of `onNodeContextMenu` (see GraphCanvas.jsx, "Annotation overlays render
  // their own context menu"), so its "Add nearby" section is unreachable
  // without this. Domain graph nodes (`custom`, schema types) are
  // deliberately left unrendered, matching every pre-existing test in this
  // file, which drives them only through the synthetic `onNodeContextMenu`
  // handler below — real domain node components need far more context
  // (schema, node colour resolvers, …) this harness does not set up, and
  // some carry no `data` at all here, which the real component would not
  // tolerate.
  const MockReactFlow = (props) => {
    store.handlers = props;
    return (
      <div data-testid="react-flow">
        {(props.nodes || [])
          .filter((n) => OVERLAY_NODE_TYPES.has(n.type))
          .map((n) => {
            const Component = props.nodeTypes?.[n.type];
            return Component ? (
              <Component key={n.id} id={n.id} type={n.type} data={n.data} selected={!!n.selected} />
            ) : null;
          })}
        {props.children}
      </div>
    );
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

  // The bug this whole file's earlier tests missed: every prior "Nearby
  // object menu" test here targets a graph node (`target_type: 'node'`).
  // Nothing exercised the OTHER half of `attachNearbyAnnotation`'s own type
  // check — `target_type: 'annotation'` — through a real, mounted annotation
  // context menu. That gap is exactly what let an arrow (also an annotation
  // node type) slip through the guard: it only excluded `frame`/`group`,
  // missing arrow's own exclusion from `findSnapTarget`. Attaching a new
  // label to an existing icon exercises the same `target_type: 'annotation'`
  // branch a fixed guard must still allow, right alongside arrow's own now
  // excluded case (see ArrowNode.test.jsx's "Nearby object menu" suite).
  it('attaches a new label to an existing icon annotation (target_type: "annotation"), and follows it', () => {
    const onAnnotationChange = vi.fn();
    const { rerender } = render(
      <GraphCanvas nodes={[]} edges={[]} onAnnotationChange={onAnnotationChange} />
    );

    const icon = {
      id: 'icon-1',
      type: 'icon',
      position: { x: 200, y: 200 },
      width: 0,
      height: 0,
      data: { icon: 'circle' },
    };
    store.nodes = [icon];
    // `store.nodes` is mutated directly (the same idiom the drag-to-attach
    // tests above use), which does not itself trigger a React re-render —
    // unlike those tests, this one needs the icon's OWN component actually
    // mounted (to right-click it), so force one: `rerender` re-invokes
    // `useNodesState()`, which reads the now-updated `store.nodes`.
    act(() => {
      rerender(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={onAnnotationChange} />);
    });

    act(() => {
      fireEvent.contextMenu(screen.getByTitle('circle'));
    });
    fireEvent.click(screen.getByRole('button', { name: '+ Label' }));

    const created = store.nodes.find((n) => n.type === 'label');
    expect(created).toBeTruthy();
    // Same `{target_id, target_type, offset}` triple computeDroppedAttachment
    // would produce for a drop near this same icon — `target_type` is
    // 'annotation' here because the target is itself an annotation kind, not
    // a graph node, per ANNOTATION_TYPES.has(target.type) in
    // attachNearbyAnnotation.
    expect(created.data.attachment).toEqual({
      target_id: 'icon-1',
      target_type: 'annotation',
      offset: { x: 36, y: -36 },
    });
    expect(created.position).toEqual({ x: 236, y: 164 });
    expect(created.data.locked).toBeFalsy();
    expect(onAnnotationChange).toHaveBeenCalledWith('create');

    // Follows the icon when it moves, via the exact same resolveAttachedPosition
    // the drag-to-attach and graph-node-target paths both use.
    const movedCenters = new Map([['icon-1', { x: 500, y: 500 }]]);
    expect(resolveAttachedPosition(created, movedCenters)).toEqual({ x: 536, y: 464 });
  });

  // The reproduced bug itself, at this same live-GraphCanvas level: an arrow
  // is an annotation node type too, so before this fix its own context menu
  // rendered the "Add nearby" section exactly like the icon case above —
  // creating an annotation attached to the arrow that could never follow it
  // (arrows are skipped when the attachment-follow effect's centre lookup is
  // built) and would silently detach on the next drag-triggered recompute
  // (arrows are also excluded from `findSnapTarget`'s own candidacy). Fixed
  // by excluding `arrow` from `attachNearbyAnnotation`'s guard and removing
  // the section from `ArrowNode`'s own menu entirely.
  it('never offers "Add nearby" on an arrow\'s own context menu', () => {
    const { rerender } = render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);

    const arrow = {
      id: 'arrow-1',
      type: 'arrow',
      position: { x: 0, y: 0 },
      data: { dx: 100, dy: 0 },
    };
    store.nodes = [arrow];
    act(() => {
      rerender(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    });

    act(() => {
      fireEvent.contextMenu(document.querySelector('.graph-arrow-node'));
    });

    expect(screen.queryByText('Add nearby')).toBeNull();
    expect(screen.queryByRole('button', { name: '+ Label' })).toBeNull();
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
