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
      drag(screen.getByRole('slider'), { from: 300, to: 150 });
      expect(onSnapPointChange).toHaveBeenCalledWith('full');
    });

    it('dragging the handle down from half requests the peek snap point', () => {
      const { onSnapPointChange } = renderSheet({ snapPoint: 'half' });
      drag(screen.getByRole('slider'), { from: 150, to: 300 });
      expect(onSnapPointChange).toHaveBeenCalledWith('peek');
    });

    it('dragging down from peek closes the sheet instead of requesting a lower snap point', () => {
      const { onClose, onSnapPointChange } = renderSheet({ snapPoint: 'peek' });
      drag(screen.getByRole('slider'), { from: 150, to: 300 });
      expect(onClose).toHaveBeenCalledTimes(1);
      expect(onSnapPointChange).not.toHaveBeenCalled();
    });

    it('dragging up from full is a no-op past the top snap point', () => {
      const { onSnapPointChange } = renderSheet({ snapPoint: 'full' });
      drag(screen.getByRole('slider'), { from: 300, to: 150 });
      expect(onSnapPointChange).not.toHaveBeenCalled();
    });

    it('a small drag under the threshold changes nothing', () => {
      const { onClose, onSnapPointChange } = renderSheet({ snapPoint: 'half' });
      drag(screen.getByRole('slider'), { from: 150, to: 170 });
      expect(onClose).not.toHaveBeenCalled();
      expect(onSnapPointChange).not.toHaveBeenCalled();
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

  it('uses i18n keys as default labels for close and drag handle', () => {
    renderSheet();
    expect(screen.getByRole('slider')).toHaveAttribute('aria-label', 'bottom_sheet.drag_handle');
  });

  it('accepts prop overrides for labels instead of the i18n defaults', () => {
    renderSheet({ dragHandleLabel: 'Custom handle', title: 'My sheet', closeLabel: 'Dismiss' });
    expect(screen.getByRole('slider')).toHaveAttribute('aria-label', 'Custom handle');
    expect(screen.getByLabelText('Dismiss')).toBeInTheDocument();
  });
});
