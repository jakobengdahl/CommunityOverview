/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, act } from '@testing-library/react';
import BottomSheet from './BottomSheet';

vi.mock('../i18n', () => ({
  useI18n: () => ({ t: (key) => key }),
}));

function renderSheet(props = {}) {
  const onClose = vi.fn();
  const onSnapPointChange = vi.fn();
  const utils = render(
    <BottomSheet
      isOpen
      snapPoint="half"
      onClose={onClose}
      onSnapPointChange={onSnapPointChange}
      {...props}
    >
      <button type="button">first</button>
      <button type="button">last</button>
    </BottomSheet>
  );
  return { ...utils, onClose, onSnapPointChange };
}

// jsdom has no PointerEvent constructor (confirmed against the jsdom version
// this repo pins), so fireEvent.pointer* silently produces events with no
// clientY. Dispatch plain Events with clientY/pointerId patched on instead -
// React's synthetic event just proxies property reads through to whatever
// native event it wraps, so the handler sees the same `event.clientY` it
// would from a real PointerEvent.
function firePointerEvent(el, type, clientY, pointerId = 1) {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'clientY', { value: clientY, configurable: true });
  Object.defineProperty(event, 'pointerId', { value: pointerId, configurable: true });
  act(() => {
    el.dispatchEvent(event);
  });
}

function drag(handle, { from, to }) {
  firePointerEvent(handle, 'pointerdown', from);
  firePointerEvent(handle, 'pointermove', to);
  firePointerEvent(handle, 'pointerup', to);
}

// Mirrors the fake in useVisualViewportInset.test.jsx: a minimal
// VisualViewport whose height a test can shrink to simulate an on-screen
// keyboard opening, and whose listeners are observable.
function makeVisualViewport({ height, offsetTop = 0 }) {
  const listeners = { resize: new Set(), scroll: new Set() };
  return {
    height,
    offsetTop,
    addEventListener: (event, handler) => listeners[event]?.add(handler),
    removeEventListener: (event, handler) => listeners[event]?.delete(handler),
    emit(event, { height: newHeight, offsetTop: newOffsetTop = 0 } = {}) {
      if (newHeight !== undefined) this.height = newHeight;
      this.offsetTop = newOffsetTop;
      listeners[event]?.forEach((handler) => handler());
    },
  };
}

describe('BottomSheet', () => {
  let originalOverflow;

  beforeEach(() => {
    originalOverflow = document.body.style.overflow;
  });

  afterEach(() => {
    cleanup();
    document.body.style.overflow = originalOverflow;
    vi.restoreAllMocks();
  });

  it('renders nothing when closed', () => {
    render(<BottomSheet isOpen={false} onClose={() => {}} />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  describe('snap points', () => {
    it.each([
      ['peek', 16],
      ['half', 50],
      ['full', 92],
    ])('sets sheet height for the %s snap point', (snapPoint, expectedPercent) => {
      renderSheet({ snapPoint });
      const dialog = screen.getByRole('dialog');
      expect(dialog.style.height).toBe(`${expectedPercent}%`);
    });

    it('dragging the handle up from half requests the full snap point', () => {
      const { onSnapPointChange } = renderSheet({ snapPoint: 'half' });
      drag(screen.getByTestId('bottom-sheet-handle'), { from: 300, to: 150 });
      expect(onSnapPointChange).toHaveBeenCalledWith('full');
    });

    it('dragging the handle down from half requests the peek snap point', () => {
      const { onSnapPointChange } = renderSheet({ snapPoint: 'half' });
      drag(screen.getByTestId('bottom-sheet-handle'), { from: 150, to: 300 });
      expect(onSnapPointChange).toHaveBeenCalledWith('peek');
    });

    it('dragging down from peek closes the sheet instead of requesting a lower snap point', () => {
      const { onClose, onSnapPointChange } = renderSheet({ snapPoint: 'peek' });
      drag(screen.getByTestId('bottom-sheet-handle'), { from: 150, to: 300 });
      expect(onClose).toHaveBeenCalledTimes(1);
      expect(onSnapPointChange).not.toHaveBeenCalled();
    });

    it('dragging up from full is a no-op past the top snap point', () => {
      const { onSnapPointChange } = renderSheet({ snapPoint: 'full' });
      drag(screen.getByTestId('bottom-sheet-handle'), { from: 300, to: 150 });
      expect(onSnapPointChange).not.toHaveBeenCalled();
    });

    it('a small drag under the threshold changes nothing', () => {
      const { onClose, onSnapPointChange } = renderSheet({ snapPoint: 'half' });
      drag(screen.getByTestId('bottom-sheet-handle'), { from: 150, to: 170 });
      expect(onClose).not.toHaveBeenCalled();
      expect(onSnapPointChange).not.toHaveBeenCalled();
    });

    it('ignores a second pointer landing on the handle mid-drag instead of resetting the gesture', () => {
      const { onSnapPointChange } = renderSheet({ snapPoint: 'half' });
      const handle = screen.getByTestId('bottom-sheet-handle');

      // First finger starts a drag toward "full" (delta -150 from 300->150).
      firePointerEvent(handle, 'pointerdown', 300, 1);
      // A second finger lands mid-gesture at a different Y - must be ignored,
      // not overwrite the first pointer's start position.
      firePointerEvent(handle, 'pointerdown', 500, 2);
      // The second pointer lifting must not finish the first pointer's drag.
      firePointerEvent(handle, 'pointerup', 500, 2);
      expect(onSnapPointChange).not.toHaveBeenCalled();

      // The original pointer finishes the drag using its own origin (300).
      firePointerEvent(handle, 'pointerup', 150, 1);
      expect(onSnapPointChange).toHaveBeenCalledWith('full');
    });

    it('clears drag state left behind when isOpen closes mid-drag, so the next open can drag again', () => {
      const onSnapPointChange = vi.fn();
      const { rerender } = render(
        <BottomSheet
          isOpen
          snapPoint="half"
          onClose={() => {}}
          onSnapPointChange={onSnapPointChange}
        >
          <button type="button">first</button>
        </BottomSheet>
      );

      // Start a drag but never fire pointerup - e.g. Escape or a surface
      // manager closes the sheet mid-gesture, unmounting the handle before
      // finishDrag runs and clears dragStateRef.
      firePointerEvent(screen.getByTestId('bottom-sheet-handle'), 'pointerdown', 300, 1);

      rerender(
        <BottomSheet
          isOpen={false}
          snapPoint="half"
          onClose={() => {}}
          onSnapPointChange={onSnapPointChange}
        >
          <button type="button">first</button>
        </BottomSheet>
      );
      rerender(
        <BottomSheet
          isOpen
          snapPoint="half"
          onClose={() => {}}
          onSnapPointChange={onSnapPointChange}
        >
          <button type="button">first</button>
        </BottomSheet>
      );

      // A fresh pointerdown must start a new drag, not be swallowed by a
      // dragStateRef the abandoned gesture left set.
      drag(screen.getByTestId('bottom-sheet-handle'), { from: 300, to: 150 });
      expect(onSnapPointChange).toHaveBeenCalledWith('full');
    });
  });

  describe('live drag feedback', () => {
    it('tracks the sheet under the finger while dragging up, not just while dragging down', () => {
      renderSheet({ snapPoint: 'half' });
      const handle = screen.getByTestId('bottom-sheet-handle');
      const dialog = screen.getByRole('dialog');

      firePointerEvent(handle, 'pointerdown', 300, 1);
      firePointerEvent(handle, 'pointermove', 250, 1);
      // Dragging up (negative delta) must move visually too, not clamp to 0 -
      // a floor there would freeze the sheet under the finger on every
      // upward drag while downward drags still tracked in real time.
      expect(dialog.style.transform).toBe('translateY(-50px)');

      firePointerEvent(handle, 'pointerup', 250, 1);
    });
  });

  describe('Escape to close', () => {
    it('closes on Escape from within the sheet', () => {
      const { onClose } = renderSheet();
      fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
      expect(onClose).toHaveBeenCalledTimes(1);
    });

    it('closes on clicking the backdrop scrim', () => {
      const { onClose } = renderSheet();
      fireEvent.click(screen.getByTestId('bottom-sheet-scrim'));
      expect(onClose).toHaveBeenCalledTimes(1);
    });

    it('clicking inside the sheet does not close it', () => {
      const { onClose } = renderSheet();
      fireEvent.click(screen.getByRole('dialog'));
      expect(onClose).not.toHaveBeenCalled();
    });
  });

  describe('focus trap', () => {
    it('moves focus into the sheet on open', () => {
      renderSheet();
      expect(screen.getByText('first')).toHaveFocus();
    });

    it('wraps Tab from the last focusable element back to the first', () => {
      renderSheet();
      const first = screen.getByText('first');
      const last = screen.getByText('last');
      last.focus();
      fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Tab' });
      expect(first).toHaveFocus();
    });

    it('wraps Shift+Tab from the first focusable element back to the last', () => {
      renderSheet();
      const first = screen.getByText('first');
      const last = screen.getByText('last');
      first.focus();
      fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Tab', shiftKey: true });
      expect(last).toHaveFocus();
    });

    it('restores focus to the previously focused element on close', () => {
      const trigger = document.createElement('button');
      trigger.textContent = 'open sheet';
      document.body.appendChild(trigger);
      trigger.focus();

      const { unmount } = renderSheet();
      expect(screen.getByText('first')).toHaveFocus();

      unmount();
      expect(trigger).toHaveFocus();
      trigger.remove();
    });
  });

  describe('prefers-reduced-motion', () => {
    it('applies the no-motion class when the media query matches', () => {
      const original = window.matchMedia;
      window.matchMedia = vi.fn((query) => ({
        matches: query === '(prefers-reduced-motion: reduce)',
        media: query,
        addEventListener: () => {},
        removeEventListener: () => {},
      }));

      renderSheet();
      expect(screen.getByRole('dialog').className).toMatch(/bottom-sheet--no-motion/);

      window.matchMedia = original;
    });

    it('omits the no-motion class when the media query does not match', () => {
      renderSheet();
      expect(screen.getByRole('dialog').className).not.toMatch(/bottom-sheet--no-motion/);
    });
  });

  describe('body scroll lock', () => {
    it('locks body scroll while open and restores it on unmount', () => {
      document.body.style.overflow = 'auto';
      const { unmount } = renderSheet();
      expect(document.body.style.overflow).toBe('hidden');
      unmount();
      expect(document.body.style.overflow).toBe('auto');
    });
  });

  describe('on-screen keyboard avoidance', () => {
    let originalVisualViewport;
    let originalInnerHeight;

    beforeEach(() => {
      originalVisualViewport = window.visualViewport;
      originalInnerHeight = window.innerHeight;
      Object.defineProperty(window, 'innerHeight', { configurable: true, value: 800 });
    });

    afterEach(() => {
      Object.defineProperty(window, 'visualViewport', {
        configurable: true,
        value: originalVisualViewport,
      });
      Object.defineProperty(window, 'innerHeight', {
        configurable: true,
        value: originalInnerHeight,
      });
    });

    it('does not set a keyboard inset while no keyboard is open', () => {
      const vv = makeVisualViewport({ height: 800 });
      Object.defineProperty(window, 'visualViewport', { configurable: true, value: vv });

      renderSheet();

      expect(
        screen.getByTestId('bottom-sheet-scrim').style.getPropertyValue('--keyboard-inset')
      ).toBe('');
    });

    it('shrinks the scrim above the keyboard once the visual viewport reports it open', () => {
      const vv = makeVisualViewport({ height: 800 });
      Object.defineProperty(window, 'visualViewport', { configurable: true, value: vv });

      renderSheet();
      act(() => {
        vv.emit('resize', { height: 480 });
      });

      expect(
        screen.getByTestId('bottom-sheet-scrim').style.getPropertyValue('--keyboard-inset')
      ).toBe('320px');
    });

    it('scrolls the focused field into view once the keyboard opens', () => {
      const vv = makeVisualViewport({ height: 800 });
      Object.defineProperty(window, 'visualViewport', { configurable: true, value: vv });

      renderSheet();
      // Move focus to a field other than whichever one the sheet's own
      // open-focus effect landed on, so this test's outcome depends on the
      // resize-driven effect below, not on that unrelated auto-focus.
      const last = screen.getByText('last');
      last.focus();
      const scrollIntoView = vi.fn();
      last.scrollIntoView = scrollIntoView;

      act(() => {
        vv.emit('resize', { height: 480 });
      });

      expect(scrollIntoView).toHaveBeenCalledWith({ block: 'nearest', behavior: 'smooth' });
    });

    it('scrolls a newly focused field into view even while the keyboard is already open', () => {
      const vv = makeVisualViewport({ height: 480 });
      Object.defineProperty(window, 'visualViewport', { configurable: true, value: vv });

      renderSheet();
      const last = screen.getByText('last');
      const scrollIntoView = vi.fn();
      last.scrollIntoView = scrollIntoView;

      // A real focus transition (not the sheet's own initial auto-focus,
      // which already landed elsewhere) - this is what a user tapping a
      // second field while the keyboard is already up looks like, and it
      // must be caught by the onFocus handler since keyboardInset itself
      // does not change here.
      last.focus();

      expect(scrollIntoView).toHaveBeenCalledWith({ block: 'nearest', behavior: 'smooth' });
    });

    it('never scrolls an element outside the sheet, even if it somehow ends up focused', () => {
      const vv = makeVisualViewport({ height: 800 });
      Object.defineProperty(window, 'visualViewport', { configurable: true, value: vv });

      renderSheet();
      const outside = document.createElement('button');
      document.body.appendChild(outside);
      const scrollIntoView = vi.fn();
      outside.scrollIntoView = scrollIntoView;
      outside.focus();

      act(() => {
        vv.emit('resize', { height: 480 });
      });

      expect(scrollIntoView).not.toHaveBeenCalled();
      outside.remove();
    });

    it('is a graceful no-op when visualViewport is unavailable', () => {
      Object.defineProperty(window, 'visualViewport', { configurable: true, value: undefined });

      expect(() => renderSheet()).not.toThrow();
      expect(
        screen.getByTestId('bottom-sheet-scrim').style.getPropertyValue('--keyboard-inset')
      ).toBe('');
    });
  });

  it('uses i18n keys as default labels for close and drag handle, including the current snap position', () => {
    renderSheet({ snapPoint: 'half' });
    expect(screen.getByTestId('bottom-sheet-handle')).toHaveAttribute(
      'aria-label',
      'bottom_sheet.drag_handle (bottom_sheet.snap_half)'
    );
  });

  it('accepts prop overrides for labels instead of the i18n defaults', () => {
    renderSheet({ dragHandleLabel: 'Custom handle', title: 'My sheet', closeLabel: 'Dismiss' });
    expect(screen.getByTestId('bottom-sheet-handle')).toHaveAttribute(
      'aria-label',
      'Custom handle (bottom_sheet.snap_half)'
    );
    expect(screen.getByLabelText('Dismiss')).toBeInTheDocument();
  });

  it('names the dialog after the title when one is given', () => {
    renderSheet({ title: 'My sheet' });
    expect(screen.getByRole('dialog')).toHaveAttribute('aria-label', 'My sheet');
  });

  it('falls back to a generic dialog label, not the close button label, when no title is given', () => {
    renderSheet();
    expect(screen.getByRole('dialog')).toHaveAttribute('aria-label', 'bottom_sheet.dialog_label');
  });

  it('updates the drag handle label as the snap point changes', () => {
    renderSheet({ snapPoint: 'full' });
    expect(screen.getByTestId('bottom-sheet-handle').getAttribute('aria-label')).toContain(
      'bottom_sheet.snap_full'
    );
  });
});
