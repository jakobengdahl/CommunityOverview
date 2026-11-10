import { vi } from 'vitest';
import { render } from '@testing-library/react';
import SessionContextMenu from './SessionContextMenu';

export function makeItems(overrides = {}) {
  return [
    { key: 'rename', label: 'Rename', onClick: vi.fn(), ...(overrides.rename || {}) },
    { key: 'copy-link', label: 'Copy link', onClick: vi.fn(), ...(overrides.copyLink || {}) },
    { key: 'delete', label: 'Delete', danger: true, onClick: vi.fn(), ...(overrides.delete || {}) },
  ];
}

export function makeMenuProps() {
  return {
    open: true,
    onToggle: vi.fn(),
    onClose: vi.fn(),
    triggerLabel: 'Session actions',
    items: makeItems(),
  };
}

export function renderMenu(props = {}) {
  return render(<SessionContextMenu {...makeMenuProps()} {...props} />);
}

export function renderPositionedMenu(initial = {}) {
  const props = makeMenuProps();
  const { rerender } = render(<SessionContextMenu {...props} {...initial} />);
  return (next) => rerender(<SessionContextMenu {...props} {...next} />);
}

export function mockMenuRect(menu, height = 100) {
  return vi.spyOn(menu, 'getBoundingClientRect').mockReturnValue({ width: 200, height });
}
