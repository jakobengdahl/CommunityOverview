import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, act } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

// Shared fitView spy so every render returns the *same* mock and the test can
// assert how many times the canvas refit across a session switch. The position
// side of the switch (a node shared between sessions adopting the new session's
// coordinates) is covered deterministically by reconcileSessionNodes.test.js;
// here we pin the viewport half of the fix.
const hoisted = vi.hoisted(() => ({ fitView: vi.fn() }));

vi.mock('reactflow', () => {
  const MockReactFlow = ({ children, nodes }) => (
    <div data-testid="react-flow">
      <div data-testid="nodes">
        {nodes?.map((node) => (
          <div key={node.id} data-testid={`node-${node.id}`}>
            {node.data?.label}
          </div>
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
      fitView: hoisted.fitView,
      getNodes: () => [],
      getEdges: () => [],
      getViewport: () => ({ x: 0, y: 0, zoom: 1 }),
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
    Handle: () => <div />,
    Position: { Top: 'top', Bottom: 'bottom', Left: 'left', Right: 'right' },
    MarkerType: { ArrowClosed: 'arrowclosed', Arrow: 'arrow' },
  };
});

const nodesA = [{ id: 'shared', name: 'Shared', type: 'Actor', description: 'a' }];
const nodesB = [
  { id: 'shared', name: 'Shared', type: 'Actor', description: 'a' },
  { id: 'b-only', name: 'B only', type: 'Initiative', description: 'b' },
];

describe('GraphCanvas — refit on session switch', () => {
  let rafSpy;

  beforeEach(() => {
    hoisted.fitView.mockClear();
    // Run the deferred fit synchronously so the assertion doesn't race a frame.
    rafSpy = vi.spyOn(globalThis, 'requestAnimationFrame').mockImplementation((cb) => {
      cb();
      return 1;
    });
    vi.spyOn(globalThis, 'cancelAnimationFrame').mockImplementation(() => {});
  });

  afterEach(() => {
    rafSpy.mockRestore();
    vi.restoreAllMocks();
  });

  it('refits the view when sessionKey changes, but not on the initial mount', () => {
    const { rerender } = render(<GraphCanvas nodes={nodesA} edges={[]} sessionKey="session-a" />);

    // Mount fitting is handled by the ReactFlow fitView prop, not this effect.
    expect(hoisted.fitView).not.toHaveBeenCalled();

    act(() => {
      rerender(<GraphCanvas nodes={nodesB} edges={[]} sessionKey="session-b" />);
    });

    expect(hoisted.fitView).toHaveBeenCalledTimes(1);
    expect(hoisted.fitView).toHaveBeenCalledWith(expect.objectContaining({ padding: 0.2 }));
  });

  it('does not refit when other props change but the session stays the same', () => {
    const { rerender } = render(<GraphCanvas nodes={nodesA} edges={[]} sessionKey="session-a" />);
    hoisted.fitView.mockClear();

    act(() => {
      rerender(
        <GraphCanvas
          nodes={nodesA}
          edges={[]}
          sessionKey="session-a"
          highlightedNodeIds={['shared']}
        />
      );
    });

    expect(hoisted.fitView).not.toHaveBeenCalled();
  });
});
