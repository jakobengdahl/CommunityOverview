// covers: frontend/web/src/components/ChatPanel.jsx
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

    act(() => {
      api.sendChatMessage.mock.calls[0][2].onProgress({ kind: 'llm_started', round: 1 });
    });
    expect(screen.getAllByRole('listitem')).toHaveLength(1);

    // User switches visualization sessions while the reply is still pending.
    act(() => {
      useGraphStore.getState().resetSessionScopedState((k) => k, 'en');
    });
    expect(useGraphStore.getState().chatMessages).toHaveLength(1);
    act(() => {
      api.sendChatMessage.mock.calls[0][2].onProgress({
        kind: 'tool_started',
        tool: 'search_graph',
      });
    });
    expect(screen.queryAllByRole('listitem')).toHaveLength(0);

    // The previous session's reply finally arrives — it must not land here.
    await act(async () => {
      resolveSend({ content: 'STALE ANSWER FROM SESSION A', toolUsed: null, toolResult: null });
    });

    const contents = messageContents();
    expect(contents).not.toContain('STALE ANSWER FROM SESSION A');
    expect(contents).not.toContain('question in session A');
    expect(useGraphStore.getState().chatMessages).toHaveLength(1);
  });

  it('shows live ordered steps and preserves them with the final answer', async () => {
    let finish;
    api.sendChatMessage.mockImplementation(() => new Promise((resolve) => (finish = resolve)));
    render(<ChatPanel />);
    await sendMessage('question');
    const report = api.sendChatMessage.mock.calls[0][2].onProgress;
    act(() => {
      report({ kind: 'llm_started', round: 1 });
      report({ kind: 'tool_started', tool: 'search_graph' });
    });
    const items = screen.getAllByRole('listitem');
    expect(items).toHaveLength(2);
    expect(items[1]).toHaveAttribute('aria-current', 'step');
    await act(async () => {
      report({ kind: 'chat_finished' });
      finish({ content: 'answer', toolResult: null });
    });
    expect(screen.getAllByRole('listitem')).toHaveLength(3);
    expect(useGraphStore.getState().chatMessages.at(-1).progressSteps).toEqual([
      { kind: 'llm_started', round: 1 },
      { kind: 'tool_started', tool: 'search_graph' },
      { kind: 'chat_finished' },
    ]);
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
