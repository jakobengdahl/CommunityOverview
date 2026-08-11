/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import SessionDrawer from './SessionDrawer';

// The lock toggle is the navigation-menu entry point for the clear-board guard
// (task: confirm before clearing a named or locked visualization).
function renderDrawer(props = {}) {
  return render(
    <SessionDrawer
      open
      onClose={vi.fn()}
      sessions={[]}
      currentSessionId="1234-5678"
      onNewSession={vi.fn()}
      onConnectSession={vi.fn()}
      onSelectSession={vi.fn()}
      onRenameSession={vi.fn()}
      onDeleteSession={vi.fn()}
      onCopySessionLink={vi.fn()}
      onOpenSettings={vi.fn()}
      {...props}
    />
  );
}

describe('SessionDrawer lock toggle', () => {
  it('shows "Lock visualization" and is not pressed when unlocked', () => {
    renderDrawer({ canvasLocked: false, onToggleLock: vi.fn() });
    const btn = screen.getByRole('button', { name: 'Lock visualization' });
    expect(btn.getAttribute('aria-pressed')).toBe('false');
  });

  it('shows "Unlock visualization" and is pressed when locked', () => {
    renderDrawer({ canvasLocked: true, onToggleLock: vi.fn() });
    const btn = screen.getByRole('button', { name: 'Unlock visualization' });
    expect(btn.getAttribute('aria-pressed')).toBe('true');
  });

  it('invokes onToggleLock when clicked', () => {
    const onToggleLock = vi.fn();
    renderDrawer({ canvasLocked: false, onToggleLock });
    fireEvent.click(screen.getByRole('button', { name: 'Lock visualization' }));
    expect(onToggleLock).toHaveBeenCalledTimes(1);
  });
});
