import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

// The shape slot (task-annotation-shapes-under-one-toolbox-slot) shows only
// the currently selected shape as a top-level button; every other variant
// has to be picked from its fold-out first. `within` scopes the picker
// selection so it can never match the slot's own current-shape button (e.g.
// selecting "Rectangle" while the slot already shows "Rectangle").
function selectShapeVariant(name) {
  fireEvent.click(screen.getByRole('button', { name: /choose a shape/i }));
  const picker = screen.getByRole('group', { name: /^shapes$/i });
  fireEvent.click(within(picker).getByRole('button', { name }));
}

const hoisted = vi.hoisted(() => ({ setNodes: vi.fn() }));

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
// the helper in GraphCanvasAnnotations.test.jsx.
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

describe('GraphCanvas bottom annotation toolbox', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // The shape slot's current variant persists in localStorage (a personal
    // preference, not component state) — clear it so one test's picker
    // selection can't leak into the next test's "what does the slot show by
    // default" assumption.
    localStorage.clear();
  });

  it('renders the toolbox as a surface distinct from the pane annotation context menu', () => {
    render(<GraphCanvas nodes={[]} edges={[]} />);
    // The toolbox is present up front (collapsed to its toggle)...
    expect(screen.getByTestId('annotation-toolbox')).toBeInTheDocument();
    // ...while the pane context menu (right-click add note/label/arrow) does
    // not exist until a right-click opens it - the two are separate surfaces,
    // not the same menu re-skinned.
    expect(screen.queryByRole('button', { name: /^add note$/i })).not.toBeInTheDocument();
  });

  it('creates a sticky note via the toolbox without going through the pane context menu', () => {
    const onAnnotationChange = vi.fn();
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={onAnnotationChange} />);

    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^note$/i }));

    expect(onAnnotationChange).toHaveBeenCalled();
    const note = findCreatedNode('note');
    expect(note).toBeTruthy();
    expect(note.style).toEqual({ width: 200, height: 140 });
  });

  it('creates a generic text annotation via the toolbox', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^text$/i }));

    const text = findCreatedNode('text');
    expect(text).toBeTruthy();
    expect(text.data).toEqual({ text: '', color: undefined, fontSize: undefined });
  });

  it('creates a frame annotation via the toolbox with a default box size', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^frame$/i }));

    const frame = findCreatedNode('frame');
    expect(frame).toBeTruthy();
    expect(frame.style).toEqual({ width: 220, height: 160 });
  });

  it.each([
    ['triangle', { width: 160, height: 139 }],
    ['hexagon', { width: 160, height: 139 }],
    ['rhombus', { width: 160, height: 160 }],
  ])('creates %s at the box its geometry needs, not the generic one', (shape, expected) => {
    // The reported bug was that these came out squashed, and the cause was the
    // creation box, not the clip-path. Asserted here, at the call site, rather
    // than only on the size helper: a helper test cannot see GraphCanvas
    // reverting to a hardcoded 160x96, which is exactly the mutant that
    // reproduces the bug.
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    selectShapeVariant(new RegExp(`^${shape}$`, 'i'));
    fireEvent.click(screen.getByRole('button', { name: new RegExp(`^${shape}$`, 'i') }));

    const node = findCreatedNode('shape');
    expect(node.data.shape).toBe(shape);
    expect(node.style).toEqual(expected);
  });

  it('creates a rectangle shape annotation via the toolbox by default', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^rectangle$/i }));

    const shape = findCreatedNode('shape');
    expect(shape).toBeTruthy();
    expect(shape.data.shape).toBe('rectangle');
  });

  it('creates a circle shape variant via the toolbox', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    selectShapeVariant(/^circle$/i);
    fireEvent.click(screen.getByRole('button', { name: /^circle$/i }));

    const shape = findCreatedNode('shape');
    expect(shape).toBeTruthy();
    expect(shape.data.shape).toBe('circle');
  });

  // task-annotation-doubleclick-to-edit-text: a shape's optional caption
  // field is present from creation (matching `text`-kind's own branch just
  // above in GraphCanvas.jsx), not merely absent until a user's first edit.
  it('creates a shape annotation with an empty caption already present', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^rectangle$/i }));

    const shape = findCreatedNode('shape');
    expect(shape.data.text).toBe('');
  });

  // task-annotation-render-direct-manipulation remaining_scope: "icon/vote_dot
  // GUI creation is still not implemented" — closed here.
  it('creates an icon annotation via the toolbox with a default icon and size', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^icon$/i }));

    const icon = findCreatedNode('icon');
    expect(icon).toBeTruthy();
    expect(icon.data.icon).toBe('circle');
    // No `style` box (fixed intrinsic size) — geometry lives in `data.size`
    // only, so it round-trips without becoming a resizable box (61d5cc7b).
    expect(icon.style).toBeUndefined();
    expect(icon.data.size).toEqual({ w: 32, h: 32 });
  });

  it('creates a vote_dot annotation via the toolbox with a default value and size', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^vote dot$/i }));

    const voteDot = findCreatedNode('vote_dot');
    expect(voteDot).toBeTruthy();
    expect(voteDot.data.value).toBe(1);
    expect(voteDot.style).toBeUndefined();
    expect(voteDot.data.size).toEqual({ w: 24, h: 24 });
  });

  it('creates a label annotation via the toolbox', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^label$/i }));

    const label = findCreatedNode('label');
    expect(label).toBeTruthy();
  });

  it('honours a host-provided label override for i18n', () => {
    render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        annotationToolboxLabels={{ toggleExpand: 'Lägg till kommentar' }}
      />
    );
    expect(screen.getByRole('button', { name: /lägg till kommentar/i })).toBeInTheDocument();
  });
});
