import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import FloatingSearch from '../src/components/FloatingSearch';
import ChatPanel from '../src/components/ChatPanel';
import useGraphStore from '../src/store/graphStore';

vi.mock('../src/services/api', () => ({
  searchGraph: vi.fn(),
  sendChatMessage: vi.fn(),
  uploadFile: vi.fn(),
  executeTool: vi.fn(),
  getNodeDetails: vi.fn(),
}));

import * as api from '../src/services/api';

describe('Federation depth runtime flow', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useGraphStore.setState({
      nodes: [],
      edges: [],
      hiddenNodeIds: [],
      chatMessages: [],
      chatPanelOpen: true,
      federationDepth: 2,
      stats: {
        federation: {
          max_selectable_depth: 4,
          search_has_multiple_graphs: false,
          graph_display_names: { local: 'Local Graph' },
        },
      },
    });
  });

  it('uses selected federation depth for both search and chat requests', async () => {
    api.searchGraph.mockResolvedValueOnce({
      nodes: [
        { id: 'local-1', type: 'Actor', name: 'Local node', metadata: {} },
      ],
      edges: [],
    });

    api.sendChatMessage.mockResolvedValueOnce({
      content: 'ok',
      toolUsed: null,
      toolResult: null,
    });

    const user = userEvent.setup();

    render(<FloatingSearch />);
    await user.type(screen.getByPlaceholderText('Search graph...'), 'lo');

    await waitFor(() => {
      expect(api.searchGraph).toHaveBeenCalled();
    });

    expect(api.searchGraph.mock.calls[0][1]).toMatchObject({ federationDepth: 2 });

    render(<ChatPanel />);
    const input = document.querySelector('textarea.chat-input');
    expect(input).toBeTruthy();
    await user.type(input, 'hello depth');
    await user.click(screen.getByRole('button', { name: /Send|Skicka/i }));

    await waitFor(() => {
      expect(api.sendChatMessage).toHaveBeenCalled();
    });

    expect(api.sendChatMessage.mock.calls[0][2]).toEqual({ federationDepth: 2 });
  });
});
