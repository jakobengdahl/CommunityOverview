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

  it('renders a scrim and the full-width mobile class when open on a mobile viewport', () => {
    setMobile(true);
    const { container } = renderDrawer();

    expect(screen.getByTestId('session-drawer-scrim')).toBeInTheDocument();
    expect(container.querySelector('.session-drawer')).toHaveClass('session-drawer--mobile');
  });

  it('does not render a scrim when closed, even on a mobile viewport', () => {
    setMobile(true);
    renderDrawer({ open: false });

    expect(screen.queryByTestId('session-drawer-scrim')).not.toBeInTheDocument();
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
