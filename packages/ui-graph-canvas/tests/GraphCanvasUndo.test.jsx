import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent, act, cleanup } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

// A live node store so onNodeDragStop's getFlowNodes() and applyPositionMoves'
// setNodes read/write the same array — enough to exercise the real drag →
// keyboard-undo → persist wiring (including group re-parenting) end to end.
const store = vi.hoisted(() => ({ nodes: [], handlers: {} }));

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
    useEdgesState: () => [[], vi.fn(), vi.fn()],
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

const inputNodes = [{ id: 'node-1', name: 'Node 1', type: 'Actor', description: 'a' }];

const nodeById = (id) => store.nodes.find((n) => n.id === id);

describe('GraphCanvas undo/redo of node moves', () => {
  beforeEach(() => {
    store.nodes = [];
    store.handlers = {};
  });
  afterEach(() => cleanup());

  it('drag then Ctrl+Z restores the prior position (in the store) and persists it; Ctrl+Shift+Z redoes', () => {
    const onNodePositionChange = vi.fn();
    render(
      <GraphCanvas nodes={inputNodes} edges={[]} onNodePositionChange={onNodePositionChange} />
    );

    // Simulate a drag from (30,40) to (100,50). Non-origin start so the
    // input-sync effect preserves it across the re-render (it only re-lays-out
    // nodes still parked at x===0).
    const startNode = { id: 'node-1', type: 'custom', position: { x: 30, y: 40 } };
    act(() => {
      store.handlers.onNodeDragStart?.({}, startNode, [startNode]);
      store.nodes = [{ id: 'node-1', type: 'custom', position: { x: 100, y: 50 }, data: {} }];
      const endNode = { id: 'node-1', type: 'custom', position: { x: 100, y: 50 } };
      store.handlers.onNodeDragStop?.({}, endNode, [endNode]);
    });
    onNodePositionChange.mockClear();

    // Undo: prior position both persisted (callback) and applied (store).
    act(() => {
      fireEvent.keyDown(document, { key: 'z', ctrlKey: true });
    });
    expect(onNodePositionChange).toHaveBeenCalledWith('node-1', { x: 30, y: 40 });
    expect(nodeById('node-1').position).toEqual({ x: 30, y: 40 });

    onNodePositionChange.mockClear();

    // Redo: new position reapplied and persisted.
    act(() => {
      fireEvent.keyDown(document, { key: 'z', ctrlKey: true, shiftKey: true });
    });
    expect(onNodePositionChange).toHaveBeenCalledWith('node-1', { x: 100, y: 50 });
    expect(nodeById('node-1').position).toEqual({ x: 100, y: 50 });
  });

  it('undo of a drag that entered a group restores both the parent and the coordinate space', () => {
    const onNodePositionChange = vi.fn();
    render(
      <GraphCanvas nodes={inputNodes} edges={[]} onNodePositionChange={onNodePositionChange} />
    );

    const group = {
      id: 'g1',
      type: 'group',
      position: { x: 0, y: 0 },
      style: { width: 400, height: 400 },
      data: { label: 'G' },
    };
    // Drag node-1 from absolute (500,500) — outside the group — to (100,100),
    // inside the group's bounds, so onNodeDragStop re-parents it.
    const startNode = {
      id: 'node-1',
      type: 'custom',
      position: { x: 500, y: 500 },
      parentId: undefined,
    };
    act(() => {
      store.nodes = [group, startNode];
      store.handlers.onNodeDragStart?.({}, startNode, [startNode]);
      store.nodes = [
        group,
        {
          id: 'node-1',
          type: 'custom',
          position: { x: 100, y: 100 },
          parentId: undefined,
          data: {},
        },
      ];
      const endNode = { id: 'node-1', type: 'custom', position: { x: 100, y: 100 } };
      store.handlers.onNodeDragStop?.({}, endNode, [endNode]);
    });

    // After the drag the node is a child of the group at a parent-relative pos.
    expect(nodeById('node-1').parentId).toBe('g1');
    expect(nodeById('node-1').position).toEqual({ x: 100, y: 100 });

    onNodePositionChange.mockClear();

    // Undo: the node returns to its absolute pre-drag position AND leaves the
    // group. (The bug this guards: restoring position without parentId would
    // render the node relative to the group — far from where it started.)
    act(() => {
      fireEvent.keyDown(document, { key: 'z', ctrlKey: true });
    });
    expect(onNodePositionChange).toHaveBeenCalledWith('node-1', { x: 500, y: 500 });
    expect(nodeById('node-1').parentId).toBeUndefined();
    expect(nodeById('node-1').position).toEqual({ x: 500, y: 500 });
  });

  it('Ctrl+Z is a no-op when there is nothing to undo', () => {
    const onNodePositionChange = vi.fn();
    render(
      <GraphCanvas nodes={inputNodes} edges={[]} onNodePositionChange={onNodePositionChange} />
    );
    store.nodes = [{ id: 'node-1', type: 'custom', position: { x: 10, y: 10 }, data: {} }];
    onNodePositionChange.mockClear();
    act(() => {
      fireEvent.keyDown(document, { key: 'z', ctrlKey: true });
    });
    expect(onNodePositionChange).not.toHaveBeenCalled();
  });

  it('a drag that does not move the node records nothing to undo', () => {
    const onNodePositionChange = vi.fn();
    render(
      <GraphCanvas nodes={inputNodes} edges={[]} onNodePositionChange={onNodePositionChange} />
    );
    const node = { id: 'node-1', type: 'custom', position: { x: 20, y: 20 } };
    act(() => {
      store.handlers.onNodeDragStart?.({}, node, [node]);
      store.nodes = [{ id: 'node-1', type: 'custom', position: { x: 20, y: 20 }, data: {} }];
      store.handlers.onNodeDragStop?.({}, node, [node]);
    });
    onNodePositionChange.mockClear();
    act(() => {
      fireEvent.keyDown(document, { key: 'z', ctrlKey: true });
    });
    expect(onNodePositionChange).not.toHaveBeenCalled();
  });
});
