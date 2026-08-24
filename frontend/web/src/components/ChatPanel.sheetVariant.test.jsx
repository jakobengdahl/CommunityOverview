/**
 * @vitest-environment jsdom
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import ChatPanel from './ChatPanel';
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

  it('is a graceful no-op when visualViewport is unavailable: composer has no inline keyboard margin', () => {
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: undefined });

    render(<ChatPanel variant="sheet" />);
    const composer = screen
      .getByPlaceholderText('chat.placeholder')
      .closest('.chat-input-container');

    expect(composer.style.marginBottom).toBe('');
  });

  it('lifts the composer above the keyboard via visualViewport once it shrinks', () => {
    const vv = makeVisualViewport({ height: 800 });
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: vv });

    render(<ChatPanel variant="sheet" />);
    const composer = screen
      .getByPlaceholderText('chat.placeholder')
      .closest('.chat-input-container');
    expect(composer.style.marginBottom).toBe('');

    act(() => {
      vv.emit('resize', { height: 480 });
    });

    expect(composer.style.marginBottom).toBe('320px');
  });

  it('does not apply the keyboard margin in the floating (desktop) variant even with a shrunk visualViewport', () => {
    const vv = makeVisualViewport({ height: 480 });
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: vv });

    render(<ChatPanel />);
    const composer = screen
      .getByPlaceholderText('chat.placeholder')
      .closest('.chat-input-container');

    expect(composer.style.marginBottom).toBe('');
  });
});
