import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent, act, cleanup, screen } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

// task-annotation-accessible-shared-controls: the cross-cutting mechanisms
// that live in GraphCanvas.jsx itself rather than in one annotation kind's
// own component — the overlap-object picker, explicit touch multi-select
// mode, and "ready to edit" selection/focus on creation. Same store-based
// `<ReactFlow>` mock as GraphCanvasAlignDistribute.test.jsx (captures every
// prop GraphCanvas passes to `<ReactFlow>`, including `onNodeClick`, and
// actually applies `setNodes` updaters to a live array) — reused rather than
// a second mock shape, since this file needs exactly the same access.
const store = vi.hoisted(() => ({ nodes: [], edges: [], handlers: {} }));

// Creating from the toolbox is a two-step gesture (task-annotation-tool-modes):
// the toolbox arms a tool, and the next tap on empty canvas is where the object
// goes. The mock captures every prop GraphCanvas passes to `<ReactFlow>`, so
// the pane tap is driven through the captured `onPaneClick` rather than a DOM
// event on an element this mock never renders.
function placeOnPane(x = 120, y = 90) {
  act(() => {
    store.handlers.onPaneClick?.({ clientX: x, clientY: y });
  });
}

vi.mock('reactflow', () => {
  const MockReactFlow = (props) => {
    store.handlers = { ...store.handlers, ...props };
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
      (changes) => {
        store.handlers.lastNodesChangeCall = changes;
      },
    ],
    useEdgesState: () => [store.edges, vi.fn(), vi.fn()],
    addEdge: (_params, edges) => edges,
    useReactFlow: () => ({
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
      fitView: vi.fn(),
      zoomIn: vi.fn(),
      zoomOut: vi.fn(),
    }),
    useOnSelectionChange: ({ onChange }) => {
      store.handlers.selectionOnChange = onChange;
    },
    Background: () => <div />,
    Controls: () => <div />,
    MiniMap: () => <div />,
    SelectionMode: { Partial: 'partial' },
  };
});

const nodeById = (id) => store.nodes.find((n) => n.id === id);
const noteNode = (id, x, y, w = 40, h = 40, dataOverrides = {}) => ({
  id,
  type: 'note',
  position: { x, y },
  width: w,
  height: h,
  data: dataOverrides,
});

function clickNode(node, clientX = 0, clientY = 0) {
  act(() => {
    store.handlers.onNodeClick?.({ clientX, clientY }, node);
  });
}

// `useNodesState`'s mock returns `store.nodes` fresh on every GraphCanvas
// render, but mutating `store.nodes` directly (as the tests below do, to set
// up a scenario) does not itself trigger one — mirrors
// GraphCanvasAlignDistribute.test.jsx's `openMultiMenu` forcing a render via
// `selectionOnChange` before reading anything that closure captured. Every
// call site below that mutates `store.nodes` needs this before the next
// `onNodeClick`/toolbox click, or that handler still runs against whatever
// `nodes` array closed over the LAST real render.
function setNodesAndRerender(nodes) {
  store.nodes = nodes;
  act(() => {
    store.handlers.selectionOnChange?.({ nodes: [], edges: [] });
  });
}

describe('GraphCanvas: the overlap-object picker', () => {
  beforeEach(() => {
    store.nodes = [];
    store.edges = [];
    store.handlers = {};
  });
  afterEach(() => cleanup());

  it("offers a picker when a click lands inside more than one annotation's box, listing each by its accessible name", () => {
    render(<GraphCanvas nodes={[]} edges={[]} />);
    const a = noteNode('a', -10, -10, 40, 40, { text: 'First' });
    const b = noteNode('b', -5, -5, 30, 30, { text: 'Second' });
    setNodesAndRerender([a, b]);

    clickNode(a, 0, 0); // screenToFlowPosition echoes (0,0) back — inside both boxes.

    expect(screen.getByText('Multiple objects here — choose one')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Sticky note, First' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Sticky note, Second' })).toBeInTheDocument();
  });

  it('offers nothing when the click point is inside exactly one box', () => {
    render(<GraphCanvas nodes={[]} edges={[]} />);
    const a = noteNode('a', -10, -10, 40, 40);
    const farAway = noteNode('far', 900, 900, 10, 10);
    setNodesAndRerender([a, farAway]);

    clickNode(a, 0, 0);

    expect(screen.queryByText('Multiple objects here — choose one')).toBeNull();
  });

  it('picking a candidate selects only that node and dismisses the picker', () => {
    render(<GraphCanvas nodes={[]} edges={[]} />);
    const a = noteNode('a', -10, -10, 40, 40, { text: 'First' });
    const b = noteNode('b', -5, -5, 30, 30, { text: 'Second' });
    setNodesAndRerender([a, b]);
    clickNode(a, 0, 0);

    fireEvent.click(screen.getByRole('button', { name: 'Sticky note, Second' }));

    expect(screen.queryByText('Multiple objects here — choose one')).toBeNull();
    // selectAndFocusNode dispatches through onNodesChange (the ReactFlow
    // change-descriptor API), not a raw setNodes mutation — assert the call
    // shape rather than store.nodes, matching this mock's own onNodesChange
    // (a spy, not wired to applyNodeChanges — see GraphCanvasAlignDistribute
    // .test.jsx's identical mock for why).
    expect(store.handlers.lastNodesChangeCall).toEqual(
      expect.arrayContaining([{ id: 'b', type: 'select', selected: true }])
    );
  });

  it('Escape dismisses an open overlap picker', () => {
    render(<GraphCanvas nodes={[]} edges={[]} />);
    const a = noteNode('a', -10, -10, 40, 40);
    const b = noteNode('b', -5, -5, 30, 30);
    setNodesAndRerender([a, b]);
    clickNode(a, 0, 0);
    expect(screen.getByText('Multiple objects here — choose one')).toBeInTheDocument();

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(screen.queryByText('Multiple objects here — choose one')).toBeNull();
  });
});

describe('GraphCanvas: explicit touch multi-select mode', () => {
  beforeEach(() => {
    store.nodes = [];
    store.edges = [];
    store.handlers = {};
  });
  afterEach(() => cleanup());

  it('the toggle is offered only on a compact (touch-first) host', () => {
    render(<GraphCanvas nodes={[]} edges={[]} compactMode="off" />);
    expect(screen.queryByRole('button', { name: 'Select multiple' })).toBeNull();
  });

  it('once active, tapping a node ADDS it to the selection rather than replacing it — the touch equivalent of holding Shift/Ctrl', () => {
    render(<GraphCanvas nodes={[]} edges={[]} compactMode="on" />);
    const a = noteNode('a', 0, 0);
    const b = noteNode('b', 900, 900); // far away — no overlap-picker interference
    setNodesAndRerender([a, b]);

    const toggle = screen.getByRole('button', { name: 'Select multiple' });
    expect(toggle.getAttribute('aria-pressed')).toBe('false');
    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-pressed')).toBe('true');

    // Simulate what ReactFlow's own click-to-select already did to the store
    // by the time onNodeClick fires for the second tap (select `a` alone —
    // exactly the "replace" default this mode exists to override).
    setNodesAndRerender([{ ...a, selected: true }, b]);
    act(() => {
      store.handlers.selectionOnChange({ nodes: [{ ...a, selected: true }], edges: [] });
    });

    clickNode({ ...b, selected: true }, 900, 900);

    // Net effect: `a` (selected before this click) is restored alongside
    // `b` — an ADD, not a replace.
    expect(store.handlers.lastNodesChangeCall).toEqual(
      expect.arrayContaining([{ id: 'a', type: 'select', selected: true }])
    );
  });

  it('inactive (the default): a plain tap does not synthesize any extra selection changes', () => {
    render(<GraphCanvas nodes={[]} edges={[]} compactMode="on" />);
    const a = noteNode('a', 0, 0);
    const b = noteNode('b', 900, 900);
    setNodesAndRerender([a, b]);
    act(() => {
      store.handlers.selectionOnChange({ nodes: [{ ...a, selected: true }], edges: [] });
    });
    store.handlers.lastNodesChangeCall = undefined;

    clickNode(b, 900, 900);

    expect(store.handlers.lastNodesChangeCall).toBeUndefined();
  });
});

describe('GraphCanvas: keyboard creation lands selected and focused, ready to edit', () => {
  beforeEach(() => {
    store.nodes = [];
    store.edges = [];
    store.handlers = {};
  });
  afterEach(() => cleanup());

  it('a toolbox-created annotation is selected immediately, and every previously-selected node is deselected', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    store.nodes = [{ ...noteNode('already-selected', 0, 0), selected: true }];

    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^note$/i }));
    placeOnPane();

    const created = store.nodes.find((n) => n.type === 'note' && n.id !== 'already-selected');
    expect(created?.selected).toBe(true);
    expect(nodeById('already-selected').selected).toBe(false);
  });
});

describe('GraphCanvas: creation focus must never escape an open modal mobile sheet', () => {
  // This suite's `reactflow` mock (above) never actually renders a real
  // `.react-flow__node[data-id]` wrapper for a created node - real ReactFlow
  // does that internally from the `nodes` prop, which this mock ignores in
  // favour of just rendering `children`. createAnnotation's own focus call
  // (GraphCanvas.jsx) looks one up by querying `reactFlowWrapper.current`,
  // so each test here plants a stand-in matching what production would have
  // rendered, inside the mocked `<ReactFlow>`'s own DOM (a real descendant
  // of the wrapper `reactFlowWrapper` is `ref`ed to) - then asserts on real
  // DOM focus, not on an implementation detail.
  let dateNowSpy;

  beforeEach(() => {
    store.nodes = [];
    store.edges = [];
    store.handlers = {};
    // Deterministic id (createAnnotation: `${kind}-${Date.now()}`) so the
    // stand-in element below can be planted with the exact data-id the real
    // focus call will look up.
    dateNowSpy = vi.spyOn(Date, 'now').mockReturnValue(1735500000000);
    // createAnnotation defers its focus call to a rAF; running it
    // synchronously keeps these tests assertion-after-act rather than
    // needing a real animation-frame wait.
    vi.stubGlobal('requestAnimationFrame', (cb) => {
      cb();
      return 0;
    });
  });

  afterEach(() => {
    dateNowSpy.mockRestore();
    vi.unstubAllGlobals();
    cleanup();
  });

  function mountFocusTarget(id) {
    const el = document.createElement('div');
    el.className = 'react-flow__node';
    el.setAttribute('data-id', id);
    el.tabIndex = -1; // focusable in jsdom, matching ReactFlow's own node wrapper
    screen.getByTestId('react-flow').appendChild(el);
    return el;
  }

  it('DESKTOP (no modal sheet open): focus lands on the newly created annotation — the keyboard-accessibility case this behaviour exists to establish', () => {
    render(<GraphCanvas nodes={[]} edges={[]} compactMode="off" onAnnotationChange={vi.fn()} />);
    const target = mountFocusTarget('note-1735500000000');

    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^note$/i }));
    placeOnPane();

    expect(document.activeElement).toBe(target);
  });

  it("MOBILE with the annotate BottomSheet open (isCompact + annotationToolboxPortalContainer, the sheet's own open signal): focus must NOT move onto the canvas node hidden behind the still-open modal sheet", () => {
    const portalContainer = document.createElement('div');
    document.body.appendChild(portalContainer);
    try {
      render(
        <GraphCanvas
          nodes={[]}
          edges={[]}
          compactMode="on"
          annotationToolboxPortalContainer={portalContainer}
          onAnnotationChange={vi.fn()}
        />
      );
      const target = mountFocusTarget('note-1735500000000');

      fireEvent.click(screen.getByRole('button', { name: /^note$/i }));
      placeOnPane();

      expect(document.activeElement).not.toBe(target);
      // The invariant is "focus never left the sheet", not merely "focus
      // avoided this one element" - confirm it landed nowhere inside the
      // canvas wrapper at all, mobile sheet or not.
      expect(screen.getByTestId('react-flow').contains(document.activeElement)).toBe(false);
    } finally {
      portalContainer.remove();
    }
  });

  it('MOBILE compact but with NO modal sheet open (no portal container - the fallback inline strip): focus still lands on the new annotation, same as desktop', () => {
    render(<GraphCanvas nodes={[]} edges={[]} compactMode="on" onAnnotationChange={vi.fn()} />);
    const target = mountFocusTarget('note-1735500000000');

    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^note$/i }));
    placeOnPane();

    expect(document.activeElement).toBe(target);
  });
});
