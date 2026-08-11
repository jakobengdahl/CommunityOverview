import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent, act, cleanup } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

// A live node store so onNodeDragStop's getFlowNodes() and applyPositionMoves'
// setNodes read/write the same array — enough to exercise the real drag →
// keyboard-undo → persist wiring end to end.
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

function setLiveNode(position) {
  store.nodes = [{ id: 'node-1', type: 'custom', position, data: { label: 'Node 1' } }];
}

describe('GraphCanvas undo/redo of node moves', () => {
  beforeEach(() => {
    store.nodes = [];
    store.handlers = {};
  });
  afterEach(() => cleanup());

  it('drag then Ctrl+Z restores the prior position and persists it; Ctrl+Shift+Z redoes', () => {
    const onNodePositionChange = vi.fn();
    render(
      <GraphCanvas nodes={inputNodes} edges={[]} onNodePositionChange={onNodePositionChange} />
    );

    // Simulate a drag from (0,0) to (100,50).
    const startNode = { id: 'node-1', type: 'custom', position: { x: 0, y: 0 } };
    act(() => {
      store.handlers.onNodeDragStart?.({}, startNode, [startNode]);
      setLiveNode({ x: 100, y: 50 });
      const endNode = { id: 'node-1', type: 'custom', position: { x: 100, y: 50 } };
      store.handlers.onNodeDragStop?.({}, endNode, [endNode]);
    });
    onNodePositionChange.mockClear();

    // Undo: prior position persisted through the same callback a move uses.
    act(() => {
      fireEvent.keyDown(document, { key: 'z', ctrlKey: true });
    });
    expect(onNodePositionChange).toHaveBeenCalledWith('node-1', { x: 0, y: 0 });

    onNodePositionChange.mockClear();

    // Redo: new position reapplied and persisted.
    act(() => {
      fireEvent.keyDown(document, { key: 'z', ctrlKey: true, shiftKey: true });
    });
    expect(onNodePositionChange).toHaveBeenCalledWith('node-1', { x: 100, y: 50 });
  });

  it('Ctrl+Z is a no-op when there is nothing to undo', () => {
    const onNodePositionChange = vi.fn();
    render(
      <GraphCanvas nodes={inputNodes} edges={[]} onNodePositionChange={onNodePositionChange} />
    );
    setLiveNode({ x: 10, y: 10 });
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
      setLiveNode({ x: 20, y: 20 });
      store.handlers.onNodeDragStop?.({}, node, [node]);
    });
    onNodePositionChange.mockClear();
    act(() => {
      fireEvent.keyDown(document, { key: 'z', ctrlKey: true });
    });
    expect(onNodePositionChange).not.toHaveBeenCalled();
  });
});
