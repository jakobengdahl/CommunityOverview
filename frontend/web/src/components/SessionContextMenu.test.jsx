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
