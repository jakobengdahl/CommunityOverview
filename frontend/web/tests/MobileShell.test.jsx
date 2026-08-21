import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import MobileShell from '../src/components/MobileShell';
import useGraphStore from '../src/store/graphStore';

vi.mock('../src/i18n', () => ({
  useI18n: () => ({
    t: (key, _params, fallback) => fallback ?? key,
  }),
}));

vi.mock('../src/components/FloatingSearch', () => ({
  default: ({ variant }) => <div data-testid="floating-search" data-variant={variant} />,
}));

vi.mock('../src/components/FloatingToolbar', () => ({
  default: ({ variant, onCreateNode }) => (
    <div data-testid="floating-toolbar" data-variant={variant}>
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
    onClear: vi.fn(),
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

describe('MobileShell', () => {
  beforeEach(() => {
    useGraphStore.setState({ chatPanelOpen: false });
  });

  it('renders the five-slot bottom nav and no surface open by default', () => {
    render(<MobileShell {...baseProps()} />);

    expect(screen.getByLabelText('mobile_nav.graph')).toBeInTheDocument();
    expect(screen.getByLabelText('mobile_nav.search')).toBeInTheDocument();
    expect(screen.getByLabelText('mobile_nav.create')).toBeInTheDocument();
    expect(screen.getByLabelText('mobile_nav.chat')).toBeInTheDocument();
    expect(screen.getByLabelText('mobile_nav.menu')).toBeInTheDocument();

    expect(screen.queryByTestId('floating-search')).not.toBeInTheDocument();
    expect(screen.queryByTestId('session-drawer')).not.toBeInTheDocument();
  });

  it('opens the search sheet and closes it again on toggle, without opening chat', () => {
    render(<MobileShell {...baseProps()} />);

    fireEvent.click(screen.getByLabelText('mobile_nav.search'));
    expect(screen.getByTestId('floating-search')).toHaveAttribute('data-variant', 'sheet');

    fireEvent.click(screen.getByLabelText('mobile_nav.search'));
    expect(screen.queryByTestId('floating-search')).not.toBeInTheDocument();
  });

  it('switching from search to create shows exactly one sheet surface at a time', () => {
    render(<MobileShell {...baseProps()} />);

    fireEvent.click(screen.getByLabelText('mobile_nav.search'));
    expect(screen.getByTestId('floating-search')).toBeInTheDocument();
    expect(screen.queryByTestId('floating-toolbar')).not.toBeInTheDocument();

    fireEvent.click(screen.getByLabelText('mobile_nav.create'));
    expect(screen.queryByTestId('floating-search')).not.toBeInTheDocument();
    expect(screen.getByTestId('floating-toolbar')).toBeInTheDocument();
  });

  it('opening chat minimizes any open sheet, and opening the sheet minimizes chat', () => {
    const onCreateNodeForType = vi.fn();
    render(<MobileShell {...baseProps({ onCreateNodeForType })} />);

    fireEvent.click(screen.getByLabelText('mobile_nav.create'));
    expect(screen.getByTestId('floating-toolbar')).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText('mobile_nav.chat'));
    expect(useGraphStore.getState().chatPanelOpen).toBe(true);
    // Opening chat must close the bottom sheet — the canvas is never covered
    // by more than one surface at a time.
    expect(screen.queryByTestId('floating-toolbar')).not.toBeInTheDocument();

    fireEvent.click(screen.getByLabelText('mobile_nav.search'));
    expect(useGraphStore.getState().chatPanelOpen).toBe(false);
    expect(screen.getByTestId('floating-search')).toBeInTheDocument();
  });

  it('opening the menu drawer minimizes chat', () => {
    useGraphStore.setState({ chatPanelOpen: true });
    render(<MobileShell {...baseProps()} />);

    fireEvent.click(screen.getByLabelText('mobile_nav.menu'));
    expect(screen.getByTestId('session-drawer')).toBeInTheDocument();
    expect(useGraphStore.getState().chatPanelOpen).toBe(false);
  });

  it('the Graph nav item closes every surface and returns to the canvas', () => {
    render(<MobileShell {...baseProps()} />);

    fireEvent.click(screen.getByLabelText('mobile_nav.menu'));
    expect(screen.getByTestId('session-drawer')).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText('mobile_nav.graph'));
    expect(screen.queryByTestId('session-drawer')).not.toBeInTheDocument();
    expect(useGraphStore.getState().chatPanelOpen).toBe(false);
  });

  it('picking a node type from the create sheet closes the sheet and forwards the type', () => {
    const onCreateNodeForType = vi.fn();
    render(<MobileShell {...baseProps({ onCreateNodeForType })} />);

    fireEvent.click(screen.getByLabelText('mobile_nav.create'));
    fireEvent.click(screen.getByText('create-actor'));

    expect(onCreateNodeForType).toHaveBeenCalledWith('Actor');
    expect(screen.queryByTestId('floating-toolbar')).not.toBeInTheDocument();
  });
});
