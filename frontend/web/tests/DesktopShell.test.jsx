import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import DesktopShell from '../src/components/DesktopShell';

vi.mock('../src/components/FloatingHeader', () => ({
  default: ({ sessionId, onToggleDrawer, onClear }) => (
    <div data-testid="floating-header">
      <span>{sessionId}</span>
      <button onClick={onToggleDrawer}>toggle-drawer</button>
      <button onClick={onClear}>clear</button>
    </div>
  ),
}));

vi.mock('../src/components/FloatingSearch', () => ({
  default: () => <div data-testid="floating-search" />,
}));

vi.mock('../src/components/FloatingToolbar', () => ({
  default: ({ onCreateNode }) => (
    <div data-testid="floating-toolbar">
      <button onClick={() => onCreateNode('Actor')}>create-actor</button>
    </div>
  ),
}));

vi.mock('../src/components/SessionDrawer', () => ({
  default: ({ open }) => (open ? <div data-testid="session-drawer" /> : null),
}));

vi.mock('../src/components/ChatPanel', () => ({
  default: () => <div data-testid="chat-panel" />,
}));

function baseProps(overrides = {}) {
  return {
    sessionId: 'session-1',
    roster: [],
    currentClientId: 'me',
    onClear: vi.fn(),
    drawerOpen: false,
    onToggleDrawer: vi.fn(),
    onCloseDrawer: vi.fn(),
    sessions: [],
    currentSessionId: 'session-1',
    onNewSession: vi.fn(),
    onConnectSession: vi.fn(),
    onSelectSession: vi.fn(),
    onRenameSession: vi.fn(),
    onDeleteSession: vi.fn(),
    onCopySessionLink: vi.fn(),
    onCopyTriggerUrl: vi.fn(),
    onOpenSettings: vi.fn(),
    onOpenActivity: vi.fn(),
    canvasLocked: false,
    onToggleLock: vi.fn(),
    suspendEscape: false,
    onCreateNodeForType: vi.fn(),
    onCreateAgent: vi.fn(),
    onCreateSubscription: vi.fn(),
    onSaveView: vi.fn(),
    onCreateGroup: vi.fn(),
    onCreateActiveKnowledgeCollection: vi.fn(),
    llmAvailable: true,
    akcShortName: undefined,
    ...overrides,
  };
}

describe('DesktopShell', () => {
  it('renders the floating overlay chrome unconditionally, matching pre-split App.jsx', () => {
    render(<DesktopShell {...baseProps()} />);

    expect(screen.getByTestId('floating-header')).toBeInTheDocument();
    expect(screen.getByTestId('floating-search')).toBeInTheDocument();
    expect(screen.getByTestId('floating-toolbar')).toBeInTheDocument();
    expect(screen.getByTestId('chat-panel')).toBeInTheDocument();
    expect(screen.getByText('session-1')).toBeInTheDocument();
  });

  it('does not render the chat panel when the LLM is unavailable', () => {
    render(<DesktopShell {...baseProps({ llmAvailable: false })} />);
    expect(screen.queryByTestId('chat-panel')).not.toBeInTheDocument();
  });

  it('renders the session drawer only when drawerOpen is true', () => {
    const { rerender } = render(<DesktopShell {...baseProps({ drawerOpen: false })} />);
    expect(screen.queryByTestId('session-drawer')).not.toBeInTheDocument();

    rerender(<DesktopShell {...baseProps({ drawerOpen: true })} />);
    expect(screen.getByTestId('session-drawer')).toBeInTheDocument();
  });

  it('forwards the hamburger toggle to onToggleDrawer', () => {
    const onToggleDrawer = vi.fn();
    render(<DesktopShell {...baseProps({ onToggleDrawer })} />);

    fireEvent.click(screen.getByText('toggle-drawer'));
    expect(onToggleDrawer).toHaveBeenCalledTimes(1);
  });

  it('forwards a toolbar create-node pick to onCreateNodeForType', () => {
    const onCreateNodeForType = vi.fn();
    render(<DesktopShell {...baseProps({ onCreateNodeForType })} />);

    fireEvent.click(screen.getByText('create-actor'));
    expect(onCreateNodeForType).toHaveBeenCalledWith('Actor');
  });
});
