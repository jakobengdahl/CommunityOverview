import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/react';
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

vi.mock('reactflow', () => {
  const MockReactFlow = ({ children, nodes, edges }) => (
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
      getNodes: () => [],
      getEdges: () => [],
      setNodes: vi.fn(),
      setEdges: vi.fn(),
      screenToFlowPosition: ({ x, y }) => ({ x, y }),
      setCenter: vi.fn(),
      zoomIn: vi.fn(),
      zoomOut: vi.fn(),
      getViewport: () => ({ x: 0, y: 0, zoom: 1 }),
    }),
    useOnSelectionChange: () => {},
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
