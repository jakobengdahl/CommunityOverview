// covers: frontend/web/src/components/SessionDrawer.jsx
/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react';
import SessionDrawer from './SessionDrawer';
import SessionContextMenu from './SessionContextMenu';
import { readFileSync } from 'node:fs';

if (typeof window.PointerEvent === 'undefined') {
  window.PointerEvent = class PointerEvent extends window.Event {
    constructor(type, init = {}) {
      super(type, init);
      this.pointerId = init.pointerId || 0;
      this.pointerType = init.pointerType || '';
      this.isPrimary = init.isPrimary || false;
      this.clientX = init.clientX || 0;
      this.clientY = init.clientY || 0;
      this.screenX = init.screenX || 0;
      this.screenY = init.screenY || 0;
    }
  };
}

// jsdom has no layout; positioning tests use explicit geometry.
const _nativeGetBoundingClientRect = HTMLElement.prototype.getBoundingClientRect;

beforeEach(() => {
  setMobile(false);
  vi.stubGlobal('innerWidth', 1024);
  vi.stubGlobal('innerHeight', 768);
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function () {
    if (this.getAttribute('role') === 'menu') {
      return { x: 0, y: 0, left: 0, top: 0, right: 200, bottom: 100, width: 200, height: 100 };
    }
    return _nativeGetBoundingClientRect.call(this);
  });
  vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(200);
  vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(100);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  document.body.innerHTML = '';
});

function makeMql(matches) {
  return {
    matches,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  };
}

function setMobile(isMobile) {
  window.matchMedia = vi.fn((query) => makeMql(query === '(max-width: 768px)' ? isMobile : false));
}

function dispatchPointer(el, type, opts = {}) {
  act(() =>
    el.dispatchEvent(
      new window.PointerEvent(type, {
        bubbles: true,
        cancelable: true,
        pointerType: opts.pointerType || '',
        clientX: opts.clientX ?? 0,
        clientY: opts.clientY ?? 0,
      })
    )
  );
}

function renderDrawer(props = {}) {
  const container = document.createElement('div');
  document.body.appendChild(container);
  return render(
    <SessionDrawer
      open
      onClose={vi.fn()}
      sessions={[{ id: 'abc-123', name: 'Test session' }]}
      currentSessionId="abc-123"
      onNewSession={vi.fn()}
      onConnectSession={vi.fn()}
      onSelectSession={vi.fn()}
      onRenameSession={vi.fn()}
      onDeleteSession={vi.fn()}
      onCopySessionLink={vi.fn()}
      onCopyTriggerUrl={vi.fn()}
      onOpenSettings={vi.fn()}
      {...props}
    />,
    { container }
  );
}

describe('SessionDrawer — right-click opens the context menu', () => {
  it('anchors the kebab menu after closing a pointer menu and opening session abc-123', () => {
    renderDrawer({
      sessions: [
        { id: 'abc-123', name: 'Test session' },
        { id: 'def-456', name: 'Other session' },
      ],
    });
    const sessionName = screen.getByRole('button', { name: 'Test session abc-123' });
    const triggers = screen.getAllByRole('button', { name: 'Session actions' });

    fireEvent.contextMenu(sessionName, { clientX: 100, clientY: 200, button: 2 });
    expect(screen.getByRole('menu')).toHaveClass('pointer-triggered');

    fireEvent.click(triggers[0]);
    expect(screen.queryByRole('menu')).toBeNull();
    expect(triggers[0]).toHaveAttribute('aria-expanded', 'false');

    const trigger = triggers[0];
    vi.spyOn(trigger, 'getBoundingClientRect').mockReturnValue({
      x: 40,
      y: 60,
      width: 20,
      height: 35,
    });
    fireEvent.click(trigger);
    const menu = screen.getByRole('menu');
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(menu).not.toHaveClass('pointer-triggered');
    expect(menu.style.left).toBe('40px');
    expect(menu.style.top).toBe('95px');
  });

  it('flips to left-alignment when the pointer is near the right edge', async () => {
    renderDrawer();
    const sessionName = screen.getByRole('button', { name: 'Test session abc-123' });

    Object.defineProperty(window, 'innerWidth', {
      configurable: true,
      value: 500,
      writable: true,
    });

    const bcSpy = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect');
    bcSpy.mockReturnValue({
      width: 200,
      x: 450,
      y: 100,
      top: 100,
      left: 450,
      right: 650,
      bottom: 200,
      height: 100,
      toJSON: () => ({}),
    });

    try {
      fireEvent.contextMenu(sessionName, {
        clientX: 450,
        clientY: 100,
        button: 2,
      });

      const menuEl = await waitFor(() => screen.getByRole('menu', { hidden: true }));

      expect(menuEl.style.left).toBe('250px');
      expect(menuEl.style.top).toBe('100px');
      expect(menuEl.style.right).toBe('');
    } finally {
      bcSpy.mockRestore();
    }
  });

  it('opens the menu with the same items as the kebab button', () => {
    renderDrawer();
    const sessionName = screen.getByRole('button', { name: 'Test session abc-123' });

    fireEvent.contextMenu(sessionName, { clientX: 100, clientY: 100, button: 2 });

    const items = screen.getAllByRole('menuitem');
    const labels = items.map((el) => el.textContent);
    expect(labels).toContain('Name session');
    expect(labels).toContain('Copy link');
    expect(labels).toContain('Delete session');
  });

  it('closes on Escape', async () => {
    renderDrawer();
    const sessionName = screen.getByRole('button', { name: 'Test session abc-123' });

    fireEvent.contextMenu(sessionName, { clientX: 100, clientY: 100, button: 2 });
    await waitFor(() => screen.getByRole('menu', { hidden: true }));

    fireEvent.keyDown(document, { key: 'Escape', bubbles: true });
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('closes when clicking outside the menu', async () => {
    renderDrawer();
    const sessionName = screen.getByRole('button', { name: 'Test session abc-123' });

    fireEvent.contextMenu(sessionName, { clientX: 100, clientY: 100, button: 2 });
    await waitFor(() => screen.getByRole('menu', { hidden: true }));

    fireEvent.pointerDown(screen.getByRole('button', { name: 'Close menu' }));
    expect(screen.queryByRole('menu')).toBeNull();
  });
});

describe('SessionDrawer — right-click opens the context menu (def-456)', () => {
  it('anchors the kebab menu after closing a pointer menu and opening session def-456', async () => {
    renderDrawer({
      sessions: [
        { id: 'abc-123', name: 'Test session' },
        { id: 'def-456', name: 'Other session' },
      ],
    });
    const sessionName = screen.getByRole('button', { name: 'Test session abc-123' });
    const triggers = screen.getAllByRole('button', { name: 'Session actions' });

    fireEvent.contextMenu(sessionName, { clientX: 100, clientY: 200, button: 2 });
    expect(screen.getByRole('menu')).toHaveClass('pointer-triggered');

    fireEvent.click(triggers[0]);
    expect(screen.queryByRole('menu')).toBeNull();
    expect(triggers[0]).toHaveAttribute('aria-expanded', 'false');

    const trigger = triggers[1];
    fireEvent.click(trigger);
    await waitFor(() => {
      const menu = screen.getByRole('menu');
      expect(trigger).toHaveAttribute('aria-expanded', 'true');
      expect(menu).not.toHaveClass('pointer-triggered');
      expect(menu.style.left).toBeTruthy();
      expect(menu.style.top).toBeTruthy();
    });
  });
});

describe('SessionDrawer — right-click opens the context menu (prevent browser)', () => {
  it('prevents the browser menu and opens the session menu at the pointer position', async () => {
    renderDrawer();
    const sessionName = screen.getByRole('button', { name: 'Test session abc-123' });

    expect(
      fireEvent.contextMenu(sessionName, {
        clientX: 100,
        clientY: 200,
        button: 2,
      })
    ).toBe(false);

    const dropdown = await waitFor(() => screen.getByRole('menu', { hidden: true }));
    expect(dropdown).toHaveClass('pointer-triggered');
    expect(dropdown.style.left).toBe('100px');
    expect(dropdown.style.top).toBe('200px');
  });
});

describe('SessionDrawer — long press on touch', () => {
  beforeEach(() => {
    vi.useFakeTimers({ advanceTimers: true });
    setMobile(true);
  });

  it('opens the menu after ~500 ms of continuous touch', async () => {
    renderDrawer();
    const sessionName = screen.getByRole('button', { name: 'Test session abc-123' });

    dispatchPointer(sessionName, 'pointerdown', { pointerType: 'touch' });
    await act(async () => {
      vi.advanceTimersByTime(500);
    });

    expect(screen.queryByRole('menu')).toBeTruthy();
  });

  it('keeps the menu and mobile drawer open after the long-press release click', () => {
    const onClose = vi.fn();
    const onSelectSession = vi.fn(() => onClose());
    renderDrawer({ onClose, onSelectSession });
    const sessionName = screen.getByRole('button', { name: 'Test session abc-123' });

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

  it.each(['touch', 'pen'])('keeps a %s long-press menu open on kebab release and permits later activation', (pointerType) => {
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
  });

  it('cancels a pending long press when the browser cancels the touch', () => {
    renderDrawer();
    const sessionName = screen.getByRole('button', { name: 'Test session abc-123' });

    dispatchPointer(sessionName, 'pointerdown', { pointerType: 'touch' });
    act(() => vi.advanceTimersByTime(250));
    dispatchPointer(sessionName, 'pointercancel');
    act(() => vi.advanceTimersByTime(1000));

    expect(screen.queryByRole('menu')).toBeNull();
  });

  it.each(['blur', 'visibilitychange'])('cancels an interrupted press on %s and permits the next tap', (event) => {
    const onSelectSession = vi.fn();
    const onClose = vi.fn();
    renderDrawer({ onSelectSession, onClose });
    const name = screen.getByRole('button', { name: 'Test session abc-123' });

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
  });

  it.each(['blur', 'visibilitychange'])('clears release-click suppression on %s', (event) => {
    const onSelectSession = vi.fn();
    renderDrawer({ onSelectSession });
    const name = screen.getByRole('button', { name: 'Test session abc-123' });
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
    renderDrawer({ onSelectSession });
    const name = screen.getByRole('button', { name: 'Test session abc-123' });
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
    renderDrawer({ onSelectSession, onClose });
    const name = screen.getByRole('button', { name: 'Test session abc-123' });
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

  it.each(['Enter', ' '])('allows %s activation after a long-press release without a click', (key) => {
    const onSelectSession = vi.fn();
    const onClose = vi.fn();
    renderDrawer({ onSelectSession, onClose });
    const name = screen.getByRole('button', { name: 'Test session abc-123' });
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
  });

  it('clears suppression on Escape after a release without a click', () => {
    const onSelectSession = vi.fn();
    const onClose = vi.fn();
    renderDrawer({ onSelectSession, onClose });
    const name = screen.getByRole('button', { name: 'Test session abc-123' });
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
    renderDrawer();
    const name = screen.getByRole('button', { name: 'Test session abc-123' });
    dispatchPointer(name, 'pointerdown', { pointerType: 'touch' });
    act(() => vi.advanceTimersByTime(250));
    fireEvent.keyDown(document, { key: 'Escape' });
    act(() => vi.advanceTimersByTime(1000));
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('does not open when the pointer is not touch or pen', () => {
    renderDrawer();
    const sessionName = screen.getByRole('button', { name: 'Test session abc-123' });

    dispatchPointer(sessionName, 'pointerdown', { pointerType: 'mouse' });
    act(() => vi.advanceTimersByTime(500));

    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('does not open when the touch is shorter than 500 ms', () => {
    renderDrawer();
    const sessionName = screen.getByRole('button', { name: 'Test session abc-123' });

    dispatchPointer(sessionName, 'pointerdown', { pointerType: 'touch' });
    dispatchPointer(sessionName, 'pointerup');

    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('cancels if the finger moves beyond ~10 px before 500 ms', () => {
    renderDrawer();
    const sessionName = screen.getByRole('button', { name: 'Test session abc-123' });

    dispatchPointer(sessionName, 'pointerdown', { pointerType: 'touch', clientX: 0, clientY: 0 });
    dispatchPointer(sessionName, 'pointermove', { clientX: 15, clientY: 0 });
    act(() => vi.advanceTimersByTime(1000));

    expect(screen.queryByRole('menu')).toBeNull();
  });

  it.each(['pointerup', 'pointerleave'])('%s cancels the timer permanently', (type) => {
    renderDrawer();
    const name = screen.getByRole('button', { name: 'Test session abc-123' });
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
    renderDrawer({ onSelectSession });
    const name = screen.getByRole('button', { name: 'Test session abc-123' });
    const row = name.closest('.session-drawer-session');
    dispatchPointer(row, 'pointerdown', { pointerType: 'pen' });
    act(() => vi.advanceTimersByTime(500));
    dispatchPointer(row, 'pointerup');
    fireEvent.mouseDown(row); // Compatibility events from release must leave the menu open.
    expect(fireEvent.click(row)).toBe(false);
    expect(screen.getByRole('menu')).toBeVisible();
    dispatchPointer(name, 'pointerdown', { pointerType: 'pen' });
    act(() => vi.advanceTimersByTime(500));
    dispatchPointer(document.body, 'pointerdown', { pointerType: 'mouse' });
    fireEvent.click(name);
    expect(onSelectSession).toHaveBeenCalledWith('abc-123');
  });

  it('closes on Escape', async () => {
    renderDrawer();
    const sessionName = screen.getByRole('button', { name: 'Test session abc-123' });

    dispatchPointer(sessionName, 'pointerdown', { pointerType: 'touch' });
    await act(async () => {
      vi.advanceTimersByTime(500);
    });
    expect(screen.queryByRole('menu')).toBeTruthy();

    fireEvent.keyDown(document, { key: 'Escape', bubbles: true });
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('closes when clicking outside the menu', async () => {
    renderDrawer();
    const sessionName = screen.getByRole('button', { name: 'Test session abc-123' });

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
    renderDrawer({ onSelectSession });
    const sessionName = screen.getByRole('button', { name: 'Test session abc-123' });

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
    renderDrawer({ onSelectSession });
    const sessionName = screen.getByRole('button', { name: 'Test session abc-123' });

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
    renderDrawer();
    const sessionName = screen.getByRole('button', { name: 'Test session abc-123' });

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

describe('SessionDrawer — context menu outside rows', () => {
  it('does not suppress the browser menu when right-clicking non-session elements', () => {
    renderDrawer();

    const outside = document.createElement('div');
    outside.id = 'outside-target';
    document.body.appendChild(outside);

    const caught = [];
    const handler = (e) => {
      caught.push(e.defaultPrevented);
    };
    document.addEventListener('contextmenu', handler, true);

    fireEvent.contextMenu(outside, {
      clientX: 10,
      clientY: 10,
      button: 2,
    });

    expect(caught).toEqual([false]);

    document.removeEventListener('contextmenu', handler, true);
    outside.remove();
  });
});

describe('SessionContextMenu — positioning edge cases', () => {
  function makeItems(overrides = {}) {
    return [
      { key: 'rename', label: 'Rename', onClick: vi.fn(), ...(overrides.rename || {}) },
      { key: 'copy-link', label: 'Copy link', onClick: vi.fn(), ...(overrides.copyLink || {}) },
      {
        key: 'delete',
        label: 'Delete',
        danger: true,
        onClick: vi.fn(),
        ...(overrides.delete || {}),
      },
    ];
  }

  it('allows the menu minimum width to shrink below 170px on narrow viewports', () => {
    // jsdom does not perform CSS layout; check the sizing rule directly.
    const menuCss = readFileSync(new URL('./SessionContextMenu.css', import.meta.url), 'utf8');
    const dropdown = menuCss.match(/\.session-context-menu-dropdown\s*\{([^}]+)\}/)[1];
    expect(dropdown).toMatch(/min-width:\s*min\(170px, calc\(100vw - 16px\)\)/);
    expect(dropdown).toMatch(/max-width:\s*calc\(100vw - 16px\)/);
  });

  it.each(['resize', 'orientationchange'])(
    'repositions an open menu on %s and uses a fresh anchor',
    (event) => {
      renderDrawer();
      const name = screen.getByRole('button', { name: 'Test session abc-123' });
      fireEvent.contextMenu(name, { clientX: 900, clientY: 650 });
      const menu = screen.getByRole('menu');
      expect(menu.style.left).toBe('700px');
      expect(menu.style.top).toBe('650px');

      vi.stubGlobal('innerWidth', 320);
      vi.stubGlobal('innerHeight', 240);
      fireEvent(window, new Event(event));
      expect(menu.style.left).toBe('112px');
      expect(menu.style.top).toBe('132px');

      fireEvent.contextMenu(name, { clientX: 30, clientY: 40 });
      expect(menu.style.left).toBe('30px');
      expect(menu.style.top).toBe('40px');
      vi.stubGlobal('innerWidth', 1024);
      vi.stubGlobal('innerHeight', 768);
      fireEvent(window, new Event(event));
      expect(menu.style.left).toBe('30px');
      expect(menu.style.top).toBe('40px');
    }
  );

  it('menu near the right edge stays inside the viewport', async () => {
    const props = {
      open: true,
      onToggle: vi.fn(),
      onClose: vi.fn(),
      triggerLabel: 'Session actions',
      items: makeItems(),
    };
    const { rerender } = render(<SessionContextMenu {...props} />);
    Object.defineProperty(window, 'innerWidth', {
      configurable: true,
      value: 1024,
      writable: true,
    });
    const vw = window.innerWidth;
    const menu = screen.getByRole('menu');
    const rectSpy = vi
      .spyOn(menu, 'getBoundingClientRect')
      .mockReturnValue({ width: 200, height: 100, x: 0, y: 0 });
    try {
      rerender(<SessionContextMenu {...props} pointerPosition={{ x: vw - 10, y: 80 }} />);
      await act(async () => {
        for (let i = 0; i < 5; i++) await Promise.resolve();
      });
      const freshMenu = screen.getByRole('menu');
      expect(freshMenu.style.left).toBeTruthy();
      expect(freshMenu.style.left).toBe(`${vw - 210}px`);
      expect(freshMenu.style.top).toBe('80px');
      expect(parseFloat(freshMenu.style.left) + 200).toBeLessThanOrEqual(vw - 8);
    } finally {
      rectSpy.mockRestore();
    }
  });

  it('menu near the bottom edge stays inside the viewport', async () => {
    const props = {
      open: true,
      onToggle: vi.fn(),
      onClose: vi.fn(),
      triggerLabel: 'Session actions',
      items: makeItems(),
    };
    const { rerender } = render(<SessionContextMenu {...props} />);
    const vh = window.innerHeight;
    const menu = screen.getByRole('menu');
    const rectSpy = vi
      .spyOn(menu, 'getBoundingClientRect')
      .mockReturnValue({ width: 200, height: 100, x: 0, y: 0 });
    try {
      rerender(<SessionContextMenu {...props} pointerPosition={{ x: 100, y: vh - 10 }} />);
      await waitFor(() => {
        expect(screen.getByRole('menu').style.top).toBeTruthy();
      });
      const freshMenu = screen.getByRole('menu');
      expect(freshMenu.style.top).toBe(`${vh - 110}px`);
      expect(parseFloat(freshMenu.style.top)).toBeLessThanOrEqual(vh - 108); // viewport - height - 8px margin
    } finally {
      rectSpy.mockRestore();
    }
  });

  it('kebab after a right-click uses the button anchor', async () => {
    const props = {
      open: true,
      onToggle: vi.fn(),
      onClose: vi.fn(),
      triggerLabel: 'Session actions',
      items: makeItems(),
    };
    const { rerender } = render(
      <SessionContextMenu {...props} pointerPosition={{ x: 100, y: 100 }} />
    );
    const menu = screen.getByRole('menu');
    const rectSpy = vi
      .spyOn(menu, 'getBoundingClientRect')
      .mockReturnValue({ width: 200, height: 100, x: 0, y: 0 });
    try {
      rerender(
        <SessionContextMenu
          {...props}
          pointerPosition={{ x: 100, y: 100 }}
          kebabAnchor={{ x: 300, y: 50, width: 20, height: 35 }}
        />
      );
      await waitFor(() => {
        const m = screen.getByRole('menu');
        expect(m.style.left).toBeTruthy();
      });
      const freshMenu = await screen.findByRole('menu');
      expect(freshMenu.style.left).toBe('300px');
      expect(freshMenu.style.top).toBe('85px');
      expect(freshMenu).not.toHaveClass('pointer-triggered');
    } finally {
      rectSpy.mockRestore();
    }
  });
});
