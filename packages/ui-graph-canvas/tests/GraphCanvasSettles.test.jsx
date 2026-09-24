import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act, fireEvent } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

// Regression test for a real render-stability bug (not a harness artifact):
// several effects in GraphCanvas.jsx call ReactFlow's `setNodes`/`setEdges`
// with a freshly-built array on every run, regardless of whether the content
// actually changed. Every OTHER test in this package's `reactflow` mock makes
// `useNodesState`/`useEdgesState` either a pure pass-through
// (`(initial) => [initial, vi.fn(), vi.fn()]`) or a module-level store that
// isn't wired through React state at all, so calling the captured updater
// never actually re-renders the component under test. That hides the bug
// completely: it only shows up once `setNodes`/`setEdges` drives a *real*
// re-render, because only then can a content-identical-but-freshly-built
// array feed back into the very render that produced it.
//
// This mock is deliberately the odd one out in this suite: `useNodesState`/
// `useEdgesState` wrap React's own `useState`, so the setters behave exactly
// as they do with real ReactFlow for what matters here (a `setState` call
// that changes reference is a real commit, and one that doesn't is a
// no-op — real ReactFlow's own store makes that same distinction, which is
// why this bug has never been visible against the genuine library, only
// against a harness that skips it).
//
// Confirmed on unmodified origin/main before this fix: rendering GraphCanvas
// against this mock with only `nodes`/`edges` passed never settles — React
// logs "Maximum update depth exceeded" and the render count grows without
// bound. Root cause: GraphCanvasInner's own default parameter values
// (`highlightedNodeIds = []`, `nodeMarks = {}`, `dimmedEdgeIds = []`, …) are
// fresh object/array literals on every invocation of the component function,
// which cascades through the `reactFlowNodes`/`reactFlowEdges` memos into the
// "Update nodes when input changes" and "Update edges when input changes"
// effects on every single render, each of which used to commit a brand new
// (if content-identical) array unconditionally.
let renderCount = 0;
// What GraphCanvas last handed ReactFlow, and the selection listener it
// registered — the focus-view and remote-marker cases below assert on the
// committed node array itself (positions, data, reference identity).
let flowNodes = [];
let selectionOnChange = null;

vi.mock('reactflow', () => {
  const MockReactFlow = ({ children, nodes, edges }) => {
    flowNodes = nodes || [];
    return (
      <div data-testid="react-flow">
        <div data-testid="nodes">
          {nodes?.map((n) => (
            <div key={n.id} data-testid={`node-${n.id}`}>
              {n.data?.label}
            </div>
          ))}
        </div>
        <div data-testid="edges">
          {edges?.map((e) => (
            <div key={e.id} data-testid={`edge-${e.id}`} />
          ))}
        </div>
        {children}
      </div>
    );
  };
  return {
    default: MockReactFlow,
    ReactFlow: MockReactFlow,
    ReactFlowProvider: ({ children }) => <div>{children}</div>,
    // Real state, unlike every other test in this suite (see module comment
    // above) — this is the whole point of this regression test.
    useNodesState: (initialNodes) => {
      renderCount += 1;
      // A genuine, still-looping render never settles on its own (React's own
      // "too many re-renders" guard fires within its synchronous recursion
      // limit), so a low bound here turns a regression back into a fast,
      // readable failure instead of a hung test worker.
      if (renderCount > 60) {
        throw new Error('useNodesState render count exceeded 60 - GraphCanvas is not settling');
      }
      const [nodes, setNodes] = React.useState(initialNodes);
      return [nodes, setNodes, vi.fn()];
    },
    useEdgesState: (initialEdges) => {
      const [edges, setEdges] = React.useState(initialEdges);
      return [edges, setEdges, vi.fn()];
    },
    useReactFlow: () => ({
      fitView: vi.fn(),
      // ReactFlow's live store; entering the focus view snapshots it so that
      // leaving can restore the pre-focus canvas.
      getNodes: () => flowNodes,
      getEdges: () => [],
      setNodes: vi.fn(),
      setEdges: vi.fn(),
      screenToFlowPosition: ({ x, y }) => ({ x, y }),
      setCenter: vi.fn(),
      zoomIn: vi.fn(),
      zoomOut: vi.fn(),
      getViewport: () => ({ x: 0, y: 0, zoom: 1 }),
    }),
    useOnSelectionChange: ({ onChange }) => {
      selectionOnChange = onChange;
    },
    addEdge: (params, eds) => [...eds, params],
    Background: () => <div data-testid="background" />,
    Controls: () => <div data-testid="controls" />,
    MiniMap: () => <div data-testid="minimap" />,
    SelectionMode: { Partial: 'partial' },
    Handle: ({ type }) => <div data-testid={`handle-${type}`} />,
    Position: { Top: 'top', Bottom: 'bottom', Left: 'left', Right: 'right' },
    MarkerType: { ArrowClosed: 'arrowclosed', Arrow: 'arrow' },
  };
});

const sampleNodes = [
  { id: 'node-1', name: 'Node 1', type: 'Actor', description: 'a' },
  { id: 'node-2', name: 'Node 2', type: 'Initiative', description: 'b' },
];
const sampleEdges = [{ id: 'edge-1', source: 'node-1', target: 'node-2', type: 'RELATES_TO' }];

// A generous bound, not a tight prediction of "the" render count: the point of
// this regression test is distinguishing "settles" from "hangs or grows
// unboundedly", which an infinite loop always blows past. A real settle
// stabilizes within a handful of renders regardless of exactly how many
// effects happen to run on mount.
const SETTLE_BOUND = 20;

describe('GraphCanvas settles under real (non-pass-through) useNodesState/useEdgesState', () => {
  beforeEach(() => {
    renderCount = 0;
    flowNodes = [];
    selectionOnChange = null;
    vi.clearAllMocks();
  });

  it('a bare render with only nodes/edges settles instead of hanging or growing unboundedly', () => {
    render(<GraphCanvas nodes={sampleNodes} edges={sampleEdges} />);

    expect(renderCount).toBeGreaterThan(0);
    expect(renderCount).toBeLessThan(SETTLE_BOUND);
  });

  it('renders the actual node/edge content once settled (the fix does not just suppress work)', () => {
    render(<GraphCanvas nodes={sampleNodes} edges={sampleEdges} />);

    expect(document.querySelector('[data-testid="node-node-1"]')).not.toBeNull();
    expect(document.querySelector('[data-testid="node-node-2"]')).not.toBeNull();
    expect(document.querySelector('[data-testid="edge-edge-1"]')).not.toBeNull();
  });

  it('re-rendering with fresh-but-equal array/object props (a host re-render passing new literals) still settles', () => {
    // Every prop below is passed as a brand-new literal on each render, the
    // same shape GraphCanvasInner's own unstable default parameters produce
    // internally — this exercises the fix under the identical pattern without
    // relying on those specific defaults.
    const Wrapper = () => (
      <GraphCanvas
        nodes={sampleNodes}
        edges={sampleEdges}
        highlightedNodeIds={[]}
        hiddenNodeIds={[]}
        hiddenEdgeIds={[]}
        dimmedNodeIds={[]}
        dimmedEdgeIds={[]}
        nodeMarks={{}}
        pulsedNodeIds={{}}
      />
    );
    const { rerender } = render(<Wrapper />);
    expect(renderCount).toBeLessThan(SETTLE_BOUND);

    rerender(<Wrapper />);
    rerender(<Wrapper />);

    // Three independent rerenders, each with fresh literal props, must not
    // accumulate into unbounded growth — settling after each is the point.
    expect(renderCount).toBeLessThan(SETTLE_BOUND * 3);
  });
});

// A triangle, so the focus view on any corner keeps all three nodes: the node
// count is the same inside and outside focus, and nothing that feeds a node's
// `data` changes either. Entering or leaving focus is then a render whose only
// difference is positions — the one case where the settle check's structural
// comparison must still see `position`, or it hands back the stale array.
const triangleNodes = [
  { id: 'node-1', name: 'Node 1', type: 'Actor' },
  { id: 'node-2', name: 'Node 2', type: 'Initiative' },
  { id: 'node-3', name: 'Node 3', type: 'Initiative' },
];
const triangleEdges = [
  { id: 'edge-1', source: 'node-1', target: 'node-2', type: 'RELATES_TO' },
  { id: 'edge-2', source: 'node-2', target: 'node-3', type: 'RELATES_TO' },
  { id: 'edge-3', source: 'node-3', target: 'node-1', type: 'RELATES_TO' },
];
const positionsById = () => new Map(flowNodes.map((n) => [n.id, n.position]));

describe('GraphCanvas settle check still commits a position-only change', () => {
  beforeEach(() => {
    renderCount = 0;
    flowNodes = [];
    selectionOnChange = null;
  });

  it('moves the root to the focus centre and back to its pre-focus position, with an unchanged node count', () => {
    render(<GraphCanvas nodes={triangleNodes} edges={triangleEdges} compactMode="on" />);
    const before = positionsById();
    expect(before.size).toBe(3);
    expect(before.get('node-1')).not.toEqual({ x: 0, y: 0 });

    act(() => selectionOnChange({ nodes: [{ id: 'node-1', type: 'custom' }], edges: [] }));
    act(() => {
      fireEvent.click(screen.getByRole('button', { name: 'Focus on selected node' }));
    });

    const focused = positionsById();
    expect(focused.size).toBe(3);
    expect(focused.get('node-1')).toEqual({ x: 0, y: 0 });
    for (const id of ['node-2', 'node-3']) {
      expect(focused.get(id)).not.toEqual(before.get(id));
    }

    // Leaving focus: the root was at {x: 0}, the node count is unchanged, and
    // every node must land back where it was before focus.
    act(() => {
      fireEvent.click(screen.getByRole('button', { name: 'Back to whole graph' }));
    });

    const restored = positionsById();
    expect(restored.size).toBe(3);
    for (const id of ['node-1', 'node-2', 'node-3']) {
      expect(restored.get(id)).toEqual(before.get(id));
    }
  });
});

// `remoteMarkerEqual` is module-private, so it is exercised through the two
// effects that use it: the remote-selection and remote-lease mirrors that
// stamp a collaborator's marker onto an annotation node's data.
const NOTE = [{ id: 'note-1', kind: 'note', position: { x: 10, y: 10 }, text: 'a note' }];
const MARKER = { clientId: 'c2', color: '#e6194b', displayName: 'Ada' };
const noteNode = () => flowNodes.find((n) => n.id === 'note-1');

const MIRRORS = [
  { prop: 'remoteSelections', dataKey: 'remoteSelection' },
  { prop: 'remoteLeases', dataKey: 'remoteLease' },
];

describe.each(MIRRORS)(
  'GraphCanvas $prop mirror compares markers by value',
  ({ prop, dataKey }) => {
    beforeEach(() => {
      renderCount = 0;
      flowNodes = [];
      selectionOnChange = null;
    });

    // The other mirror's map keeps one identity across every rerender below, so
    // its effect never re-runs and cannot mask what this one did.
    const otherProp = prop === 'remoteSelections' ? 'remoteLeases' : 'remoteSelections';
    const stableOther = {};
    const renderWith = (marker) => (
      <GraphCanvas
        nodes={[]}
        edges={[]}
        annotationsToRestore={NOTE}
        {...{ [prop]: { 'note-1': marker }, [otherProp]: stableOther }}
      />
    );

    it.each(['clientId', 'color', 'displayName'])(
      'a marker differing only in %s replaces the previous one',
      (field) => {
        const { rerender } = render(renderWith({ ...MARKER }));
        expect(noteNode().data[dataKey]).toEqual(MARKER);

        const changed = { ...MARKER, [field]: `${MARKER[field]}-changed` };
        rerender(renderWith(changed));

        expect(noteNode().data[dataKey]).toEqual(changed);
      }
    );

    it('a fresh but content-equal marker, twice in a row, keeps the committed node array and node', () => {
      const { rerender } = render(renderWith({ ...MARKER }));
      const committedNodes = flowNodes;
      const committedNote = noteNode();
      expect(committedNote.data[dataKey]).toEqual(MARKER);

      rerender(renderWith({ ...MARKER }));
      expect(flowNodes).toBe(committedNodes);
      expect(noteNode()).toBe(committedNote);

      rerender(renderWith({ ...MARKER }));
      expect(flowNodes).toBe(committedNodes);
      expect(noteNode()).toBe(committedNote);
    });
  }
);
