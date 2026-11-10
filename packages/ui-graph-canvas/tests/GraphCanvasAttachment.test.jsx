import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, act, cleanup, screen, fireEvent } from '@testing-library/react';
import { GraphCanvas } from '../src/index';
import { resolveAttachedPosition } from '../src/utils/annotations';

// A live node store so onNodeDragStop's getFlowNodes()/setNodes read and
// write the same array, the same harness GraphCanvasUndo.test.jsx uses to
// exercise real drag wiring end to end.
const store = vi.hoisted(() => ({
  nodes: [],
  edges: [],
  handlers: {},
}));

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

vi.mock('reactflow', async () => {
  // Renders only annotation-kind nodes through the real `nodeTypes` map
  // GraphCanvas builds (GenericAnnotationNode, NoteNode, ArrowNode, …) —
  // exactly like real ReactFlow does — so an annotation's OWN context menu
  // (which every kind opens itself via a local `onContextMenu` handler,
  // rather than through the `onNodeContextMenu` prop this file's earlier
  // tests drive) actually mounts here. Domain graph nodes (`custom`, schema
  // types) are deliberately left unrendered, matching every pre-existing
  // test in this file, which drives them only through the synthetic
  // `onNodeContextMenu` handler below — real domain node components need
  // far more context (schema, node colour resolvers, …) this harness does
  // not set up, and some carry no `data` at all here, which the real
  // component would not tolerate.
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
      getEdges: () => store.edges,
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

const resetStore = () => {
  store.nodes = [];
  store.edges = [];
  store.handlers = {};
};

// task-annotation-render-direct-manipulation: dragging a label/text/icon
// near a node (re)attaches it; dragging one outside every snap zone
// detaches it and keeps the dropped position (docs/ANNOTATION_CONTRACT.md's
// "Attachment and detach behavior"). `vote_dot` used to be a fourth member
// of this list; task-annotation-vote-dot-simplify retired it — see this
// describe block's own dedicated "never attaches or re-attaches a dropped
// vote_dot" test below. The follow-while-attached side of this
// (an attached overlay tracking its target's movement) is covered at the
// pure-function level by resolveAttachedPosition in overlaySerialization.test.js,
// the same split the pre-existing arrow-anchor effect uses.
describe('GraphCanvas attachment: drag-to-attach/detach', () => {
  beforeEach(() => resetStore());
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
    const icon = {
      id: 'icon-1',
      type: 'icon',
      position: { x: 5, y: 5 },
      data: {
        attachment: { target_id: 'node-far', target_type: 'node', offset: { x: 505, y: 505 } },
      },
    };
    store.nodes = [far, near, icon];

    act(() => {
      store.handlers.onNodeDragStop?.({}, icon, [icon]);
    });

    expect(nodeById('icon-1').data.attachment).toEqual({
      target_id: 'node-near',
      target_type: 'node',
      offset: { x: 5, y: 5 },
    });
  });

  // Explicit regression for task-annotation-vote-dot-simplify: dropping a
  // vote_dot near a node used to (re)attach it exactly like the icon case
  // above (`vote_dot` was a member of ATTACHABLE_OVERLAY_KINDS). It no longer
  // is, so this same drag-stop handler must leave a vote_dot's geometry and
  // any stored `attachment` completely alone — not cleared, not re-targeted,
  // just ignored — whether the drop lands near a target or not.
  it('never attaches or re-attaches a dropped vote_dot, even one carrying a stale attachment', () => {
    const onAnnotationChange = vi.fn();
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={onAnnotationChange} />);

    const near = { id: 'node-near', type: 'custom', position: { x: 0, y: 0 }, width: 0, height: 0 };
    const staleAttachment = {
      target_id: 'node-far',
      target_type: 'node',
      offset: { x: 505, y: 505 },
    };
    const dot = {
      id: 'dot-1',
      type: 'vote_dot',
      position: { x: 5, y: 5 },
      data: { attachment: staleAttachment },
    };
    store.nodes = [near, dot];

    act(() => {
      store.handlers.onNodeDragStop?.({}, dot, [dot]);
    });

    // Dropped right on top of `near` — an icon/label here would have just
    // (re)attached to it (see the test above). A vote_dot does neither: its
    // stale attachment survives untouched rather than being cleared or
    // pointed at the new nearby target.
    expect(nodeById('dot-1').data.attachment).toEqual(staleAttachment);
    expect(onAnnotationChange).not.toHaveBeenCalled();
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
// The drag-to-attach path (computeDroppedAttachment) is tested above.
// The now-removed "Nearby object menu" used to provide an alternative
// creation entry point; it is no longer in scope here.
// Exercised through the real GraphCanvas node-context-menu render
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

// dec-annotation-lock-semantics point 1: `locked` freezes ALL geometry
// change, including the follow effects that resolve a binding's geometry —
// not only user-initiated edits. These mount with the bound target already
// far from the overlay's stored geometry, so an unfixed effect (which
// resolves on every `nodes` change, mount included) would visibly move it.
describe('GraphCanvas attachment/anchor: locked annotations freeze geometry', () => {
  beforeEach(() => resetStore());
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

// Explicit regression for task-annotation-vote-dot-simplify: the "keep an
// attached overlay glued to its target" follow effect
// (GraphCanvas.jsx) is gated on ATTACHABLE_OVERLAY_KINDS, which no longer
// includes `vote_dot`. A vote_dot carrying a stale, stored `attachment` (no
// migration was written) must not be resolved against it — unlike the
// locked-label case above, this one is not even locked, so if the guard
// were keyed on anything other than the kind itself, this would be the case
// that slips through and silently starts following again.
describe('GraphCanvas attachment: a vote_dot never follows a stale attachment', () => {
  beforeEach(() => resetStore());
  afterEach(() => cleanup());

  it('does not move an unlocked vote_dot toward a target named by its stale attachment', () => {
    const target = {
      id: 'node-1',
      type: 'custom',
      position: { x: 900, y: 900 },
      width: 40,
      height: 40,
    };
    const dot = {
      id: 'dot-1',
      type: 'vote_dot',
      position: { x: 0, y: 0 },
      data: {
        attachment: { target_id: 'node-1', target_type: 'node', offset: { x: 0, y: 0 } },
      },
    };
    store.nodes = [target, dot];

    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);

    // An attached label/icon at this same offset would have resolved to the
    // target's centre by now; the vote_dot must stay exactly where it was
    // declared, not throw, and not crash the canvas.
    expect(nodeById('dot-1').position).toEqual({ x: 0, y: 0 });
    expect(screen.getByTestId('react-flow')).toBeInTheDocument();
  });
});
