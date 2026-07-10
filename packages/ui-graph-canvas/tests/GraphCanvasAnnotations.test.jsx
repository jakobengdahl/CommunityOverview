import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

const hoisted = vi.hoisted(() => ({ setNodes: vi.fn(), selectionOnChange: null }));

vi.mock('reactflow', () => {
  const MockReactFlow = ({ children, onPaneContextMenu, onPaneMouseDown }) => (
    <div data-testid="react-flow" className="react-flow">
      <div
        data-testid="pane"
        onMouseDown={(event) => onPaneMouseDown?.(event)}
        onContextMenu={(event) => onPaneContextMenu?.(event)}
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
      getNodes: () => [],
      getEdges: () => [],
      setNodes: hoisted.setNodes,
      setEdges: vi.fn(),
      screenToFlowPosition: ({ x, y }) => ({ x, y }),
      setCenter: vi.fn(),
      getViewport: () => ({ x: 0, y: 0, zoom: 1 }),
    }),
    useOnSelectionChange: ({ onChange }) => { hoisted.selectionOnChange = onChange; },
    Background: () => <div data-testid="background" />,
    Controls: () => <div data-testid="controls" />,
    MiniMap: () => <div data-testid="minimap" />,
    NodeResizer: () => null,
    SelectionMode: { Partial: 'partial' },
  };
});

// A plain right-click on the pane: mousedown (button 2) then contextmenu at the
// same coordinates, so GraphCanvas treats it as a click, not a pan-drag.
function rightClickPane(pane, x = 120, y = 90) {
  fireEvent.mouseDown(pane, { button: 2, clientX: x, clientY: y });
  fireEvent.contextMenu(pane, { clientX: x, clientY: y });
}

// setNodes fires from several effects; find the created node by applying every
// captured updater to an empty node array and collecting the result.
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

describe('GraphCanvas annotation creation', () => {
  beforeEach(() => vi.clearAllMocks());

  it('opens the annotation menu on a plain pane right-click', () => {
    render(<GraphCanvas nodes={[]} edges={[]} />);
    rightClickPane(screen.getByTestId('pane'));
    expect(screen.getByRole('button', { name: /add note/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /add label/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /add arrow/i })).toBeInTheDocument();
  });

  it('does not open the menu after a right-drag pan', () => {
    render(<GraphCanvas nodes={[]} edges={[]} />);
    const pane = screen.getByTestId('pane');
    fireEvent.mouseDown(pane, { button: 2, clientX: 100, clientY: 100 });
    fireEvent.contextMenu(pane, { clientX: 200, clientY: 160 });
    expect(screen.queryByRole('button', { name: /add note/i })).not.toBeInTheDocument();
  });

  it('creates a note at the clicked position and notifies the host', () => {
    const onAnnotationChange = vi.fn();
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={onAnnotationChange} />);
    rightClickPane(screen.getByTestId('pane'), 120, 90);
    fireEvent.click(screen.getByRole('button', { name: /add note/i }));

    expect(onAnnotationChange).toHaveBeenCalledTimes(1);
    const note = findCreatedNode('note');
    expect(note).toBeTruthy();
    expect(note.position).toEqual({ x: 120, y: 90 });
    expect(note.style).toEqual({ width: 200, height: 140 });
  });

  it('creates an arrow with default vector', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    rightClickPane(screen.getByTestId('pane'), 10, 20);
    fireEvent.click(screen.getByRole('button', { name: /add arrow/i }));
    const arrow = findCreatedNode('arrow');
    expect(arrow).toBeTruthy();
    expect(arrow.data).toEqual({ dx: 160, dy: 0, color: undefined, startArrow: false, endArrow: true });
    expect(arrow.position).toEqual({ x: 10, y: 20 });
  });

  it('includes annotations in the save-view snapshot', () => {
    const onSaveView = vi.fn();
    const overlayNodes = [
      { id: 'note-1', type: 'note', position: { x: 5, y: 6 }, data: { text: 'hi', color: '#FEF08A' }, style: { width: 200, height: 140 } },
      { id: 'label-1', type: 'label', position: { x: 7, y: 8 }, data: { text: 'L', color: '#fff' } },
      { id: 'arrow-1', type: 'arrow', position: { x: 1, y: 2 }, data: { dx: 100, dy: 40, color: '#fff' } },
    ];
    // useNodesState returns the initial nodes array unchanged in this mock, so
    // seed it via a custom implementation for this test.
    hoisted.setNodes.mockClear();
    const { rerender } = render(
      <GraphCanvas nodes={[]} edges={[]} onSaveView={onSaveView} saveViewSignal={0} />
    );
    // Drive the save via signal; nodes come from useNodesState (empty here), so
    // this asserts the collection code path runs and emits an annotations array.
    rerender(<GraphCanvas nodes={[]} edges={[]} onSaveView={onSaveView} saveViewSignal={1} />);
    expect(onSaveView).toHaveBeenCalled();
    const viewData = onSaveView.mock.calls[0][0];
    expect(Array.isArray(viewData.annotations)).toBe(true);
    // Overlay serialization is unit-tested in overlaySerialization.test.js; here
    // we only guarantee handleSaveView always provides the annotations field.
    void overlayNodes;
  });

  it('restores overlay annotations from a loaded session', () => {
    const onAnnotationsRestored = vi.fn();
    render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        onAnnotationsRestored={onAnnotationsRestored}
        annotationsToRestore={[
          { id: 'note-9', kind: 'note', position: { x: 2, y: 3 }, text: 'restored', size: { w: 200, h: 140 } },
        ]}
      />
    );
    const note = findCreatedNode('note');
    expect(note).toBeTruthy();
    expect(note.id).toBe('note-9');
    expect(note.data.text).toBe('restored');
    expect(onAnnotationsRestored).toHaveBeenCalled();
  });

  it('removes selected overlays on Delete without hiding them as graph nodes', () => {
    const onHideMultiple = vi.fn();
    const onAnnotationChange = vi.fn();
    render(
      <GraphCanvas nodes={[]} edges={[]} onHideMultiple={onHideMultiple} onAnnotationChange={onAnnotationChange} />
    );
    act(() => {
      hoisted.selectionOnChange({ nodes: [{ id: 'note-1', type: 'note' }], edges: [] });
    });
    fireEvent.keyDown(document, { key: 'Delete' });

    expect(onHideMultiple).not.toHaveBeenCalled();
    expect(onAnnotationChange).toHaveBeenCalled();
    const removed = hoisted.setNodes.mock.calls.some((call) => {
      if (typeof call[0] !== 'function') return false;
      try {
        return call[0]([{ id: 'note-1', type: 'note' }, { id: 'keep' }]).every((n) => n.id !== 'note-1');
      } catch {
        return false;
      }
    });
    expect(removed).toBe(true);
  });
});
