// End-to-end coverage for wiring createFreehandStrokeCapture into an actual
// canvas drawing mode reachable from the annotation toolbox (remaining_scope
// item 1 on task-annotation-freehand-vector-drawing): arming the tool,
// drawing a stroke with real pointer events (including coalesced samples and
// device pressure), the constant-width fallback for pressure-less input, and
// the concurrent-touch-input guidance (item 4).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, act } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

const store = vi.hoisted(() => ({ handlers: {} }));

vi.mock('reactflow', () => {
  const MockReactFlow = (props) => {
    store.handlers = props;
    return (
      <div data-testid="react-flow" className="react-flow">
        {props.children}
      </div>
    );
  };
  return {
    __esModule: true,
    default: MockReactFlow,
    ReactFlow: MockReactFlow,
    ReactFlowProvider: ({ children }) => <div>{children}</div>,
    useNodesState: (initial) => [
      initial || [],
      (updater) => {
        store.nodes = typeof updater === 'function' ? updater(store.nodes || []) : updater;
      },
      vi.fn(),
    ],
    useEdgesState: (initial) => [initial || [], vi.fn(), vi.fn()],
    useReactFlow: () => ({
      fitView: vi.fn(),
      zoomIn: vi.fn(),
      zoomOut: vi.fn(),
      getNodes: () => store.nodes || [],
      getEdges: () => [],
      setNodes: (updater) => {
        store.nodes = typeof updater === 'function' ? updater(store.nodes || []) : updater;
      },
      setEdges: vi.fn(),
      screenToFlowPosition: ({ x, y }) => ({ x, y }),
      setCenter: vi.fn(),
      getViewport: () => ({ x: 0, y: 0, zoom: 1 }),
    }),
    useOnSelectionChange: () => {},
    Background: () => <div data-testid="background" />,
    Controls: () => <div data-testid="controls" />,
    MiniMap: () => <div data-testid="minimap" />,
    NodeResizer: () => null,
    SelectionMode: { Partial: 'partial' },
  };
});

// jsdom has no PointerEvent constructor — see GraphCanvasTouch.test.jsx's
// identical helper. Extended here with `pressure` and `coalesced` (a plain
// array of {x, y, pressure} used to synthesize getCoalescedEvents()).
function pointerEvent(
  type,
  {
    pointerId = 1,
    pointerType = 'mouse',
    clientX = 0,
    clientY = 0,
    pressure,
    button = 0,
    coalesced,
  } = {}
) {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX, clientY, button });
  Object.defineProperty(event, 'pointerId', { value: pointerId });
  Object.defineProperty(event, 'pointerType', { value: pointerType });
  if (pressure !== undefined) Object.defineProperty(event, 'pressure', { value: pressure });
  if (coalesced) {
    Object.defineProperty(event, 'getCoalescedEvents', {
      value: () =>
        coalesced.map((c) =>
          pointerEvent('pointermove', {
            pointerId,
            pointerType,
            clientX: c.x,
            clientY: c.y,
            pressure: c.pressure,
          })
        ),
    });
  }
  return event;
}

function findCreatedFreehandNode() {
  return (store.nodes || []).find((n) => n.type === 'freehand');
}

describe('GraphCanvas freehand drawing mode', () => {
  beforeEach(() => {
    store.nodes = [];
    store.handlers = {};
  });
  afterEach(() => cleanup());

  it('arms drawing mode from the toolbox and disables panning/marquee-selection/node-dragging while active', () => {
    render(<GraphCanvas nodes={[]} edges={[]} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    expect(store.handlers.panOnDrag).toEqual([0, 2]);

    fireEvent.click(screen.getByRole('button', { name: /^freehand$/i }));
    expect(store.handlers.panOnDrag).toBe(false);
    expect(store.handlers.selectionOnDrag).toBe(false);
    expect(store.handlers.nodesDraggable).toBe(false);
    expect(screen.getByRole('button', { name: /^freehand$/i })).toHaveAttribute(
      'aria-pressed',
      'true'
    );
  });

  it('draws a stroke end to end and creates a freehand annotation anchored at the first point', () => {
    const onAnnotationChange = vi.fn();
    const { container } = render(
      <GraphCanvas nodes={[]} edges={[]} onAnnotationChange={onAnnotationChange} />
    );
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^freehand$/i }));

    const rf = container.querySelector('[data-testid="react-flow"]');
    act(() => {
      rf.dispatchEvent(pointerEvent('pointerdown', { clientX: 10, clientY: 10 }));
      rf.dispatchEvent(pointerEvent('pointermove', { clientX: 20, clientY: 10 }));
      rf.dispatchEvent(pointerEvent('pointermove', { clientX: 30, clientY: 10 }));
      rf.dispatchEvent(pointerEvent('pointerup', { clientX: 30, clientY: 10 }));
    });

    const node = findCreatedFreehandNode();
    expect(node).toBeTruthy();
    expect(node.position).toEqual({ x: 10, y: 10 });
    // node-relative to the anchor (first point) — see annotations.js's
    // GENERIC_OVERLAY_FIELDS comment on freehand's `points` convention.
    expect(node.data.points).toEqual([
      { x: 0, y: 0 },
      { x: 10, y: 0 },
      { x: 20, y: 0 },
    ]);
    expect(onAnnotationChange).toHaveBeenCalledWith('create');
  });

  it('auto-disarms drawing mode after one stroke (single-shot tool)', () => {
    const { container } = render(<GraphCanvas nodes={[]} edges={[]} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^freehand$/i }));
    const rf = container.querySelector('[data-testid="react-flow"]');
    act(() => {
      rf.dispatchEvent(pointerEvent('pointerdown', { clientX: 0, clientY: 0 }));
      rf.dispatchEvent(pointerEvent('pointermove', { clientX: 5, clientY: 5 }));
      rf.dispatchEvent(pointerEvent('pointerup', { clientX: 5, clientY: 5 }));
    });

    expect(store.handlers.nodesDraggable).toBe(true);
    expect(screen.getByRole('button', { name: /^freehand$/i })).toHaveAttribute(
      'aria-pressed',
      'false'
    );
  });

  it('feeds each coalesced sample as its own point', () => {
    const { container } = render(<GraphCanvas nodes={[]} edges={[]} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^freehand$/i }));
    const rf = container.querySelector('[data-testid="react-flow"]');

    act(() => {
      rf.dispatchEvent(pointerEvent('pointerdown', { clientX: 0, clientY: 0 }));
      rf.dispatchEvent(
        pointerEvent('pointermove', {
          clientX: 30,
          clientY: 0,
          coalesced: [
            { x: 10, y: 0 },
            { x: 20, y: 0 },
            { x: 30, y: 0 },
          ],
        })
      );
      rf.dispatchEvent(pointerEvent('pointerup', { clientX: 30, clientY: 0 }));
    });

    const node = findCreatedFreehandNode();
    expect(node.data.points).toEqual([
      { x: 0, y: 0 },
      { x: 10, y: 0 },
      { x: 20, y: 0 },
      { x: 30, y: 0 },
    ]);
  });

  it('captures device pressure onto persisted points and marks pressureSource as device', () => {
    const { container } = render(<GraphCanvas nodes={[]} edges={[]} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^freehand$/i }));
    const rf = container.querySelector('[data-testid="react-flow"]');

    act(() => {
      rf.dispatchEvent(
        pointerEvent('pointerdown', { pointerType: 'pen', clientX: 0, clientY: 0, pressure: 0.3 })
      );
      rf.dispatchEvent(
        pointerEvent('pointermove', { pointerType: 'pen', clientX: 10, clientY: 0, pressure: 0.9 })
      );
      rf.dispatchEvent(pointerEvent('pointerup', { pointerType: 'pen', clientX: 10, clientY: 0 }));
    });

    const node = findCreatedFreehandNode();
    expect(node.data.points[0].pressure).toBe(0.3);
    expect(node.data.points[1].pressure).toBe(0.9);
    expect(node.data.pressureSource).toBe('device');
    expect(node.data.pointerType).toBe('pen');
  });

  it('falls back to no pressureSource for a pressure-less device (mouse/touch/pressure-less pen)', () => {
    const { container } = render(<GraphCanvas nodes={[]} edges={[]} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^freehand$/i }));
    const rf = container.querySelector('[data-testid="react-flow"]');

    act(() => {
      rf.dispatchEvent(
        pointerEvent('pointerdown', { pointerType: 'touch', clientX: 0, clientY: 0 })
      );
      rf.dispatchEvent(
        pointerEvent('pointermove', { pointerType: 'touch', clientX: 10, clientY: 0 })
      );
      rf.dispatchEvent(
        pointerEvent('pointerup', { pointerType: 'touch', clientX: 10, clientY: 0 })
      );
    });

    const node = findCreatedFreehandNode();
    expect(node.data.pressureSource).toBeUndefined();
    expect(node.data.points.every((p) => !('pressure' in p))).toBe(true);
    // Constant-width fallback: the created stroke keeps the toolbox's default
    // width rather than inventing a velocity-derived one.
    expect(node.data.strokeWidth).toBe(2);
  });

  it('discards a stray tap (below minPoints) without creating an annotation', () => {
    const { container } = render(<GraphCanvas nodes={[]} edges={[]} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^freehand$/i }));
    const rf = container.querySelector('[data-testid="react-flow"]');
    act(() => {
      rf.dispatchEvent(pointerEvent('pointerdown', { clientX: 5, clientY: 5 }));
      rf.dispatchEvent(pointerEvent('pointerup', { clientX: 5, clientY: 5 }));
    });

    expect(findCreatedFreehandNode()).toBeUndefined();
  });

  it('cancels an in-progress stroke on Escape and disarms the tool without creating anything', () => {
    const { container } = render(<GraphCanvas nodes={[]} edges={[]} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^freehand$/i }));
    const rf = container.querySelector('[data-testid="react-flow"]');
    act(() => {
      rf.dispatchEvent(pointerEvent('pointerdown', { clientX: 0, clientY: 0 }));
      rf.dispatchEvent(pointerEvent('pointermove', { clientX: 10, clientY: 0 }));
    });

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(findCreatedFreehandNode()).toBeUndefined();
    expect(screen.getByRole('button', { name: /^freehand$/i })).toHaveAttribute(
      'aria-pressed',
      'false'
    );

    // A fresh arm afterwards behaves normally — cancel does not wedge state.
    fireEvent.click(screen.getByRole('button', { name: /^freehand$/i }));
    act(() => {
      rf.dispatchEvent(pointerEvent('pointerdown', { clientX: 0, clientY: 0 }));
      rf.dispatchEvent(pointerEvent('pointermove', { clientX: 10, clientY: 0 }));
      rf.dispatchEvent(pointerEvent('pointerup', { clientX: 10, clientY: 0 }));
    });
    expect(findCreatedFreehandNode()).toBeTruthy();
  });

  it('suppresses a concurrent second pointer while a stroke is active and surfaces a notice', () => {
    const { container } = render(<GraphCanvas nodes={[]} edges={[]} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^freehand$/i }));
    const rf = container.querySelector('[data-testid="react-flow"]');

    act(() => {
      rf.dispatchEvent(
        pointerEvent('pointerdown', { pointerId: 1, pointerType: 'pen', clientX: 0, clientY: 0 })
      );
    });
    act(() => {
      rf.dispatchEvent(
        pointerEvent('pointerdown', {
          pointerId: 2,
          pointerType: 'touch',
          clientX: 50,
          clientY: 50,
        })
      );
    });

    expect(
      screen.getByText(/finish the current stroke before starting another/i)
    ).toBeInTheDocument();

    // The primary (pen) stroke is unaffected by the ignored second pointer.
    act(() => {
      rf.dispatchEvent(
        pointerEvent('pointermove', { pointerId: 1, pointerType: 'pen', clientX: 10, clientY: 0 })
      );
      rf.dispatchEvent(
        pointerEvent('pointerup', { pointerId: 1, pointerType: 'pen', clientX: 10, clientY: 0 })
      );
    });
    const node = findCreatedFreehandNode();
    expect(node.data.points).toEqual([
      { x: 0, y: 0 },
      { x: 10, y: 0 },
    ]);
  });

  it('does not start a stroke on a non-primary mouse button (leaves right-drag pan/context-menu alone)', () => {
    const { container } = render(<GraphCanvas nodes={[]} edges={[]} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^freehand$/i }));
    const rf = container.querySelector('[data-testid="react-flow"]');
    act(() => {
      rf.dispatchEvent(
        pointerEvent('pointerdown', { pointerType: 'mouse', button: 2, clientX: 0, clientY: 0 })
      );
      rf.dispatchEvent(
        pointerEvent('pointermove', { pointerType: 'mouse', clientX: 10, clientY: 0 })
      );
      rf.dispatchEvent(
        pointerEvent('pointerup', { pointerType: 'mouse', clientX: 10, clientY: 0 })
      );
    });

    expect(findCreatedFreehandNode()).toBeUndefined();
  });
});
