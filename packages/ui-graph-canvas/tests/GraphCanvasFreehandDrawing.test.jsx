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

  it('previews the in-progress stroke in a visible colour too', () => {
    // The preview is the first thing a user sees, before any annotation
    // exists — if it is invisible the tool reads as broken mid-gesture, not
    // just after. It has its own colour prop, so it needs its own assertion.
    const { container } = render(
      <GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />
    );
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^freehand$/i }));

    const rf = container.querySelector('[data-testid="react-flow"]');
    act(() => {
      rf.dispatchEvent(pointerEvent('pointerdown', { clientX: 10, clientY: 10 }));
      rf.dispatchEvent(pointerEvent('pointermove', { clientX: 40, clientY: 25 }));
    });

    const preview = container.querySelector(
      '[data-testid="freehand-preview-overlay"] path[stroke]'
    );
    expect(preview).toBeTruthy();
    const value = preview.getAttribute('stroke');
    const [r, g, b] = [1, 3, 5].map((i) => parseInt(value.slice(i, i + 2), 16));
    expect((0.299 * r + 0.587 * g + 0.114 * b) / 255).toBeLessThan(0.3);
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
    // The stroke a user actually draws is written WITH an explicit colour, so
    // this — not the node component's fallback — is the value that decides
    // whether they see anything. It was a near-white on a light canvas, and a
    // fix aimed at the fallback left this path untouched. Asserted as a
    // property, since "too light" is the bug, not "not this constant".
    const [r, g, b] = [1, 3, 5].map((i) => parseInt(node.data.color.slice(i, i + 2), 16));
    expect((0.299 * r + 0.587 * g + 0.114 * b) / 255).toBeLessThan(0.3);
    // node-relative to the anchor (first point) — see annotations.js's
    // GENERIC_OVERLAY_FIELDS comment on freehand's `points` convention.
    expect(node.data.points).toEqual([
      { x: 0, y: 0 },
      { x: 10, y: 0 },
      { x: 20, y: 0 },
    ]);
    expect(onAnnotationChange).toHaveBeenCalledWith('create');
  });

  it('stays armed after a stroke, so the next press draws another line', () => {
    // Inverted from the original single-shot contract. Disarming after one
    // stroke meant lifting the pen and putting it down again panned the canvas
    // instead of drawing — the tool was gone with nothing on screen saying so,
    // and a drawing is rarely one stroke. Escape, the toolbox button and the
    // mobile sheet closing are the ways out.
    const { container } = render(<GraphCanvas nodes={[]} edges={[]} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^freehand$/i }));
    const rf = container.querySelector('[data-testid="react-flow"]');
    const stroke = (offset) => {
      act(() => {
        rf.dispatchEvent(pointerEvent('pointerdown', { clientX: offset, clientY: offset }));
        rf.dispatchEvent(pointerEvent('pointermove', { clientX: offset + 5, clientY: offset + 5 }));
        rf.dispatchEvent(pointerEvent('pointerup', { clientX: offset + 5, clientY: offset + 5 }));
      });
    };

    stroke(0);
    expect(store.handlers.nodesDraggable).toBe(false);
    expect(screen.getByRole('button', { name: /^freehand$/i })).toHaveAttribute(
      'aria-pressed',
      'true'
    );

    stroke(50);
    expect((store.nodes || []).filter((n) => n.type === 'freehand')).toHaveLength(2);
  });

  it('disarms on Escape, which is the way out of a sticky drawing mode', () => {
    render(<GraphCanvas nodes={[]} edges={[]} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^freehand$/i }));

    act(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    });

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
