/**
 * @vitest-environment jsdom
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
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

describe('ChatPanel model profiles', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.sendChatMessage.mockResolvedValue({ content: 'ok', toolUsed: null, toolResult: null });
    setStore();
  });

  it('renders enabled profile selector with default preselected and sends selected profile id', async () => {
    setStore({
      chatMessages: [],
      modelProfiles: [
        { id: 'fast', name: 'Fast', default: true },
        { id: 'deep', name: 'Deep', default: false },
      ],
      selectedModelProfileId: 'fast',
    });

    render(<ChatPanel />);
    const selector = screen.getByLabelText('chat.model_profile');
    expect(selector.value).toBe('fast');

    fireEvent.change(selector, { target: { value: 'deep' } });
    expect(useGraphStore.getState().setSelectedModelProfileId).toHaveBeenCalledWith('deep');

    useGraphStore.setState({ selectedModelProfileId: 'deep' });
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'hello' } });
    await act(async () => {
      fireEvent.click(screen.getByText('chat.send'));
    });

    await waitFor(() => expect(api.sendChatMessage).toHaveBeenCalled());
    expect(api.sendChatMessage.mock.calls[0][2].modelProfileId).toBe('deep');
  });

  it('disables selector when selection is disabled', () => {
    setStore({
      modelProfiles: [{ id: 'default', name: 'Default', default: true }],
      modelProfileSelectionEnabled: false,
      selectedModelProfileId: 'default',
    });

    render(<ChatPanel />);
    expect(screen.getByLabelText('chat.model_profile').disabled).toBe(true);
  });
});
