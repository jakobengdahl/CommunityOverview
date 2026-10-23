import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { GraphCanvas } from '../src/index';
import { LONG_PRESS_DELAY_MS } from '../src/utils/longPress';

const hoisted = vi.hoisted(() => ({
  onNodesChange: vi.fn(),
  onEdgesChange: vi.fn(),
  selectionOnChange: null,
  selected: false,
}));

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
    panOnDrag,
  }) => (
    <div
      data-testid="react-flow"
      className="react-flow"
      data-pan-on-drag={JSON.stringify(panOnDrag)}
    >
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
      const nodes = (initialNodes || []).map((n) => ({
        ...n,
        selected: hoisted.selected && n.id === 'node-1',
      }));
      return [nodes, vi.fn(), hoisted.onNodesChange];
    },
    useEdgesState: (initialEdges) => [
      (initialEdges || []).map((e) => ({ ...e, selected: hoisted.selected })),
      vi.fn(),
      hoisted.onEdgesChange,
    ],
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

beforeEach(() => {
  vi.clearAllMocks();
  hoisted.selected = false;
});

afterEach(() => vi.useRealTimers());

describe('GraphCanvas right-drag pan does not open a context menu', () => {
  beforeEach(() => {
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
    ['5px down (at tolerance)', { moveByY: 5 }, true],
    ['5px up (at tolerance)', { moveByY: -5 }, true],
    ['5px diagonally (per-axis tolerance)', { moveBy: 5, moveByY: 5 }, true],
    ['6px diagonally', { moveBy: 6, moveByY: 6 }, false],
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

  it('resets after a gated contextmenu even without another press', () => {
    rightGesture('node-node-1', { moveBy: 40 });
    expect(menuOpen()).toBe(false);
    fireEvent.contextMenu(screen.getByTestId('node-node-1'), {
      button: 2,
      clientX: 300,
      clientY: 300,
    });
    expect(menuOpen()).toBe(true);
  });

  it('ignores left-button pane presses before a button-2 contextmenu', () => {
    const el = screen.getByTestId('node-node-1');
    fireEvent.mouseDown(screen.getByTestId('pane'), { ...rightDown, button: 0 });
    fireEvent.contextMenu(el, { button: 2, clientX: 300, clientY: 300 });
    expect(menuOpen()).toBe(true);
  });

  it.each([0, 1])('a button-%s press disarms an earlier right-press', (button) => {
    const pane = screen.getByTestId('pane');
    fireEvent.mouseDown(pane, rightDown);
    fireEvent.mouseDown(pane, { ...rightDown, button });
    fireEvent.contextMenu(screen.getByTestId('node-node-1'), {
      button: 2,
      clientX: 300,
      clientY: 300,
    });
    expect(menuOpen()).toBe(true);
  });

  it('does not let a stale right-press suppress a later non-right contextmenu', () => {
    fireEvent.mouseDown(screen.getByTestId('pane'), rightDown);
    const node = screen.getByTestId('node-node-1');
    // Ctrl+click / the keyboard menu key: contextmenu with no preceding right press.
    fireEvent.contextMenu(node, { button: 0, clientX: 600, clientY: 400 });
    expect(menuOpen()).toBe(true);
  });

  it('allows a pen long-press after an armed right-press', () => {
    vi.useFakeTimers();
    const pane = screen.getByTestId('pane');
    fireEvent.mouseDown(pane, rightDown);
    // jsdom has no PointerEvent; supply the fields read by the detector.
    const press = new MouseEvent('pointerdown', {
      bubbles: true,
      clientX: 300,
      clientY: 300,
    });
    Object.defineProperties(press, {
      pointerType: { value: 'pen' },
      pointerId: { value: 1 },
    });
    fireEvent(pane, press);
    act(() => vi.advanceTimersByTime(LONG_PRESS_DELAY_MS));
    expect(document.querySelector('.pane-context-menu')).not.toBeNull();
  });
});

describe('GraphCanvas right-drag gate follows what can actually pan', () => {
  it('enables right-button panning on desktop', () => {
    render(<GraphCanvas nodes={nodes} edges={edges} touchMode="off" />);
    expect(screen.getByTestId('react-flow')).toHaveAttribute('data-pan-on-drag', '[0,2]');
  });

  it('does not suppress menus in touch mode, where the right button never pans', () => {
    render(<GraphCanvas nodes={nodes} edges={edges} touchMode="on" />);
    rightGesture('node-node-1', { moveBy: 40 });
    expect(menuOpen()).toBe(true);
  });

  it.each(['Freehand', 'Eraser'])('does not gate drift while %s disables panning', (tool) => {
    render(<GraphCanvas nodes={nodes} edges={edges} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: tool, exact: true }));
    expect(screen.getByTestId('react-flow')).toHaveAttribute('data-pan-on-drag', 'false');
    rightGesture('node-node-1', { moveBy: 40 });
    expect(menuOpen()).toBe(true);
  });

  it.each([
    ['Shift', { shiftKey: true }],
    ['Control', { ctrlKey: true }],
    ['Space', {}],
  ])('does not gate drift when %s prevents right-button panning', (code, modifiers) => {
    render(<GraphCanvas nodes={nodes} edges={edges} />);
    fireEvent.keyDown(document, { key: code === 'Space' ? ' ' : code, code, ...modifiers });
    fireEvent.mouseDown(screen.getByTestId('pane'), { ...rightDown, ...modifiers });
    fireEvent.contextMenu(screen.getByTestId('node-node-1'), {
      button: 2,
      clientX: 300,
      clientY: 300,
      ...modifiers,
    });
    expect(menuOpen()).toBe(true);
    fireEvent.keyUp(document, { key: code === 'Space' ? ' ' : code, code });
  });

  it.each(['keyup', 'blur'])('gates right-drag again after Space is cleared by %s', (reset) => {
    render(<GraphCanvas nodes={nodes} edges={edges} />);
    fireEvent.keyDown(document, { key: ' ', code: 'Space' });
    if (reset === 'keyup') fireEvent.keyUp(document, { key: ' ', code: 'Space' });
    else fireEvent.blur(window);
    rightGesture('node-node-1', { moveBy: 40 });
    expect(menuOpen()).toBe(false);
  });

  it('clears selected nodes and edges after a moved pane press', () => {
    hoisted.selected = true;
    render(<GraphCanvas nodes={nodes} edges={edges} />);
    rightGesture('pane', { moveBy: 40 });
    expect(menuOpen()).toBe(false);
    expect(hoisted.onNodesChange).toHaveBeenCalledTimes(1);
    expect(hoisted.onNodesChange).toHaveBeenCalledWith([
      { id: 'node-1', type: 'select', selected: false },
    ]);
    expect(hoisted.onEdgesChange).toHaveBeenCalledTimes(1);
    expect(hoisted.onEdgesChange).toHaveBeenCalledWith([
      { id: 'edge-1', type: 'select', selected: false },
    ]);
  });
});
