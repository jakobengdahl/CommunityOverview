import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

// The shape slot shows only the currently selected shape as a top-level
// button; every other variant has to be picked from its fold-out first.
// `within` scopes the picker selection so it can never match the slot's own
// current-shape button (e.g. selecting "Rectangle" while the slot already
// shows "Rectangle").
function selectShapeVariant(name) {
  fireEvent.click(screen.getByRole('button', { name: /choose a shape/i }));
  const picker = screen.getByRole('group', { name: /^shapes$/i });
  fireEvent.click(within(picker).getByRole('button', { name }));
}

// Creating from the toolbox is a two-step gesture (task-annotation-tool-modes):
// the toolbox arms a tool, and the next tap on empty canvas is where the object
// goes. Clicking a toolbox item on its own no longer creates anything, which is
// the point — it used to drop every object at the viewport centre, so placing
// several meant dragging each one out of the pile the last had made.
function placeOnPane(x = 120, y = 90) {
  fireEvent.click(screen.getByTestId('pane'), { clientX: x, clientY: y });
}

const hoisted = vi.hoisted(() => ({ setNodes: vi.fn() }));

vi.mock('reactflow', () => {
  const MockReactFlow = ({ children, onPaneContextMenu, onPaneMouseDown, onPaneClick }) => (
    <div data-testid="react-flow" className="react-flow">
      <div
        data-testid="pane"
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

    // Arming alone must not create anything — that is the regression this
    // guards, since the old contract created on the toolbox click itself.
    expect(onAnnotationChange).not.toHaveBeenCalled();

    placeOnPane();

    expect(onAnnotationChange).toHaveBeenCalled();
    const note = findCreatedNode('note');
    expect(note).toBeTruthy();
    expect(note.style).toEqual({ width: 200, height: 140 });
  });

  it('creates a generic text annotation via the toolbox', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^text$/i }));

    placeOnPane();

    const text = findCreatedNode('text');
    expect(text).toBeTruthy();
    expect(text.data).toEqual({ text: '', color: undefined, fontSize: undefined });
    // No regression from the "Nearby object menu" creation-time attachment
    // entry point: today's plain one-click toolbox creation (no target
    // anchor involved) must still produce an unattached annotation.
    expect(text.data.attachment).toBeUndefined();
    // Unlike `shape` (see the semantic-default-layer test below), every
    // other kind gets no explicit `zIndex` at creation, which resolves to
    // the unchanged 0 default through the existing `zIndex ?? 0` fallbacks.
    expect(text.zIndex).toBeUndefined();
  });

  // task-annotation-merge-frame-into-shape-rectangle: a toolbox-created shape
  // leaves fill/border unset so GenericAnnotationNode's own defaults apply (a
  // solid fill, no border) — the same "plain shape" look a shape always had.
  // The retired `frame` toolbox button's look (transparent fill, coloured
  // border) is reached afterwards via the right-click editor, not a separate
  // creation-time default.
  it('creates a rectangle shape via the toolbox with fill/border left unset', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^rectangle$/i }));

    placeOnPane();

    const shape = findCreatedNode('shape');
    expect(shape).toBeTruthy();
    expect(shape.data.fill).toBeUndefined();
    expect(shape.data.border).toBeUndefined();
  });

  // task-annotation-render-direct-manipulation's remaining "semantic default
  // layers" scope: a shape is the one kind this creation path gives an
  // explicit non-zero starting layer, so it opens one behind everything else
  // (docs/ANNOTATION_CONTRACT.md's Layer order section) instead of needing a
  // manual send-to-back. Every other kind stays at the unchanged 0 default —
  // see the text creation test above for that side of the contrast.
  it('creates a shape via the toolbox one layer behind the default (zIndex -1)', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^rectangle$/i }));

    placeOnPane();

    const shape = findCreatedNode('shape');
    expect(shape.zIndex).toBe(-1);
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

    placeOnPane();

    const node = findCreatedNode('shape');
    expect(node.data.shape).toBe(shape);
    expect(node.style).toEqual(expected);
  });

  it('creates a rectangle shape annotation via the toolbox by default', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^rectangle$/i }));

    placeOnPane();

    const shape = findCreatedNode('shape');
    expect(shape).toBeTruthy();
    expect(shape.data.shape).toBe('rectangle');
  });

  it('creates a circle shape variant via the toolbox', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    selectShapeVariant(/^circle$/i);
    fireEvent.click(screen.getByRole('button', { name: /^circle$/i }));

    placeOnPane();

    const shape = findCreatedNode('shape');
    expect(shape).toBeTruthy();
    expect(shape.data.shape).toBe('circle');
  });

  // A shape's optional caption field is present from creation (matching
  // `text`-kind's own branch just above in GraphCanvas.jsx), not merely
  // absent until a user's first edit.
  it('creates a shape annotation with an empty caption already present', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^rectangle$/i }));

    placeOnPane();

    const shape = findCreatedNode('shape');
    expect(shape.data.text).toBe('');
  });

  it('creates an icon annotation via the toolbox with the default icon and size', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^icon: circle$/i }));

    placeOnPane();

    const icon = findCreatedNode('icon');
    expect(icon).toBeTruthy();
    expect(icon.data.icon).toBe('circle');
    // No `style` box (fixed intrinsic size) — geometry lives in `data.size`
    // only, so it round-trips without becoming a resizable box (61d5cc7b).
    expect(icon.style).toBeUndefined();
    expect(icon.data.size).toEqual({ w: 32, h: 32 });
    // No regression: plain one-click creation with no nearby target produces
    // an unattached icon, same as before the "Nearby object menu" entry point.
    expect(icon.data.attachment).toBeUndefined();
  });

  it('creates the icon selected in the toolbox icon slot', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /choose an icon/i }));
    const picker = screen.getByRole('group', { name: /^icons$/i });
    fireEvent.click(within(picker).getByRole('button', { name: /^star$/i }));
    fireEvent.click(screen.getByRole('button', { name: /^icon: star$/i }));

    placeOnPane();

    const icon = findCreatedNode('icon');
    expect(icon).toBeTruthy();
    expect(icon.data.icon).toBe('star');
    expect(icon.data.size).toEqual({ w: 32, h: 32 });
  });

  // task-annotation-vote-dot-simplify: no `value` any more (there is nothing
  // to count), and no `attachment` (it is not one of ATTACHABLE_OVERLAY_KINDS
  // — created plain and never pre-wired to a target).
  it('creates a vote_dot annotation via the toolbox with no value, no attachment, and a fixed size', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^vote dot$/i }));

    placeOnPane();

    const voteDot = findCreatedNode('vote_dot');
    expect(voteDot).toBeTruthy();
    expect(voteDot.data.value).toBeUndefined();
    expect(voteDot.style).toBeUndefined();
    expect(voteDot.data.size).toEqual({ w: 24, h: 24 });
    expect(voteDot.data.attachment).toBeUndefined();
  });

  it('creates a label annotation via the toolbox', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    fireEvent.click(screen.getByRole('button', { name: /^label$/i }));

    placeOnPane();

    const label = findCreatedNode('label');
    expect(label).toBeTruthy();
    expect(label.data.attachment).toBeUndefined();
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
