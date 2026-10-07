import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import FloatingSearch from '../src/components/FloatingSearch';
import useGraphStore from '../src/store/graphStore';

vi.mock('../src/services/api', () => ({
  searchGraph: vi.fn(),
  getNodeDetails: vi.fn(),
  getRelatedNodes: vi.fn(),
  resolveTeleport: vi.fn(),
}));

import * as api from '../src/services/api';

const FEDERATED_RESULT = {
  id: 'federated::esam-main::1',
  type: 'Actor',
  name: 'Shared capability',
  metadata: { origin_graph_id: 'esam-main', origin_graph_name: 'eSam' },
};

const LOCAL_RESULT = {
  id: 'local-1',
  type: 'Actor',
  name: 'Local initiative',
  metadata: {},
};

function teleportButtons() {
  return document.querySelectorAll('.floating-search-result-teleport');
}

describe('FloatingSearch teleport action (task-federated-graph-teleport)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useGraphStore.setState({
      nodes: [],
      hiddenNodeIds: [],
      federationDepth: 1,
      stats: {
        federation: {
          search_has_multiple_graphs: true,
          graph_display_names: { local: 'Local Graph', 'esam-main': 'eSam' },
          max_selectable_depth: 4,
        },
      },
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  async function searchWith(nodes, props = {}) {
    api.searchGraph.mockResolvedValueOnce({ nodes, edges: [] });
    render(<FloatingSearch {...props} />);
    const user = userEvent.setup();
    await user.type(screen.getByPlaceholderText('Search graph...'), 'sh');
    await waitFor(() => {
      expect(document.querySelectorAll('.floating-search-result-row')).toHaveLength(nodes.length);
    });
    return user;
  }

  it('offers the action only on a result another graph owns', async () => {
    await searchWith([FEDERATED_RESULT, LOCAL_RESULT], { onTeleport: vi.fn() });

    await waitFor(() => expect(teleportButtons()).toHaveLength(1));
    expect(screen.getByRole('button', { name: /open in source graph/i })).toBeTruthy();
  });

  it('offers nothing when the host passes no teleport handler', async () => {
    await searchWith([FEDERATED_RESULT, LOCAL_RESULT]);

    expect(teleportButtons()).toHaveLength(0);
  });

  it('hands the node and the live query to the handler', async () => {
    const onTeleport = vi.fn();
    const user = await searchWith([FEDERATED_RESULT], { onTeleport });

    await user.click(screen.getByRole('button', { name: /open in source graph/i }));

    expect(onTeleport).toHaveBeenCalledWith(
      'federated::esam-main::1',
      expect.objectContaining({ id: 'federated::esam-main::1' }),
      { searchQuery: 'sh' }
    );
  });

  it('does not select the result into the canvas when teleporting', async () => {
    // Clicking the row still brings the node onto this canvas; only the
    // teleport button leaves for the source graph, so the two actions on one
    // row must not both fire from one click.
    const onTeleport = vi.fn();
    const addNodesToVisualization = vi.fn();
    useGraphStore.setState({ addNodesToVisualization });
    const user = await searchWith([FEDERATED_RESULT], { onTeleport });

    await user.click(screen.getByRole('button', { name: /open in source graph/i }));

    expect(onTeleport).toHaveBeenCalledTimes(1);
    expect(addNodesToVisualization).not.toHaveBeenCalled();
  });

  it('keeps the ordinary result action working beside it', async () => {
    const onTeleport = vi.fn();
    const addNodesToVisualization = vi.fn();
    useGraphStore.setState({ addNodesToVisualization });
    api.getRelatedNodes.mockResolvedValue({ nodes: [], edges: [] });
    const user = await searchWith([FEDERATED_RESULT], { onTeleport });

    await user.click(screen.getByText('eSam: Shared capability'));

    await waitFor(() => expect(addNodesToVisualization).toHaveBeenCalled());
    expect(onTeleport).not.toHaveBeenCalled();
  });

  it('still shows the graph prefix on a row that carries the action', async () => {
    await searchWith([FEDERATED_RESULT], { onTeleport: vi.fn() });

    expect(screen.getByText('eSam: Shared capability')).toBeInTheDocument();
  });

  it('marks the row so the extra action is visible in its layout', async () => {
    await searchWith([FEDERATED_RESULT, LOCAL_RESULT], { onTeleport: vi.fn() });

    await waitFor(() =>
      expect(document.querySelectorAll('.floating-search-result-row.has-teleport')).toHaveLength(1)
    );
    expect(document.querySelectorAll('.floating-search-result-row')).toHaveLength(2);
  });
});
