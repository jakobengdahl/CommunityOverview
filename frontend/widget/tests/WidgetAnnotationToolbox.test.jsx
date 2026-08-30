/**
 * Regression coverage for the widget's annotation toolbox on a narrow
 * viewport.
 *
 * `Widget.jsx` mounts `GraphCanvas` directly with no `MobileShell` and no
 * `annotationToolboxPortalContainer` wired - it never opted into the mobile
 * shared-surface integration (task-annotation-responsive-bottom-toolbox).
 * `GraphCanvas`'s `compactMode` is left at its default ('auto'), so on a
 * narrow viewport it self-detects `isCompact=true` via matchMedia.
 *
 * Before the shared-surface integration, that combination rendered the
 * always-on compact strip. The integration briefly regressed it: every
 * `isCompact && !annotationToolboxPortalContainer` case rendered nothing,
 * silently dropping a live, working feature from this consumer. This test
 * drives the real `GraphCanvas` (reactflow mocked) through the real `Widget`
 * component - not a mocked GraphCanvas - so it actually exercises the render
 * path Widget.jsx depends on, and would have failed against that regression.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import Widget from '../src/Widget';
import * as mcp from '../src/mcpClient';

vi.mock('reactflow', () => {
  const MockReactFlow = ({ children }) => <div data-testid="react-flow">{children}</div>;
  return {
    __esModule: true,
    default: MockReactFlow,
    ReactFlow: MockReactFlow,
    ReactFlowProvider: ({ children }) => <div>{children}</div>,
    useNodesState: (initial) => [initial || [], vi.fn(), vi.fn()],
    useEdgesState: (initial) => [initial || [], vi.fn(), vi.fn()],
    addEdge: (_params, edges) => edges,
    useReactFlow: () => ({
      fitView: vi.fn(),
      zoomIn: vi.fn(),
      zoomOut: vi.fn(),
      getNodes: () => [],
      getEdges: () => [],
      setNodes: vi.fn(),
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
    MarkerType: { ArrowClosed: 'arrowclosed' },
    Position: { Top: 'top', Bottom: 'bottom', Left: 'left', Right: 'right' },
  };
});

vi.mock('@community-graph/ui-graph-canvas/styles', () => ({}));

vi.mock('../src/mcpClient', () => ({
  isMCPAvailable: vi.fn(),
  searchGraph: vi.fn(),
  getRelatedNodes: vi.fn(),
  getNodeDetails: vi.fn(),
  addNodes: vi.fn(),
  updateNode: vi.fn(),
  deleteNodes: vi.fn(),
}));

describe('Widget annotation toolbox on a narrow viewport', () => {
  let originalMatchMedia;

  beforeEach(() => {
    vi.clearAllMocks();
    mcp.isMCPAvailable.mockReturnValue(true);
    originalMatchMedia = window.matchMedia;
  });

  afterEach(() => {
    cleanup();
    window.matchMedia = originalMatchMedia;
  });

  it('falls back to the inline compact strip - Widget never wires annotationToolboxPortalContainer, so it must not lose the toolbox on a narrow viewport', () => {
    window.matchMedia = vi.fn().mockImplementation((query) => ({
      matches: query === '(max-width: 768px)',
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }));

    render(<Widget />);

    const toolbox = screen.getByTestId('annotation-toolbox');
    expect(toolbox).toHaveClass('annotation-toolbox--compact');
    expect(toolbox).not.toHaveClass('annotation-toolbox--sheet');
  });

  it('renders the desktop toolbox on a wide viewport, unaffected by the mobile fallback', () => {
    window.matchMedia = vi.fn().mockImplementation(() => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }));

    render(<Widget />);

    const toolbox = screen.getByTestId('annotation-toolbox');
    expect(toolbox).not.toHaveClass('annotation-toolbox--compact');
    expect(toolbox).not.toHaveClass('annotation-toolbox--sheet');
  });
});
