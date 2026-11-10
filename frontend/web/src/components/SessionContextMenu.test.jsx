/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import SessionContextMenu from './SessionContextMenu';

function makeItems(overrides = {}) {
  return [
    { key: 'rename', label: 'Rename', onClick: vi.fn(), ...(overrides.rename || {}) },
    { key: 'copy-link', label: 'Copy link', onClick: vi.fn(), ...(overrides.copyLink || {}) },
    { key: 'delete', label: 'Delete', danger: true, onClick: vi.fn(), ...(overrides.delete || {}) },
  ];
}

describe('SessionContextMenu', () => {
  it.each([100, 200, 800])('keeps a %ipx-tall pointer menu within the viewport', (height) => {
    const props = {
      open: true,
      onToggle: vi.fn(),
      onClose: vi.fn(),
      triggerLabel: 'Session actions',
      items: makeItems(),
    };
    const { rerender } = render(<SessionContextMenu {...props} />);
    const menu = screen.getByRole('menu');
    const rectSpy = vi.spyOn(menu, 'getBoundingClientRect').mockReturnValue({ width: 200, height });
    try {
      for (const y of [window.innerHeight - 10, 80]) {
        rerender(<SessionContextMenu {...props} pointerPosition={{ x: 100, y }} />);
        expect(menu.style.top).toBe(`${Math.max(0, Math.min(y, window.innerHeight - height))}px`);
      }
    } finally {
      rectSpy.mockRestore();
    }
  });

  it.each([
    ['left to right', 100, -50],
    ['right to left', -50, 100],
  ])('clears the previous constraint when moving %s across the right edge', (_, firstOffset, nextOffset) => {
    const props = {
      open: true,
      onToggle: vi.fn(),
      onClose: vi.fn(),
      triggerLabel: 'Session actions',
      items: makeItems(),
    };
    const { rerender } = render(<SessionContextMenu {...props} />);
    const menu = screen.getByRole('menu');
    const rectSpy = vi.spyOn(menu, 'getBoundingClientRect').mockReturnValue({ width: 200, height: 100 });
    try {
      for (const offset of [firstOffset, nextOffset]) {
        const x = offset < 0 ? window.innerWidth + offset : offset;
        rerender(<SessionContextMenu {...props} pointerPosition={{ x, y: 80 }} />);
        expect(screen.getByRole('menu')).toBe(menu);
        expect(menu.style.top).toBe('80px');
        expect(menu.style.left).toBe(offset < 0 ? '' : `${x}px`);
        expect(menu.style.right).toBe(offset < 0 ? `${window.innerWidth - x}px` : '');
      }
    } finally {
      rectSpy.mockRestore();
    }
  });

  it('hides the item list until opened', () => {
    render(
      <SessionContextMenu
        open={false}
        onToggle={vi.fn()}
        onClose={vi.fn()}
        triggerLabel="Session actions"
        items={makeItems()}
      />
    );
    expect(screen.getByLabelText('Session actions')).toBeTruthy();
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('renders every action descriptor when open — the extension seam', () => {
    render(
      <SessionContextMenu
        open
        onToggle={vi.fn()}
        onClose={vi.fn()}
        triggerLabel="Session actions"
        items={makeItems()}
      />
    );
    const items = screen.getAllByRole('menuitem');
    expect(items.map((el) => el.textContent)).toEqual(['Rename', 'Copy link', 'Delete']);
  });

  it('toggling is delegated to the parent (single-open control)', () => {
    const onToggle = vi.fn();
    render(
      <SessionContextMenu
        open={false}
        onToggle={onToggle}
        onClose={vi.fn()}
        triggerLabel="Session actions"
        items={makeItems()}
      />
    );
    fireEvent.click(screen.getByLabelText('Session actions'));
    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it('choosing an item closes the menu and runs its action', () => {
    const onClose = vi.fn();
    const copyLink = vi.fn();
    render(
      <SessionContextMenu
        open
        onToggle={vi.fn()}
        onClose={onClose}
        triggerLabel="Session actions"
        items={makeItems({ copyLink: { onClick: copyLink } })}
      />
    );
    fireEvent.click(screen.getByText('Copy link'));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(copyLink).toHaveBeenCalledTimes(1);
  });

  it('closes when the pointer goes down outside the menu', () => {
    const onClose = vi.fn();
    render(
      <div>
        <SessionContextMenu
          open
          onToggle={vi.fn()}
          onClose={onClose}
          triggerLabel="Session actions"
          items={makeItems()}
        />
        <button>elsewhere</button>
      </div>
    );
    fireEvent.mouseDown(screen.getByText('elsewhere'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('a pointer down on the trigger does not count as outside', () => {
    const onClose = vi.fn();
    render(
      <SessionContextMenu
        open
        onToggle={vi.fn()}
        onClose={onClose}
        triggerLabel="Session actions"
        items={makeItems()}
      />
    );
    fireEvent.mouseDown(screen.getByLabelText('Session actions'));
    expect(onClose).not.toHaveBeenCalled();
  });
});
