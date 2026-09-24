/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import { useRef } from 'react';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { useModalFocusTrap } from './useModalFocusTrap';

function Trap({ active, children }) {
  const ref = useRef(null);
  const trapTabKey = useModalFocusTrap(ref, active);
  return (
    <div ref={ref} tabIndex={-1} data-testid="trap" onKeyDown={trapTabKey}>
      {children}
    </div>
  );
}

const threeButtons = (
  <>
    <button type="button">one</button>
    <button type="button">two</button>
    <button type="button" disabled>
      disabled
    </button>
    <button type="button">three</button>
  </>
);

describe('useModalFocusTrap', () => {
  let originalOverflow;

  afterEach(() => {
    cleanup();
    if (originalOverflow !== undefined) document.body.style.overflow = originalOverflow;
  });

  it('does nothing while inactive', () => {
    originalOverflow = document.body.style.overflow;
    document.body.style.overflow = 'auto';
    const trigger = document.createElement('button');
    document.body.appendChild(trigger);
    trigger.focus();

    render(<Trap active={false}>{threeButtons}</Trap>);
    expect(trigger).toHaveFocus();
    expect(document.body.style.overflow).toBe('auto');
    trigger.remove();
  });

  it('focuses the first enabled focusable descendant, else the container', () => {
    const { unmount } = render(<Trap active>{threeButtons}</Trap>);
    expect(screen.getByText('one')).toHaveFocus();
    unmount();

    render(
      <Trap active>
        <span>nothing focusable</span>
      </Trap>
    );
    expect(screen.getByTestId('trap')).toHaveFocus();
  });

  it('skips disabled controls when wrapping', () => {
    render(<Trap active>{threeButtons}</Trap>);
    screen.getByText('one').focus();
    fireEvent.keyDown(screen.getByTestId('trap'), { key: 'Tab', shiftKey: true });
    expect(screen.getByText('three')).toHaveFocus();
  });

  it('prevents the default move when it wraps Tab at the last element', () => {
    render(<Trap active>{threeButtons}</Trap>);
    screen.getByText('three').focus();
    const notPrevented = fireEvent.keyDown(screen.getByTestId('trap'), { key: 'Tab' });
    expect(notPrevented).toBe(false);
    expect(screen.getByText('one')).toHaveFocus();
  });

  it('prevents the default move when it wraps Shift+Tab at the first element', () => {
    render(<Trap active>{threeButtons}</Trap>);
    screen.getByText('one').focus();
    const notPrevented = fireEvent.keyDown(screen.getByTestId('trap'), {
      key: 'Tab',
      shiftKey: true,
    });
    expect(notPrevented).toBe(false);
    expect(screen.getByText('three')).toHaveFocus();
  });

  it('prevents the default move when it pulls escaped focus back in', () => {
    render(<Trap active>{threeButtons}</Trap>);
    screen.getByTestId('trap').focus();
    const notPrevented = fireEvent.keyDown(screen.getByTestId('trap'), { key: 'Tab' });
    expect(notPrevented).toBe(false);
    expect(screen.getByText('one')).toHaveFocus();
  });

  it('skips disabled controls at both ends when wrapping', () => {
    render(
      <Trap active>
        <button type="button" disabled>
          disabled first
        </button>
        <button type="button">one</button>
        <button type="button">two</button>
        <button type="button" disabled>
          disabled last
        </button>
      </Trap>
    );
    expect(screen.getByText('one')).toHaveFocus();

    screen.getByText('two').focus();
    expect(fireEvent.keyDown(screen.getByTestId('trap'), { key: 'Tab' })).toBe(false);
    expect(screen.getByText('one')).toHaveFocus();

    expect(fireEvent.keyDown(screen.getByTestId('trap'), { key: 'Tab', shiftKey: true })).toBe(
      false
    );
    expect(screen.getByText('two')).toHaveFocus();
  });

  it('ignores keys other than Tab, even at the last element', () => {
    render(<Trap active>{threeButtons}</Trap>);
    const last = screen.getByText('three');
    last.focus();
    expect(fireEvent.keyDown(screen.getByTestId('trap'), { key: 'Enter' })).toBe(true);
    expect(fireEvent.keyDown(screen.getByTestId('trap'), { key: 'ArrowDown' })).toBe(true);
    expect(last).toHaveFocus();
  });

  it('leaves Tab between inner elements to the browser', () => {
    render(<Trap active>{threeButtons}</Trap>);
    screen.getByText('one').focus();
    const notPrevented = fireEvent.keyDown(screen.getByTestId('trap'), { key: 'Tab' });
    expect(notPrevented).toBe(true);
    expect(screen.getByText('one')).toHaveFocus();
  });

  it('swallows Tab when nothing inside is focusable', () => {
    render(
      <Trap active>
        <span>nothing focusable</span>
      </Trap>
    );
    const notPrevented = fireEvent.keyDown(screen.getByTestId('trap'), { key: 'Tab' });
    expect(notPrevented).toBe(false);
  });

  it('does not restore focus to an element removed while the trap was active', () => {
    const trigger = document.createElement('button');
    document.body.appendChild(trigger);
    trigger.focus();

    const { rerender } = render(<Trap active>{threeButtons}</Trap>);
    trigger.remove();
    // jsdom ignores focus() on a detached element, so assert the call itself.
    const focusSpy = vi.spyOn(trigger, 'focus');
    rerender(<Trap active={false}>{threeButtons}</Trap>);
    expect(focusSpy).not.toHaveBeenCalled();
  });

  it('does not re-steal focus when re-rendered while still active', () => {
    const { rerender } = render(<Trap active>{threeButtons}</Trap>);
    screen.getByText('two').focus();
    rerender(<Trap active>{threeButtons}</Trap>);
    expect(screen.getByText('two')).toHaveFocus();
  });

  it('restores the overflow value it found, not a hard-coded default', () => {
    originalOverflow = document.body.style.overflow;
    document.body.style.overflow = 'clip';
    const { rerender } = render(<Trap active>{threeButtons}</Trap>);
    expect(document.body.style.overflow).toBe('hidden');
    rerender(<Trap active={false}>{threeButtons}</Trap>);
    expect(document.body.style.overflow).toBe('clip');
  });
});
