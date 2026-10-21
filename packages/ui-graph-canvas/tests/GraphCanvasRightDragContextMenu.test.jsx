import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

const hoisted = vi.hoisted(() => ({ onNodesChange: null, selectionOnChange: null }));

// d3-zoom/d3-drag stop a mousedown from bubbling in real ReactFlow, so the mock does too.
const stopBubble = (event) => event.stopPropagation();

vi.mock('reactflow', () => {
  const MockReactFlow = ({
    children,
    nodes,
    edges,
    onEdgeContextMenu,
    onNodeContextMenu,
    onPaneContextMenu,
    onSelectionContextMenu,
  }) => (
    <div data-testid="react-flow" className="react-flow">
      <div
        data-testid="pane"
        onMouseDown={stopBubble}
        onContextMenu={(event) => onPaneContextMenu?.(event)}
      />
      <div
        data-testid="selection"
        className="nopan"
        onMouseDown={stopBubble}
        onContextMenu={(event) => onSelectionContextMenu?.(event)}
      />
      <div data-testid="nodes">
        {nodes?.map((node) => (
          <div
            key={node.id}
            data-testid={`node-${node.id}`}
            className="nopan"
            onMouseDown={stopBubble}
            onContextMenu={(event) => onNodeContextMenu?.(event, node)}
          >
            {node.data?.label}
          </div>
        ))}
      </div>
      <div data-testid="edges">
        {edges?.map((edge) => (
          <div
            key={edge.id}
            data-testid={`edge-${edge.id}`}
            onMouseDown={stopBubble}
            className={`nopan react-flow__edge${edge.className ? ` ${edge.className}` : ''}`}
            data-animated={String(!!edge.animated)}
            data-stroke={edge.style?.stroke ?? ''}
            data-stroke-width={String(edge.style?.strokeWidth ?? '')}
            data-stroke-opacity={String(edge.style?.strokeOpacity ?? '')}
            data-edge-opacity-var={String(edge.style?.['--edge-opacity'] ?? '')}
            data-marker-end={edge.markerEnd ? String(edge.markerEnd.type) : ''}
            data-marker-start={edge.markerStart ? String(edge.markerStart.type) : ''}
            onContextMenu={(event) => onEdgeContextMenu?.(event, edge)}
          >
            {edge.label || edge.type}
          </div>
        ))}
      </div>
      {children}
    </div>
  );

  return {
    default: MockReactFlow,
    ReactFlow: MockReactFlow,
    ReactFlowProvider: ({ children }) => <div>{children}</div>,
    useNodesState: (initialNodes) => {
      if (!hoisted.onNodesChange) hoisted.onNodesChange = vi.fn();
      return [initialNodes || [], vi.fn(), hoisted.onNodesChange];
    },
    useEdgesState: (initialEdges) => [initialEdges || [], vi.fn(), vi.fn()],
    useReactFlow: () => ({
      fitView: vi.fn(),
      getNodes: () => [],
      getEdges: () => [],
      setNodes: vi.fn(),
      setEdges: vi.fn(),
      screenToFlowPosition: () => ({ x: 0, y: 0 }),
      setCenter: vi.fn(),
    }),
    useOnSelectionChange: ({ onChange }) => {
      hoisted.selectionOnChange = onChange;
    },
    Background: () => <div data-testid="background" />,
    Controls: () => <div data-testid="controls" />,
    MiniMap: () => <div data-testid="minimap" />,
    SelectionMode: { Partial: 'partial' },
    Handle: ({ type }) => <div data-testid={`handle-${type}`} />,
    Position: { Top: 'top', Bottom: 'bottom', Left: 'left', Right: 'right' },
    MarkerType: { ArrowClosed: 'arrowclosed', Arrow: 'arrow' },
  };
});

const nodes = [
  { id: 'node-1', name: 'Node 1', type: 'Actor', description: 'a' },
  { id: 'node-2', name: 'Node 2', type: 'Initiative', description: 'b' },
];
const edges = [{ id: 'edge-1', source: 'node-1', target: 'node-2', type: 'RELATES_TO' }];

const rightDown = { button: 2, clientX: 100, clientY: 100 };

// A pan can only start on the pane: nodes, edges and the selection box carry
// `nopan`, so by default the press lands on the pane and the release on `testId`.
function rightGesture(testId, { moveBy = 0, moveByY = 0, pressOn = 'pane' } = {}) {
  const el = screen.getByTestId(testId);
  fireEvent.mouseDown(screen.getByTestId(pressOn), rightDown);
  fireEvent.contextMenu(el, { button: 2, clientX: 100 + moveBy, clientY: 100 + moveByY });
}

// Every menu variant (node, edge, pane, selection) renders .graph-context-menu.
const menuOpen = () => document.querySelector('.graph-context-menu') !== null;

describe('GraphCanvas right-drag pan does not open a context menu', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    render(<GraphCanvas nodes={nodes} edges={edges} />);
    act(() =>
      hoisted.selectionOnChange?.({ nodes: [{ id: 'node-1', type: 'custom' }], edges: [] })
    );
  });

  it.each([['pane'], ['node-node-1'], ['edge-edge-1'], ['selection']])(
    'opens the menu on a right-click without movement (%s)',
    (testId) => {
      rightGesture(testId);
      expect(menuOpen()).toBe(true);
    }
  );

  it.each([['pane'], ['node-node-1'], ['edge-edge-1'], ['selection']])(
    'opens no menu when the pointer moved past the tolerance before release (%s)',
    (testId) => {
      rightGesture(testId, { moveBy: 40 });
      expect(menuOpen()).toBe(false);
    }
  );

  it.each([
    ['5px right (at tolerance)', { moveBy: 5 }, true],
    ['6px right', { moveBy: 6 }, false],
    ['6px left', { moveBy: -6 }, false],
    ['6px down', { moveByY: 6 }, false],
    ['6px up', { moveByY: -6 }, false],
  ])('applies the tolerance in every direction (%s)', (_label, move, opens) => {
    rightGesture('node-node-1', move);
    expect(menuOpen()).toBe(opens);
  });

  it.each([['node-node-1'], ['edge-edge-1'], ['selection']])(
    'still opens the menu when a press on a nopan element drifts, since nothing panned (%s)',
    (testId) => {
      rightGesture(testId, { moveBy: 40, pressOn: testId });
      expect(menuOpen()).toBe(true);
    }
  );

  it('does not let a finished drag suppress the next plain right-click', () => {
    rightGesture('node-node-1', { moveBy: 40 });
    rightGesture('node-node-1', { pressOn: 'node-node-1' });
    expect(menuOpen()).toBe(true);
  });

  it('ignores left-button presses when deciding whether a drag happened', () => {
    const el = screen.getByTestId('node-node-1');
    fireEvent.mouseDown(el, { button: 0, clientX: 100, clientY: 100 });
    fireEvent.contextMenu(el, { clientX: 300, clientY: 300 });
    expect(menuOpen()).toBe(true);
  });

  it('does not let a stale right-press suppress a later non-right contextmenu', () => {
    fireEvent.mouseDown(screen.getByTestId('pane'), rightDown);
    const node = screen.getByTestId('node-node-1');
    // Ctrl+click / the keyboard menu key: contextmenu with no preceding right press.
    fireEvent.contextMenu(node, { button: 0, clientX: 600, clientY: 400 });
    expect(menuOpen()).toBe(true);
  });
});
