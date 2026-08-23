import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, act, cleanup } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

// A live node store, same pattern as GraphCanvasUndo.test.jsx: useNodesState's
// setter writes straight into `store.nodes` so effects that run in sequence on
// the same commit (restore-from-session, then the input-reconciliation effect)
// see each other's writes, the way they do in the real app. setStoreNodes is
// hoisted alongside `store` and defined once so it has a stable identity across
// renders (real ReactFlow's setNodes is stable too) — an unstable setNodes
// reference is in every reconciliation/restore effect's dep array, so it would
// make them all re-run on every render and mask which effect actually reacted
// to which prop change.
const { store, setStoreNodes } = vi.hoisted(() => {
  const store = { nodes: [], edges: [] };
  const setStoreNodes = (updater) => {
    store.nodes = typeof updater === 'function' ? updater(store.nodes) : updater;
  };
  return { store, setStoreNodes };
});

vi.mock('reactflow', () => {
  const MockReactFlow = (props) => <div data-testid="react-flow">{props.children}</div>;
  return {
    __esModule: true,
    default: MockReactFlow,
    ReactFlow: MockReactFlow,
    ReactFlowProvider: ({ children }) => <div>{children}</div>,
    useNodesState: () => [store.nodes, setStoreNodes, vi.fn()],
    useEdgesState: () => [store.edges, vi.fn(), vi.fn()],
    addEdge: (_params, edges) => edges,
    useReactFlow: () => ({
      getNodes: () => store.nodes,
      getEdges: () => [],
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
    NodeResizer: () => null,
    SelectionMode: { Partial: 'partial' },
  };
});

const GROUPS_TO_RESTORE = [
  { id: 'g1', position: { x: 0, y: 0 }, label: 'Group 1', style: { width: 300, height: 200 } },
];
const ANNOTATIONS_TO_RESTORE = [
  { id: 'note-1', kind: 'note', position: { x: 10, y: 10 }, text: 'a note' },
  { id: 'label-1', kind: 'label', position: { x: 20, y: 20 }, text: 'a label' },
  // Generic v1 annotation type (docs/ANNOTATION_CONTRACT.md), rendered through
  // GenericAnnotationNode rather than dedicated per-type UX.
  { id: 'frame-1', kind: 'frame', position: { x: 30, y: 30 }, color: '#fff' },
];

const manualNodeIds = () => new Set(store.nodes.map((n) => n.id));
const expectOverlaysPresent = () => {
  const ids = manualNodeIds();
  expect(ids.has('g1')).toBe(true);
  expect(ids.has('note-1')).toBe(true);
  expect(ids.has('label-1')).toBe(true);
  expect(ids.has('frame-1')).toBe(true);
};

describe('GraphCanvas preserves groups and annotations across an in-place node edit', () => {
  beforeEach(() => {
    store.nodes = [];
    store.edges = [];
  });
  afterEach(() => cleanup());

  it('keeps group/note/label/generic-annotation overlays when the input nodes change without a clear signal', () => {
    const nodeV1 = { id: 'node-1', name: 'Node 1', type: 'Actor', description: 'a' };
    const { rerender } = render(
      <GraphCanvas
        nodes={[nodeV1]}
        edges={[]}
        groupsToRestore={GROUPS_TO_RESTORE}
        annotationsToRestore={ANNOTATIONS_TO_RESTORE}
      />
    );

    // Sanity check: the session-restore path actually placed all four overlays
    // on the canvas before the edit under test happens.
    expectOverlaysPresent();

    // Simulate an ordinary in-place node edit (handleNodeUpdate et al.): the
    // same node id comes back with updated fields, clearGroupsFlag stays false
    // (its default — the bug was graphStore's updateVisualization always
    // raising it), and sessionKey does not change.
    const nodeV2 = { ...nodeV1, description: 'edited' };
    act(() => {
      rerender(
        <GraphCanvas
          nodes={[nodeV2]}
          edges={[]}
          groupsToRestore={GROUPS_TO_RESTORE}
          annotationsToRestore={ANNOTATIONS_TO_RESTORE}
        />
      );
    });

    expectOverlaysPresent();
  });

  it('still clears every overlay on an explicit clearGroupsFlag (genuine replace/clear)', () => {
    const nodeV1 = { id: 'node-1', name: 'Node 1', type: 'Actor', description: 'a' };
    const { rerender } = render(
      <GraphCanvas
        nodes={[nodeV1]}
        edges={[]}
        groupsToRestore={GROUPS_TO_RESTORE}
        annotationsToRestore={ANNOTATIONS_TO_RESTORE}
      />
    );
    expectOverlaysPresent();

    act(() => {
      rerender(
        <GraphCanvas
          nodes={[]}
          edges={[]}
          clearGroupsFlag={true}
          groupsToRestore={GROUPS_TO_RESTORE}
          annotationsToRestore={ANNOTATIONS_TO_RESTORE}
        />
      );
    });

    const ids = manualNodeIds();
    expect(ids.has('g1')).toBe(false);
    expect(ids.has('note-1')).toBe(false);
    expect(ids.has('label-1')).toBe(false);
    expect(ids.has('frame-1')).toBe(false);
  });
});
