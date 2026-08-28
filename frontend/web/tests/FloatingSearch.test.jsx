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
      nodes: [{ id: 'local-1', type: 'Actor', name: 'Only local node', metadata: {} }],
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

  it('hides depth indicator when graph is not federated (max depth 1)', async () => {
    useGraphStore.setState({
      stats: {
        federation: {
          search_has_multiple_graphs: false,
          graph_display_names: { local: 'Local Graph' },
          max_selectable_depth: 1,
        },
      },
    });

    api.searchGraph.mockResolvedValueOnce({
      nodes: [{ id: 'local-1', type: 'Actor', name: 'Only local node', metadata: {} }],
      edges: [],
    });

    render(<FloatingSearch />);
    const user = userEvent.setup();

    await user.type(screen.getByPlaceholderText('Search graph...'), 'lo');

    await waitFor(() => {
      expect(screen.getByText('Only local node')).toBeInTheDocument();
      expect(screen.queryByText(/Depth/)).not.toBeInTheDocument();
    });
  });

  it('opens a saved view whose annotations are stored only in the v1 document', async () => {
    useGraphStore.setState({
      stats: {
        federation: {
          search_has_multiple_graphs: false,
          graph_display_names: { local: 'Local Graph' },
          max_selectable_depth: 1,
        },
      },
    });
    api.searchGraph.mockResolvedValueOnce({
      nodes: [
        {
          id: 'view-1',
          type: 'SavedView',
          name: 'Annotated view',
          metadata: {
            node_ids: ['node-a'],
            positions: { 'node-a': { x: 1, y: 2 } },
            annotation_document: {
              schema_version: 1,
              annotations: [
                {
                  id: 'note-1',
                  type: 'note',
                  kind: 'note',
                  position: { x: 10, y: 20 },
                  text: 'reopened note',
                },
              ],
            },
          },
        },
      ],
      edges: [],
    });
    api.getNodeDetails.mockResolvedValueOnce({
      success: true,
      node: { id: 'node-a', type: 'Actor', name: 'Actor A' },
      edges: [],
    });

    render(<FloatingSearch />);
    const user = userEvent.setup();
    await user.type(screen.getByPlaceholderText('Search graph...'), 'annotated');

    await user.click(await screen.findByText('Annotated view'));

    await waitFor(() => {
      expect(useGraphStore.getState().pendingAnnotations).toEqual([
        expect.objectContaining({
          id: 'note-1',
          kind: 'note',
          position: { x: 10, y: 20 },
          text: 'reopened note',
        }),
      ]);
    });
  });

  it('uses the measured header edge instead of overlapping top chrome', async () => {
    const header = document.createElement('div');
    header.className = 'floating-header';
    header.getBoundingClientRect = () => ({ right: 350, bottom: 58 });
    document.body.appendChild(header);

    render(<FloatingSearch />);

    await waitFor(() => {
      const search = document.querySelector('.floating-search');
      expect(search.style.getPropertyValue('--floating-search-left')).toBe('362px');
      expect(search.style.getPropertyValue('--floating-header-bottom')).toBe('58px');
    });
    header.remove();
  });

  it('stacks below the header rather than collapsing in constrained desktop space', async () => {
    const header = document.createElement('div');
    header.className = 'floating-header';
    header.getBoundingClientRect = () => ({ right: 800, bottom: 58 });
    document.body.appendChild(header);

    render(<FloatingSearch />);

    await waitFor(() => {
      const search = document.querySelector('.floating-search');
      expect(search.dataset.stacked).toBe('true');
      expect(Number.parseInt(search.style.getPropertyValue('--floating-search-width'), 10)).toBe(
        400
      );
    });
    header.remove();
  });
});
