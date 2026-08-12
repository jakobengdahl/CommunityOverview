import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import SessionDrawer from '../src/components/SessionDrawer';
import { I18nProvider } from '../src/i18n';

const SESSIONS = [
  { id: '1111-2222', name: 'Energy analysis', updatedAt: 2000 },
  { id: '3333-4444', name: null, updatedAt: 1000 },
];

function renderDrawer(overrides = {}) {
  const props = {
    open: true,
    onClose: vi.fn(),
    sessions: SESSIONS,
    currentSessionId: '1111-2222',
    onNewSession: vi.fn(),
    onConnectSession: vi.fn(),
    onSelectSession: vi.fn(),
    onRenameSession: vi.fn(),
    onDeleteSession: vi.fn(),
    onCopySessionLink: vi.fn(),
    onOpenSettings: vi.fn(),
    ...overrides,
  };
  render(
    <I18nProvider>
      <SessionDrawer {...props} />
    </I18nProvider>
  );
  return props;
}

describe('SessionDrawer', () => {
  it('shows session names when set, otherwise the session id', () => {
    renderDrawer();
    expect(screen.getByText('Energy analysis')).toBeInTheDocument();
    expect(screen.getByText('3333-4444')).toBeInTheDocument();
  });

  it('invokes the menu action callbacks', () => {
    const props = renderDrawer();

    fireEvent.click(screen.getByText('Start new session'));
    expect(props.onNewSession).toHaveBeenCalled();

    fireEvent.click(screen.getByText('Connect to session (via ID)'));
    expect(props.onConnectSession).toHaveBeenCalled();

    fireEvent.click(screen.getByText('Settings'));
    expect(props.onOpenSettings).toHaveBeenCalled();

    fireEvent.click(screen.getByText('Energy analysis'));
    expect(props.onSelectSession).toHaveBeenCalledWith('1111-2222');
  });

  it('exposes per-session actions behind the context menu', () => {
    const props = renderDrawer();
    // Open the first session's menu, then pick Delete.
    fireEvent.click(screen.getAllByLabelText('Session actions')[0]);
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete session' }));
    expect(props.onDeleteSession).toHaveBeenCalledWith('1111-2222');
  });

  it('copies a session link from the context menu', () => {
    const props = renderDrawer();
    fireEvent.click(screen.getAllByLabelText('Session actions')[0]);
    fireEvent.click(screen.getByRole('menuitem', { name: 'Copy link' }));
    expect(props.onCopySessionLink).toHaveBeenCalledWith('1111-2222');
  });

  it('renames a session from the context menu', () => {
    const props = renderDrawer();
    fireEvent.click(screen.getAllByLabelText('Session actions')[0]);
    fireEvent.click(screen.getByRole('menuitem', { name: 'Name session' }));
    expect(props.onRenameSession).toHaveBeenCalledWith('1111-2222');
  });

  it('Escape peels off an open menu before it closes the drawer', () => {
    const props = renderDrawer();
    fireEvent.click(screen.getAllByLabelText('Session actions')[0]);
    expect(screen.getByRole('menu')).toBeInTheDocument();

    // First Escape closes the menu but leaves the drawer open.
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(props.onClose).not.toHaveBeenCalled();

    // Second Escape closes the drawer.
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(props.onClose).toHaveBeenCalled();
  });

  it('filters sessions from the search field', () => {
    renderDrawer();

    fireEvent.click(screen.getByText('Search previous sessions'));
    fireEvent.change(screen.getByPlaceholderText('Search by name or ID...'), {
      target: { value: 'energy' },
    });

    expect(screen.getByText('Energy analysis')).toBeInTheDocument();
    expect(screen.queryByText('3333-4444')).not.toBeInTheDocument();
  });

  it('closes on Escape when open', () => {
    const props = renderDrawer();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(props.onClose).toHaveBeenCalled();
  });
});
