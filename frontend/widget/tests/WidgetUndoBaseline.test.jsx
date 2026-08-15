/**
 * Undo baseline of the widget's canvas.
 *
 * The widget replaces its whole node list on every search, so each search
 * establishes a new position baseline. Moves recorded against the layout the
 * user has left must not survive it — the node ids they name are often still on
 * the canvas, so an undo would otherwise teleport a node to a coordinate from
 * the previous search's layout.
 *
 * These tests drive the real GraphCanvas (reactflow mocked) through the widget,
 * so they cover the search → baseline → history wiring end to end rather than
 * the prop value alone.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act, cleanup, waitFor } from '@testing-library/react';
import Widget from '../src/Widget';
import * as mcp from '../src/mcpClient';

// A live node store so onNodeDragStop's getNodes() and applyPositionMoves'
// setNodes read and write the same array.
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
    useEdgesState: () => [
      store.edges,
      (updater) => {
        store.edges = typeof updater === 'function' ? updater(store.edges) : updater;
      },
      vi.fn(),
    ],
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
    MarkerType: { ArrowClosed: 'arrowclosed' },
    Position: { Top: 'top', Bottom: 'bottom', Left: 'left', Right: 'right' },
  };
});

vi.mock('@community-graph/ui-graph-canvas/styles', () => ({}));

vi.mock('../src/mcpClient', () => ({
  isMCPAvailable: vi.fn(),
  searchGraph: vi.fn(),
  getRelatedNodes: vi.fn(),
  getNodeDetails: vi.fn(),
  addNodes: vi.fn(),
  updateNode: vi.fn(),
  deleteNodes: vi.fn(),
}));

const NODE = { id: 'n1', name: 'Node 1', type: 'Actor' };

const nodeById = (id) => store.nodes.find((n) => n.id === id);

const search = async (query) => {
  fireEvent.change(screen.getByPlaceholderText('Search graph...'), { target: { value: query } });
  fireEvent.click(screen.getByRole('button', { name: 'Search' }));
  await waitFor(() => expect(mcp.searchGraph).toHaveBeenCalledWith(query));
  await waitFor(() => expect(nodeById('n1')).toBeDefined());
};

// Drag n1 from (30,40) to (100,50), leaving that move on the undo stack.
const dragNode = () => {
  const startNode = { id: 'n1', type: 'custom', position: { x: 30, y: 40 } };
  act(() => {
    store.nodes = [{ ...startNode, data: {} }];
    store.handlers.onNodeDragStart?.({}, startNode, [startNode]);
    store.nodes = [{ id: 'n1', type: 'custom', position: { x: 100, y: 50 }, data: {} }];
    const endNode = { id: 'n1', type: 'custom', position: { x: 100, y: 50 } };
    store.handlers.onNodeDragStop?.({}, endNode, [endNode]);
  });
};

const pressUndo = () => {
  act(() => {
    fireEvent.keyDown(document, { key: 'z', ctrlKey: true });
  });
};

describe('Widget canvas undo baseline', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    store.nodes = [];
    store.edges = [];
    store.handlers = {};
    mcp.isMCPAvailable.mockReturnValue(true);
  });

  afterEach(() => cleanup());

  it('discards the undo history when a search replaces the node list', async () => {
    mcp.searchGraph.mockResolvedValue({ nodes: [NODE], edges: [] });

    render(<Widget />);
    await search('first');
    dragNode();

    // A second search replaces the canvas contents. This node is in the new
    // result too, and lands on the coordinate that search's layout gives it.
    await search('second');
    act(() => {
      store.nodes = [{ id: 'n1', type: 'custom', position: { x: 800, y: 900 }, data: {} }];
    });

    pressUndo();

    expect(nodeById('n1').position).toEqual({ x: 800, y: 900 });
  });

  it('keeps the undo history when an expand adds nodes to the current layout', async () => {
    mcp.searchGraph.mockResolvedValue({ nodes: [NODE], edges: [] });
    mcp.getRelatedNodes.mockResolvedValue({
      nodes: [NODE, { id: 'n2', name: 'Node 2', type: 'Initiative' }],
      edges: [],
    });

    render(<Widget />);
    await search('first');
    const expandN1 = nodeById('n1').data.onExpand;
    dragNode();

    // Expand appends nodes and leaves the ones already placed alone, so the
    // recorded move still refers to the layout on screen.
    await act(async () => {
      expandN1();
    });
    await waitFor(() => expect(mcp.getRelatedNodes).toHaveBeenCalledWith('n1'));

    pressUndo();

    expect(nodeById('n1').position).toEqual({ x: 30, y: 40 });
  });
});
