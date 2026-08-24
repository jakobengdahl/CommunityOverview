/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import SessionDrawer from './SessionDrawer';

// Mirrors the fake MediaQueryList pattern in useViewportMode.test.jsx and
// FloatingToolbar.touch.test.jsx so this exercises the real hook.
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

describe('SessionDrawer mobile overlay', () => {
  let originalMatchMedia;

  beforeEach(() => {
    originalMatchMedia = window.matchMedia;
  });

  afterEach(() => {
    window.matchMedia = originalMatchMedia;
  });

  it('renders no scrim and the desktop docked class on a wide viewport', () => {
    setMobile(false);
    const { container } = renderDrawer();

    expect(screen.queryByTestId('session-drawer-scrim')).not.toBeInTheDocument();
    expect(container.querySelector('.session-drawer')).not.toHaveClass('session-drawer--mobile');
  });

  it('renders an open scrim and the full-width mobile class when open on a mobile viewport', () => {
    setMobile(true);
    const { container } = renderDrawer();

    expect(screen.getByTestId('session-drawer-scrim')).toHaveClass('open');
    expect(container.querySelector('.session-drawer')).toHaveClass('session-drawer--mobile');
  });

  it('keeps the scrim mounted but not open when closed on a mobile viewport', () => {
    // Mounted-but-faded (rather than unmounted) so it fades out over the same
    // transition as the drawer's own slide-out instead of vanishing abruptly.
    setMobile(true);
    renderDrawer({ open: false });

    expect(screen.getByTestId('session-drawer-scrim')).not.toHaveClass('open');
  });

  it('marks the mobile drawer as a modal dialog only while open', () => {
    setMobile(true);
    const { rerender } = renderDrawer({ open: true, onClose: vi.fn() });

    const dialog = screen.getByRole('dialog', { name: 'Sessions' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');

    rerender(
      <SessionDrawer
        open={false}
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
      />
    );
    expect(screen.getByRole('dialog', { hidden: true })).toHaveAttribute('aria-modal', 'false');
  });

  it('does not expose a dialog role on desktop', () => {
    setMobile(false);
    renderDrawer();

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('moves focus into the drawer and traps Tab inside it on a mobile viewport', () => {
    setMobile(true);
    renderDrawer();

    const closeButton = screen.getByRole('button', { name: 'Close menu' });
    expect(closeButton).toHaveFocus();

    // Shift+Tab from the first focusable element wraps to the last.
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true });
    const buttons = screen.getAllByRole('button');
    expect(buttons[buttons.length - 1]).toHaveFocus();
  });

  it('restores focus to the previously-focused element on close in mobile mode', () => {
    setMobile(true);
    const trigger = document.createElement('button');
    document.body.appendChild(trigger);
    trigger.focus();

    const { rerender } = renderDrawer({ open: true });
    expect(trigger).not.toHaveFocus();

    rerender(
      <SessionDrawer
        open={false}
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
      />
    );
    expect(trigger).toHaveFocus();
    trigger.remove();
  });

  it('closes on scrim tap', () => {
    setMobile(true);
    const onClose = vi.fn();
    renderDrawer({ onClose });

    fireEvent.click(screen.getByTestId('session-drawer-scrim'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('still closes on Escape in mobile mode', () => {
    setMobile(true);
    const onClose = vi.fn();
    renderDrawer({ onClose });

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
