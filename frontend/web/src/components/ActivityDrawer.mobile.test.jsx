/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { I18nProvider } from '../i18n';
import ActivityDrawer from './ActivityDrawer';

vi.mock('../services/api', () => ({
  getGraphHistory: vi.fn(async () => ({ entries: [] })),
  getSessionActivity: vi.fn(async () => ({ activity: [] })),
  undoSessionAction: vi.fn(async () => ({})),
}));

// Mirrors the fake MediaQueryList pattern in SessionDrawer.mobile.test.jsx /
// useViewportMode.test.jsx so this exercises the real hook.
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
    <I18nProvider>
      <ActivityDrawer
        open
        onClose={vi.fn()}
        sessionId="1234-5678"
        currentClientId="client-me"
        roster={[]}
        {...props}
      />
    </I18nProvider>
  );
}

// Every test awaits this so the drawer's initial session-activity fetch (an
// unavoidable side effect of mounting, mocked to resolve empty) settles
// before assertions run — otherwise its resolution lands after the test body
// returns and React warns about an unwrapped act().
async function settled() {
  await screen.findByText('No session activity yet');
}

describe('ActivityDrawer mobile overlay', () => {
  let originalMatchMedia;

  beforeEach(() => {
    originalMatchMedia = window.matchMedia;
  });

  afterEach(() => {
    window.matchMedia = originalMatchMedia;
    cleanup();
  });

  it('renders no scrim and the desktop docked class on a wide viewport', async () => {
    setMobile(false);
    const { container } = renderDrawer();
    await settled();

    expect(screen.queryByTestId('activity-drawer-scrim')).not.toBeInTheDocument();
    expect(container.querySelector('.activity-drawer')).not.toHaveClass('activity-drawer--mobile');
  });

  it('renders an open scrim and the full-width mobile class when open on a mobile viewport', async () => {
    setMobile(true);
    const { container } = renderDrawer();
    await settled();

    expect(screen.getByTestId('activity-drawer-scrim')).toHaveClass('open');
    expect(container.querySelector('.activity-drawer')).toHaveClass('activity-drawer--mobile');
  });

  it('marks the mobile drawer as a modal dialog only while open', async () => {
    setMobile(true);
    const { rerender } = renderDrawer({ open: true });
    await settled();

    const dialog = screen.getByRole('dialog', { name: 'Recent activity' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');

    rerender(
      <I18nProvider>
        <ActivityDrawer
          open={false}
          onClose={vi.fn()}
          sessionId="1234-5678"
          currentClientId="client-me"
          roster={[]}
        />
      </I18nProvider>
    );
    expect(screen.getByRole('dialog', { hidden: true })).toHaveAttribute('aria-modal', 'false');
  });

  it('does not expose a dialog role on desktop', async () => {
    setMobile(false);
    renderDrawer();
    await settled();

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('moves focus into the drawer and traps Tab inside it on a mobile viewport', async () => {
    setMobile(true);
    const { container } = renderDrawer();
    await settled();

    // The header's Close button precedes Refresh in DOM/tab order and is the
    // drawer's guaranteed-enabled header control (Refresh is disabled while
    // its tab is loading) — assert against the actual enabled buttons rather
    // than a specific one, so this does not depend on that loading timing.
    const enabledButtons = () => Array.from(container.querySelectorAll('button:not([disabled])'));
    const focusable = enabledButtons();
    expect(focusable[0]).toHaveFocus();

    // Shift+Tab from the first focusable element wraps to the last.
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true });
    expect(enabledButtons()[enabledButtons().length - 1]).toHaveFocus();
  });

  it('restores focus to the previously-focused element on close in mobile mode', async () => {
    setMobile(true);
    const trigger = document.createElement('button');
    document.body.appendChild(trigger);
    trigger.focus();

    const { rerender } = renderDrawer({ open: true });
    await settled();
    expect(trigger).not.toHaveFocus();

    rerender(
      <I18nProvider>
        <ActivityDrawer
          open={false}
          onClose={vi.fn()}
          sessionId="1234-5678"
          currentClientId="client-me"
          roster={[]}
        />
      </I18nProvider>
    );
    expect(trigger).toHaveFocus();
    trigger.remove();
  });

  it('locks body scroll while open on a mobile viewport and restores it on close', async () => {
    setMobile(true);
    const previousOverflow = document.body.style.overflow;

    const { rerender } = renderDrawer({ open: true });
    await settled();
    expect(document.body.style.overflow).toBe('hidden');

    rerender(
      <I18nProvider>
        <ActivityDrawer
          open={false}
          onClose={vi.fn()}
          sessionId="1234-5678"
          currentClientId="client-me"
          roster={[]}
        />
      </I18nProvider>
    );
    expect(document.body.style.overflow).toBe(previousOverflow);
  });

  it('does not lock body scroll on desktop', async () => {
    setMobile(false);
    const previousOverflow = document.body.style.overflow;

    renderDrawer({ open: true });
    await settled();
    expect(document.body.style.overflow).toBe(previousOverflow);
  });

  it('closes on scrim tap', async () => {
    setMobile(true);
    const onClose = vi.fn();
    renderDrawer({ onClose });
    await settled();

    fireEvent.click(screen.getByTestId('activity-drawer-scrim'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('still closes on Escape in mobile mode', async () => {
    setMobile(true);
    const onClose = vi.fn();
    renderDrawer({ onClose });
    await settled();

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('blurs the focused close button before calling onClose, so aria-hidden never commits while a descendant is still focused', async () => {
    setMobile(true);
    let activeElementAtCloseTime;
    const onClose = vi.fn(() => {
      activeElementAtCloseTime = document.activeElement;
    });
    renderDrawer({ onClose });
    await settled();

    const closeButton = screen.getByRole('button', { name: 'Close' });
    closeButton.focus();
    expect(closeButton).toHaveFocus();

    fireEvent.click(closeButton);
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(activeElementAtCloseTime).not.toBe(closeButton);
  });
});
