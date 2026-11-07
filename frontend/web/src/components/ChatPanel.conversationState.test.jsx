// covers: frontend/web/src/components/ChatPanel.jsx, frontend/web/src/components/PendingActionCard.jsx
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import ChatPanel from './ChatPanel';
import useGraphStore from '../store/graphStore';
import fixture from '../../../../docs/fixtures/chat_turn2_request.json';

// The real api.js and store: the signed conversation_state is kept and sent by
// api.sendChatMessage per conversation key, so only the wire shows it.

const welcome = { id: 'welcome', role: 'assistant', content: 'welcome' };

function streamed(response) {
  const bytes = new TextEncoder().encode(
    `data: ${JSON.stringify({ kind: 'chat_finished', response })}\n\n`
  );
  const read = vi
    .fn()
    .mockResolvedValueOnce({ value: bytes, done: false })
    .mockResolvedValue({ done: true });
  return {
    ok: true,
    body: { getReader: () => ({ read, cancel: vi.fn(), releaseLock: vi.fn() }) },
  };
}

const json = (response) => ({ ok: true, json: async () => response });
const sentBody = (call) => JSON.parse(global.fetch.mock.calls[call][1].body);

function resetStore(patch = {}) {
  useGraphStore.setState({
    chatMessages: [welcome],
    chatConversationId: crypto.randomUUID(),
    nodes: [],
    edges: [],
    selectedGraphNodes: [],
    activeExperts: [],
    federationDepth: 1,
    selectedModelProfileId: null,
    modelProfiles: [],
    chatPanelOpen: true,
    presentation: {},
    ...patch,
  });
}

const chatInput = () => document.querySelector('.chat-input');

async function send(text) {
  fireEvent.change(chatInput(), { target: { value: text } });
  fireEvent.click(screen.getByRole('button', { name: 'Send' }));
}

describe('ChatPanel conversation state', () => {
  beforeEach(() => {
    global.fetch.mockReset();
  });

  it('sends turn 2 exactly as the checked-in fixture after a selection and a cancelled delete', async () => {
    const agency = { id: 'n1', type: 'Actor', name: 'Agency' };
    resetStore({ nodes: [agency], selectedGraphNodes: [agency] });
    global.fetch
      .mockResolvedValueOnce(
        streamed({
          content: fixture.messages[1].content,
          toolUsed: 'delete_nodes',
          toolResult: {
            requires_confirmation: true,
            nodes_to_delete: [agency],
            affected_edges: [],
            node_ids: ['n1'],
          },
          conversation_state: 'state-1',
        })
      )
      .mockResolvedValueOnce(streamed({ content: 'ok', conversation_state: 'state-2' }));
    render(<ChatPanel />);

    await send('Delete the agency');
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }));
    fireEvent.click(screen.getByTitle('Clear selection'));
    await send(fixture.messages[2].content);

    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(2));
    const body = sentBody(1);
    expect(body.conversation_state).toBe('state-1');
    expect({ ...body, conversation_state: fixture.conversation_state }).toEqual(fixture);
  });

  it('keeps the signed reply in the replayed history when a tool side effect throws', async () => {
    resetStore({
      setNodeMarks: () => {
        throw new Error('boom');
      },
    });
    vi.spyOn(console, 'error').mockImplementation(() => {});
    global.fetch
      .mockResolvedValueOnce(
        streamed({
          content: 'marked',
          toolResult: { action: 'mark_nodes', marks: [] },
          conversation_state: 'state-1',
        })
      )
      .mockResolvedValueOnce(streamed({ content: 'ok', conversation_state: 'state-2' }));
    render(<ChatPanel />);

    await send('mark them');
    await waitFor(() => expect(chatInput()).not.toBeDisabled());
    await send('next');

    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(2));
    expect(sentBody(1).messages).toEqual([
      { role: 'user', content: 'mark them' },
      { role: 'assistant', content: 'marked' },
      { role: 'user', content: 'next' },
    ]);
  });

  it('carries the key through every call site and starts a new conversation from the New conversation button', async () => {
    const form = { title: 'F', fields: [{ id: 'q', label: 'Q', type: 'text' }] };
    resetStore({
      chatMessages: [
        welcome,
        { id: 'u', role: 'user', content: 'hi' },
        {
          id: 'a',
          role: 'assistant',
          content: 'cards',
          proposal: { node: { name: 'N', type: 'Actor' }, similar_nodes: [] },
          deleteConfirmation: { nodes_to_delete: [], affected_edges: [], node_ids: [] },
          form,
        },
      ],
    });
    let n = 0;
    global.fetch.mockImplementation(async () => {
      n += 1;
      return streamed({ content: `reply ${n}`, conversation_state: `state-${n}` });
    });
    render(<ChatPanel />);

    const actions = [
      () => send('typed'),
      () => fireEvent.click(screen.getByRole('button', { name: 'Approve' })),
      () => fireEvent.click(screen.getByRole('button', { name: 'Reject' })),
      () => fireEvent.click(screen.getByRole('button', { name: 'Confirm deletion' })),
      () => fireEvent.click(screen.getByRole('button', { name: 'Submit' })),
    ];
    for (const [index, act] of actions.entries()) {
      await waitFor(() => expect(chatInput()).not.toBeDisabled());
      await act();
      await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(index + 1));
      // Each request replays the state the previous one returned for this key.
      expect(sentBody(index).conversation_state).toBe(index ? `state-${index}` : undefined);
    }

    await waitFor(() => expect(chatInput()).not.toBeDisabled());
    fireEvent.click(screen.getByRole('button', { name: 'New conversation' }));
    await send('fresh');
    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(6));
    expect(sentBody(5).conversation_state).toBeUndefined();
    expect(sentBody(5).messages).toEqual([{ role: 'user', content: 'fresh' }]);
  });

  it('shows a pending action exactly as recorded, runs it only through the confirmation route and applies its result', async () => {
    resetStore();
    const action = { token: 't-1', tool: 'add_nodes', arguments: { nodes: [{ name: 'X' }] } };
    global.fetch
      .mockResolvedValueOnce(
        streamed({
          content: 'needs confirmation',
          pending_actions: [action],
          untrusted_content_used: true,
        })
      )
      .mockResolvedValueOnce(
        // The route returns the raw tool result, not a chat response.
        json({
          success: true,
          message: 'Added X',
          added_node_ids: ['x1'],
          action: 'add_to_visualization',
          nodes: [{ id: 'x1', type: 'Actor', name: 'X' }],
          edges: [],
        })
      );
    render(<ChatPanel />);
    await send('read the file');

    const run = await screen.findByRole('button', { name: 'Confirm and run' });
    expect(screen.getByText('add_nodes')).toBeInTheDocument();
    const shown = document.querySelector('.pending-action-arguments').textContent;
    expect(JSON.parse(shown)).toEqual(action.arguments);
    fireEvent.click(run);

    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(2));
    const [url, options] = global.fetch.mock.calls[1];
    expect(url).toMatch(/\/ui\/chat\/confirm$/);
    expect(JSON.parse(options.body)).toEqual({ token: 't-1' });
    const status = await screen.findByRole('status');
    expect(within(status).getByText('Added X')).toBeInTheDocument();
    await waitFor(() =>
      expect(useGraphStore.getState().nodes.map((node) => node.id)).toEqual(['x1'])
    );
    expect(screen.queryByRole('button', { name: 'Confirm and run' })).toBeNull();
  });

  it('shows the reason of a confirmed action whose tool result failed with only a message', async () => {
    resetStore();
    const action = { token: 't-2', tool: 'add_nodes', arguments: { nodes: [{ name: 'Y' }] } };
    const reason = 'Error validating input: bad annotation';
    global.fetch
      .mockResolvedValueOnce(streamed({ content: 'needs confirmation', pending_actions: [action] }))
      .mockResolvedValueOnce(
        json({ success: false, message: reason, added_node_ids: [], added_edge_ids: [] })
      );
    render(<ChatPanel />);
    await send('read the file');
    fireEvent.click(await screen.findByRole('button', { name: 'Confirm and run' }));

    const status = await screen.findByRole('status');
    expect(status.textContent).toContain(reason);
    expect(useGraphStore.getState().nodes).toEqual([]);
  });
});
