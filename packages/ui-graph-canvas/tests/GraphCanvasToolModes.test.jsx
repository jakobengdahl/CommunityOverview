// The annotation toolbox arms a tool; the canvas decides where the object goes
// (task-annotation-tool-modes). Covers the three behaviours that motivated the
// change — sticky placement, an eraser, and a stylus's inverted tip — plus the
// long-press pointer type that a stylus actually reports.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, act, within } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

const store = vi.hoisted(() => ({ handlers: {}, nodes: [] }));

vi.mock('reactflow', () => {
  const MockReactFlow = (props) => {
    store.handlers = props;
    return (
      <div data-testid="react-flow" className="react-flow">
        {/* ReactFlow's real nesting: the nodes live INSIDE the pane
            (.react-flow__pane > .react-flow__viewport > .react-flow__nodes).
            Rendering them as siblings made "a press on a node must not place"
            pass against a DOM that does not exist — the guard it covered was
            inert in production. Node stand-ins are appended to `viewport`. */}
        <div data-testid="pane" className="react-flow__pane">
          <div data-testid="viewport" className="react-flow__viewport">
            <div className="react-flow__nodes" />
          </div>
        </div>
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
    useEdgesState: (initial) => [initial || [], vi.fn(), vi.fn()],
    useReactFlow: () => ({
      fitView: vi.fn(),
      zoomIn: vi.fn(),
      zoomOut: vi.fn(),
      getNodes: () => store.nodes,
      getEdges: () => [],
      getNode: (id) => store.nodes.find((n) => n.id === id),
      setNodes: (updater) => {
        store.nodes = typeof updater === 'function' ? updater(store.nodes) : updater;
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

// jsdom has no PointerEvent constructor — same helper shape the freehand and
// touch suites already use.
function pointerEvent(
  type,
  { pointerId = 1, pointerType = 'mouse', clientX = 0, clientY = 0 } = {}
) {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX, clientY });
  Object.defineProperty(event, 'pointerId', { value: pointerId });
  Object.defineProperty(event, 'pointerType', { value: pointerType });
  return event;
}

function openToolbox() {
  fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
}

function arm(name) {
  fireEvent.click(screen.getByRole('button', { name }));
}

// Placement is a pointer gesture on the canvas wrapper, not ReactFlow's
// `onPaneClick` (task-annotation-drag-to-draw). It had to move: `onPaneClick`
// never fires while the pane is in selection mode — the desktop default — so
// riding on it meant an armed tool produced nothing at all with a mouse.
// Driving the real events is also the only way to cover drag-sizing.
function pressPane(x, y) {
  const pane = document.querySelector('.react-flow__pane');
  act(() => {
    pane.dispatchEvent(pointerEvent('pointerdown', { clientX: x, clientY: y }));
  });
}

function movePointer(x, y) {
  const pane = document.querySelector('.react-flow__pane');
  act(() => {
    pane.dispatchEvent(pointerEvent('pointermove', { clientX: x, clientY: y }));
  });
}

function releasePointer(x, y) {
  const pane = document.querySelector('.react-flow__pane');
  act(() => {
    pane.dispatchEvent(pointerEvent('pointerup', { clientX: x, clientY: y }));
  });
}

// A press and release at the same point: the plain click-to-place gesture.
function tapPane(x = 100, y = 100) {
  pressPane(x, y);
  releasePointer(x, y);
}

// Press, drag, release — sizes the object for the kinds that have a box.
function dragOnPane(x1, y1, x2, y2) {
  pressPane(x1, y1);
  movePointer(x2, y2);
  releasePointer(x2, y2);
}

// The shape slot shows only the current variant; every other one has to be
// picked from its fold-out first. Picking also arms the tool.
function selectShapeVariant(name) {
  fireEvent.click(screen.getByRole('button', { name: /choose a shape/i }));
  const picker = screen.getByRole('group', { name: /^shapes$/i });
  fireEvent.click(within(picker).getByRole('button', { name }));
}

// A node stand-in mounted where ReactFlow actually mounts nodes: inside the
// pane's viewport. jsdom gives every element a zero-size rect; the eraser
// measures the element it just removed to know what area to ignore, so the
// test has to supply a real one.
function mountNodeElement(id, rect = { left: 0, top: 0, right: 120, bottom: 80 }) {
  const el = document.createElement('div');
  el.className = 'react-flow__node';
  el.setAttribute('data-id', id);
  el.getBoundingClientRect = () => ({
    ...rect,
    width: rect.right - rect.left,
    height: rect.bottom - rect.top,
  });
  screen.getByTestId('viewport').appendChild(el);
  return el;
}

function countOf(type) {
  return store.nodes.filter((n) => n.type === type).length;
}

describe('GraphCanvas annotation tool modes', () => {
  beforeEach(() => {
    store.nodes = [];
    store.handlers = {};
    localStorage.clear();
  });
  afterEach(() => cleanup());

  it('arms a tool rather than creating, then places one object per tap on empty canvas', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    openToolbox();

    arm(/^vote dot$/i);
    // The whole point of the change: the click on the toolbox creates nothing.
    // It used to drop an object at the viewport centre, so placing several
    // meant dragging each one out of the pile the last had made.
    expect(countOf('vote_dot')).toBe(0);

    tapPane(120, 80);
    tapPane(200, 160);
    tapPane(260, 240);
    // Sticky: the tool stays armed, so three taps are three dots.
    expect(countOf('vote_dot')).toBe(3);
  });

  it('places each object at the tapped point, not at a fixed location', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    openToolbox();
    arm(/^vote dot$/i);

    tapPane(120, 80);
    tapPane(300, 220);

    const positions = store.nodes.filter((n) => n.type === 'vote_dot').map((n) => n.position);
    expect(positions).toEqual([
      { x: 120, y: 80 },
      { x: 300, y: 220 },
    ]);
  });

  it('select is the way back: arming it stops placement and restores plain canvas behaviour', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    openToolbox();
    arm(/^vote dot$/i);
    tapPane();
    expect(countOf('vote_dot')).toBe(1);

    arm(/^select$/i);
    tapPane();
    tapPane();
    expect(countOf('vote_dot')).toBe(1);
  });

  it('picking a shape variant arms that tool, so the next tap draws the shape just chosen', () => {
    // Choosing "circle" from the fold-out and then drawing used to produce
    // whatever tool was armed before — the picker changed the variant without
    // selecting the tool.
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    openToolbox();
    fireEvent.click(screen.getByRole('button', { name: /choose a shape/i }));
    const picker = screen.getByRole('group', { name: /^shapes$/i });
    fireEvent.click(
      [...picker.querySelectorAll('button')].find((b) =>
        /circle/i.test(b.textContent + b.ariaLabel)
      )
    );

    tapPane(50, 50);
    const shape = store.nodes.find((n) => n.type === 'shape');
    expect(shape).toBeTruthy();
    expect(shape.data.shape).toBe('circle');
  });

  describe('drag to draw', () => {
    it('sizes a shape from the drag, instead of always using the default box', () => {
      render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
      openToolbox();
      arm(/^rectangle$/i);

      dragOnPane(100, 100, 340, 260);

      const shape = store.nodes.find((n) => n.type === 'shape');
      expect(shape.style).toEqual({ width: 240, height: 160 });
      expect(shape.position).toEqual({ x: 100, y: 100 });
    });

    it('falls back to the default box for a press with no real drag', () => {
      render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
      openToolbox();
      arm(/^rectangle$/i);

      tapPane(100, 100);

      const shape = store.nodes.find((n) => n.type === 'shape');
      // The plain click-to-place gesture is unchanged — a press that never
      // moved carries no size to apply.
      expect(shape.style).toEqual({ width: 160, height: 96 });
    });

    it('mirrors the shape when the drag goes left or up from the press point', () => {
      // Dragging left from the press point means "point it left", which for a
      // directional variant (triangle, process arrow) is the only way to aim
      // it without rotating it by hand afterwards.
      render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
      openToolbox();
      arm(/^rectangle$/i);

      dragOnPane(300, 300, 120, 160);

      const shape = store.nodes.find((n) => n.type === 'shape');
      expect(shape.data.flipX).toBe(true);
      expect(shape.data.flipY).toBe(true);
      // The box is still anchored at the top-left of the swept area.
      expect(shape.position).toEqual({ x: 120, y: 160 });
      expect(shape.style).toEqual({ width: 180, height: 140 });
    });

    it('leaves a point-sized kind unsized however far the pointer is dragged', () => {
      // A vote dot has a fixed intrinsic size; a drag has nothing to apply.
      render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
      openToolbox();
      arm(/^vote dot$/i);

      dragOnPane(100, 100, 400, 400);

      const dot = store.nodes.find((n) => n.type === 'vote_dot');
      expect(dot.style).toBeUndefined();
      expect(dot.position).toEqual({ x: 100, y: 100 });
    });

    it('sizes a regular subtype from the longer side of the drag, not from dx alone', () => {
      // A regular subtype's proportion is recomputed from its WIDTH
      // (regularShapeSize), so sizing it from dx meant a mostly-vertical drag
      // always produced a minimum-size triangle however far the user dragged.
      render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
      openToolbox();
      selectShapeVariant(/^triangle$/i);

      dragOnPane(100, 100, 130, 500);

      const shape = store.nodes.find((n) => n.type === 'shape');
      expect(shape.data.shape).toBe('triangle');
      // 400px is the long side; the height follows the subtype's own ratio.
      expect(shape.style.width).toBe(400);
      expect(shape.style.height).toBe(Math.round(400 / (2 / Math.sqrt(3))));
    });

    it('never produces a box under the resizer’s own minimum, even after re-proportioning', () => {
      // The clamp has to survive `regularShapeSize`, which recomputes height
      // from width: clamping first meant the height was simply discarded, and
      // a triangle came out 40x35 — under the minimum the clamp exists for.
      render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
      openToolbox();
      selectShapeVariant(/^triangle$/i);

      dragOnPane(100, 100, 108, 112);

      const shape = store.nodes.find((n) => n.type === 'shape');
      expect(shape.style.width).toBeGreaterThanOrEqual(40);
      expect(shape.style.height).toBeGreaterThanOrEqual(40);
      // And still equal-sided. Flooring the two dimensions independently
      // also satisfies the two assertions above — it produced a 40x40
      // "triangle", which NodeResizer's keepAspectRatio then locked in. The
      // ratio is what the re-proportioning exists to preserve, so it is what
      // has to be asserted.
      const aspect = 2 / Math.sqrt(3);
      expect(shape.style.width / shape.style.height).toBeCloseTo(aspect, 1);
    });

    it('does not commit anything from a second contact during a pinch', () => {
      // A second pointer abandons the placement — and the abandonment has to
      // last until every contact lifts, or a third one (a palm, or the first
      // finger re-landing) starts a fresh placement mid-pinch and commits an
      // object when it lifts.
      render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
      openToolbox();
      arm(/^rectangle$/i);

      const pane = document.querySelector('.react-flow__pane');
      act(() => {
        pane.dispatchEvent(pointerEvent('pointerdown', { pointerId: 1, clientX: 50, clientY: 50 }));
        pane.dispatchEvent(
          pointerEvent('pointerdown', { pointerId: 2, clientX: 150, clientY: 60 })
        );
        // The first finger lifts BEFORE the third contact. That ordering is
        // what makes this bite: with the suspension keyed on a pointerup's
        // `buttons` — 0 on every pointerup, so the guard never fired — it
        // cleared here, and the third contact then started a fresh
        // placement that committed on release.
        pane.dispatchEvent(pointerEvent('pointerup', { pointerId: 1, clientX: 50, clientY: 50 }));
        pane.dispatchEvent(pointerEvent('pointerdown', { pointerId: 3, clientX: 90, clientY: 90 }));
        pane.dispatchEvent(pointerEvent('pointerup', { pointerId: 3, clientX: 90, clientY: 90 }));
        pane.dispatchEvent(pointerEvent('pointerup', { pointerId: 2, clientX: 150, clientY: 60 }));
      });

      expect(store.nodes.filter((n) => n.type === 'shape')).toHaveLength(0);
    });

    it('places again once every contact has lifted', () => {
      // The suspension must not be sticky beyond the gesture that caused it.
      render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
      openToolbox();
      arm(/^rectangle$/i);

      const pane = document.querySelector('.react-flow__pane');
      act(() => {
        pane.dispatchEvent(pointerEvent('pointerdown', { pointerId: 1, clientX: 50, clientY: 50 }));
        pane.dispatchEvent(
          pointerEvent('pointerdown', { pointerId: 2, clientX: 150, clientY: 60 })
        );
        pane.dispatchEvent(pointerEvent('pointerup', { pointerId: 1, clientX: 50, clientY: 50 }));
        pane.dispatchEvent(pointerEvent('pointerup', { pointerId: 2, clientX: 150, clientY: 60 }));
      });
      tapPane(200, 200);

      expect(store.nodes.filter((n) => n.type === 'shape')).toHaveLength(1);
    });

    it('swallows the click the gesture synthesizes, so the new object stays selected', () => {
      // Blocking `mousedown` (to stop the canvas panning under the drag) means
      // the canvas never records a press position, so its own click handler
      // cannot tell the gesture's trailing click from a plain one and falls
      // through to clearing the selection — deselecting the annotation just
      // created and focused.
      render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
      openToolbox();
      arm(/^rectangle$/i);

      const pane = document.querySelector('.react-flow__pane');
      dragOnPane(100, 100, 200, 180);
      const click = new MouseEvent('click', { bubbles: true, cancelable: true });
      act(() => {
        pane.dispatchEvent(click);
      });

      expect(click.defaultPrevented).toBe(true);
    });

    it('never starts a placement from a press that landed on an existing node', () => {
      // An armed tool must not make the rest of the canvas unusable: pressing
      // an object still selects and drags it. The node stand-in is mounted
      // INSIDE the pane, which is where ReactFlow actually puts it — as a
      // sibling this passed while the production guard did nothing.
      store.nodes = [{ id: 'note-1', type: 'note', position: { x: 0, y: 0 }, data: {} }];
      render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
      openToolbox();
      arm(/^rectangle$/i);

      const nodeEl = mountNodeElement('note-1');
      act(() => {
        nodeEl.dispatchEvent(pointerEvent('pointerdown', { clientX: 10, clientY: 10 }));
        nodeEl.dispatchEvent(pointerEvent('pointerup', { clientX: 10, clientY: 10 }));
      });

      expect(store.nodes.find((n) => n.type === 'shape')).toBeUndefined();
    });

    it('never starts a placement from a press on an edge, a control or the selection box', () => {
      render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
      openToolbox();
      arm(/^rectangle$/i);

      const viewport = screen.getByTestId('viewport');
      const edgeEl = document.createElement('div');
      edgeEl.className = 'react-flow__edge';
      edgeEl.setAttribute('data-testid', 'rf__edge-edge-1');
      viewport.appendChild(edgeEl);
      const handle = document.createElement('div');
      handle.className = 'react-flow__resize-control handle';
      viewport.appendChild(handle);
      // The multi-selection box is a DIRECT child of the pane, outside the
      // viewport, so no node/edge selector reaches it — and its rect covers
      // the whole selection with `pointer-events: all`. Pressing it to drag
      // the selection created an annotation on top of it instead.
      const selectionEl = document.createElement('div');
      selectionEl.className = 'react-flow__nodesselection';
      const rect = document.createElement('div');
      rect.className = 'react-flow__nodesselection-rect';
      selectionEl.appendChild(rect);
      document.querySelector('.react-flow__pane').appendChild(selectionEl);

      for (const el of [edgeEl, handle, rect]) {
        act(() => {
          el.dispatchEvent(pointerEvent('pointerdown', { clientX: 10, clientY: 10 }));
          el.dispatchEvent(pointerEvent('pointerup', { clientX: 10, clientY: 10 }));
        });
      }

      expect(store.nodes.find((n) => n.type === 'shape')).toBeUndefined();
    });
  });

  describe('eraser', () => {
    function eraseOver(el, { pointerType = 'mouse' } = {}, at = 10) {
      act(() => {
        el.dispatchEvent(pointerEvent('pointerdown', { pointerType, clientX: at, clientY: at }));
        el.dispatchEvent(pointerEvent('pointerup', { pointerType, clientX: at, clientY: at }));
      });
    }

    // The eraser resolves its target through elementFromPoint, so the test has
    // to answer that the way a real canvas would.
    function withElementAtPoint(el, fn) {
      const original = document.elementFromPoint;
      document.elementFromPoint = () => el;
      try {
        fn();
      } finally {
        document.elementFromPoint = original;
      }
    }

    it('deletes an annotation dragged over while the eraser tool is armed', () => {
      store.nodes = [{ id: 'note-1', type: 'note', position: { x: 0, y: 0 }, data: {} }];
      render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
      openToolbox();
      arm(/^eraser$/i);

      const el = mountNodeElement('note-1');
      withElementAtPoint(el, () => eraseOver(screen.getByTestId('react-flow')));

      expect(store.nodes.find((n) => n.id === 'note-1')).toBeUndefined();
    });

    it('only HIDES a graph node, never deletes it — graph data is not the eraser’s to destroy', () => {
      const onHide = vi.fn();
      // A graph node arrives through the `nodes` PROP, unlike an annotation,
      // which lives only in ReactFlow's own state — seeding store.nodes here
      // would be overwritten by the prop sync on the first render.
      const graphNode = {
        id: 'graph-1',
        type: 'custom',
        position: { x: 0, y: 0 },
        data: { label: 'A' },
      };
      render(
        <GraphCanvas nodes={[graphNode]} edges={[]} onHide={onHide} onAnnotationChange={vi.fn()} />
      );
      openToolbox();
      arm(/^eraser$/i);

      const el = mountNodeElement('graph-1');
      withElementAtPoint(el, () => eraseOver(screen.getByTestId('react-flow')));

      expect(onHide).toHaveBeenCalledWith('graph-1');
      // Hiding is the host's business; nothing was removed from the canvas here.
      expect(store.nodes.find((n) => n.id === 'graph-1')).toBeTruthy();
    });

    it("erases from a stylus's inverted tip without any tool being armed", () => {
      // Flipping the pen over IS the gesture; having to arm a tool first would
      // defeat it. `select` is armed here — the resting state.
      store.nodes = [{ id: 'note-1', type: 'note', position: { x: 0, y: 0 }, data: {} }];
      render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);

      const el = mountNodeElement('note-1');
      withElementAtPoint(el, () =>
        eraseOver(screen.getByTestId('react-flow'), { pointerType: 'eraser' })
      );

      expect(store.nodes.find((n) => n.id === 'note-1')).toBeUndefined();
    });

    it('leaves an annotation alone when no eraser is involved at all', () => {
      store.nodes = [{ id: 'note-1', type: 'note', position: { x: 0, y: 0 }, data: {} }];
      render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);

      const el = mountNodeElement('note-1');
      withElementAtPoint(el, () => eraseOver(screen.getByTestId('react-flow')));

      expect(store.nodes.find((n) => n.id === 'note-1')).toBeTruthy();
    });

    it('keeps erasing across a whole sweep, not just the first object', () => {
      // The regression: erasing changes `nodes`, and a `nodes` dependency tore
      // the listeners down and rebuilt them after the first hit — losing the
      // in-flight gesture, so the user had to lift and press again per object.
      store.nodes = [
        { id: 'note-1', type: 'note', position: { x: 0, y: 0 }, data: {} },
        { id: 'note-2', type: 'note', position: { x: 0, y: 0 }, data: {} },
      ];
      render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
      openToolbox();
      arm(/^eraser$/i);

      // Two objects side by side, as they would be on a real canvas — the
      // sweep has to leave the first one's footprint to reach the second.
      const first = mountNodeElement('note-1', { left: 0, top: 0, right: 50, bottom: 50 });
      const second = mountNodeElement('note-2', { left: 200, top: 0, right: 250, bottom: 50 });
      const rf = screen.getByTestId('react-flow');
      // One press, then movement across two objects, then one release.
      withElementAtPoint(first, () => {
        act(() => {
          rf.dispatchEvent(pointerEvent('pointerdown', { clientX: 10, clientY: 10 }));
        });
      });
      withElementAtPoint(second, () => {
        act(() => {
          rf.dispatchEvent(pointerEvent('pointermove', { clientX: 220, clientY: 20 }));
        });
      });
      act(() => {
        rf.dispatchEvent(pointerEvent('pointerup', { clientX: 220, clientY: 20 }));
      });

      expect(store.nodes.find((n) => n.id === 'note-1')).toBeUndefined();
      expect(store.nodes.find((n) => n.id === 'note-2')).toBeUndefined();
    });

    it('does not cascade down a stack of objects at one spot', () => {
      // A small annotation sitting on a graph node: erasing the annotation
      // must not hide the node on the very next pixel of movement. The
      // per-object key alone does not prevent this — whatever is underneath
      // is simply a new key.
      const onHide = vi.fn();
      const graphNode = {
        id: 'graph-1',
        type: 'custom',
        position: { x: 0, y: 0 },
        data: { label: 'A' },
      };
      store.nodes = [{ id: 'note-1', type: 'note', position: { x: 0, y: 0 }, data: {} }];
      // `graph-1` has to be a REAL node, delivered through the prop: without
      // it `eraseAt` bails at its "node not found" guard and the test passes
      // no matter what the cascade guard does.
      render(
        <GraphCanvas nodes={[graphNode]} edges={[]} onHide={onHide} onAnnotationChange={vi.fn()} />
      );
      openToolbox();
      arm(/^eraser$/i);

      const annotation = mountNodeElement('note-1');
      const underneath = mountNodeElement('graph-1');
      const rf = screen.getByTestId('react-flow');
      withElementAtPoint(annotation, () => {
        act(() => {
          rf.dispatchEvent(pointerEvent('pointerdown', { clientX: 0, clientY: 0 }));
        });
      });
      // Barely any movement — the same spot, now showing what was underneath.
      withElementAtPoint(underneath, () => {
        act(() => {
          rf.dispatchEvent(pointerEvent('pointermove', { clientX: 2, clientY: 1 }));
        });
      });

      expect(store.nodes.find((n) => n.id === 'note-1')).toBeUndefined();
      expect(onHide).not.toHaveBeenCalled();
    });

    it('refuses to erase a group, whose children it cannot re-parent', () => {
      // Deleting a group is not a filter: its children have to be un-parented
      // and their positions converted back to absolute. A bare removal leaves
      // them pointing at a parent that no longer exists — and a group's large
      // empty interior is exactly what a sweeping eraser lands on.
      const onHide = vi.fn();
      store.nodes = [{ id: 'group-1', type: 'group', position: { x: 0, y: 0 }, data: {} }];
      render(<GraphCanvas nodes={[]} edges={[]} onHide={onHide} onAnnotationChange={vi.fn()} />);
      openToolbox();
      arm(/^eraser$/i);

      const el = mountNodeElement('group-1');
      withElementAtPoint(el, () => eraseOver(screen.getByTestId('react-flow')));

      expect(store.nodes.find((n) => n.id === 'group-1')).toBeTruthy();
      // And not merely spared deletion by falling through to the hide path:
      // `group` is not in OVERLAY_TYPES, so without the explicit refusal the
      // node would survive here while quietly being hidden instead. Asserting
      // only survival passed with the guard removed.
      expect(onHide).not.toHaveBeenCalled();
    });

    it('refuses to erase a locked annotation, or one another client is editing', () => {
      store.nodes = [
        { id: 'locked', type: 'note', position: { x: 0, y: 0 }, data: { locked: true } },
        { id: 'claimed', type: 'note', position: { x: 0, y: 0 }, data: {} },
      ];
      // The live lease arrives as a prop, not as seeded node data: the canvas
      // owns `data.remoteLease` and rewrites it from `remoteLeases` on every
      // change, so a hand-seeded value is cleared before the gesture runs.
      render(
        <GraphCanvas
          nodes={[]}
          edges={[]}
          remoteLeases={{ claimed: { clientId: 'c2', color: '#e6194b', displayName: 'Ada' } }}
          onAnnotationChange={vi.fn()}
        />
      );
      openToolbox();
      arm(/^eraser$/i);

      for (const id of ['locked', 'claimed']) {
        const el = mountNodeElement(id);
        withElementAtPoint(el, () => eraseOver(screen.getByTestId('react-flow')));
      }

      expect(store.nodes.find((n) => n.id === 'locked')).toBeTruthy();
      expect(store.nodes.find((n) => n.id === 'claimed')).toBeTruthy();
    });

    it('recovers when the release is never delivered to the canvas', () => {
      // A sweep that ends outside the wrapper used to latch the gesture
      // forever: `erasingPointerId` stayed set and every later erase was
      // rejected, so the eraser silently died.
      store.nodes = [
        { id: 'note-1', type: 'note', position: { x: 0, y: 0 }, data: {} },
        { id: 'note-2', type: 'note', position: { x: 0, y: 0 }, data: {} },
      ];
      render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
      openToolbox();
      arm(/^eraser$/i);

      const rf = screen.getByTestId('react-flow');
      const first = mountNodeElement('note-1');
      withElementAtPoint(first, () => {
        act(() => {
          rf.dispatchEvent(pointerEvent('pointerdown', { clientX: 0, clientY: 0 }));
        });
      });
      // The release lands on the window, never on the wrapper.
      act(() => {
        window.dispatchEvent(pointerEvent('pointerup', { clientX: 999, clientY: 999 }));
      });

      const second = mountNodeElement('note-2');
      withElementAtPoint(second, () => eraseOver(rf, {}, 200));
      expect(store.nodes.find((n) => n.id === 'note-2')).toBeUndefined();
    });

    it('does not cascade into what a HIDE reveals either', () => {
      // The gate was armed only for annotation deletes, so hiding a graph node
      // revealed the edge beneath it and the next move hid that too.
      const onHide = vi.fn();
      const onHideEdge = vi.fn();
      const graphNode = { id: 'graph-1', type: 'custom', position: { x: 0, y: 0 }, data: {} };
      render(
        <GraphCanvas
          nodes={[graphNode]}
          edges={[]}
          onHide={onHide}
          onHideEdge={onHideEdge}
          onAnnotationChange={vi.fn()}
        />
      );
      openToolbox();
      arm(/^eraser$/i);

      const node = mountNodeElement('graph-1', { left: 0, top: 0, right: 60, bottom: 60 });
      const edgeEl = document.createElement('div');
      edgeEl.className = 'react-flow__edge';
      edgeEl.setAttribute('data-testid', 'rf__edge-edge-1');
      screen.getByTestId('react-flow').appendChild(edgeEl);
      const rf = screen.getByTestId('react-flow');

      withElementAtPoint(node, () => {
        act(() => {
          rf.dispatchEvent(pointerEvent('pointerdown', { clientX: 10, clientY: 10 }));
        });
      });
      withElementAtPoint(edgeEl, () => {
        act(() => {
          rf.dispatchEvent(pointerEvent('pointermove', { clientX: 14, clientY: 12 }));
        });
      });

      expect(onHide).toHaveBeenCalledWith('graph-1');
      expect(onHideEdge).not.toHaveBeenCalled();
    });

    it('disables panning, marquee selection and node dragging while armed', () => {
      render(<GraphCanvas nodes={[]} edges={[]} />);
      openToolbox();
      arm(/^eraser$/i);
      expect(store.handlers.panOnDrag).toBe(false);
      expect(store.handlers.selectionOnDrag).toBe(false);
      expect(store.handlers.nodesDraggable).toBe(false);
    });
  });
});
