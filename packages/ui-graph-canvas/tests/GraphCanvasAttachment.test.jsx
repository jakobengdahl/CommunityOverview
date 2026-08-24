import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, act, cleanup } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

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
