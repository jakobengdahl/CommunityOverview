/**
 * @vitest-environment jsdom
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
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

// Uses the REAL store so addChatMessage, resetSessionScopedState and the
// assistant epoch all behave as in production — the epoch guard can only be
// exercised against the real reset action.
function resetStore() {
  useGraphStore.setState({
    presentation: {},
    nodes: [],
    edges: [],
    chatMessages: [{ id: 'welcome', role: 'assistant', content: 'welcome' }],
    activeExperts: [],
    selectedGraphNodes: [],
    selectedNodeId: null,
    detailNode: null,
    editingNode: null,
    contextMenu: null,
    modelProfiles: [],
    stats: { federation: { max_selectable_depth: 1 } },
    sessionEpoch: 0,
  });
}

const messageContents = () => useGraphStore.getState().chatMessages.map((m) => m.content);

async function sendMessage(text) {
  fireEvent.change(screen.getByRole('textbox'), { target: { value: text } });
  await act(async () => {
    fireEvent.click(screen.getByText('chat.send'));
  });
}

describe('ChatPanel session-switch epoch guard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetStore();
  });

  it('drops an in-flight assistant response when the session switched mid-request', async () => {
    let resolveSend;
    api.sendChatMessage.mockImplementation(() => new Promise((resolve) => (resolveSend = resolve)));

    render(<ChatPanel />);
    await sendMessage('question in session A');

    // The user's message is showing and the request is in flight.
    expect(messageContents()).toContain('question in session A');

    // User switches visualization sessions while the reply is still pending.
    act(() => {
      useGraphStore.getState().resetSessionScopedState((k) => k, 'en');
    });
    expect(useGraphStore.getState().chatMessages).toHaveLength(1);

    // The previous session's reply finally arrives — it must not land here.
    await act(async () => {
      resolveSend({ content: 'STALE ANSWER FROM SESSION A', toolUsed: null, toolResult: null });
    });

    const contents = messageContents();
    expect(contents).not.toContain('STALE ANSWER FROM SESSION A');
    expect(contents).not.toContain('question in session A');
    expect(useGraphStore.getState().chatMessages).toHaveLength(1);
  });

  it('appends the response normally when no session switch happens', async () => {
    api.sendChatMessage.mockResolvedValue({
      content: 'fresh answer',
      toolUsed: null,
      toolResult: null,
    });

    render(<ChatPanel />);
    await sendMessage('question');

    expect(messageContents()).toContain('fresh answer');
  });
});
