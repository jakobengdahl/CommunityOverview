/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import SessionContextMenu from './SessionContextMenu';
import { makeItems, makeMenuProps, renderMenu, mockMenuRect } from './SessionContextMenu.testUtils';

beforeEach(() => {
  vi.stubGlobal('innerWidth', 1024);
  vi.stubGlobal('innerHeight', 768);
  vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(200);
  vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(100);
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('SessionContextMenu', () => {
  it.each([100, 200, 800])('keeps a %ipx-tall pointer menu within the viewport', (height) => {
    const props = makeMenuProps();
    const { rerender } = render(<SessionContextMenu {...props} />);
    const menu = screen.getByRole('menu');
    const rectSpy = mockMenuRect(menu, height);
    try {
      for (const y of [window.innerHeight - 10, 80]) {
        rerender(<SessionContextMenu {...props} pointerPosition={{ x: 100, y }} />);
        expect(menu.style.top).toBe(
          `${Math.max(8, Math.min(y + height > window.innerHeight - 8 ? y - height : y, window.innerHeight - height - 8))}px`
        );
      }
    } finally {
      rectSpy.mockRestore();
    }
  });

  it.each([
    ['left to right', 100, -50],
    ['right to left', -50, 100],
  ])('always sets left when moving %s across the right edge', (_, firstOffset, nextOffset) => {
    const props = makeMenuProps();
    const { rerender } = render(<SessionContextMenu {...props} />);
    const menu = screen.getByRole('menu');
    const rectSpy = mockMenuRect(menu);
    try {
      for (const offset of [firstOffset, nextOffset]) {
        const x = offset < 0 ? window.innerWidth + offset : offset;
        rerender(<SessionContextMenu {...props} pointerPosition={{ x, y: 80 }} />);
        expect(screen.getByRole('menu')).toBe(menu);
        expect(menu.style.left).toBeTruthy();
        expect(menu.style.top).toBe('80px');
        expect(menu.style.right).toBe('');
      }
    } finally {
      rectSpy.mockRestore();
    }
  });

  it('hides the item list until opened', () => {
    renderMenu({ open: false });
    expect(screen.getByLabelText('Session actions')).toBeTruthy();
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('renders every action descriptor when open — the extension seam', () => {
    renderMenu();
    const items = screen.getAllByRole('menuitem');
    expect(items.map((el) => el.textContent)).toEqual(['Rename', 'Copy link', 'Delete']);
  });

  it('toggling is delegated to the parent (single-open control)', () => {
    const onToggle = vi.fn();
    renderMenu({ open: false, onToggle });
    fireEvent.click(screen.getByLabelText('Session actions'));
    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it('choosing an item closes the menu and runs its action', () => {
    const onClose = vi.fn();
    const copyLink = vi.fn();
    renderMenu({ onClose, items: makeItems({ copyLink: { onClick: copyLink } }) });
    fireEvent.click(screen.getByText('Copy link'));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(copyLink).toHaveBeenCalledTimes(1);
  });

  it('closes when the pointer goes down outside the menu', () => {
    const onClose = vi.fn();
    render(
      <div>
        <SessionContextMenu {...makeMenuProps()} onClose={onClose} />
        <button>elsewhere</button>
      </div>
    );
    fireEvent.pointerDown(screen.getByText('elsewhere'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('a pointer down on the trigger does not count as outside', () => {
    const onClose = vi.fn();
    renderMenu({ onClose });
    fireEvent.pointerDown(screen.getByLabelText('Session actions'));
    expect(onClose).not.toHaveBeenCalled();
  });
});
