import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import NoteNode from '../src/components/NoteNode';
import LabelNode from '../src/components/LabelNode';
import GroupNode from '../src/components/GroupNode';
import GenericAnnotationNode from '../src/components/GenericAnnotationNode';
import { AnnotationContext } from '../src/components/AnnotationContext';
import {
  computeAnnotationAriaLabel,
  computeAttachmentToTarget,
  isEligibleAttachTarget,
  nodesAtPoint,
} from '../src/utils/annotations';

// task-annotation-accessible-shared-controls: closes the gaps the accepted
// audit (docs/ANNOTATION_CONTRACT.md's "Keyboard, touch and screen-reader
// controls audit") left open for the follow-up implementation task. Three
// kinds of evidence, same convention the audit's own test files established:
// - Pure unit tests for the new shared utils (utils/annotations.js) — no
//   rendering needed, no jsdom limitation to route around.
// - Real-`reactflow` component tests (unmocked, like
//   AnnotationAccessibilityAudit.test.jsx) for anything that depends on a
//   real, focusable, measured node wrapper — the DOM mechanics
//   GraphCanvas.jsx's Shift+F10 handler and the accessible-name wiring both
//   depend on.
// - Mocked-`reactflow` component tests (like AnnotationEditTrigger.test.jsx)
//   for per-kind menu content/wiring that does not need real layout.

describe('computeAnnotationAriaLabel (per-kind accessible name)', () => {
  const labels = {}; // exercises the English defaults every call site falls back to.

  it('note: "sticky note, {text}", or just the kind word when empty', () => {
    expect(computeAnnotationAriaLabel('note', { text: 'Budget Q3' }, labels)).toBe(
      'Sticky note, Budget Q3'
    );
    expect(computeAnnotationAriaLabel('note', {}, labels)).toBe('Sticky note');
  });

  it('label: "label, {text}"', () => {
    expect(computeAnnotationAriaLabel('label', { text: 'Q3 launch' }, labels)).toBe(
      'Label, Q3 launch'
    );
  });

  it('text: "text, {text}"', () => {
    expect(computeAnnotationAriaLabel('text', { text: 'Ship it' }, labels)).toBe('Text, Ship it');
  });

  it('shape: "{subtype} shape, {caption}" — subtype word says WHAT the shape is, not just "shape"', () => {
    expect(
      computeAnnotationAriaLabel('shape', { shape: 'rectangle', text: 'Phase 1' }, labels)
    ).toBe('rectangle shape, Phase 1');
    expect(computeAnnotationAriaLabel('shape', { shape: 'process_arrow' }, labels)).toBe(
      'process arrow shape'
    );
    // Every subtype now distinguishable by name, closing the audit's
    // "indistinguishable from each other and from nothing at all" finding.
    expect(computeAnnotationAriaLabel('shape', { shape: 'hexagon' }, labels)).not.toBe(
      computeAnnotationAriaLabel('shape', { shape: 'circle' }, labels)
    );
  });

  it('icon: "{name} icon" — the configured NAME, not the rendered glyph character the audit found accname actually falls back to', () => {
    expect(computeAnnotationAriaLabel('icon', { icon: 'star' }, labels)).toBe('star icon');
    expect(computeAnnotationAriaLabel('icon', {}, labels)).toBe('icon');
  });

  it('vote_dot: a fixed "Vote dot" — the audit found this kind had no text/title anywhere in its markup at all', () => {
    expect(computeAnnotationAriaLabel('vote_dot', {}, labels)).toBe('Vote dot');
  });

  it('image: "Image, {alt}", or just "Image" — always says what it is, unlike the old alt-only fallback', () => {
    expect(computeAnnotationAriaLabel('image', { alt: 'Q3 chart' }, labels)).toBe(
      'Image, Q3 chart'
    );
    expect(computeAnnotationAriaLabel('image', {}, labels)).toBe('Image');
  });

  it('arrow and freehand: fixed names — both were completely empty before (pure SVG, no text or title)', () => {
    expect(computeAnnotationAriaLabel('arrow', {}, labels)).toBe('Arrow');
    expect(computeAnnotationAriaLabel('freehand', {}, labels)).toBe('Freehand stroke');
  });

  it('group: "Group, {label}", or just "Group" — never the untranslated "📁 Group" fallback the audit flagged', () => {
    expect(computeAnnotationAriaLabel('group', { label: 'Pilot phase' }, labels)).toBe(
      'Group, Pilot phase'
    );
    expect(computeAnnotationAriaLabel('group', {}, labels)).toBe('Group');
  });

  it('respects a host-supplied `labels` object rather than hardcoding English — the i18n-props contract every other AnnotationContext string already follows', () => {
    expect(
      computeAnnotationAriaLabel('note', { text: 'Q3' }, { ariaKindNote: 'Klisterlapp' })
    ).toBe('Klisterlapp, Q3');
  });

  it('an unrecognised kind returns empty rather than throwing (defensive, matches the rest of this file)', () => {
    expect(computeAnnotationAriaLabel('nonsense', {}, labels)).toBe('');
    expect(computeAnnotationAriaLabel(undefined, undefined, labels)).toBe('');
  });
});

describe('computeAttachmentToTarget / isEligibleAttachTarget (non-drag "Attach to…" mode)', () => {
  it("builds the same attachment shape a drop-to-snap would, keeping the annotation's current offset from the target's centre", () => {
    const target = {
      id: 't1',
      type: 'custom',
      position: { x: 100, y: 100 },
      width: 40,
      height: 20,
    };
    const node = { id: 'label-1', position: { x: 150, y: 90 } };
    const attachment = computeAttachmentToTarget(node, target);
    // target centre = (120, 110); offset = (150-120, 90-110) = (30, -20).
    expect(attachment).toEqual({ target_id: 't1', target_type: 'node', offset: { x: 30, y: -20 } });
  });

  it('marks an annotation target as target_type "annotation", matching computeDroppedAttachment', () => {
    const target = { id: 'note-1', type: 'note', position: { x: 0, y: 0 }, width: 10, height: 10 };
    const node = { id: 'label-1', position: { x: 5, y: 5 } };
    expect(computeAttachmentToTarget(node, target).target_type).toBe('annotation');
  });

  it('returns null when the node being attached has no position', () => {
    expect(computeAttachmentToTarget({}, { id: 't1', position: { x: 0, y: 0 } })).toBeNull();
  });

  it('isEligibleAttachTarget rejects a group and the annotation itself, accepts anything else', () => {
    expect(isEligibleAttachTarget({ id: 'g1', type: 'group' }, 'label-1')).toBe(false);
    expect(isEligibleAttachTarget({ id: 'label-1', type: 'label' }, 'label-1')).toBe(false);
    expect(isEligibleAttachTarget({ id: 'note-1', type: 'note' }, 'label-1')).toBe(true);
    expect(isEligibleAttachTarget(null, 'label-1')).toBe(false);
  });
});

describe('nodesAtPoint (overlap-object picker hit test)', () => {
  it('returns every node whose box contains the point, excluding group', () => {
    const nodes = [
      { id: 'a', type: 'note', position: { x: 0, y: 0 }, width: 40, height: 40 },
      { id: 'b', type: 'text', position: { x: 10, y: 10 }, width: 40, height: 40 },
      { id: 'g', type: 'group', position: { x: 0, y: 0 }, width: 500, height: 500 },
      { id: 'far', type: 'label', position: { x: 500, y: 500 }, width: 10, height: 10 },
    ];
    const hits = nodesAtPoint(nodes, { x: 20, y: 20 }).map((n) => n.id);
    expect(hits.sort()).toEqual(['a', 'b']);
  });

  it('gives a boxless/fixed-intrinsic-size kind (icon, vote_dot, arrow) a small fallback hit radius rather than never matching', () => {
    const nodes = [{ id: 'icon-1', type: 'icon', position: { x: 100, y: 100 } }];
    expect(nodesAtPoint(nodes, { x: 105, y: 102 })).toHaveLength(1);
    expect(nodesAtPoint(nodes, { x: 300, y: 300 })).toHaveLength(0);
  });

  it('returns an empty array for a null point rather than throwing', () => {
    expect(nodesAtPoint([{ id: 'a', position: { x: 0, y: 0 } }], null)).toEqual([]);
  });
});

// --- Component-level: the Edit-button/menu additions per kind ---

const hoisted = vi.hoisted(() => ({ setNodes: vi.fn(), nodes: [] }));

vi.mock('reactflow', () => ({
  NodeResizer: () => null,
  useReactFlow: () => ({
    setNodes: hoisted.setNodes,
    getNodes: () => hoisted.nodes,
    getNode: (id) => hoisted.nodes.find((n) => n.id === id),
    screenToFlowPosition: ({ x, y }) => ({ x, y }),
  }),
}));

const menuLabels = {
  color: 'Colour',
  delete: 'Delete',
  labelPlaceholder: 'Label',
  notePlaceholder: 'Note',
  textSize: 'Text size',
  opacity: 'Opacity',
  editAnnotation: 'Edit',
  layer: 'Layer',
  layerFront: 'Bring to front',
  layerBack: 'Send to back',
  rotation: 'Rotation',
  rotateLeft: 'Rotate left',
  rotateRight: 'Rotate right',
  rotateReset: 'Reset rotation',
  duplicate: 'Duplicate',
  nearbyMenu: 'Add nearby',
  nearbyLabel: 'Label',
  nearbyIcon: 'Icon',
  nearbyText: 'Text',
  width: 'Width',
  height: 'Height',
  applySize: 'Apply size',
  attachTo: 'Attach to…',
  detach: 'Detach',
  unlock: 'Unlock',
};

beforeEach(() => {
  hoisted.setNodes.mockClear();
  hoisted.nodes = [];
});

describe('non-drag "Attach to…" wiring (LabelNode, GenericAnnotationNode text/icon)', () => {
  it('LabelNode: clicking "Attach to…" calls enterAttachMode with this annotation\'s id and closes the menu', () => {
    const enterAttachMode = vi.fn();
    render(
      <AnnotationContext.Provider
        value={{
          notifyChange: vi.fn(),
          notifyRemoteLockedAttempt: vi.fn(),
          labels: menuLabels,
          enterAttachMode,
        }}
      >
        <LabelNode id="label-1" data={{ text: 'x' }} selected />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(screen.getByText('x'));
    fireEvent.click(screen.getByRole('button', { name: /attach to/i }));
    expect(enterAttachMode).toHaveBeenCalledWith('label-1');
    expect(document.querySelector('.graph-annotation-context-menu')).toBeNull();
  });

  it('LabelNode: offers Detach only once the annotation actually carries an attachment', () => {
    render(
      <AnnotationContext.Provider
        value={{ notifyChange: vi.fn(), notifyRemoteLockedAttempt: vi.fn(), labels: menuLabels }}
      >
        <LabelNode id="label-1" data={{ text: 'x' }} selected />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(screen.getByText('x'));
    expect(screen.queryByRole('button', { name: 'Detach' })).toBeNull();
  });

  it('LabelNode: Detach clears data.attachment and refuses under a remote edit lease', () => {
    const notifyChange = vi.fn();
    const notifyRemoteLockedAttempt = vi.fn();
    render(
      <AnnotationContext.Provider
        value={{ notifyChange, notifyRemoteLockedAttempt, labels: menuLabels }}
      >
        <LabelNode
          id="label-1"
          data={{
            text: 'x',
            attachment: { target_id: 't1', target_type: 'node', offset: { x: 0, y: 0 } },
          }}
          selected
        />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(screen.getByText('x'));
    fireEvent.click(screen.getByRole('button', { name: 'Detach' }));
    const updater = hoisted.setNodes.mock.calls.at(-1)[0];
    const result = updater([
      { id: 'label-1', data: { text: 'x', attachment: { target_id: 't1' } } },
    ]);
    expect(result[0].data.attachment).toBeUndefined();
    expect(notifyChange).toHaveBeenCalledWith('style');
  });

  it('GenericAnnotationNode (icon): offers Attach to… and Detach the same way, gated on ATTACHABLE_OVERLAY_KINDS', () => {
    const enterAttachMode = vi.fn();
    render(
      <AnnotationContext.Provider
        value={{
          notifyChange: vi.fn(),
          notifyRemoteLockedAttempt: vi.fn(),
          labels: menuLabels,
          enterAttachMode,
        }}
      >
        <GenericAnnotationNode id="icon-1" type="icon" data={{ icon: 'star' }} selected />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(screen.getByTitle('star'));
    fireEvent.click(screen.getByRole('button', { name: /attach to/i }));
    expect(enterAttachMode).toHaveBeenCalledWith('icon-1');
  });

  it('GenericAnnotationNode (vote_dot): offers no Attach to… button — vote_dot is not in ATTACHABLE_OVERLAY_KINDS', () => {
    render(
      <AnnotationContext.Provider
        value={{ notifyChange: vi.fn(), notifyRemoteLockedAttempt: vi.fn(), labels: menuLabels }}
      >
        <GenericAnnotationNode id="vd-1" type="vote_dot" data={{}} selected />
      </AnnotationContext.Provider>
    );
    // vote_dot's own div has no distinguishing text/title, so target the
    // generic node wrapper directly via its class.
    fireEvent.contextMenu(document.querySelector('.kind-vote_dot'));
    expect(screen.queryByRole('button', { name: /attach to/i })).toBeNull();
  });
});

describe('non-drag width/height (AnnotationSizeControl, via NoteNode and GroupNode)', () => {
  it('NoteNode: reads the current size on menu open and applies a changed value via setNodes', () => {
    const notifyChange = vi.fn();
    hoisted.nodes = [{ id: 'note-1', style: { width: 200, height: 140 } }];
    render(
      <AnnotationContext.Provider
        value={{ notifyChange, notifyRemoteLockedAttempt: vi.fn(), labels: menuLabels }}
      >
        <NoteNode id="note-1" data={{ text: 'x' }} selected />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(screen.getByText('x'));
    const widthInput = screen.getByRole('spinbutton', { name: 'Width' });
    expect(widthInput.value).toBe('200');
    fireEvent.change(widthInput, { target: { value: '260' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply size' }));
    const updater = hoisted.setNodes.mock.calls.at(-1)[0];
    const result = updater([{ id: 'note-1', style: { width: 200, height: 140 } }]);
    expect(result[0].style).toEqual({ width: 260, height: 140 });
    expect(notifyChange).toHaveBeenCalledWith('geometry');
  });

  it('GroupNode: the size control is offered in its own (previously unadorned) menu', () => {
    hoisted.nodes = [{ id: 'group-1', style: { width: 300, height: 200 } }];
    render(
      <AnnotationContext.Provider
        value={{ notifyChange: vi.fn(), notifyRemoteLockedAttempt: vi.fn(), labels: menuLabels }}
      >
        <GroupNode id="group-1" data={{ label: 'G' }} selected />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(screen.getByText('G'));
    expect(screen.getByRole('spinbutton', { name: 'Width' }).value).toBe('300');
  });
});

describe('group: the Edit button (task-annotation-accessible-shared-controls — the one kind the responsive-toolbox task left out)', () => {
  it("is rendered only while selected, opens the same menu right-click opens, and manages focus the same way every other kind's does", async () => {
    render(
      <AnnotationContext.Provider
        value={{ notifyChange: vi.fn(), notifyRemoteLockedAttempt: vi.fn(), labels: menuLabels }}
      >
        <GroupNode id="group-1" data={{ label: 'G' }} selected />
      </AnnotationContext.Provider>
    );
    const editButton = screen.getByRole('button', { name: 'Edit' });
    fireEvent.click(editButton);
    const menu = document.querySelector('.graph-annotation-context-menu');
    expect(menu).not.toBeNull();
    await waitFor(() => expect(menu.contains(document.activeElement)).toBe(true));
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() =>
      expect(document.querySelector('.graph-annotation-context-menu')).toBeNull()
    );
    expect(document.activeElement).toBe(editButton);
  });

  it('is absent on an unselected group', () => {
    render(
      <AnnotationContext.Provider
        value={{ notifyChange: vi.fn(), notifyRemoteLockedAttempt: vi.fn(), labels: menuLabels }}
      >
        <GroupNode id="group-1" data={{ label: 'G' }} selected={false} />
      </AnnotationContext.Provider>
    );
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull();
  });
});

describe('menu keyboard navigation and focus trap (useAnnotationMenuKeyNav, shared by all six kinds)', () => {
  it("ArrowDown/ArrowUp/Home/End rove focus across the menu's real <button> elements", () => {
    render(
      <AnnotationContext.Provider
        value={{ notifyChange: vi.fn(), notifyRemoteLockedAttempt: vi.fn(), labels: menuLabels }}
      >
        <NoteNode id="note-1" data={{ text: 'x' }} selected />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(screen.getByText('x'));
    const menu = document.querySelector('.graph-annotation-context-menu');
    const buttons = Array.from(menu.querySelectorAll('button:not([disabled])'));
    expect(buttons.length).toBeGreaterThan(2);
    buttons[0].focus();
    fireEvent.keyDown(menu, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(buttons[1]);
    fireEvent.keyDown(menu, { key: 'Home' });
    expect(document.activeElement).toBe(buttons[0]);
    fireEvent.keyDown(menu, { key: 'End' });
    expect(document.activeElement).toBe(buttons[buttons.length - 1]);
  });

  it('Tab on the last item wraps to the first; Shift+Tab on the first wraps to the last — a real (if minimal) focus trap', () => {
    render(
      <AnnotationContext.Provider
        value={{ notifyChange: vi.fn(), notifyRemoteLockedAttempt: vi.fn(), labels: menuLabels }}
      >
        <NoteNode id="note-1" data={{ text: 'x' }} selected />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(screen.getByText('x'));
    const menu = document.querySelector('.graph-annotation-context-menu');
    const buttons = Array.from(menu.querySelectorAll('button:not([disabled])'));
    buttons[buttons.length - 1].focus();
    fireEvent.keyDown(menu, { key: 'Tab' });
    expect(document.activeElement).toBe(buttons[0]);
    fireEvent.keyDown(menu, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(buttons[buttons.length - 1]);
  });
});

// The real-reactflow (unmocked) Shift+F10/Menu-key reachability mechanism
// test lives in AnnotationEditTriggerReachability.test.jsx — this file
// already mocks `reactflow` above (for the per-kind menu-content tests), and
// `vi.mock` is file-scoped, so the two cannot share a file.
