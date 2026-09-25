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
  let originalUserAgent;

  beforeEach(() => {
    originalMatchMedia = window.matchMedia;
    originalUserAgent = window.navigator.userAgent;
    window.localStorage.clear();
  });

  afterEach(() => {
    window.matchMedia = originalMatchMedia;
    Object.defineProperty(window.navigator, 'userAgent', {
      configurable: true,
      value: originalUserAgent,
    });
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

  it('shows a one-time iOS Add to Home Screen hint in the mobile menu', () => {
    setMobile(true);
    Object.defineProperty(window.navigator, 'userAgent', {
      configurable: true,
      value: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)',
    });

    renderDrawer();

    expect(screen.getByText('Use Share, then Add to Home Screen.')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss install hint' }));
    expect(screen.queryByText('Use Share, then Add to Home Screen.')).not.toBeInTheDocument();

    renderDrawer();
    expect(screen.queryByText('Use Share, then Add to Home Screen.')).not.toBeInTheDocument();
    expect(window.localStorage.getItem('app_install_ios_hint_dismissed')).toBe('true');
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

  it('wraps Tab from the last focusable element back to the first on a mobile viewport', () => {
    setMobile(true);
    renderDrawer();
    const buttons = screen.getAllByRole('button');
    buttons[buttons.length - 1].focus();

    fireEvent.keyDown(document, { key: 'Tab' });
    expect(screen.getByRole('button', { name: 'Close menu' })).toHaveFocus();
  });

  it('pulls focus back into the drawer on Tab when it has escaped on a mobile viewport', () => {
    setMobile(true);
    renderDrawer();
    const outside = document.createElement('button');
    document.body.appendChild(outside);
    outside.focus();

    fireEvent.keyDown(document, { key: 'Tab' });
    expect(screen.getByRole('button', { name: 'Close menu' })).toHaveFocus();
    outside.remove();
  });

  it('neither moves focus in nor traps Tab on desktop', () => {
    setMobile(false);
    const trigger = document.createElement('button');
    document.body.appendChild(trigger);
    trigger.focus();

    renderDrawer();
    expect(trigger).toHaveFocus();
    const notPrevented = fireEvent.keyDown(document, { key: 'Tab' });
    expect(notPrevented).toBe(true);
    expect(trigger).toHaveFocus();
    trigger.remove();
  });

  it('stops trapping Tab while a stacked dialog suspends Escape', () => {
    setMobile(true);
    const onClose = vi.fn();
    renderDrawer({ suspendEscape: true, onClose });
    const buttons = screen.getAllByRole('button');
    buttons[buttons.length - 1].focus();

    const notPrevented = fireEvent.keyDown(document, { key: 'Tab' });
    expect(notPrevented).toBe(true);
    expect(buttons[buttons.length - 1]).toHaveFocus();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).not.toHaveBeenCalled();
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

  it('locks body scroll while open on a mobile viewport and restores it on close', () => {
    setMobile(true);
    const previousOverflow = document.body.style.overflow;

    const { rerender } = renderDrawer({ open: true });
    expect(document.body.style.overflow).toBe('hidden');

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
    expect(document.body.style.overflow).toBe(previousOverflow);
  });

  it('does not lock body scroll on desktop', () => {
    setMobile(false);
    const previousOverflow = document.body.style.overflow;

    renderDrawer({ open: true });
    expect(document.body.style.overflow).toBe(previousOverflow);
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

  it('blurs the focused close button before calling onClose, so aria-hidden never commits while a descendant is still focused', () => {
    setMobile(true);
    let activeElementAtCloseTime;
    const onClose = vi.fn(() => {
      activeElementAtCloseTime = document.activeElement;
    });
    renderDrawer({ onClose });

    const closeButton = screen.getByRole('button', { name: 'Close menu' });
    expect(closeButton).toHaveFocus();

    fireEvent.click(closeButton);
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(activeElementAtCloseTime).not.toBe(closeButton);
  });

  it('blurs focus before calling onClose on Escape too', () => {
    setMobile(true);
    let activeElementAtCloseTime;
    const onClose = vi.fn(() => {
      activeElementAtCloseTime = document.activeElement;
    });
    renderDrawer({ onClose });

    const closeButton = screen.getByRole('button', { name: 'Close menu' });
    expect(closeButton).toHaveFocus();

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(activeElementAtCloseTime).not.toBe(closeButton);
  });

  it('blurs the focused Activity button before calling onOpenActivity, since MobileShell closes the surface around that callback rather than through onClose', () => {
    setMobile(true);
    let activeElementWhenActivityFires;
    const onOpenActivity = vi.fn(() => {
      activeElementWhenActivityFires = document.activeElement;
    });
    renderDrawer({ onOpenActivity });

    const activityButton = screen.getByRole('button', { name: 'Recent activity' });
    activityButton.focus();
    expect(activityButton).toHaveFocus();

    fireEvent.click(activityButton);
    expect(onOpenActivity).toHaveBeenCalledTimes(1);
    expect(activeElementWhenActivityFires).not.toBe(activityButton);
  });

  it('does not blur on close in desktop mode (no focus trap to race against)', () => {
    setMobile(false);
    const onClose = vi.fn();
    renderDrawer({ onClose });

    const closeButton = screen.getByRole('button', { name: 'Close menu' });
    closeButton.focus();
    fireEvent.click(closeButton);

    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
