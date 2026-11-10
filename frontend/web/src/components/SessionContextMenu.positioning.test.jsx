// covers: frontend/web/src/components/SessionContextMenu.jsx
/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi } from 'vitest';
import { screen, fireEvent, waitFor, act } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { renderPositionedMenu } from './SessionContextMenu.testUtils';
import { renderDrawer } from './SessionDrawer.menuTestUtils';

describe('SessionContextMenu — drawer anchors and flipping', () => {
  it('flips to left-alignment when the pointer is near the right edge', async () => {
    renderDrawer();
    const sessionName = screen.getByRole('button', { name: 'Test session abc-123' });

    vi.stubGlobal('innerWidth', 500);
    fireEvent.contextMenu(sessionName, { clientX: 450, clientY: 100, button: 2 });

    const menuEl = await waitFor(() => screen.getByRole('menu', { hidden: true }));

    expect(menuEl.style.left).toBe('250px');
    expect(menuEl.style.top).toBe('100px');
    expect(menuEl.style.right).toBe('');
  });
  it('allows the menu minimum width to shrink below 170px on narrow viewports', () => {
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
    const updateMenu = renderPositionedMenu();
    const vw = window.innerWidth;
    updateMenu({ pointerPosition: { x: vw - 10, y: 80 } });
    await act(async () => {
      for (let i = 0; i < 5; i++) await Promise.resolve();
    });
    const freshMenu = screen.getByRole('menu');
    expect(freshMenu.style.left).toBeTruthy();
    expect(freshMenu.style.left).toBe(`${vw - 210}px`);
    expect(freshMenu.style.top).toBe('80px');
    expect(parseFloat(freshMenu.style.left) + 200).toBeLessThanOrEqual(vw - 8);
  });

  it('menu near the bottom edge stays inside the viewport', async () => {
    const updateMenu = renderPositionedMenu();
    const vh = window.innerHeight;
    updateMenu({ pointerPosition: { x: 100, y: vh - 10 } });
    await waitFor(() => {
      expect(screen.getByRole('menu').style.top).toBeTruthy();
    });
    const freshMenu = screen.getByRole('menu');
    expect(freshMenu.style.top).toBe(`${vh - 110}px`);
    expect(parseFloat(freshMenu.style.top)).toBeLessThanOrEqual(vh - 108); // viewport - height - 8px margin
  });

  it('kebab after a right-click uses the button anchor', async () => {
    const updateMenu = renderPositionedMenu({ pointerPosition: { x: 100, y: 100 } });
    updateMenu({
      pointerPosition: { x: 100, y: 100 },
      kebabAnchor: { x: 300, y: 50, width: 20, height: 35 },
    });
    await waitFor(() => {
      const m = screen.getByRole('menu');
      expect(m.style.left).toBeTruthy();
    });
    const freshMenu = await screen.findByRole('menu');
    expect(freshMenu.style.left).toBe('300px');
    expect(freshMenu.style.top).toBe('85px');
    expect(freshMenu).not.toHaveClass('pointer-triggered');
  });
});
