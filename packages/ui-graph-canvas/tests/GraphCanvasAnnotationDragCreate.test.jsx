// Drag-to-create from the annotation toolbox (task-annotation-toolbox-drag-to-create):
// dragging a toolbox item creates the annotation at the point where the
// gesture ends, rather than always at the viewport centre (that click path is
// covered by GraphCanvasAnnotationToolbox.test.jsx). Two independent
// mechanisms reach GraphCanvas here — HTML5 dataTransfer for a fine pointer
// (mirrors FloatingToolbar's own node-type drag, and the OS/node-type drop
// paths GraphCanvasImageIngest.test.jsx already covers) and Pointer Events for
// a coarse one — so both get their own drop-position assertions.
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

const hoisted = vi.hoisted(() => ({ setNodes: vi.fn() }));

vi.mock('reactflow', () => {
  const MockReactFlow = ({ children, onPaneContextMenu, onPaneMouseDown, onDrop, onDragOver }) => (
    <div data-testid="react-flow" className="react-flow">
      <div
        data-testid="pane"
        onMouseDown={(event) => onPaneMouseDown?.(event)}
        onContextMenu={(event) => onPaneContextMenu?.(event)}
        onDrop={(event) => onDrop?.(event)}
        onDragOver={(event) => onDragOver?.(event)}
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

// setNodes fires from several effects; find the created node by applying every
// captured updater to an empty node array and collecting the result. Mirrors
// the helper in GraphCanvasAnnotationToolbox.test.jsx/GraphCanvasAnnotations.test.jsx.
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

// A malformed payload must add nothing, whatever it might be — count nodes
// actually added across every captured updater rather than asserting
// setNodes itself was never called, since other, unrelated effects are free
// to call it too.
function totalNodesCreated() {
  let count = 0;
  for (const call of hoisted.setNodes.mock.calls) {
    const updater = call[0];
    if (typeof updater !== 'function') continue;
    try {
      const result = updater([]);
      if (Array.isArray(result)) count += result.length;
    } catch {
      // ignore
    }
  }
  return count;
}

// jsdom has no DragEvent constructor (https://github.com/jsdom/jsdom/issues/2913),
// so dispatch a plain Event with dataTransfer/clientX/clientY attached by
// hand — the same helper GraphCanvasImageIngest.test.jsx uses for the drop
// side of the very same limitation.
function dispatchDrop(target, { dataTransfer, clientX, clientY }) {
  const event = new Event('drop', { bubbles: true, cancelable: true });
  event.dataTransfer = dataTransfer;
  event.clientX = clientX;
  event.clientY = clientY;
  target.dispatchEvent(event);
}

// jsdom has no PointerEvent constructor either — same pattern
// AnnotationToolbox.test.jsx/GraphCanvasFreehandDrawing.test.jsx use.
function pointerEvent(type, { pointerId = 1, clientX = 0, clientY = 0 } = {}) {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX, clientY });
  Object.defineProperty(event, 'pointerId', { value: pointerId });
  return event;
}

// The toolbox's pointer-drag handlers are plain addEventListener listeners
// (not React's synthetic event system), so a dispatch outside act() leaves
// the resulting state update unflushed when the next line asserts on it.
function dispatch(target, event) {
  act(() => {
    target.dispatchEvent(event);
  });
}

describe('GraphCanvas annotation toolbox drag-to-create', () => {
  beforeEach(() => vi.clearAllMocks());

  describe('fine pointer: HTML5 dataTransfer drop', () => {
    it('creates a note at the drop position, not the viewport centre', () => {
      render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      dispatchDrop(screen.getByTestId('pane'), {
        dataTransfer: {
          files: [],
          getData: (fmt) =>
            fmt === 'application/annotation-kind' ? JSON.stringify({ kind: 'note' }) : '',
        },
        clientX: 321,
        clientY: 654,
      });

      const note = findCreatedNode('note');
      expect(note).toBeTruthy();
      expect(note.position).toEqual({ x: 321, y: 654 });
    });

    it('carries the shape option through to the created node', () => {
      render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      dispatchDrop(screen.getByTestId('pane'), {
        dataTransfer: {
          files: [],
          getData: (fmt) =>
            fmt === 'application/annotation-kind'
              ? JSON.stringify({ kind: 'shape', shape: 'hexagon' })
              : '',
        },
        clientX: 15,
        clientY: 25,
      });

      const shape = findCreatedNode('shape');
      expect(shape).toBeTruthy();
      expect(shape.data.shape).toBe('hexagon');
      expect(shape.position).toEqual({ x: 15, y: 25 });
    });

    it('does not fall through to onDropCreateNode for an annotation-kind payload', () => {
      const onDropCreateNode = vi.fn();
      render(
        <GraphCanvas
          nodes={[]}
          edges={[]}
          onAnnotationChange={vi.fn()}
          onDropCreateNode={onDropCreateNode}
        />
      );
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      dispatchDrop(screen.getByTestId('pane'), {
        dataTransfer: {
          files: [],
          getData: (fmt) =>
            fmt === 'application/annotation-kind' ? JSON.stringify({ kind: 'label' }) : '',
        },
        clientX: 5,
        clientY: 5,
      });

      expect(onDropCreateNode).not.toHaveBeenCalled();
      expect(findCreatedNode('label')).toBeTruthy();
    });

    it('ignores a malformed annotation-kind payload instead of throwing', () => {
      render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      expect(() =>
        dispatchDrop(screen.getByTestId('pane'), {
          dataTransfer: {
            files: [],
            getData: (fmt) => (fmt === 'application/annotation-kind' ? 'not-json{' : ''),
          },
          clientX: 5,
          clientY: 5,
        })
      ).not.toThrow();
      expect(totalNodesCreated()).toBe(0);
    });
  });

  describe('coarse pointer: pointer-events drag', () => {
    it('creates a label at the release position via the toolbox pointer-drag path', () => {
      render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} touchMode="on" />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      const labelButton = screen.getByRole('button', { name: /^label$/i });
      dispatch(labelButton, pointerEvent('pointerdown', { clientX: 0, clientY: 0 }));
      dispatch(window, pointerEvent('pointermove', { clientX: 200, clientY: 80 }));
      dispatch(window, pointerEvent('pointerup', { clientX: 200, clientY: 80 }));

      const label = findCreatedNode('label');
      expect(label).toBeTruthy();
      expect(label.position).toEqual({ x: 200, y: 80 });
    });

    it('never drag-creates image or freehand, even past the movement threshold', () => {
      render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} touchMode="on" />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      const imageButton = screen.getByRole('button', { name: /^image$/i });
      dispatch(imageButton, pointerEvent('pointerdown', { clientX: 0, clientY: 0 }));
      dispatch(window, pointerEvent('pointermove', { clientX: 200, clientY: 80 }));
      dispatch(window, pointerEvent('pointerup', { clientX: 200, clientY: 80 }));

      expect(findCreatedNode('image')).toBeUndefined();
      expect(totalNodesCreated()).toBe(0);
    });
  });
});
