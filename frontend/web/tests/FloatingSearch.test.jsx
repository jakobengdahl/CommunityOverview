import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import FloatingSearch from '../src/components/FloatingSearch';
import useGraphStore from '../src/store/graphStore';

vi.mock('../src/services/api', () => ({
  searchGraph: vi.fn(),
  getNodeDetails: vi.fn(),
}));

import * as api from '../src/services/api';

describe('FloatingSearch federation labels', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useGraphStore.setState({
      nodes: [],
      hiddenNodeIds: [],
      federationDepth: 1,
      stats: {
        federation: {
          search_has_multiple_graphs: true,
          graph_display_names: {
            local: 'Local Graph',
            'esam-main': 'eSam',
          },
          max_selectable_depth: 4,
        },
      },
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('shows graph prefix in search results when multiple graphs exist', async () => {
    api.searchGraph.mockResolvedValueOnce({
      nodes: [
        {
          id: 'federated::esam-main::1',
          type: 'Actor',
          name: 'Shared capability',
          metadata: { origin_graph_id: 'esam-main' },
        },
        {
          id: 'local-1',
          type: 'Actor',
          name: 'Local initiative',
          metadata: {},
        },
      ],
      edges: [],
    });

    render(<FloatingSearch />);
    const user = userEvent.setup();

    await user.type(screen.getByPlaceholderText('Search graph...'), 'sh');

    await waitFor(() => {
      expect(screen.getByText('eSam: Shared capability')).toBeInTheDocument();
      expect(screen.getByText('Local Graph: Local initiative')).toBeInTheDocument();
      expect(screen.getByText('Depth 1/4')).toBeInTheDocument();
    });
  });

  it('shows only node names when only local graph is available', async () => {
    useGraphStore.setState({
      stats: {
        federation: {
          search_has_multiple_graphs: false,
          graph_display_names: { local: 'Local Graph' },
          max_selectable_depth: 2,
        },
      },
    });

    api.searchGraph.mockResolvedValueOnce({
      nodes: [
        { id: 'local-1', type: 'Actor', name: 'Only local node', metadata: {} },
      ],
      edges: [],
    });

    render(<FloatingSearch />);
    const user = userEvent.setup();

    await user.type(screen.getByPlaceholderText('Search graph...'), 'lo');

    await waitFor(() => {
      expect(screen.getByText('Only local node')).toBeInTheDocument();
      expect(screen.queryByText('Local Graph: Only local node')).not.toBeInTheDocument();
      expect(screen.getByText('Depth 1/2')).toBeInTheDocument();
    });
  });
});
