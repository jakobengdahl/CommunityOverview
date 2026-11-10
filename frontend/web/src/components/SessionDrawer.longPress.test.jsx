// covers: frontend/web/src/components/SessionDrawer.jsx
/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, act } from '@testing-library/react';
import {
  renderDrawer,
  renderSessionRow,
  setMobile,
  dispatchPointer,
} from './SessionDrawer.menuTestUtils';

describe('SessionDrawer — long press on touch', () => {
  beforeEach(() => {
    vi.useFakeTimers({ advanceTimers: true });
    setMobile(true);
  });

  it('opens the menu after ~500 ms of continuous touch', async () => {
    const { sessionName } = renderSessionRow();

    dispatchPointer(sessionName, 'pointerdown', { pointerType: 'touch' });
    await act(async () => {
      vi.advanceTimersByTime(500);
    });

    expect(screen.queryByRole('menu')).toBeTruthy();
  });

  it('keeps the menu and mobile drawer open after the long-press release click', () => {
    const onClose = vi.fn();
    const onSelectSession = vi.fn(() => onClose());
    const { sessionName } = renderSessionRow({ onClose, onSelectSession });

    dispatchPointer(sessionName, 'pointerdown', { pointerType: 'touch' });
    act(() => vi.advanceTimersByTime(500));
    dispatchPointer(sessionName, 'pointerup');

    expect(screen.getByRole('menu')).toBeVisible();
    expect(onSelectSession).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.click(sessionName);
    expect(onSelectSession).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('menu')).toBeVisible();

    dispatchPointer(sessionName, 'pointerdown', { pointerType: 'touch' });
    act(() => vi.advanceTimersByTime(10));
    dispatchPointer(sessionName, 'pointerup');
    fireEvent.click(sessionName, { detail: 0 });
    expect(onSelectSession).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['row', 'touch'],
    ['outside', 'touch'],
    ['row', 'mouse'],
    ['outside', 'mouse'],
  ])(
    'preserves the held release suppression when a second pointer clicks %s with %s',
    (location, pointerType) => {
      const onClose = vi.fn();
      const onSelectSession = vi.fn(() => onClose());
      const { sessionName: name } = renderSessionRow({ onClose, onSelectSession });
      const trigger = screen.getByRole('button', { name: 'Session actions' });
      dispatchPointer(name, 'pointerdown', { pointerType: 'touch', pointerId: 1 });
      act(() => vi.advanceTimersByTime(500));
      expect(screen.getByRole('menu')).toBeVisible();

      const secondTarget = location === 'row' ? name : document.body;
      dispatchPointer(secondTarget, 'pointerdown', { pointerType, pointerId: 2 });
      dispatchPointer(secondTarget, 'pointerup', { pointerType, pointerId: 2 });
      fireEvent.click(secondTarget);
      expect(onSelectSession).not.toHaveBeenCalled();
      expect(onClose).not.toHaveBeenCalled();
      act(() => vi.advanceTimersByTime(1000));
      dispatchPointer(name, 'pointerup', { pointerType: 'touch', pointerId: 1 });
      expect(fireEvent.click(name)).toBe(false);
      expect(onSelectSession).not.toHaveBeenCalled();
      expect(onClose).not.toHaveBeenCalled();

      dispatchPointer(name, 'pointerdown', { pointerType: 'touch', pointerId: 3 });
      dispatchPointer(name, 'pointerup', { pointerType: 'touch', pointerId: 3 });
      fireEvent.click(name);
      expect(onSelectSession).toHaveBeenCalledTimes(1);
      expect(onClose).toHaveBeenCalledTimes(1);
      fireEvent.keyDown(document, { key: 'Escape' });
      fireEvent.click(trigger);
      expect(screen.getByRole('menu')).toBeVisible();
      dispatchPointer(document.body, 'pointerdown', { pointerType: 'mouse' });
      fireEvent.click(document.body);
      expect(screen.queryByRole('menu')).toBeNull();
    }
  );

  it('does not restart a pending long press when another finger interrupts it', () => {
    const { sessionName: name } = renderSessionRow();
    dispatchPointer(name, 'pointerdown', { pointerType: 'touch', pointerId: 1 });
    act(() => vi.advanceTimersByTime(250));
    dispatchPointer(name, 'pointerdown', { pointerType: 'touch', pointerId: 2 });
    dispatchPointer(name, 'pointercancel', { pointerType: 'touch', pointerId: 2 });
    act(() => vi.advanceTimersByTime(1000));
    expect(screen.queryByRole('menu')).toBeNull();
    dispatchPointer(name, 'pointerup', { pointerType: 'touch', pointerId: 1 });
    act(() => vi.advanceTimersByTime(1000));
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it.each(['touch', 'pen'])(
    'keeps a %s long-press menu open on kebab release and permits later activation',
    (pointerType) => {
      const onClose = vi.fn();
      const onSelectSession = vi.fn();
      renderDrawer({ onClose, onSelectSession });
      const trigger = screen.getByRole('button', { name: 'Session actions' });

      dispatchPointer(trigger, 'pointerdown', { pointerType, clientX: 120, clientY: 160 });
      act(() => vi.advanceTimersByTime(500));
      dispatchPointer(trigger, 'pointerup', { pointerType });
      expect(fireEvent.click(trigger.querySelector('svg'))).toBe(false);
      expect(screen.getByRole('menu')).toBeVisible();
      expect(screen.getByRole('menu')).toHaveClass('pointer-triggered');
      expect(screen.getByRole('menu').style.left).toBe('120px');
      expect(screen.getByRole('menu').style.top).toBe('160px');
      expect(trigger).toHaveAttribute('aria-expanded', 'true');
      expect(onSelectSession).not.toHaveBeenCalled();
      expect(onClose).not.toHaveBeenCalled();

      dispatchPointer(trigger, 'pointerdown', { pointerType });
      dispatchPointer(trigger, 'pointerup', { pointerType });
      fireEvent.click(trigger);
      expect(screen.queryByRole('menu')).toBeNull();
      trigger.focus();
      fireEvent.keyDown(trigger, { key: 'Enter' });
      fireEvent.click(trigger, { detail: 0 });
      expect(screen.getByRole('menu')).not.toHaveClass('pointer-triggered');

      dispatchPointer(document.body, 'pointerdown', { pointerType: 'mouse' });
      fireEvent.click(document.body);
      expect(screen.queryByRole('menu')).toBeNull();
    }
  );

  it('cancels a pending long press when the browser cancels the touch', () => {
    const { sessionName } = renderSessionRow();

    dispatchPointer(sessionName, 'pointerdown', { pointerType: 'touch' });
    act(() => vi.advanceTimersByTime(250));
    dispatchPointer(sessionName, 'pointercancel');
    act(() => vi.advanceTimersByTime(1000));

    expect(screen.queryByRole('menu')).toBeNull();
  });

  it.each(['blur', 'visibilitychange'])(
    'cancels an interrupted press on %s and permits the next tap',
    (event) => {
      const onSelectSession = vi.fn();
      const onClose = vi.fn();
      const { sessionName: name } = renderSessionRow({ onSelectSession, onClose });

      dispatchPointer(name, 'pointerdown', { pointerType: 'touch' });
      act(() => vi.advanceTimersByTime(250));
      if (event === 'visibilitychange') vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
      fireEvent(event === 'blur' ? window : document, new Event(event));
      // Returning to the page must not revive the interrupted press.
      if (event === 'visibilitychange') {
        vi.spyOn(document, 'hidden', 'get').mockReturnValue(false);
        fireEvent(document, new Event(event));
      } else {
        fireEvent.focus(window);
      }
      act(() => vi.advanceTimersByTime(1000));
      expect(screen.queryByRole('menu')).toBeNull();
      expect(onSelectSession).not.toHaveBeenCalled();
      expect(onClose).not.toHaveBeenCalled();

      dispatchPointer(name, 'pointerdown', { pointerType: 'touch' });
      dispatchPointer(name, 'pointerup', { pointerType: 'touch' });
      fireEvent.click(name);
      expect(onSelectSession).toHaveBeenCalledTimes(1);
      expect(onSelectSession).toHaveBeenCalledWith('abc-123');
    }
  );

  it.each(['blur', 'visibilitychange'])('clears release-click suppression on %s', (event) => {
    const onSelectSession = vi.fn();
    const { sessionName: name } = renderSessionRow({ onSelectSession });
    dispatchPointer(name, 'pointerdown', { pointerType: 'touch' });
    act(() => vi.advanceTimersByTime(500));
    expect(screen.getByRole('menu')).toBeVisible();

    if (event === 'visibilitychange') vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
    fireEvent(event === 'blur' ? window : document, new Event(event));
    fireEvent.click(name, { detail: 0 });
    expect(onSelectSession).toHaveBeenCalledTimes(1);
    expect(onSelectSession).toHaveBeenCalledWith('abc-123');
  });

  it('allows a mouse click on the original target after a fired long press is cancelled', () => {
    const onSelectSession = vi.fn();
    const { sessionName: name } = renderSessionRow({ onSelectSession });
    dispatchPointer(name, 'pointerdown', { pointerType: 'touch' });
    act(() => vi.advanceTimersByTime(500));
    dispatchPointer(name, 'pointercancel');
    dispatchPointer(name, 'pointerdown', { pointerType: 'mouse' });
    fireEvent.click(name);
    expect(onSelectSession).toHaveBeenCalledWith('abc-123');
  });

  it('allows keyboard selection after a fired long press is cancelled', () => {
    const onSelectSession = vi.fn();
    const onClose = vi.fn();
    const { sessionName: name } = renderSessionRow({ onSelectSession, onClose });
    dispatchPointer(name, 'pointerdown', { pointerType: 'touch' });
    act(() => vi.advanceTimersByTime(500));
    dispatchPointer(name, 'pointercancel');
    act(() => vi.advanceTimersByTime(1000));
    expect(screen.getByRole('menu')).toBeVisible();
    expect(onSelectSession).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();

    name.focus();
    fireEvent.keyDown(name, { key: 'Enter' });
    fireEvent.click(name, { detail: 0 });
    expect(onSelectSession).toHaveBeenCalledTimes(1);
    expect(onSelectSession).toHaveBeenCalledWith('abc-123');
  });

  it.each(['touch', 'pen'])(
    'allows assistive clicks after a %s long-press release emits no click',
    (pointerType) => {
      const onSelectSession = vi.fn();
      const onClose = vi.fn();
      const { sessionName: name } = renderSessionRow({ onSelectSession, onClose });
      const trigger = screen.getByRole('button', { name: 'Session actions' });

      for (const target of [name, trigger]) {
        dispatchPointer(target, 'pointerdown', { pointerType });
        act(() => vi.advanceTimersByTime(500));
        dispatchPointer(target, 'pointerup', { pointerType });
        act(() => vi.advanceTimersByTime(0));
        expect(screen.getByRole('menu')).toBeVisible();

        // Assistive technology can activate without pointer or key events.
        expect(fireEvent.click(target, { detail: 0 })).toBe(true);
      }

      expect(onSelectSession).toHaveBeenCalledTimes(1);
      expect(onSelectSession).toHaveBeenCalledWith('abc-123');
      expect(screen.queryByRole('menu')).toBeNull();
      expect(onClose).not.toHaveBeenCalled();
      fireEvent.click(trigger, { detail: 0 });
      expect(screen.getByRole('menu')).not.toHaveClass('pointer-triggered');
      dispatchPointer(document.body, 'pointerdown', { pointerType: 'mouse' });
      fireEvent.click(document.body);
      expect(screen.queryByRole('menu')).toBeNull();
    }
  );

  it.each(['Enter', ' '])(
    'allows %s activation after a long-press release without a click',
    (key) => {
      const onSelectSession = vi.fn();
      const onClose = vi.fn();
      const { sessionName: name } = renderSessionRow({ onSelectSession, onClose });
      const trigger = screen.getByRole('button', { name: 'Session actions' });

      for (const target of [name, trigger]) {
        dispatchPointer(target, 'pointerdown', { pointerType: 'touch' });
        act(() => vi.advanceTimersByTime(500));
        dispatchPointer(target, 'pointerup', { pointerType: 'touch' });
        expect(screen.getByRole('menu')).toBeVisible();

        target.focus();
        fireEvent.keyDown(target, { key });
        fireEvent.keyUp(target, { key });
        fireEvent.click(target, { detail: 0 });
      }

      expect(onSelectSession).toHaveBeenCalledTimes(1);
      expect(onSelectSession).toHaveBeenCalledWith('abc-123');
      expect(screen.queryByRole('menu')).toBeNull();
      expect(onClose).not.toHaveBeenCalled();
    }
  );

  it.each(['touch', 'pen'])(
    'suppresses the held %s release after Escape and permits subsequent interactions',
    (pointerType) => {
      const onSelectSession = vi.fn();
      const onClose = vi.fn();
      const { sessionName: name } = renderSessionRow({ onSelectSession, onClose });
      const trigger = screen.getByRole('button', { name: 'Session actions' });

      for (const target of [name, trigger]) {
        dispatchPointer(target, 'pointerdown', { pointerType });
        act(() => vi.advanceTimersByTime(500));
        expect(screen.getByRole('menu')).toBeVisible();
        fireEvent.keyDown(document, { key: 'Escape' });
        expect(screen.queryByRole('menu')).toBeNull();
        dispatchPointer(target, 'pointerup', { pointerType });
        expect(fireEvent.click(target)).toBe(false);
        expect(screen.queryByRole('menu')).toBeNull();
        expect(onSelectSession).not.toHaveBeenCalled();
        expect(onClose).not.toHaveBeenCalled();
      }

      dispatchPointer(name, 'pointerdown', { pointerType: 'touch' });
      dispatchPointer(name, 'pointerup', { pointerType: 'touch' });
      fireEvent.click(name);
      expect(onSelectSession).toHaveBeenCalledTimes(1);
      expect(onSelectSession).toHaveBeenCalledWith('abc-123');
      fireEvent.click(trigger);
      expect(screen.getByRole('menu')).toBeVisible();
      fireEvent.pointerDown(document.body);
      fireEvent.click(document.body);
      expect(screen.queryByRole('menu')).toBeNull();
      expect(onClose).not.toHaveBeenCalled();
    }
  );

  it('clears suppression on Escape after a release without a click', () => {
    const onSelectSession = vi.fn();
    const onClose = vi.fn();
    const { sessionName: name } = renderSessionRow({ onSelectSession, onClose });
    const trigger = screen.getByRole('button', { name: 'Session actions' });

    for (const target of [name, trigger]) {
      dispatchPointer(target, 'pointerdown', { pointerType: 'touch' });
      act(() => vi.advanceTimersByTime(500));
      dispatchPointer(target, 'pointerup', { pointerType: 'touch' });
      fireEvent.keyDown(document, { key: 'Escape' });
      expect(screen.queryByRole('menu')).toBeNull();
      target.focus();
      fireEvent.click(target, { detail: 0 });
    }

    expect(onSelectSession).toHaveBeenCalledTimes(1);
    expect(onSelectSession).toHaveBeenCalledWith('abc-123');
    expect(screen.getByRole('menu')).not.toHaveClass('pointer-triggered');
    expect(onClose).not.toHaveBeenCalled();
  });

  it('cancels an interrupted touch when Escape is pressed before the timer fires', () => {
    const { sessionName: name } = renderSessionRow();
    dispatchPointer(name, 'pointerdown', { pointerType: 'touch' });
    act(() => vi.advanceTimersByTime(250));
    fireEvent.keyDown(document, { key: 'Escape' });
    act(() => vi.advanceTimersByTime(1000));
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('does not open when the pointer is not touch or pen', () => {
    const { sessionName } = renderSessionRow();

    dispatchPointer(sessionName, 'pointerdown', { pointerType: 'mouse' });
    act(() => vi.advanceTimersByTime(500));

    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('does not open when the touch is shorter than 500 ms', () => {
    const { sessionName } = renderSessionRow();

    dispatchPointer(sessionName, 'pointerdown', { pointerType: 'touch' });
    dispatchPointer(sessionName, 'pointerup');

    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('cancels if the finger moves beyond ~10 px before 500 ms', () => {
    const { sessionName } = renderSessionRow();

    dispatchPointer(sessionName, 'pointerdown', { pointerType: 'touch', clientX: 0, clientY: 0 });
    dispatchPointer(sessionName, 'pointermove', { clientX: 15, clientY: 0 });
    act(() => vi.advanceTimersByTime(1000));

    expect(screen.queryByRole('menu')).toBeNull();
  });

  it.each(['pointerup', 'pointerleave'])('%s cancels the timer permanently', (type) => {
    const { sessionName: name } = renderSessionRow();
    dispatchPointer(name, 'pointerdown', { pointerType: 'pen' });
    act(() => vi.advanceTimersByTime(250));
    if (type === 'pointerleave') fireEvent.pointerOut(name, { relatedTarget: document.body });
    else dispatchPointer(name, type);
    act(() => vi.advanceTimersByTime(1000));
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('clears a pending row timer on unmount', () => {
    const { unmount } = renderDrawer();
    const baselineTimers = vi.getTimerCount();
    dispatchPointer(screen.getByRole('button', { name: 'Test session abc-123' }), 'pointerdown', {
      pointerType: 'touch',
    });
    expect(vi.getTimerCount()).toBeGreaterThan(baselineTimers);
    unmount();
    expect(vi.getTimerCount()).toBeLessThanOrEqual(baselineTimers);
    act(() => vi.advanceTimersByTime(1000));
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('suppresses a release on the row and clears stale suppression on any new pointerdown', () => {
    const onSelectSession = vi.fn();
    const { sessionName: name } = renderSessionRow({ onSelectSession });
    const row = name.closest('.session-drawer-session');
    dispatchPointer(row, 'pointerdown', { pointerType: 'pen' });
    act(() => vi.advanceTimersByTime(500));
    dispatchPointer(row, 'pointerup');
    fireEvent.mouseDown(row); // Compatibility events from release must leave the menu open.
    expect(fireEvent.click(row)).toBe(false);
    expect(screen.getByRole('menu')).toBeVisible();
    dispatchPointer(name, 'pointerdown', { pointerType: 'pen' });
    act(() => vi.advanceTimersByTime(500));
    dispatchPointer(name, 'pointerup');
    dispatchPointer(document.body, 'pointerdown', { pointerType: 'mouse' });
    fireEvent.click(name);
    expect(onSelectSession).toHaveBeenCalledWith('abc-123');
  });

  it('closes on Escape', async () => {
    const { sessionName } = renderSessionRow();

    dispatchPointer(sessionName, 'pointerdown', { pointerType: 'touch' });
    await act(async () => {
      vi.advanceTimersByTime(500);
    });
    expect(screen.queryByRole('menu')).toBeTruthy();

    fireEvent.keyDown(document, { key: 'Escape', bubbles: true });
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('closes when clicking outside the menu', async () => {
    const { sessionName } = renderSessionRow();

    dispatchPointer(sessionName, 'pointerdown', { pointerType: 'touch' });
    await act(async () => {
      vi.advanceTimersByTime(500);
    });
    expect(screen.queryByRole('menu')).toBeTruthy();

    fireEvent.pointerDown(screen.getByRole('button', { name: 'Close menu' }));
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('following click does not select the session after a long press', () => {
    const onSelectSession = vi.fn();
    const { sessionName } = renderSessionRow({ onSelectSession });

    dispatchPointer(sessionName, 'pointerdown', { pointerType: 'touch' });
    act(() => vi.advanceTimersByTime(500));
    dispatchPointer(sessionName, 'pointerup');
    const bubbled = vi.fn();
    document.addEventListener('click', bubbled);
    try {
      expect(fireEvent.click(sessionName)).toBe(false);
      expect(bubbled).not.toHaveBeenCalled();
    } finally {
      document.removeEventListener('click', bubbled);
    }
    expect(screen.getByRole('menu')).toBeVisible();
    expect(onSelectSession).not.toHaveBeenCalled();
  });

  it('normal tap selects the session after a long press was suppressed', () => {
    const onSelectSession = vi.fn();
    const { sessionName } = renderSessionRow({ onSelectSession });

    dispatchPointer(sessionName, 'pointerdown', { pointerType: 'touch' });
    act(() => vi.advanceTimersByTime(500));
    dispatchPointer(sessionName, 'pointerup');
    expect(screen.getByRole('menu')).toBeVisible();

    dispatchPointer(sessionName, 'pointerdown', { pointerType: 'touch' });
    dispatchPointer(sessionName, 'pointerup');
    fireEvent.click(sessionName, { detail: 0 });
    expect(onSelectSession).toHaveBeenCalledTimes(1);
  });

  it('outside click still closes the menu after a long press', async () => {
    const { sessionName } = renderSessionRow();

    dispatchPointer(sessionName, 'pointerdown', { pointerType: 'touch' });
    await act(async () => {
      vi.advanceTimersByTime(500);
    });
    expect(screen.getByRole('menu')).toBeVisible();

    const outside = screen.getByRole('button', { name: 'Close menu' });
    dispatchPointer(outside, 'pointerdown', { pointerType: 'mouse' });
    fireEvent.pointerDown(outside);
    fireEvent.click(outside);
    expect(screen.queryByRole('menu')).toBeNull();
    fireEvent.click(sessionName);
    expect(screen.queryByRole('menu')).toBeNull();
  });
});
