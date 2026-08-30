import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, act, cleanup } from '@testing-library/react';
import { GraphCanvas } from '../src/index';
import { LONG_PRESS_DELAY_MS, LONG_PRESS_TOLERANCE_PX } from '../src/utils/longPress';

// Mirrors real ReactFlow's DOM contract closely enough for the touch-target
// resolution in GraphCanvas to work: node wrappers carry `data-id` + the
// `react-flow__node` class, edges carry `data-testid="rf__edge-<id>"` + the
// `react-flow__edge` class, and the multi-select overlay carries the
// `react-flow__nodesselection-rect` class (see node_modules/reactflow's own
// NodeWrapper/EdgeWrapper/NodesSelection markup, which this pins against).
const store = vi.hoisted(() => ({ nodes: [], edges: [], handlers: {} }));

vi.mock('reactflow', () => {
  const MockReactFlow = (props) => {
    store.handlers = props;
    return (
      <div data-testid="react-flow">
        <div data-testid="pane-background" style={{ width: 500, height: 500 }} />
        {props.nodes?.map((node) => (
          <div key={node.id} className="react-flow__node" data-id={node.id}>
            {node.data?.label}
          </div>
        ))}
        {props.edges?.map((edge) => (
          <div key={edge.id} className="react-flow__edge" data-testid={`rf__edge-${edge.id}`} />
        ))}
        <div className="react-flow__nodesselection-rect" data-testid="selection-rect" />
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
    useEdgesState: () => [store.edges, vi.fn(), vi.fn()],
    addEdge: (_params, edges) => edges,
    useReactFlow: () => ({
      getNodes: () => store.nodes,
      getEdges: () => store.edges,
      setNodes: vi.fn(),
      setEdges: vi.fn(),
      screenToFlowPosition: ({ x, y }) => ({ x, y }),
      setCenter: vi.fn(),
      getViewport: () => ({ x: 0, y: 0, zoom: 1 }),
      fitView: vi.fn(),
    }),
    useOnSelectionChange: ({ onChange }) => {
      store.selectionOnChange = onChange;
    },
    Background: () => <div />,
    Controls: () => <div />,
    MiniMap: () => <div />,
    SelectionMode: { Partial: 'partial' },
    MarkerType: { ArrowClosed: 'arrowclosed', Arrow: 'arrow' },
  };
});

const nodeA = {
  id: 'node-a',
  type: 'custom',
  position: { x: 0, y: 0 },
  data: { nodeType: 'Actor', label: 'A' },
};
const nodeB = {
  id: 'node-b',
  type: 'custom',
  position: { x: 10, y: 10 },
  data: { nodeType: 'Actor', label: 'B' },
};
const edgeAB = { id: 'edge-a-b', source: 'node-a', target: 'node-b', label: 'RELATES_TO' };

// jsdom has no PointerEvent constructor, so a MouseEvent stands in with the
// pointer-specific fields (pointerId/pointerType) attached directly — the
// production code only reads clientX/clientY/pointerId/pointerType/target,
// all of which this satisfies identically to a real PointerEvent.
function pointerEvent(
  type,
  { pointerId = 1, pointerType = 'touch', clientX = 0, clientY = 0 } = {}
) {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX, clientY });
  Object.defineProperty(event, 'pointerId', { value: pointerId });
  Object.defineProperty(event, 'pointerType', { value: pointerType });
  return event;
}

function noMenusOpen() {
  return (
    !document.querySelector('.node-context-menu') &&
    !document.querySelector('.multi-node-context-menu') &&
    !document.querySelector('.edge-context-menu') &&
    !document.querySelector('.pane-context-menu')
  );
}

describe('GraphCanvas touch interaction', () => {
  beforeEach(() => {
    store.nodes = [];
    store.edges = [];
    store.handlers = {};
    vi.useFakeTimers();
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  describe('touchMode prop → ReactFlow pan/selection props', () => {
    it('hands the annotation toolbox the pointer signal, not the width one', () => {
      // The defect this closes was the toolbox captioning off `compact`, a
      // viewport-WIDTH signal, so a coarse pointer on a wide screen got an
      // unlabelled icon grid. Asserted here, at the wiring, because a test of
      // the toolbox alone only proves it honours a prop it is handed — not
      // that GraphCanvas hands it one. Width explicitly off, pointer on.
      render(<GraphCanvas nodes={[]} edges={[]} touchMode="on" compactMode="off" />);
      const toolbox = document.querySelector('[data-testid="annotation-toolbox"]');
      expect(toolbox.className).toContain('annotation-toolbox--touch');
      expect(toolbox.className).not.toContain('annotation-toolbox--compact');
    });

    it('touchMode="off" keeps the exact desktop values — right-drag pans, left-drag marquee-selects', () => {
      render(<GraphCanvas nodes={[]} edges={[]} touchMode="off" />);
      expect(store.handlers.panOnDrag).toEqual([0, 2]);
      expect(store.handlers.selectionOnDrag).toBe(true);
    });

    it('default (no touchMode passed) on a non-coarse pointer matches the pre-touch desktop values', () => {
      const matchMediaSpy = vi.spyOn(window, 'matchMedia').mockReturnValue({
        matches: false,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      });
      render(<GraphCanvas nodes={[]} edges={[]} />);
      expect(store.handlers.panOnDrag).toEqual([0, 2]);
      expect(store.handlers.selectionOnDrag).toBe(true);
      matchMediaSpy.mockRestore();
    });

    it('touchMode="on" enables one-finger-drag pan (panOnDrag=true) and disables marquee selection', () => {
      render(<GraphCanvas nodes={[]} edges={[]} touchMode="on" />);
      expect(store.handlers.panOnDrag).toBe(true);
      expect(store.handlers.selectionOnDrag).toBe(false);
    });

    it('touchMode="auto" adopts touch props when matchMedia reports a coarse pointer', () => {
      const matchMediaSpy = vi.spyOn(window, 'matchMedia').mockReturnValue({
        matches: true,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      });
      render(<GraphCanvas nodes={[]} edges={[]} touchMode="auto" />);
      expect(store.handlers.panOnDrag).toBe(true);
      expect(store.handlers.selectionOnDrag).toBe(false);
      matchMediaSpy.mockRestore();
    });

    it('touchMode="off" overrides a coarse-pointer matchMedia result', () => {
      const matchMediaSpy = vi.spyOn(window, 'matchMedia').mockReturnValue({
        matches: true,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      });
      render(<GraphCanvas nodes={[]} edges={[]} touchMode="off" />);
      expect(store.handlers.panOnDrag).toEqual([0, 2]);
      expect(store.handlers.selectionOnDrag).toBe(true);
      matchMediaSpy.mockRestore();
    });

    it('touchMode="on" forces touch props even when matchMedia reports a fine (mouse) pointer', () => {
      const matchMediaSpy = vi.spyOn(window, 'matchMedia').mockReturnValue({
        matches: false,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      });
      render(<GraphCanvas nodes={[]} edges={[]} touchMode="on" />);
      expect(store.handlers.panOnDrag).toBe(true);
      expect(store.handlers.selectionOnDrag).toBe(false);
      matchMediaSpy.mockRestore();
    });
  });

  describe('long-press reaches the same context menus a right-click reaches', () => {
    it('long-press on a node opens the node context menu at the press position', () => {
      store.nodes = [nodeA];
      const { container } = render(<GraphCanvas nodes={[nodeA]} edges={[]} touchMode="on" />);
      const nodeEl = container.querySelector('.react-flow__node[data-id="node-a"]');

      act(() => {
        nodeEl.dispatchEvent(pointerEvent('pointerdown', { clientX: 40, clientY: 60 }));
      });
      act(() => {
        vi.advanceTimersByTime(LONG_PRESS_DELAY_MS);
      });

      const menu = document.querySelector('.node-context-menu');
      expect(menu).toBeTruthy();
      expect(menu.style.left).toBe('40px');
      expect(menu.style.top).toBe('60px');
    });

    it('long-press on an edge opens the edge context menu', () => {
      store.nodes = [nodeA, nodeB];
      store.edges = [edgeAB];
      const { container } = render(
        <GraphCanvas nodes={[nodeA, nodeB]} edges={[edgeAB]} touchMode="on" />
      );
      const edgeEl = container.querySelector(`[data-testid="rf__edge-${edgeAB.id}"]`);

      act(() => {
        edgeEl.dispatchEvent(pointerEvent('pointerdown', { clientX: 15, clientY: 15 }));
      });
      act(() => {
        vi.advanceTimersByTime(LONG_PRESS_DELAY_MS);
      });

      expect(document.querySelector('.edge-context-menu')).toBeTruthy();
    });

    it('long-press on the empty pane opens the pane (add note/label/arrow) menu', () => {
      const { getByTestId } = render(<GraphCanvas nodes={[]} edges={[]} touchMode="on" />);
      const pane = getByTestId('pane-background');

      act(() => {
        pane.dispatchEvent(pointerEvent('pointerdown', { clientX: 200, clientY: 200 }));
      });
      act(() => {
        vi.advanceTimersByTime(LONG_PRESS_DELAY_MS);
      });

      expect(document.querySelector('.pane-context-menu')).toBeTruthy();
    });

    it('long-press on the multi-select overlay opens the selection (multi-node) context menu', () => {
      store.nodes = [
        { ...nodeA, selected: true },
        { ...nodeB, selected: true },
      ];
      const { getByTestId } = render(
        <GraphCanvas nodes={[nodeA, nodeB]} edges={[]} touchMode="on" />
      );
      // Populate the component's selectedNodes state the way ReactFlow's real
      // useOnSelectionChange would after a prior multi-select.
      act(() => {
        store.selectionOnChange({ nodes: [nodeA, nodeB], edges: [] });
      });
      const selectionRect = getByTestId('selection-rect');

      act(() => {
        selectionRect.dispatchEvent(pointerEvent('pointerdown', { clientX: 5, clientY: 5 }));
      });
      act(() => {
        vi.advanceTimersByTime(LONG_PRESS_DELAY_MS);
      });

      expect(document.querySelector('.multi-node-context-menu')).toBeTruthy();
    });

    it('does nothing before the long-press delay elapses', () => {
      store.nodes = [nodeA];
      const { container } = render(<GraphCanvas nodes={[nodeA]} edges={[]} touchMode="on" />);
      const nodeEl = container.querySelector('.react-flow__node[data-id="node-a"]');

      act(() => {
        nodeEl.dispatchEvent(pointerEvent('pointerdown', { clientX: 40, clientY: 60 }));
      });
      act(() => {
        vi.advanceTimersByTime(LONG_PRESS_DELAY_MS - 1);
      });

      expect(noMenusOpen()).toBe(true);
    });

    it('cancels when the finger moves into a pan (movement past tolerance before the delay fires)', () => {
      store.nodes = [nodeA];
      const { container } = render(<GraphCanvas nodes={[nodeA]} edges={[]} touchMode="on" />);
      const nodeEl = container.querySelector('.react-flow__node[data-id="node-a"]');

      act(() => {
        nodeEl.dispatchEvent(pointerEvent('pointerdown', { clientX: 40, clientY: 60 }));
        // A pointermove need not land back on the exact same element — any
        // descendant of the canvas wrapper bubbles up to its listener, just
        // like a real touchmove would.
        nodeEl.dispatchEvent(
          pointerEvent('pointermove', {
            clientX: 40 + LONG_PRESS_TOLERANCE_PX + 5,
            clientY: 60,
          })
        );
      });
      act(() => {
        vi.advanceTimersByTime(LONG_PRESS_DELAY_MS);
      });

      expect(noMenusOpen()).toBe(true);
    });

    it('cancels on a second finger touching down (start of a pinch, not a hold)', () => {
      store.nodes = [nodeA];
      const { container } = render(<GraphCanvas nodes={[nodeA]} edges={[]} touchMode="on" />);
      const nodeEl = container.querySelector('.react-flow__node[data-id="node-a"]');
      const rf = container.querySelector('[data-testid="react-flow"]');

      act(() => {
        nodeEl.dispatchEvent(
          pointerEvent('pointerdown', { pointerId: 1, clientX: 40, clientY: 60 })
        );
        rf.dispatchEvent(pointerEvent('pointerdown', { pointerId: 2, clientX: 200, clientY: 200 }));
      });
      act(() => {
        vi.advanceTimersByTime(LONG_PRESS_DELAY_MS);
      });

      expect(noMenusOpen()).toBe(true);
    });

    it('cancels when the pointer lifts before the delay elapses (a plain tap)', () => {
      store.nodes = [nodeA];
      const { container } = render(<GraphCanvas nodes={[nodeA]} edges={[]} touchMode="on" />);
      const nodeEl = container.querySelector('.react-flow__node[data-id="node-a"]');

      act(() => {
        nodeEl.dispatchEvent(pointerEvent('pointerdown', { clientX: 40, clientY: 60 }));
        nodeEl.dispatchEvent(pointerEvent('pointerup', {}));
      });
      act(() => {
        vi.advanceTimersByTime(LONG_PRESS_DELAY_MS);
      });

      expect(noMenusOpen()).toBe(true);
    });

    // A stylus reports `pointerType: 'pen'`, not 'touch'. Restricting the
    // long-press detector to 'touch' meant a pen press armed nothing at all,
    // so on a pen-first device — which has no right-click of its own — no
    // annotation's context menu was reachable. The target resolution was
    // already there; only the pointer type was turned away.
    it("long-press with a PEN on a node opens that node's context menu", () => {
      store.nodes = [nodeA];
      const { container } = render(<GraphCanvas nodes={[nodeA]} edges={[]} touchMode="on" />);
      const nodeEl = container.querySelector('.react-flow__node[data-id="node-a"]');

      act(() => {
        nodeEl.dispatchEvent(
          pointerEvent('pointerdown', { pointerType: 'pen', clientX: 40, clientY: 60 })
        );
      });
      act(() => {
        vi.advanceTimersByTime(LONG_PRESS_DELAY_MS);
      });

      const menu = document.querySelector('.node-context-menu');
      expect(menu).toBeTruthy();
      expect(menu.style.left).toBe('40px');
      expect(menu.style.top).toBe('60px');
    });

    it('a pen long-press works even with touch mode off — a pen is not a finger', () => {
      // `touchMode` is the host's coarse-pointer signal, and a hybrid device
      // driven by pen can legitimately report a fine pointer. Gating the pen
      // on that signal would put the menu back out of reach exactly where the
      // pen is the primary input.
      store.nodes = [nodeA];
      const { container } = render(<GraphCanvas nodes={[nodeA]} edges={[]} touchMode="off" />);
      const nodeEl = container.querySelector('.react-flow__node[data-id="node-a"]');

      act(() => {
        nodeEl.dispatchEvent(
          pointerEvent('pointerdown', { pointerType: 'pen', clientX: 40, clientY: 60 })
        );
      });
      act(() => {
        vi.advanceTimersByTime(LONG_PRESS_DELAY_MS);
      });

      expect(document.querySelector('.node-context-menu')).toBeTruthy();
    });

    it('a mouse pointerdown (pointerType "mouse") never starts a long-press, even in touchMode="on"', () => {
      store.nodes = [nodeA];
      const { container } = render(<GraphCanvas nodes={[nodeA]} edges={[]} touchMode="on" />);
      const nodeEl = container.querySelector('.react-flow__node[data-id="node-a"]');

      act(() => {
        nodeEl.dispatchEvent(
          pointerEvent('pointerdown', { pointerType: 'mouse', clientX: 40, clientY: 60 })
        );
      });
      act(() => {
        vi.advanceTimersByTime(LONG_PRESS_DELAY_MS);
      });

      expect(noMenusOpen()).toBe(true);
    });
  });

  describe('desktop mode (touchMode="off") is not touched by touch pointer events', () => {
    it('a held touch pointerdown never opens a context menu when touch mode is off', () => {
      store.nodes = [nodeA];
      const { container } = render(<GraphCanvas nodes={[nodeA]} edges={[]} touchMode="off" />);
      const nodeEl = container.querySelector('.react-flow__node[data-id="node-a"]');

      act(() => {
        nodeEl.dispatchEvent(pointerEvent('pointerdown', { clientX: 40, clientY: 60 }));
      });
      act(() => {
        vi.advanceTimersByTime(LONG_PRESS_DELAY_MS);
      });

      expect(noMenusOpen()).toBe(true);
    });
  });
});
