import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import NoteNode from '../src/components/NoteNode';
import GenericAnnotationNode from '../src/components/GenericAnnotationNode';
import { AnnotationContext } from '../src/components/AnnotationContext';

// task-annotation-responsive-bottom-toolbox: the contextual "Edit" surface's
// visible entry point (a real, focusable button, distinct from the
// pre-existing right-click path) and its two destinations — a floating menu
// anchored to the button (desktop, or a compact host with no mobile sheet
// wired) and the host's mobile edit sheet (compact + integrated host).
// Covers the shared `useAnnotationEditTrigger` hook's contract through one
// representative dedicated-component kind (NoteNode) and one representative
// GenericAnnotationNode kind (text) — the hook itself is what every other
// kind (LabelNode, ArrowNode, FreehandAnnotationNode, and every other
// GenericAnnotationNode kind) shares, so this is not re-tested six more
// times per NoteNode/LabelNode's own existing precedent of not duplicating
// coverage for shared mechanisms.

const hoisted = vi.hoisted(() => ({ setNodes: vi.fn() }));

vi.mock('reactflow', () => ({
  NodeResizer: () => <div data-testid="resizer" />,
  useReactFlow: () => ({
    setNodes: hoisted.setNodes,
    screenToFlowPosition: (p) => p,
    getNodes: () => [],
  }),
}));

const labels = {
  color: 'Colour',
  delete: 'Delete',
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
};

function renderNote(data, { selected = true, editSheet } = {}) {
  const notifyChange = vi.fn();
  const notifyRemoteLockedAttempt = vi.fn();
  const value = { notifyChange, notifyRemoteLockedAttempt, labels };
  if (editSheet) value.editSheet = editSheet;
  const utils = render(
    <AnnotationContext.Provider value={value}>
      <NoteNode id="note-1" data={data} selected={selected} />
    </AnnotationContext.Provider>
  );
  return { notifyChange, notifyRemoteLockedAttempt, ...utils };
}

describe('the Edit trigger button (task-annotation-responsive-bottom-toolbox)', () => {
  beforeEach(() => vi.clearAllMocks());

  it('is not rendered on an unselected annotation', () => {
    renderNote({ text: 'x' }, { selected: false });
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull();
  });

  it('is a real, focusable button on a selected annotation — reachable without right-click', () => {
    renderNote({ text: 'x' }, { selected: true });
    const button = screen.getByRole('button', { name: 'Edit' });
    expect(button.tagName).toBe('BUTTON');
    expect(button.getAttribute('type')).toBe('button');
  });

  it('opens the same property editor right-click opens, as a floating (non-sheet) menu, when the host has no mobile edit sheet wired', () => {
    renderNote({ text: 'x' }, { selected: true });
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    const menu = document.querySelector('.graph-annotation-context-menu');
    expect(menu).not.toBeNull();
    expect(menu.classList.contains('sheet')).toBe(false);
    // The same colour/text-size/opacity/delete controls the right-click path renders.
    expect(screen.getByText('Colour')).toBeInTheDocument();
    expect(screen.getByText('Opacity')).toBeInTheDocument();
  });

  it('moves focus into the menu on open and returns it to the Edit button on close', async () => {
    renderNote({ text: 'x' }, { selected: true });
    const editButton = screen.getByRole('button', { name: 'Edit' });
    fireEvent.click(editButton);
    await waitFor(() => {
      const menu = document.querySelector('.graph-annotation-context-menu');
      expect(menu?.contains(document.activeElement)).toBe(true);
    });
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => {
      expect(document.querySelector('.graph-annotation-context-menu')).toBeNull();
    });
    expect(document.activeElement).toBe(editButton);
  });

  it('does NOT move/return focus for a menu opened the pre-existing way, by right-click', async () => {
    renderNote({ text: 'x' }, { selected: true });
    const before = document.activeElement;
    fireEvent.contextMenu(screen.getByText('x'));
    expect(document.querySelector('.graph-annotation-context-menu')).not.toBeNull();
    // NoteNode's own dismiss-listener registration is deferred a tick (see
    // its `setTimeout(..., 0)`) so the contextmenu event that opened the menu
    // never immediately closes it — flush that before Escape, matching
    // FreehandAnnotationNode.test.jsx's identical "closes on Escape" test.
    await new Promise((resolve) => setTimeout(resolve, 0));
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(document.querySelector('.graph-annotation-context-menu')).toBeNull();
    // Right-click never routes through the button-triggered focus management
    // this hook adds — activeElement is left exactly where the dismiss
    // listener found it, same as before this task.
    expect(document.activeElement).toBe(before);
  });

  it('refuses to open (and notifies instead) when another client holds the edit lease', () => {
    const { notifyRemoteLockedAttempt } = renderNote(
      { text: 'x', remoteLease: { clientId: 'c2', color: '#e6194b', displayName: 'Ada' } },
      { selected: true }
    );
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    expect(notifyRemoteLockedAttempt).toHaveBeenCalledTimes(1);
    expect(document.querySelector('.graph-annotation-context-menu')).toBeNull();
  });

  describe('sheet mode (compact/integrated host)', () => {
    function makeSheetContainer() {
      const el = document.createElement('div');
      document.body.appendChild(el);
      return el;
    }

    it('asks the host to open its sheet, then portals the same menu content into the sheet container with a `.sheet` modifier', () => {
      const container = makeSheetContainer();
      const requestOpen = vi.fn();
      renderNote(
        { text: 'x' },
        {
          selected: true,
          editSheet: { capable: true, container, requestOpen, requestClose: vi.fn() },
        }
      );
      fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
      expect(requestOpen).toHaveBeenCalledTimes(1);
      const menu = container.querySelector('.graph-annotation-context-menu');
      expect(menu).not.toBeNull();
      expect(menu.classList.contains('sheet')).toBe(true);
      // Never also portalled to document.body outside the container.
      expect(
        Array.from(document.body.children).some(
          (el) => el !== container && el.querySelector?.('.graph-annotation-context-menu.sheet')
        )
      ).toBe(false);
    });

    it('renders nothing yet (no crash) when capable but the container has not mounted', () => {
      const requestOpen = vi.fn();
      renderNote(
        { text: 'x' },
        {
          selected: true,
          editSheet: { capable: true, container: null, requestOpen, requestClose: vi.fn() },
        }
      );
      fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
      expect(requestOpen).toHaveBeenCalledTimes(1);
      expect(document.querySelector('.graph-annotation-context-menu')).toBeNull();
    });

    it('asks the host to close the sheet when the menu dismisses (outside click)', async () => {
      const container = makeSheetContainer();
      const requestClose = vi.fn();
      renderNote(
        { text: 'x' },
        {
          selected: true,
          editSheet: { capable: true, container, requestOpen: vi.fn(), requestClose },
        }
      );
      fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
      expect(container.querySelector('.graph-annotation-context-menu')).not.toBeNull();
      // See the right-click test above: the dismiss listener registers a
      // tick late.
      await new Promise((resolve) => setTimeout(resolve, 0));
      fireEvent.mouseDown(document.body);
      expect(requestClose).toHaveBeenCalledTimes(1);
    });
  });
});

describe('the Edit trigger on a GenericAnnotationNode kind (text)', () => {
  beforeEach(() => vi.clearAllMocks());

  it('is offered and opens the same right-click editor', () => {
    render(
      <AnnotationContext.Provider
        value={{ notifyChange: vi.fn(), notifyRemoteLockedAttempt: vi.fn(), labels }}
      >
        <GenericAnnotationNode id="text-1" type="text" data={{ text: 'Hi' }} selected />
      </AnnotationContext.Provider>
    );
    const button = screen.getByRole('button', { name: 'Edit' });
    fireEvent.click(button);
    expect(document.querySelector('.graph-annotation-context-menu')).not.toBeNull();
    expect(screen.getByText('Opacity')).toBeInTheDocument();
  });
});
