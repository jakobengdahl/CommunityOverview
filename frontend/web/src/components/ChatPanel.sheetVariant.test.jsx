/**
 * @vitest-environment jsdom
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import ChatPanel from './ChatPanel';
import BottomSheet from './BottomSheet';
import useGraphStore from '../store/graphStore';
import * as api from '../services/api';

vi.mock('../i18n', () => ({
  useI18n: () => ({ t: (key) => key, language: 'en' }),
}));

vi.mock('@community-graph/ui-graph-canvas', () => ({
  positionNewNodes: (nodes) => nodes,
}));

vi.mock('./ExpertAgentSelector', () => ({ default: () => <div data-testid="expert-selector" /> }));
vi.mock('./CollectionForm', () => ({ default: () => <div data-testid="collection-form" /> }));

vi.mock('../services/api', () => ({
  sendChatMessage: vi.fn(),
  uploadFile: vi.fn(),
  addNodes: vi.fn(),
}));

const baseState = {
  chatMessages: [],
  addChatMessage: vi.fn(),
  updateChatMessage: vi.fn(),
  nodes: [],
  edges: [],
  addNodesToVisualization: vi.fn(),
  updateVisualization: vi.fn(),
  clearVisualization: vi.fn(),
  chatPanelOpen: true,
  toggleChatPanel: vi.fn(),
  selectedGraphNodes: [],
  federationDepth: 1,
  stats: { federation: { max_selectable_depth: 1 } },
  clearSelectedGraphNodes: vi.fn(),
  activeExperts: [],
  availableExperts: [],
  showMinimap: false,
  presentation: {},
  startGuide: vi.fn(),
  guideChatInput: null,
  clearGuideChatInput: vi.fn(),
  getNodeColor: vi.fn(() => '#ccc'),
  requestCloseMenus: vi.fn(),
  modelProfiles: [],
  modelProfileSelectionEnabled: true,
  selectedModelProfileId: null,
  setSelectedModelProfileId: vi.fn(),
};

function setStore(overrides = {}) {
  useGraphStore.setState({ ...baseState, ...overrides }, true);
}

// A minimal fake VisualViewport, same shape as the one used by
// useVisualViewportInset.test.jsx.
function makeVisualViewport({ height, offsetTop = 0 }) {
  const listeners = { resize: new Set(), scroll: new Set() };
  return {
    height,
    offsetTop,
    addEventListener: vi.fn((event, handler) => listeners[event]?.add(handler)),
    removeEventListener: vi.fn((event, handler) => listeners[event]?.delete(handler)),
    emit(event, { height: newHeight } = {}) {
      if (newHeight !== undefined) this.height = newHeight;
      listeners[event]?.forEach((handler) => handler());
    },
  };
}

describe('ChatPanel sheet variant', () => {
  let originalVisualViewport;
  let originalInnerHeight;

  beforeEach(() => {
    vi.clearAllMocks();
    api.sendChatMessage.mockResolvedValue({ content: 'ok', toolUsed: null, toolResult: null });
    setStore();
    originalVisualViewport = window.visualViewport;
    originalInnerHeight = window.innerHeight;
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 800 });
  });

  afterEach(() => {
    Object.defineProperty(window, 'visualViewport', {
      configurable: true,
      value: originalVisualViewport,
    });
    Object.defineProperty(window, 'innerHeight', {
      configurable: true,
      value: originalInnerHeight,
    });
  });

  it('renders no minimized bar and no floating chrome — just messages and composer', () => {
    render(<ChatPanel variant="sheet" />);

    // No desktop-only header controls in sheet mode: BottomSheet supplies
    // the title/close affordance instead.
    expect(screen.queryByTitle('Minimize')).not.toBeInTheDocument();
    expect(screen.queryByText('Graph assistant')).not.toBeInTheDocument();

    // The composer and message list are still there.
    expect(screen.getByPlaceholderText('chat.placeholder')).toBeInTheDocument();
    expect(screen.getByText('chat.send')).toBeInTheDocument();
  });

  it('renders nothing when chatPanelOpen is false — the host BottomSheet owns mounting', () => {
    setStore({ chatPanelOpen: false });
    const { container } = render(<ChatPanel variant="sheet" />);

    expect(container).toBeEmptyDOMElement();
  });

  // Keyboard avoidance for the sheet variant's composer used to be ChatPanel's
  // own job (an inline marginBottom driven by useVisualViewportInset), but
  // that stacked with BottomSheet's own scrim-shrink once BottomSheet grew
  // the same mechanism (frontend/web/src/components/BottomSheet.jsx) — the
  // sheet variant is *always* mounted inside a BottomSheet (MobileShell.jsx
  // is its only caller), so double-compensating pushed the composer roughly
  // twice as far above the keyboard as intended. ChatPanel now applies none
  // of its own; BottomSheet.test.jsx's "on-screen keyboard avoidance" cases
  // are the real coverage for this behavior now. These tests just pin that
  // ChatPanel stays out of it entirely, in both variants.
  it('never applies an inline keyboard margin to the composer, in either variant', () => {
    const vv = makeVisualViewport({ height: 800 });
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: vv });

    const { rerender } = render(<ChatPanel variant="sheet" />);
    const sheetComposer = screen
      .getByPlaceholderText('chat.placeholder')
      .closest('.chat-input-container');
    expect(sheetComposer.style.marginBottom).toBe('');

    act(() => {
      vv.emit('resize', { height: 480 });
    });
    expect(sheetComposer.style.marginBottom).toBe('');

    rerender(<ChatPanel />);
    const floatingComposer = screen
      .getByPlaceholderText('chat.placeholder')
      .closest('.chat-input-container');
    expect(floatingComposer.style.marginBottom).toBe('');
  });

  it('does not double-compensate for the keyboard when actually nested inside BottomSheet, the way MobileShell.jsx really mounts it', () => {
    const vv = makeVisualViewport({ height: 800 });
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: vv });

    render(
      <BottomSheet isOpen snapPoint="half" onClose={() => {}} onSnapPointChange={() => {}}>
        <ChatPanel variant="sheet" />
      </BottomSheet>
    );
    const composer = screen
      .getByPlaceholderText('chat.placeholder')
      .closest('.chat-input-container');

    act(() => {
      vv.emit('resize', { height: 480 });
    });

    // BottomSheet itself shrinks its scrim to stay above the keyboard (see
    // BottomSheet.test.jsx); the composer must not ALSO apply its own
    // margin on top of that, or it ends up roughly twice as far above the
    // keyboard as intended.
    expect(composer.style.marginBottom).toBe('');
    expect(
      screen.getByTestId('bottom-sheet-scrim').style.getPropertyValue('--keyboard-inset')
    ).toBe('320px');
  });

  it('never subscribes to visualViewport, in either variant', () => {
    const vv = makeVisualViewport({ height: 800 });
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: vv });

    const { rerender } = render(<ChatPanel variant="sheet" />);
    expect(vv.addEventListener).not.toHaveBeenCalled();

    rerender(<ChatPanel />);
    expect(vv.addEventListener).not.toHaveBeenCalled();
  });

  it('is a graceful no-op when visualViewport is unavailable', () => {
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: undefined });

    expect(() => render(<ChatPanel variant="sheet" />)).not.toThrow();
    const composer = screen
      .getByPlaceholderText('chat.placeholder')
      .closest('.chat-input-container');
    expect(composer.style.marginBottom).toBe('');
  });
});
