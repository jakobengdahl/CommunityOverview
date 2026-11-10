// covers: frontend/web/src/components/SessionDrawer.jsx, frontend/web/src/components/SessionContextMenu.jsx
/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import SessionContextMenu from './SessionContextMenu';
import { readFileSync } from 'node:fs';
import { renderDrawer } from './SessionDrawer.menuTestUtils';

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
    const menuCss = readFileSync(`${__dirname}/SessionContextMenu.css`, 'utf8');
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
