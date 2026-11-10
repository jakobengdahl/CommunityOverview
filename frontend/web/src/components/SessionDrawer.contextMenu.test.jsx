/**
 * covers: frontend/web/src/components/SessionDrawer.jsx
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import SessionDrawer from './SessionDrawer';

// --- Helpers ---

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

function renderDrawer(props = {}) {
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
    />
  );
}

// --- Right-click on session row ---

describe('SessionDrawer — right-click opens the context menu', () => {
  it('prevents the browser menu and opens the session menu at the pointer position', async () => {
    renderDrawer();
    const sessionName = screen.getByRole('button', { name: 'Test session abc-123' });

    fireEvent.contextMenu(sessionName, {
      clientX: 100,
      clientY: 200,
      button: 2,
    });

    // The dropdown should appear with fixed positioning
    const dropdown = await waitFor(() => screen.getByRole('menu', { hidden: true }));
    expect(dropdown).toHaveClass('pointer-triggered');
    expect(dropdown.style.left).toBe('100px');
    expect(dropdown.style.top).toBe('200px');
  });

  it('flips to right-alignment when the pointer is near the right edge', async () => {
    renderDrawer();
    const sessionName = screen.getByRole('button', { name: 'Test session abc-123' });

    // Set a small viewport width so that the 450px coordinate is near the edge
    Object.defineProperty(window, 'innerWidth', {
      configurable: true,
      value: 500,
      writable: true,
    });

    // Spy on getBoundingClientRect so the menu reports as 200px wide.
    // The positioning effect reads rect.width, rect.x and rect.right.
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

    // Right-click near the right edge.
    fireEvent.contextMenu(sessionName, {
      clientX: 450,
      clientY: 100,
      button: 2,
    });

    const menuEl = await waitFor(() => screen.getByRole('menu', { hidden: true }));

    expect(menuEl.style.right).toBeTruthy();
    expect(menuEl.style.left).toBe('');

    bcSpy.mockRestore();
    delete window.innerWidth;
  });

  it('opens the menu with the same items as the kebab button', () => {
    renderDrawer();
    const sessionName = screen.getByRole('button', { name: 'Test session abc-123' });

    fireEvent.contextMenu(sessionName, { clientX: 100, clientY: 100, button: 2 });

    // Rename and Copy link are always present.
    // Copy trigger URL appears because this session is currentSessionId
    // and onCopyTriggerUrl is provided.
    // Delete is always present.
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

    // The context menu uses mousedown (not click) to detect outside clicks.
    fireEvent.mouseDown(screen.getByRole('button', { name: 'Close menu' }));
    expect(screen.queryByRole('menu')).toBeNull();
  });
});

// --- Long press on touch devices ---

describe('SessionDrawer — long press on touch', () => {
  beforeEach(() => {
    vi.useFakeTimers({ advanceTimers: true });
    setMobile(true);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('opens the menu after ~500 ms of continuous touch', async () => {
    renderDrawer();
    const sessionName = screen.getByRole('button', { name: 'Test session abc-123' });

    fireEvent.touchStart(sessionName);
    await act(async () => {
      vi.advanceTimersByTime(500);
    });

    expect(screen.queryByRole('menu')).toBeTruthy();
  });

  it('does not open when the touch is shorter than 500 ms', () => {
    renderDrawer();
    const sessionName = screen.getByRole('button', { name: 'Test session abc-123' });

    fireEvent.touchStart(sessionName);
    fireEvent.touchEnd(sessionName);
    // No timer advancement — the 500 ms timer never fires

    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('cancels if the finger moves before 500 ms', () => {
    renderDrawer();
    const sessionName = screen.getByRole('button', { name: 'Test session abc-123' });

    fireEvent.touchStart(sessionName);
    fireEvent.touchMove(sessionName);
    // Timer was cleared by touchMove

    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('closes on Escape', async () => {
    renderDrawer();
    const sessionName = screen.getByRole('button', { name: 'Test session abc-123' });

    fireEvent.touchStart(sessionName);
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

    fireEvent.touchStart(sessionName);
    await act(async () => {
      vi.advanceTimersByTime(500);
    });
    expect(screen.queryByRole('menu')).toBeTruthy();

    // The context menu uses mousedown to detect outside clicks.
    fireEvent.mouseDown(screen.getByRole('button', { name: 'Close menu' }));
    expect(screen.queryByRole('menu')).toBeNull();
  });
});

// --- Right-click outside session rows (browser menu preserved) ---

describe('SessionDrawer — context menu outside rows', () => {
  it('does not suppress the browser menu when right-clicking non-session elements', () => {
    renderDrawer();

    // Right-click outside the drawer entirely — no handler will preventDefault.
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

    // No handler called preventDefault — the native browser menu is preserved.
    expect(caught).toEqual([false]);

    document.removeEventListener('contextmenu', handler, true);
    outside.remove();
  });
});
