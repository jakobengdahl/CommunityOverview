import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import ArrowNode from '../src/components/ArrowNode';
import { AnnotationContext } from '../src/components/AnnotationContext';

const hoisted = vi.hoisted(() => ({ setNodes: vi.fn(), nodes: [] }));

vi.mock('reactflow', () => ({
  useReactFlow: () => ({
    setNodes: hoisted.setNodes,
    getNodes: () => hoisted.nodes,
    screenToFlowPosition: (p) => p,
  }),
}));

function openMenu() {
  fireEvent.contextMenu(document.querySelector('.graph-arrow-node'));
}

// The capability baseline (docs/ANNOTATION_CONTRACT.md) is that a locked
// object "remains selectable but offers only unlock or copy". NoteNode,
// LabelNode, GenericAnnotationNode and FreehandAnnotationNode have all
// rendered a `locked ? <unlock only> : <full menu>` branch for some time;
// ArrowNode was the one kind that did not, so a locked line still offered
// its colour swatches, both arrowhead toggles and Delete — and, having no
// unlock button either, could not be unlocked from the GUI at all.
describe('ArrowNode locked context menu', () => {
  beforeEach(() => {
    hoisted.setNodes.mockClear();
    hoisted.nodes = [];
  });

  const lockedData = { dx: 100, dy: 0, locked: true };

  it('hides recolour and delete controls when the line is locked', () => {
    render(<ArrowNode id="a1" type="arrow" data={lockedData} selected={false} />);
    openMenu();
    expect(screen.getByText(/Unlock/)).toBeInTheDocument();
    // Unlike Delete/recolour, Duplicate is one of the two actions the
    // capability baseline leaves reachable on a locked object.
    expect(screen.getByText(/Duplicate/)).toBeInTheDocument();
    expect(screen.queryByText(/Delete/)).toBeNull();
    expect(screen.queryByText('Start arrowhead')).toBeNull();
    expect(screen.queryByText('End arrowhead')).toBeNull();
    expect(document.querySelector('.context-menu-colors')).toBeNull();
  });

  it('unlocks the line, publishes it, and makes it draggable again', () => {
    const notifyChange = vi.fn();
    render(
      <AnnotationContext.Provider value={{ notifyChange, labels: { unlock: 'Unlock' } }}>
        <ArrowNode id="a1" type="arrow" data={lockedData} selected={false} />
      </AnnotationContext.Provider>
    );
    openMenu();
    fireEvent.click(screen.getByText(/Unlock/));
    const updated = hoisted.setNodes.mock.calls.at(-1)[0]([
      { id: 'a1', data: lockedData, draggable: false },
    ])[0];
    expect(updated.data.locked).toBe(false);
    // patchData recomputes draggability from the new data, so an unlocked,
    // unanchored line becomes draggable in the same update.
    expect(updated.draggable).toBe(true);
    // Without this the unlock is purely local: no op is published, so the
    // line is locked again on reload and no other client ever sees it. Every
    // sibling component's unlock test asserts the same thing.
    expect(notifyChange).toHaveBeenCalledWith('style');
  });

  it('leaves an anchored line non-draggable when unlocked', () => {
    // An anchored line moves only via its endpoint handles, never as a whole
    // — enforced at hydration (overlayToFlowNode), in isAnnotationDraggable
    // and in moveEndpoint. Unlocking must not override that.
    const anchored = { ...lockedData, endAnchor: 'node-7' };
    render(<ArrowNode id="a1" type="arrow" data={anchored} selected={false} />);
    openMenu();
    fireEvent.click(screen.getByText(/Unlock/));
    const updated = hoisted.setNodes.mock.calls.at(-1)[0]([
      { id: 'a1', data: anchored, draggable: false },
    ])[0];
    expect(updated.data.locked).toBe(false);
    expect(updated.draggable).toBe(false);
  });

  it('keeps recolour and delete controls available when the line is not locked', () => {
    render(<ArrowNode id="a1" type="arrow" data={{ dx: 100, dy: 0 }} selected={false} />);
    openMenu();
    expect(screen.queryByText(/Unlock/)).toBeNull();
    expect(screen.getByText(/Delete/)).toBeInTheDocument();
    expect(document.querySelector('.context-menu-colors')).toBeTruthy();
    expect(document.querySelectorAll('.color-button')).toHaveLength(7);
  });

  it('surfaces the attempt instead of unlocking while another client holds the claim', () => {
    const notifyRemoteLockedAttempt = vi.fn();
    render(
      <AnnotationContext.Provider
        value={{ notifyChange: vi.fn(), notifyRemoteLockedAttempt, labels: { unlock: 'Unlock' } }}
      >
        <ArrowNode
          id="a1"
          type="arrow"
          data={{ ...lockedData, remoteSelection: { color: '#f00', displayName: 'Ada' } }}
          selected={false}
        />
      </AnnotationContext.Provider>
    );
    openMenu();
    fireEvent.click(screen.getByText(/Unlock/));
    expect(notifyRemoteLockedAttempt).toHaveBeenCalled();
    expect(hoisted.setNodes).not.toHaveBeenCalled();
  });
});

describe('ArrowNode default colour', () => {
  beforeEach(() => {
    hoisted.setNodes.mockClear();
    hoisted.nodes = [];
  });

  // Reported from owner testing of the sibling kinds: a line drawn without
  // choosing a colour was invisible. The default was a near-white picked for
  // a dark canvas, so — as with freehand before it (PR #458) — the tool read
  // as broken rather than as mis-coloured.
  it('renders an unstyled line in a colour that is visible on a light canvas', () => {
    const { container } = render(
      <ArrowNode id="a-default" type="arrow" data={{ dx: 100, dy: 0 }} selected={false} />
    );
    // The painted line, not the transparent wide hit target drawn beneath it.
    const stroke = [...container.querySelectorAll('line')].find(
      (line) => line.getAttribute('stroke') !== 'transparent'
    );
    expect(stroke).toBeTruthy();
    const value = stroke.getAttribute('stroke');
    // Darkness, not the exact constant — the bug was "the default is too
    // light", not "the default is not this string". Asserting the literal
    // would pass again for the next near-white someone picks.
    const [r, g, b] = [1, 3, 5].map((i) => parseInt(value.slice(i, i + 2), 16));
    const luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
    expect(luminance).toBeLessThan(0.3);
  });

  // A consistency guard on the one `color` const the line, both arrowhead
  // markers and the endpoint handles all read — not a second regression test
  // for this defect. What keeps the fallback on the path a user actually
  // takes is GraphCanvasAnnotations' "creates an arrow with default vector",
  // which pins `color: undefined` on a created arrow; freehand needed its own
  // version of that check because a second explicit copy of the default in
  // the create path meant fixing the fallback alone changed nothing visible.
  it('paints both arrowheads in the same fallback colour as the line', () => {
    const { container } = render(
      <ArrowNode id="a-default" type="arrow" data={{ dx: 100, dy: 0 }} selected={false} />
    );
    const stroke = [...container.querySelectorAll('line')].find(
      (line) => line.getAttribute('stroke') !== 'transparent'
    );
    // Both markers are emitted into <defs> regardless of the head toggles,
    // so this covers the tail as well without arming startArrow.
    const heads = [...container.querySelectorAll('marker path')];
    expect(heads).toHaveLength(2);
    for (const head of heads) {
      expect(head.getAttribute('fill')).toBe(stroke.getAttribute('stroke'));
    }
  });

  it('keeps the near-white colour available as a selectable swatch', () => {
    render(<ArrowNode id="a-default" type="arrow" data={{ dx: 100, dy: 0 }} selected={false} />);
    openMenu();

    const swatches = [...document.querySelectorAll('.color-button')].map(
      (button) => button.style.backgroundColor
    );
    expect(swatches).toContain('rgb(230, 237, 243)');
  });

  // smallfix-annotation-colour-swatch-no-active-marker: the swatch grid had
  // no indication of the line's current colour at all, unlike
  // FreehandAnnotationNode's picker (FreehandAnnotationNode.jsx:298).
  it('marks the swatch matching the current colour as active, and no other', () => {
    render(
      <ArrowNode
        id="a1"
        type="arrow"
        data={{ dx: 100, dy: 0, color: '#FDE047' }}
        selected={false}
      />
    );
    openMenu();

    const buttons = [...document.querySelectorAll('.color-button')];
    const active = buttons.find((b) => b.style.backgroundColor === 'rgb(253, 224, 71)');
    const inactive = buttons.find((b) => b.style.backgroundColor === 'rgb(230, 237, 243)');
    expect(active.className).toContain('active');
    expect(inactive.className).not.toContain('active');
  });
});

// task-annotation-render-direct-manipulation / task-annotation-responsive-
// bottom-toolbox: duplication was MCP-only (`duplicate_annotation`) with no
// GUI action anywhere. See annotationDuplicateWiring.test.jsx for the
// shared-hook wiring pinned across every kind; this pins ArrowNode's own —
// including that the whole line (both ends) moves together, since a line's
// far end is `dx`/`dy` relative to the node's own position rather than an
// absolute second point.
describe('ArrowNode duplicate control', () => {
  beforeEach(() => {
    hoisted.setNodes.mockClear();
    hoisted.nodes = [];
  });

  it('creates a new, offset line and leaves the original untouched', () => {
    const source = {
      id: 'a1',
      type: 'arrow',
      position: { x: 10, y: 10 },
      data: { dx: 100, dy: 0, endArrow: true },
    };
    hoisted.nodes = [source];
    render(<ArrowNode id="a1" type="arrow" data={source.data} selected={false} />);
    openMenu();
    fireEvent.click(screen.getByText(/Duplicate/));
    const updated = hoisted.setNodes.mock.calls.at(-1)[0](hoisted.nodes);
    expect(updated).toHaveLength(2);
    const [original, copy] = updated;
    expect(original).toBe(source);
    expect(copy.id).not.toBe('a1');
    // Both ends move together: dx/dy (the far end, relative) are unchanged,
    // only the shared position (the origin) is offset.
    expect(copy.data.dx).toBe(100);
    expect(copy.data.dy).toBe(0);
    expect(copy.position).not.toEqual(source.position);
    expect(copy.data.locked).toBe(false);
  });

  it('duplicates a locked line into an unlocked copy, and leaves the source locked', () => {
    const lockedData = { dx: 100, dy: 0, locked: true };
    const source = { id: 'a1', type: 'arrow', position: { x: 0, y: 0 }, data: lockedData };
    hoisted.nodes = [source];
    render(
      <AnnotationContext.Provider
        value={{ notifyChange: vi.fn(), labels: { unlock: 'Unlock', duplicate: 'Duplicate' } }}
      >
        <ArrowNode id="a1" type="arrow" data={lockedData} selected={false} />
      </AnnotationContext.Provider>
    );
    openMenu();
    fireEvent.click(screen.getByText(/Duplicate/));
    const updated = hoisted.setNodes.mock.calls.at(-1)[0](hoisted.nodes);
    const copy = updated.find((n) => n.id !== 'a1');
    expect(copy.data.locked).toBe(false);
    expect(updated.find((n) => n.id === 'a1').data.locked).toBe(true);
  });
});

// task-annotation-render-direct-manipulation / task-annotation-responsive-
// bottom-toolbox's "Nearby object menu" contract entry point: an arrow/line
// can never be a valid attach target — `findSnapTarget` (the mechanism
// `computeDroppedAttachment` and this menu both mirror) unconditionally
// excludes `type === 'arrow'` alongside `group` (the retired `frame` kind
// used to be excluded here too — task-annotation-merge-frame-into-shape-
// rectangle folded it into `shape`, which is not excluded), and the
// attachment-follow effect never builds a centre for an arrow, so an
// attachment onto one would never resolve a position. ArrowNode's own
// context menu therefore must not offer the section at all.
describe('ArrowNode "Nearby object menu"', () => {
  it('does not render the "Add nearby" section on an arrow\'s own context menu', () => {
    const attachNearby = vi.fn();
    const data = { dx: 100, dy: 0 };
    hoisted.nodes = [{ id: 'a1', type: 'arrow', position: { x: 0, y: 0 }, data }];
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
        <ArrowNode id="a1" type="arrow" data={data} selected={false} />
      </AnnotationContext.Provider>
    );
    openMenu();
    expect(screen.queryByText('Add nearby')).toBeNull();
    expect(screen.queryByRole('button', { name: '+ Label' })).toBeNull();
    expect(attachNearby).not.toHaveBeenCalled();
  });
});
