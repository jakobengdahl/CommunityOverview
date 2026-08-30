import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

// Mirrors the reactflow mock in GraphCanvas.test.jsx. useReactFlow and
// useNodesState each hand back fresh function identities (screenToFlowPosition,
// setNodes) on every render, which reproduces exactly the "handleAddGroup loses
// memoization" scenario the createGroupSignal reset guard exists to protect
// against — see the comment above the create-group effect in GraphCanvas.jsx.
vi.mock('reactflow', () => {
  const MockReactFlow = ({ children, nodes, edges }) => (
    <div data-testid="react-flow" className="react-flow">
      <div data-testid="nodes">
        {nodes?.map((node) => (
          <div key={node.id} data-testid={`node-${node.id}`}>
            {node.data?.label}
          </div>
        ))}
      </div>
      <div data-testid="edges">
        {edges?.map((edge) => (
          <div key={edge.id} data-testid={`edge-${edge.id}`} />
        ))}
      </div>
      {children}
    </div>
  );

  return {
    default: MockReactFlow,
    ReactFlow: MockReactFlow,
    ReactFlowProvider: ({ children }) => <div>{children}</div>,
    useNodesState: (initialNodes) => [initialNodes || [], vi.fn(), vi.fn()],
    useEdgesState: (initialEdges) => [initialEdges || [], vi.fn(), vi.fn()],
    useReactFlow: () => ({
      fitView: vi.fn(),
      getNodes: () => [],
      getEdges: () => [],
      setNodes: vi.fn(),
      setEdges: vi.fn(),
      screenToFlowPosition: () => ({ x: 0, y: 0 }),
      setCenter: vi.fn(),
    }),
    useOnSelectionChange: () => {},
    Background: () => <div data-testid="background" />,
    Controls: () => <div data-testid="controls" />,
    MiniMap: () => <div data-testid="minimap" />,
    SelectionMode: { Partial: 'partial' },
    Handle: ({ type }) => <div data-testid={`handle-${type}`} />,
    Position: { Top: 'top', Bottom: 'bottom', Left: 'left', Right: 'right' },
    MarkerType: { ArrowClosed: 'arrowclosed', Arrow: 'arrow' },
  };
});

const sampleNodes = [{ id: 'node-1', name: 'Node 1', type: 'Actor', description: 'a' }];
const sampleEdges = [];

describe('GraphCanvas createGroupSignal reset guard', () => {
  // small-fix: createGroupSignal never reset (smallfix-creategroupsignal-never-reset-20260820).
  // The host never resets createGroupSignal to 0 after handling it, unlike
  // saveViewSignal. Before the fix, the effect guarded only on `createGroupSignal
  // > 0`, so any re-render that changed handleAddGroup's identity (here,
  // guaranteed by the mocked useReactFlow/useNodesState above returning fresh
  // functions every render) re-ran the effect and created a second, spurious
  // group for the same toolbar click.
  it('does not create a second group on a re-render with the same signal value and a fresh handleAddGroup reference', () => {
    const onCreateGroup = vi.fn();
    const { rerender } = render(
      <GraphCanvas
        nodes={sampleNodes}
        edges={sampleEdges}
        createGroupSignal={1}
        onCreateGroup={onCreateGroup}
      />
    );

    expect(onCreateGroup).toHaveBeenCalledTimes(1);

    // Re-render with createGroupSignal unchanged. Every mocked hook above
    // returns new function identities on this pass, so handleAddGroup is a
    // brand-new reference — the exact condition that used to retrigger group
    // creation.
    rerender(
      <GraphCanvas
        nodes={sampleNodes}
        edges={sampleEdges}
        createGroupSignal={1}
        onCreateGroup={onCreateGroup}
      />
    );

    expect(onCreateGroup).toHaveBeenCalledTimes(1);
  });

  it('still creates a group for each genuinely new signal value', () => {
    const onCreateGroup = vi.fn();
    const { rerender } = render(
      <GraphCanvas
        nodes={sampleNodes}
        edges={sampleEdges}
        createGroupSignal={1}
        onCreateGroup={onCreateGroup}
      />
    );

    expect(onCreateGroup).toHaveBeenCalledTimes(1);

    rerender(
      <GraphCanvas
        nodes={sampleNodes}
        edges={sampleEdges}
        createGroupSignal={2}
        onCreateGroup={onCreateGroup}
      />
    );

    expect(onCreateGroup).toHaveBeenCalledTimes(2);
  });
});
