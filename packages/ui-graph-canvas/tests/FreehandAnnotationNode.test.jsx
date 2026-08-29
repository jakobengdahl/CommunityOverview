import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import FreehandAnnotationNode from '../src/components/FreehandAnnotationNode';
import { AnnotationContext } from '../src/components/AnnotationContext';

const hoisted = vi.hoisted(() => ({ setNodes: vi.fn(), nodes: [] }));

vi.mock('reactflow', () => ({
  useReactFlow: () => ({ setNodes: hoisted.setNodes, getNodes: () => hoisted.nodes }),
}));

function applyLatestUpdate(node) {
  const call = hoisted.setNodes.mock.calls.at(-1);
  return call[0]([node])[0];
}

// The width/smoothing/opacity picker rows all use the shared
// `.context-menu-sizes`/`.size-button` classes (same convention LabelNode's
// font-size picker uses), and smoothing/opacity share overlapping percentage
// labels ("30%", "100%") — so a query by button text alone is ambiguous.
// Scope by row order (width, smoothing, opacity) instead, matching the
// component's fixed render order. The context menu renders through a portal
// onto `document.body` (see the component's `createPortal` call), so this
// queries the document rather than `render()`'s own `container`.
function pickerRows() {
  const [width, smoothing, opacity] = document.querySelectorAll('.context-menu-sizes');
  return { width, smoothing, opacity };
}

const straightPoints = [
  { x: 0, y: 0 },
  { x: 10, y: 0 },
  { x: 20, y: 0 },
];

describe('FreehandAnnotationNode rendering', () => {
  beforeEach(() => hoisted.setNodes.mockClear());

  it('renders a single uniform-width path when no point carries pressure', () => {
    const { container } = render(
      <FreehandAnnotationNode id="f1" data={{ points: straightPoints, strokeWidth: 3 }} />
    );
    const strokeGroup = container.querySelector('.graph-freehand-stroke');
    const visiblePaths = strokeGroup.querySelectorAll('path');
    expect(visiblePaths).toHaveLength(1);
    expect(visiblePaths[0].getAttribute('stroke-width')).toBe('3');
  });

  it('renders one segment per adjacent point pair when pressure is present', () => {
    const points = [
      { x: 0, y: 0, pressure: 0.2 },
      { x: 10, y: 0, pressure: 0.8 },
      { x: 20, y: 0, pressure: 0.4 },
    ];
    const { container } = render(<FreehandAnnotationNode id="f1" data={{ points }} />);
    const strokeGroup = container.querySelector('.graph-freehand-stroke');
    expect(strokeGroup.querySelectorAll('path')).toHaveLength(2);
  });

  it('applies opacity to the stroke group, defaulting to fully opaque', () => {
    const { container: withOpacity } = render(
      <FreehandAnnotationNode id="f1" data={{ points: straightPoints, opacity: 0.4 }} />
    );
    expect(withOpacity.querySelector('.graph-freehand-stroke').getAttribute('opacity')).toBe('0.4');

    const { container: noOpacity } = render(
      <FreehandAnnotationNode id="f2" data={{ points: straightPoints }} />
    );
    expect(noOpacity.querySelector('.graph-freehand-stroke').getAttribute('opacity')).toBe('1');
  });

  it('always renders a transparent wide hit-target path ahead of the visible stroke', () => {
    const { container } = render(
      <FreehandAnnotationNode id="f1" data={{ points: straightPoints }} />
    );
    const svg = container.querySelector('svg');
    const hitTarget = svg.querySelector('path');
    expect(hitTarget.getAttribute('stroke')).toBe('transparent');
  });
});

describe('FreehandAnnotationNode property editor', () => {
  beforeEach(() => hoisted.setNodes.mockClear());

  it('opens the property editor on right-click with color/width/smoothing/opacity controls', () => {
    render(
      <AnnotationContext.Provider
        value={{
          notifyChange: vi.fn(),
          notifyRemoteLockedAttempt: vi.fn(),
          labels: {
            freehandColor: 'Colour',
            freehandWidth: 'Stroke width',
            freehandSmoothing: 'Smoothing',
            freehandOpacity: 'Opacity',
            delete: 'Delete',
          },
        }}
      >
        <FreehandAnnotationNode id="f1" data={{ points: straightPoints }} />
      </AnnotationContext.Provider>
    );
    const node = document.querySelector('.graph-freehand-node');
    fireEvent.contextMenu(node);
    expect(screen.getByText('Colour')).toBeInTheDocument();
    expect(screen.getByText('Stroke width')).toBeInTheDocument();
    expect(screen.getByText('Smoothing')).toBeInTheDocument();
    expect(screen.getByText('Opacity')).toBeInTheDocument();
  });

  it("changes the stroke's color from the swatch picker", () => {
    render(
      <AnnotationContext.Provider value={{ notifyChange: vi.fn(), labels: {} }}>
        <FreehandAnnotationNode id="f1" data={{ points: straightPoints, color: '#e6edf3' }} />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(document.querySelector('.graph-freehand-node'));
    fireEvent.click(screen.getByLabelText('#FDE047'));
    expect(applyLatestUpdate({ id: 'f1', data: { color: '#e6edf3' } }).data.color).toBe('#FDE047');
  });

  it('changes the stroke width from the width picker', () => {
    render(
      <AnnotationContext.Provider value={{ notifyChange: vi.fn(), labels: {} }}>
        <FreehandAnnotationNode id="f1" data={{ points: straightPoints, strokeWidth: 2 }} />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(document.querySelector('.graph-freehand-node'));
    fireEvent.click(screen.getByText('8'));
    expect(applyLatestUpdate({ id: 'f1', data: { strokeWidth: 2 } }).data.strokeWidth).toBe(8);
  });

  it('changes smoothing from the smoothing picker', () => {
    render(
      <AnnotationContext.Provider value={{ notifyChange: vi.fn(), labels: {} }}>
        <FreehandAnnotationNode id="f1" data={{ points: straightPoints, smoothing: 0 }} />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(document.querySelector('.graph-freehand-node'));
    fireEvent.click(screen.getByText('60%'));
    expect(applyLatestUpdate({ id: 'f1', data: { smoothing: 0 } }).data.smoothing).toBe(0.6);
  });

  it('changes opacity from the opacity picker', () => {
    render(
      <AnnotationContext.Provider value={{ notifyChange: vi.fn(), labels: {} }}>
        <FreehandAnnotationNode id="f1" data={{ points: straightPoints, opacity: 1 }} />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(document.querySelector('.graph-freehand-node'));
    fireEvent.click(screen.getByText('50%'));
    expect(applyLatestUpdate({ id: 'f1', data: { opacity: 1 } }).data.opacity).toBe(0.5);
  });

  it('marks the current width/smoothing/opacity as active in their pickers', () => {
    render(
      <AnnotationContext.Provider value={{ notifyChange: vi.fn(), labels: {} }}>
        <FreehandAnnotationNode
          id="f1"
          data={{ points: straightPoints, strokeWidth: 5, smoothing: 0.3, opacity: 0.75 }}
        />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(document.querySelector('.graph-freehand-node'));
    const { width, smoothing, opacity } = pickerRows();
    expect(within(width).getByText('5').className).toContain('active');
    expect(within(smoothing).getByText('30%').className).toContain('active');
    expect(within(opacity).getByText('75%').className).toContain('active');
  });

  it('deletes the annotation via the context menu delete button', () => {
    render(
      <AnnotationContext.Provider value={{ notifyChange: vi.fn(), labels: { delete: 'Delete' } }}>
        <FreehandAnnotationNode id="f1" data={{ points: straightPoints }} />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(document.querySelector('.graph-freehand-node'));
    fireEvent.click(screen.getByText(/Delete/));
    const call = hoisted.setNodes.mock.calls.at(-1);
    expect(call[0]([{ id: 'f1' }, { id: 'other' }])).toEqual([{ id: 'other' }]);
  });

  it('notifies the annotation context after a style change', () => {
    const notifyChange = vi.fn();
    render(
      <AnnotationContext.Provider value={{ notifyChange, labels: {} }}>
        <FreehandAnnotationNode id="f1" data={{ points: straightPoints, opacity: 0.5 }} />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(document.querySelector('.graph-freehand-node'));
    const { opacity } = pickerRows();
    fireEvent.click(within(opacity).getByText('100%'));
    expect(notifyChange).toHaveBeenCalledWith('style');
  });

  // task-annotation-shared-session-realtime: an exclusive lease refuses even
  // opening the property editor while another client holds the claim.
  it('refuses to open the property editor while another client holds the selection claim, notifying instead', () => {
    const notifyRemoteLockedAttempt = vi.fn();
    const { container } = render(
      <AnnotationContext.Provider
        value={{ notifyChange: vi.fn(), notifyRemoteLockedAttempt, labels: {} }}
      >
        <FreehandAnnotationNode
          id="f1"
          data={{
            points: straightPoints,
            remoteSelection: { clientId: 'c2', color: '#e6194b', displayName: 'Ada' },
          }}
        />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(container.querySelector('.graph-freehand-node'));
    expect(screen.queryByLabelText('#FDE047')).toBeNull();
    expect(notifyRemoteLockedAttempt).toHaveBeenCalledTimes(1);
    expect(container.querySelector('.graph-node-remote-badge').textContent).toBe('Ada');
  });

  it('closes the context menu on Escape', async () => {
    render(
      <AnnotationContext.Provider value={{ notifyChange: vi.fn(), labels: { delete: 'Delete' } }}>
        <FreehandAnnotationNode id="f1" data={{ points: straightPoints }} />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(document.querySelector('.graph-freehand-node'));
    expect(screen.getByText(/Delete/)).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 0));
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByText(/Delete/)).toBeNull();
  });
});

// smallfix-freehand-context-menu-ignores-lock: a locked stroke's context menu
// must offer only Unlock, matching the pattern established in NoteNode/
// LabelNode/GenericAnnotationNode (smallfix-annotation-context-menus-ignore-lock).
describe('FreehandAnnotationNode locked context menu', () => {
  beforeEach(() => hoisted.setNodes.mockClear());

  it('shows only an unlock action for a locked stroke, hiding colour/width/smoothing/opacity/delete', () => {
    render(
      <AnnotationContext.Provider
        value={{
          notifyChange: vi.fn(),
          labels: {
            unlock: 'Unlock',
            duplicate: 'Duplicate',
            freehandColor: 'Colour',
            freehandWidth: 'Stroke width',
            freehandSmoothing: 'Smoothing',
            freehandOpacity: 'Opacity',
            delete: 'Delete',
          },
        }}
      >
        <FreehandAnnotationNode id="f1" data={{ points: straightPoints, locked: true }} />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(document.querySelector('.graph-freehand-node'));
    expect(screen.getByText(/Unlock/)).toBeInTheDocument();
    expect(screen.getByText(/Duplicate/)).toBeInTheDocument();
    expect(screen.queryByText('Colour')).toBeNull();
    expect(screen.queryByText('Stroke width')).toBeNull();
    expect(screen.queryByText('Smoothing')).toBeNull();
    expect(screen.queryByText('Opacity')).toBeNull();
    expect(screen.queryByText(/Delete/)).toBeNull();
  });

  it('unlocks a locked stroke, notifies the annotation context, and makes it draggable again', () => {
    const notifyChange = vi.fn();
    render(
      <AnnotationContext.Provider value={{ notifyChange, labels: { unlock: 'Unlock' } }}>
        <FreehandAnnotationNode id="f1" data={{ points: straightPoints, locked: true }} />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(document.querySelector('.graph-freehand-node'));
    fireEvent.click(screen.getByText(/Unlock/));
    const updated = applyLatestUpdate({ id: 'f1', data: { locked: true }, draggable: false });
    expect(updated.data.locked).toBe(false);
    expect(updated.draggable).toBe(true);
    expect(notifyChange).toHaveBeenCalledWith('style');
  });

  it('refuses to open the locked context menu while another client holds the selection claim, notifying instead', () => {
    const notifyChange = vi.fn();
    const notifyRemoteLockedAttempt = vi.fn();
    render(
      <AnnotationContext.Provider
        value={{ notifyChange, notifyRemoteLockedAttempt, labels: { unlock: 'Unlock' } }}
      >
        <FreehandAnnotationNode
          id="f1"
          data={{
            points: straightPoints,
            locked: true,
            remoteSelection: { clientId: 'c2', color: '#e6194b', displayName: 'Ada' },
          }}
        />
      </AnnotationContext.Provider>
    );
    // Remote lock refuses opening the menu at all — matching every other
    // remoteLocked-gated action on this component.
    fireEvent.contextMenu(document.querySelector('.graph-freehand-node'));
    expect(screen.queryByText(/Unlock/)).toBeNull();
    expect(notifyRemoteLockedAttempt).toHaveBeenCalledTimes(1);
    expect(hoisted.setNodes).not.toHaveBeenCalled();
    expect(notifyChange).not.toHaveBeenCalled();
  });

  it('still shows the full property editor when unlocked', () => {
    render(
      <AnnotationContext.Provider
        value={{
          notifyChange: vi.fn(),
          labels: {
            freehandColor: 'Colour',
            freehandWidth: 'Stroke width',
            freehandSmoothing: 'Smoothing',
            freehandOpacity: 'Opacity',
            delete: 'Delete',
          },
        }}
      >
        <FreehandAnnotationNode id="f1" data={{ points: straightPoints, locked: false }} />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(document.querySelector('.graph-freehand-node'));
    expect(screen.getByText('Colour')).toBeInTheDocument();
    expect(screen.getByText(/Delete/)).toBeInTheDocument();
  });
});

describe('freehand default colour', () => {
  // Reported from owner testing: a stroke drawn without choosing a colour was
  // invisible. The default was a near-white picked for a dark canvas, so the
  // tool looked broken rather than mis-coloured — the worst failure mode for
  // the one annotation kind whose whole interaction is "draw and see a line".
  it('is the same constant the canvas writes onto every stroke it creates', async () => {
    // The bug this fixes was two constants for one default: GraphCanvas had
    // its own copy, and because a GUI-drawn stroke is always written WITH an
    // explicit colour, that copy — not the node's fallback below — was what
    // every drawn stroke actually got. Fixing only the fallback changed
    // nothing a user could see. Assert the single source directly, since a
    // second copy reintroducing itself is the failure mode.
    const canvas = await import('../src/components/GraphCanvas.jsx?raw').catch(() => null);
    const node = await import('../src/components/FreehandAnnotationNode');
    expect(node.DEFAULT_FREEHAND_COLOR).toBe('#111827');
    if (canvas?.default) {
      expect(canvas.default).not.toMatch(/DEFAULT_FREEHAND_COLOR\s*=\s*'#/);
    }
  });

  it('renders an unstyled stroke in a colour that is visible on a light canvas', () => {
    const { container } = render(
      <FreehandAnnotationNode id="f-default" data={{ points: straightPoints }} />
    );
    // The visible stroke, not the transparent hit-area path drawn beside it.
    const stroke = container.querySelector('.graph-freehand-stroke path');
    expect(stroke).toBeTruthy();
    const value = stroke.getAttribute('stroke');
    // Darkness, not the exact constant — the test above already pins the
    // value, and the bug was "the default is too light", not "the default is
    // not this string". An assertion on the constant alone would pass again
    // for the next near-white someone picks.
    const [r, g, b] = [1, 3, 5].map((i) => parseInt(value.slice(i, i + 2), 16));
    const luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
    expect(luminance).toBeLessThan(0.3);
  });
});

// task-annotation-render-direct-manipulation / task-annotation-responsive-
// bottom-toolbox: duplication was MCP-only (`duplicate_annotation`) with no
// GUI action anywhere. See annotationDuplicateWiring.test.jsx for the
// shared-hook wiring pinned across every kind; this pins
// FreehandAnnotationNode's own — including that the whole stroke moves
// together, since `points` are sampled relative to the node's own position.
describe('FreehandAnnotationNode duplicate control', () => {
  beforeEach(() => {
    hoisted.setNodes.mockClear();
    hoisted.nodes = [];
  });

  it('creates a new, offset stroke and leaves the original untouched', () => {
    const source = {
      id: 'f1',
      type: 'freehand',
      position: { x: 5, y: 5 },
      data: { points: straightPoints, color: '#111827' },
    };
    hoisted.nodes = [source];
    render(<FreehandAnnotationNode id="f1" data={source.data} />);
    fireEvent.contextMenu(document.querySelector('.graph-freehand-node'));
    fireEvent.click(screen.getByText(/Duplicate/));
    const updated = hoisted.setNodes.mock.calls.at(-1)[0](hoisted.nodes);
    expect(updated).toHaveLength(2);
    const [original, copy] = updated;
    expect(original).toBe(source);
    expect(copy.id).not.toBe('f1');
    // The sampled points themselves are unchanged — relative to position,
    // per this component's own doc comment — only the position moves.
    expect(copy.data.points).toEqual(straightPoints);
    expect(copy.position).not.toEqual(source.position);
    expect(copy.data.locked).toBe(false);
  });

  it('duplicates a locked stroke into an unlocked copy, and leaves the source locked', () => {
    const lockedData = { points: straightPoints, locked: true };
    const source = { id: 'f1', type: 'freehand', position: { x: 0, y: 0 }, data: lockedData };
    hoisted.nodes = [source];
    render(
      <AnnotationContext.Provider
        value={{ notifyChange: vi.fn(), labels: { unlock: 'Unlock', duplicate: 'Duplicate' } }}
      >
        <FreehandAnnotationNode id="f1" data={lockedData} />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(document.querySelector('.graph-freehand-node'));
    fireEvent.click(screen.getByText(/Duplicate/));
    const updated = hoisted.setNodes.mock.calls.at(-1)[0](hoisted.nodes);
    const copy = updated.find((n) => n.id !== 'f1');
    expect(copy.data.locked).toBe(false);
    expect(updated.find((n) => n.id === 'f1').data.locked).toBe(true);
  });
});

// task-annotation-render-direct-manipulation / task-annotation-responsive-
// bottom-toolbox's "Nearby object menu" contract entry point: a freehand
// stroke is one of the annotation kinds that can itself be the target this
// menu creates a new attachable annotation near.
describe('FreehandAnnotationNode "Nearby object menu"', () => {
  it("calls the context attachNearby with this stroke's id and the picked kind", () => {
    const attachNearby = vi.fn();
    render(
      <AnnotationContext.Provider
        value={{
          notifyChange: vi.fn(),
          attachNearby,
          labels: {
            nearbyMenu: 'Add nearby',
            nearbyLabel: 'Label',
            nearbyIcon: 'Icon',
            nearbyVoteDot: 'Vote dot',
            nearbyText: 'Text',
          },
        }}
      >
        <FreehandAnnotationNode id="f1" data={{ points: straightPoints }} />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(document.querySelector('.graph-freehand-node'));
    fireEvent.click(screen.getByRole('button', { name: '+ Vote dot' }));
    expect(attachNearby).toHaveBeenCalledWith('f1', 'vote_dot');
  });
});
