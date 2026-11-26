// covers: frontend/web/src/components/SessionDrawer.jsx
/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { renderDrawer, renderSessionRow } from './SessionDrawer.menuTestUtils';

describe('SessionDrawer — right-click opens the context menu', () => {
  it('opens the menu with the same items as the kebab button', () => {
    const { sessionName } = renderSessionRow();

    fireEvent.contextMenu(sessionName, { clientX: 100, clientY: 100, button: 2 });

    const items = screen.getAllByRole('menuitem');
    const labels = items.map((el) => el.textContent);
    expect(labels).toContain('Name session');
    expect(labels).toContain('Copy link');
    expect(labels).toContain('Delete session');
  });

  it('closes on Escape', async () => {
    const { sessionName } = renderSessionRow();

    fireEvent.contextMenu(sessionName, { clientX: 100, clientY: 100, button: 2 });
    await waitFor(() => screen.getByRole('menu', { hidden: true }));

    fireEvent.keyDown(document, { key: 'Escape', bubbles: true });
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('closes when clicking outside the menu', async () => {
    const { sessionName } = renderSessionRow();

    fireEvent.contextMenu(sessionName, { clientX: 100, clientY: 100, button: 2 });
    await waitFor(() => screen.getByRole('menu', { hidden: true }));

    fireEvent.pointerDown(screen.getByRole('button', { name: 'Close menu' }));
    expect(screen.queryByRole('menu')).toBeNull();
  });
});

describe('SessionDrawer — right-click opens the context menu (prevent browser)', () => {
  it('prevents the browser menu and opens the session menu at the pointer position', async () => {
    const { sessionName } = renderSessionRow();

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

describe('SessionDrawer — switching menu triggers', () => {
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
