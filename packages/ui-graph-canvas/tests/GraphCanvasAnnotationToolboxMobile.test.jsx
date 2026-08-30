import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

// Mirrors GraphCanvasAnnotationToolbox.test.jsx's mock exactly - this suite
// is about *where* the toolbox mounts on mobile (task-annotation-responsive-
// bottom-toolbox's mobile shared-surface integration), not a second copy of
// its creation-logic coverage.
const hoisted = vi.hoisted(() => ({ setNodes: vi.fn() }));

function pointerEvent(
  type,
  { pointerId = 1, pointerType = 'mouse', clientX = 0, clientY = 0 } = {}
) {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX, clientY });
  Object.defineProperty(event, 'pointerId', { value: pointerId });
  Object.defineProperty(event, 'pointerType', { value: pointerType });
  return event;
}

// Placement is a pointer gesture on the canvas wrapper, not `onPaneClick` —
// that callback never fires while the pane is in selection mode.
function placeOnPane(x = 120, y = 90) {
  const pane = screen.getByTestId('pane');
  fireEvent(pane, pointerEvent('pointerdown', { clientX: x, clientY: y }));
  fireEvent(pane, pointerEvent('pointerup', { clientX: x, clientY: y }));
}

vi.mock('reactflow', () => {
  const MockReactFlow = ({ children, onPaneContextMenu, onPaneMouseDown, onPaneClick }) => (
    <div data-testid="react-flow" className="react-flow">
      <div
        data-testid="pane"
        className="react-flow__pane"
        onMouseDown={(event) => onPaneMouseDown?.(event)}
        onContextMenu={(event) => onPaneContextMenu?.(event)}
        onClick={(event) => onPaneClick?.(event)}
      />
      {children}
    </div>
  );
  return {
    default: MockReactFlow,
    ReactFlow: MockReactFlow,
    ReactFlowProvider: ({ children }) => <div>{children}</div>,
    useNodesState: (initial) => [initial || [], hoisted.setNodes, vi.fn()],
    useEdgesState: (initial) => [initial || [], vi.fn(), vi.fn()],
    useReactFlow: () => ({
      fitView: vi.fn(),
      zoomIn: vi.fn(),
      zoomOut: vi.fn(),
      getNodes: () => [],
      getEdges: () => [],
      setNodes: hoisted.setNodes,
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

function findCreatedNode(type) {
  for (const call of hoisted.setNodes.mock.calls) {
    const updater = call[0];
    if (typeof updater !== 'function') continue;
    let result;
    try {
      result = updater([]);
    } catch {
      continue;
    }
    const found = Array.isArray(result) && result.find((n) => n.type === type);
    if (found) return found;
  }
  return undefined;
}

describe('GraphCanvas annotation toolbox - mobile shared-surface integration', () => {
  let portalContainer;

  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    // A real, document-attached node - createPortal requires one, and the
    // host (MobileShell) always hands up a mounted div, never a detached one.
    portalContainer = document.createElement('div');
    document.body.appendChild(portalContainer);
  });

  afterEach(() => {
    cleanup();
    portalContainer.remove();
  });

  describe('mobile (compact) with no portal container wired', () => {
    it('falls back to the inline compact strip - a non-integrated host (e.g. the widget embed) must never lose the toolbox', () => {
      render(<GraphCanvas nodes={[]} edges={[]} compactMode="on" />);

      // Present, inline, in the compact (not sheet) strip...
      const toolbox = screen.getByTestId('annotation-toolbox');
      expect(toolbox).toHaveClass('annotation-toolbox--compact');
      expect(toolbox).not.toHaveClass('annotation-toolbox--sheet');
      // ...and never portaled anywhere, since no portal container was given.
      expect(portalContainer.querySelector('[data-testid="annotation-toolbox"]')).toBeNull();
    });

    it('creates annotations from the fallback compact strip exactly as the desktop one does', () => {
      const onAnnotationChange = vi.fn();
      render(
        <GraphCanvas
          nodes={[]}
          edges={[]}
          compactMode="on"
          onAnnotationChange={onAnnotationChange}
        />
      );

      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
      fireEvent.click(screen.getByRole('button', { name: /^note$/i }));

      // Two-step creation (task-annotation-tool-modes): the toolbox arms the
      // tool, the press on empty canvas places the object. Identical on mobile
      // and desktop — the sheet is a different layout of the same toolbox,
      // not a different creation contract.
      placeOnPane();

      expect(onAnnotationChange).toHaveBeenCalled();
      expect(findCreatedNode('note')).toBeTruthy();
    });
  });

  describe('mobile (compact) with a portal container wired', () => {
    it('portals the toolbox into the given container, already expanded, with the sheet variant class', () => {
      render(
        <GraphCanvas
          nodes={[]}
          edges={[]}
          compactMode="on"
          annotationToolboxPortalContainer={portalContainer}
        />
      );

      const toolbox = portalContainer.querySelector('[data-testid="annotation-toolbox"]');
      expect(toolbox).toBeTruthy();
      expect(toolbox).toHaveClass('annotation-toolbox--sheet');
      // No collapse/expand toggle in sheet mode - the whole sheet's own close
      // button is the way out, and items are visible without an extra tap.
      expect(screen.queryByRole('button', { name: /add annotation/i })).not.toBeInTheDocument();
      expect(screen.getByRole('button', { name: /^note$/i })).toBeInTheDocument();
    });

    it('creates an annotation from the portaled toolbox exactly as the desktop one does', () => {
      const onAnnotationChange = vi.fn();
      render(
        <GraphCanvas
          nodes={[]}
          edges={[]}
          compactMode="on"
          annotationToolboxPortalContainer={portalContainer}
          onAnnotationChange={onAnnotationChange}
        />
      );

      fireEvent.click(screen.getByRole('button', { name: /^note$/i }));

      // Two-step creation (task-annotation-tool-modes): the toolbox arms the
      // tool, the press on empty canvas places the object. Identical on mobile
      // and desktop — the sheet is a different layout of the same toolbox,
      // not a different creation contract.
      placeOnPane();

      expect(onAnnotationChange).toHaveBeenCalled();
      expect(findCreatedNode('note')).toBeTruthy();
    });

    it('disarms an in-progress freehand stroke when the sheet closes (container goes back to null)', () => {
      const { rerender } = render(
        <GraphCanvas
          nodes={[]}
          edges={[]}
          compactMode="on"
          annotationToolboxPortalContainer={portalContainer}
        />
      );

      fireEvent.click(screen.getByRole('button', { name: /^freehand$/i }));
      expect(screen.getByRole('button', { name: /^freehand$/i })).toHaveAttribute(
        'aria-pressed',
        'true'
      );

      // The sheet closing: MobileShell's ref callback releases the container.
      rerender(
        <GraphCanvas
          nodes={[]}
          edges={[]}
          compactMode="on"
          annotationToolboxPortalContainer={null}
        />
      );

      // Re-open the sheet and confirm freehand did not stay silently armed
      // with no visible toolbox to show for it in between.
      rerender(
        <GraphCanvas
          nodes={[]}
          edges={[]}
          compactMode="on"
          annotationToolboxPortalContainer={portalContainer}
        />
      );
      expect(screen.getByRole('button', { name: /^freehand$/i })).toHaveAttribute(
        'aria-pressed',
        'false'
      );
    });
  });

  describe('desktop is unaffected', () => {
    it('ignores annotationToolboxPortalContainer entirely: the toolbox stays in its own fixed wrapper, never in the given container', () => {
      render(
        <GraphCanvas
          nodes={[]}
          edges={[]}
          compactMode="off"
          annotationToolboxPortalContainer={portalContainer}
        />
      );

      // Present, in its own default (toolbar) chrome...
      const toolbox = screen.getByTestId('annotation-toolbox');
      expect(toolbox).not.toHaveClass('annotation-toolbox--sheet');
      expect(screen.getByRole('button', { name: /add annotation/i })).toBeInTheDocument();
      // ...and never portaled into the container a mobile host might have
      // wired up - a stray prop must not change desktop's DOM placement.
      expect(portalContainer.querySelector('[data-testid="annotation-toolbox"]')).toBeNull();
    });

    it('creates annotations via the unchanged desktop path with the prop present', () => {
      const onAnnotationChange = vi.fn();
      render(
        <GraphCanvas
          nodes={[]}
          edges={[]}
          compactMode="off"
          annotationToolboxPortalContainer={portalContainer}
          onAnnotationChange={onAnnotationChange}
        />
      );

      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
      fireEvent.click(screen.getByRole('button', { name: /^note$/i }));

      // Two-step creation (task-annotation-tool-modes): the toolbox arms the
      // tool, the press on empty canvas places the object. Identical on mobile
      // and desktop — the sheet is a different layout of the same toolbox,
      // not a different creation contract.
      placeOnPane();

      expect(onAnnotationChange).toHaveBeenCalled();
      expect(findCreatedNode('note')).toBeTruthy();
    });
  });
});
