import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

// The host app hands GraphCanvas each node's and edge's staged state and the
// translated marker labels (docs/SESSION_OVERLAY_CONTRACT.md §8.1); they must
// reach what ReactFlow renders. The mocked state hooks hand back each render's
// derived arrays, so the ReactFlow props show what GraphCanvas built.
const hoisted = vi.hoisted(() => ({ props: null }));

vi.mock('reactflow', () => {
  const MockReactFlow = (props) => {
    hoisted.props = props;
    return <div data-testid="react-flow">{props.children}</div>;
  };
  return {
    default: MockReactFlow,
    ReactFlow: MockReactFlow,
    ReactFlowProvider: ({ children }) => <div>{children}</div>,
    useNodesState: (initial) => [initial || [], vi.fn(), vi.fn()],
    useEdgesState: (initial) => [initial || [], vi.fn(), vi.fn()],
    useReactFlow: () => ({
      fitView: vi.fn(),
      getNodes: () => [],
      getEdges: () => [],
      setNodes: vi.fn(),
      setEdges: vi.fn(),
      screenToFlowPosition: ({ x, y }) => ({ x, y }),
      setCenter: vi.fn(),
      getViewport: () => ({ x: 0, y: 0, zoom: 1 }),
    }),
    useOnSelectionChange: () => {},
    Background: () => null,
    Controls: () => null,
    MiniMap: () => null,
    NodeResizer: () => null,
    SelectionMode: { Partial: 'partial' },
    MarkerType: { Arrow: 'arrow', ArrowClosed: 'arrowclosed' },
  };
});

const NODES = [
  { id: 'a', name: 'A', type: 'Actor' },
  { id: 'b', name: 'B', type: 'Actor' },
];
const EDGES = [{ id: 'e1', source: 'a', target: 'b', type: 'RELATES_TO' }];

function rendered(props) {
  render(<GraphCanvas nodes={NODES} edges={EDGES} {...props} />);
  const node = (id) => hoisted.props.nodes.find((n) => n.id === id);
  const edge = (id) => hoisted.props.edges.find((e) => e.id === id);
  return { node, edge };
}

describe('GraphCanvas staged states', () => {
  beforeEach(() => {
    hoisted.props = null;
  });

  it('passes each node and edge its staged state and the host-translated label', () => {
    const { node, edge } = rendered({
      stagedStates: { nodes: { a: 'created' }, edges: { e1: 'deleted' } },
      stagedStateLabels: { created: 'Granskad: ny', deleted: 'Granskad: raderad' },
    });
    expect(node('a').data.stagedState).toBe('created');
    expect(node('a').data.stagedLabel).toBe('Granskad: ny');
    expect(node('b').data.stagedState).toBeNull();
    expect(node('b').data.stagedLabel).toBeNull();
    expect(edge('e1').data.stagedState).toBe('deleted');
    expect(edge('e1').label).toContain('Granskad: raderad');
    expect(edge('e1').className).toContain('staged-edge-deleted');
  });

  it('falls back to the English labels for a state the host did not translate', () => {
    const { node } = rendered({ stagedStates: { nodes: { a: 'updated' }, edges: {} } });
    expect(node('a').data.stagedLabel).toBe('Staged: changed');
  });

  it('marks nothing when no staged states are given', () => {
    const { node, edge } = rendered({});
    expect(node('a').data.stagedState).toBeNull();
    expect(edge('e1').data.stagedState).toBeNull();
    expect(edge('e1').label).toBe('RELATES_TO');
  });
});
