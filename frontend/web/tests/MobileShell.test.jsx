import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';

import MobileShell from '../src/components/MobileShell';
import useGraphStore, { AI_ASSISTANT_COLLAPSED_STORAGE_KEY } from '../src/store/graphStore';

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
  default: ({ variant }) => <div data-testid="chat-panel" data-variant={variant} />,
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
    onAnnotationSheetContainerChange: vi.fn(),
    ...overrides,
  };
}

describe('MobileShell', () => {
  beforeEach(() => {
    localStorage.clear();
    useGraphStore.setState({ chatPanelOpen: false });
  });

  it('renders the six-slot bottom nav and no surface open by default', () => {
    render(<MobileShell {...baseProps()} />);

    expect(screen.getByLabelText('mobile_nav.graph')).toBeInTheDocument();
    expect(screen.getByLabelText('mobile_nav.search')).toBeInTheDocument();
    expect(screen.getByLabelText('mobile_nav.create')).toBeInTheDocument();
    expect(screen.getByLabelText('mobile_nav.annotate')).toBeInTheDocument();
    expect(screen.getByLabelText('mobile_nav.chat')).toBeInTheDocument();
    expect(screen.getByLabelText('mobile_nav.menu')).toBeInTheDocument();

    expect(screen.queryByTestId('floating-search')).not.toBeInTheDocument();
    expect(screen.queryByTestId('session-drawer')).not.toBeInTheDocument();
    expect(screen.queryByTestId('mobile-annotate-sheet-container')).not.toBeInTheDocument();
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

  it('opening the menu drawer minimizes chat without overwriting the persisted chat preference', () => {
    localStorage.setItem(AI_ASSISTANT_COLLAPSED_STORAGE_KEY, 'false');
    useGraphStore.setState({ chatPanelOpen: true });
    render(<MobileShell {...baseProps()} />);

    fireEvent.click(screen.getByLabelText('mobile_nav.menu'));
    expect(screen.getByTestId('session-drawer')).toBeInTheDocument();
    expect(useGraphStore.getState().chatPanelOpen).toBe(false);
    expect(localStorage.getItem(AI_ASSISTANT_COLLAPSED_STORAGE_KEY)).toBe('false');
  });

  it("opening chat via ChatPanel's own control (not the bottom nav) still closes the open menu", () => {
    // ChatPanel's own minimized bubble calls the store's toggleChatPanel
    // directly - it is mocked out above, so simulate that exact call site
    // rather than the mobile_nav.chat button, which goes through openChat().
    render(<MobileShell {...baseProps()} />);

    fireEvent.click(screen.getByLabelText('mobile_nav.menu'));
    expect(screen.getByTestId('session-drawer')).toBeInTheDocument();

    act(() => {
      useGraphStore.getState().toggleChatPanel();
    });
    expect(useGraphStore.getState().chatPanelOpen).toBe(true);
    expect(screen.queryByTestId('session-drawer')).not.toBeInTheDocument();
  });

  it('the Graph nav item closes every surface and returns to the canvas without persisting chat collapse', () => {
    localStorage.setItem(AI_ASSISTANT_COLLAPSED_STORAGE_KEY, 'false');
    render(<MobileShell {...baseProps()} />);

    fireEvent.click(screen.getByLabelText('mobile_nav.menu'));
    expect(screen.getByTestId('session-drawer')).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText('mobile_nav.graph'));
    expect(screen.queryByTestId('session-drawer')).not.toBeInTheDocument();
    expect(useGraphStore.getState().chatPanelOpen).toBe(false);
    expect(localStorage.getItem(AI_ASSISTANT_COLLAPSED_STORAGE_KEY)).toBe('false');
  });

  it('picking a node type from the create sheet closes the sheet and forwards the type', () => {
    const onCreateNodeForType = vi.fn();
    render(<MobileShell {...baseProps({ onCreateNodeForType })} />);

    fireEvent.click(screen.getByLabelText('mobile_nav.create'));
    fireEvent.click(screen.getByText('create-actor'));

    expect(onCreateNodeForType).toHaveBeenCalledWith('Actor');
    expect(screen.queryByTestId('floating-toolbar')).not.toBeInTheDocument();
  });

  describe('Annotate bottom sheet', () => {
    // AnnotationToolbox itself lives in ui-graph-canvas (GraphCanvas mounts
    // it, not MobileShell - see the component doc comment above), so unlike
    // the create sheet's "forwards the type" test, this describe block
    // covers MobileShell's actual responsibility: opening a titled sheet with
    // an empty container, handing that container's DOM node up to the host
    // via onAnnotationSheetContainerChange, releasing it again on close, and
    // taking part in the same mutual-exclusion set as every other surface.
    // GraphCanvasAnnotationToolboxMobile.test.jsx (ui-graph-canvas package)
    // covers the other half: that GraphCanvas actually portals
    // AnnotationToolbox into a container like this one and creates real
    // annotations from it.
    it('opens a titled sheet with an empty portal container, distinct from Create', () => {
      const onAnnotationSheetContainerChange = vi.fn();
      render(<MobileShell {...baseProps({ onAnnotationSheetContainerChange })} />);

      fireEvent.click(screen.getByLabelText('mobile_nav.annotate'));

      expect(
        screen.getByRole('dialog', { name: 'mobile_nav.annotate_panel_title' })
      ).toBeInTheDocument();
      const container = screen.getByTestId('mobile-annotate-sheet-container');
      expect(container).toBeEmptyDOMElement();
      expect(screen.queryByTestId('floating-toolbar')).not.toBeInTheDocument();

      // The most recent call is the container actually in the document now;
      // React may call a fresh ref with null before the mounted node.
      expect(onAnnotationSheetContainerChange.mock.calls.at(-1)[0]).toBe(container);
    });

    it('releases the container (calls back with null) when the sheet closes', () => {
      const onAnnotationSheetContainerChange = vi.fn();
      render(<MobileShell {...baseProps({ onAnnotationSheetContainerChange })} />);

      fireEvent.click(screen.getByLabelText('mobile_nav.annotate'));
      onAnnotationSheetContainerChange.mockClear();

      fireEvent.click(screen.getByLabelText('mobile_nav.annotate'));

      expect(screen.queryByTestId('mobile-annotate-sheet-container')).not.toBeInTheDocument();
      expect(onAnnotationSheetContainerChange).toHaveBeenCalledWith(null);
    });

    it('is mutually exclusive with Create in both directions, releasing the container each time', () => {
      const onAnnotationSheetContainerChange = vi.fn();
      render(<MobileShell {...baseProps({ onAnnotationSheetContainerChange })} />);

      fireEvent.click(screen.getByLabelText('mobile_nav.create'));
      expect(screen.getByTestId('floating-toolbar')).toBeInTheDocument();

      fireEvent.click(screen.getByLabelText('mobile_nav.annotate'));
      expect(screen.queryByTestId('floating-toolbar')).not.toBeInTheDocument();
      expect(screen.getByTestId('mobile-annotate-sheet-container')).toBeInTheDocument();

      onAnnotationSheetContainerChange.mockClear();
      fireEvent.click(screen.getByLabelText('mobile_nav.create'));
      expect(screen.queryByTestId('mobile-annotate-sheet-container')).not.toBeInTheDocument();
      expect(screen.getByTestId('floating-toolbar')).toBeInTheDocument();
      expect(onAnnotationSheetContainerChange).toHaveBeenCalledWith(null);
    });

    it('closes chat when opened, and is itself closed by opening chat', () => {
      render(<MobileShell {...baseProps()} />);

      fireEvent.click(screen.getByLabelText('mobile_nav.chat'));
      expect(useGraphStore.getState().chatPanelOpen).toBe(true);

      fireEvent.click(screen.getByLabelText('mobile_nav.annotate'));
      expect(useGraphStore.getState().chatPanelOpen).toBe(false);
      expect(screen.getByTestId('mobile-annotate-sheet-container')).toBeInTheDocument();

      fireEvent.click(screen.getByLabelText('mobile_nav.chat'));
      expect(useGraphStore.getState().chatPanelOpen).toBe(true);
      expect(screen.queryByTestId('mobile-annotate-sheet-container')).not.toBeInTheDocument();
    });

    it('the Graph nav item closes it like every other surface', () => {
      const onAnnotationSheetContainerChange = vi.fn();
      render(<MobileShell {...baseProps({ onAnnotationSheetContainerChange })} />);

      fireEvent.click(screen.getByLabelText('mobile_nav.annotate'));
      expect(screen.getByTestId('mobile-annotate-sheet-container')).toBeInTheDocument();

      fireEvent.click(screen.getByLabelText('mobile_nav.graph'));
      expect(screen.queryByTestId('mobile-annotate-sheet-container')).not.toBeInTheDocument();
      expect(onAnnotationSheetContainerChange).toHaveBeenCalledWith(null);
    });
  });

  describe('Chat bottom sheet', () => {
    it('is not mounted when chat is closed, and hosts ChatPanel in sheet variant when opened', () => {
      render(<MobileShell {...baseProps()} />);

      expect(screen.queryByTestId('chat-panel')).not.toBeInTheDocument();
      expect(screen.queryByTestId('bottom-sheet-scrim')).not.toBeInTheDocument();

      fireEvent.click(screen.getByLabelText('mobile_nav.chat'));

      expect(screen.getByTestId('chat-panel')).toHaveAttribute('data-variant', 'sheet');
      expect(
        screen.getByRole('dialog', { name: 'mobile_nav.chat_panel_title' })
      ).toBeInTheDocument();
    });

    it('closing the chat sheet (its own close control) persists the collapse like the desktop minimize does', () => {
      render(<MobileShell {...baseProps()} />);

      fireEvent.click(screen.getByLabelText('mobile_nav.chat'));
      expect(useGraphStore.getState().chatPanelOpen).toBe(true);

      fireEvent.click(screen.getByTestId('bottom-sheet-scrim'));

      expect(useGraphStore.getState().chatPanelOpen).toBe(false);
      expect(screen.queryByTestId('chat-panel')).not.toBeInTheDocument();
      expect(localStorage.getItem(AI_ASSISTANT_COLLAPSED_STORAGE_KEY)).toBe('true');
    });

    it('does not mount the chat sheet when llmAvailable is false', () => {
      render(<MobileShell {...baseProps({ llmAvailable: false })} />);

      fireEvent.click(screen.getByLabelText('mobile_nav.chat'));

      expect(useGraphStore.getState().chatPanelOpen).toBe(true);
      expect(screen.queryByTestId('chat-panel')).not.toBeInTheDocument();
    });
  });
});
