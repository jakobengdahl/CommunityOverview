/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import StatsPanel from './StatsPanel';
import useGraphStore from '../store/graphStore';

// Mock API
vi.mock('../services/api', () => ({
  executeTool: vi.fn().mockResolvedValue({
    total_nodes: 17,
    total_edges: 11,
    nodes_by_type: {
      Actor: 3,
      Initiative: 3,
      Legislation: 2
    },
    nodes_by_community: {
      eSam: 10,
      Myndigheter: 8
    }
  })
}));

describe('StatsPanel', () => {
  beforeEach(() => {
    useGraphStore.setState({
      nodes: [
        { id: '1', type: 'Actor', name: 'Test Actor', communities: ['eSam'] },
        { id: '2', type: 'Initiative', name: 'Test Initiative', communities: ['eSam'] }
      ],
      edges: [
        { id: 'e1', source: '1', target: '2', type: 'BELONGS_TO' }
      ],
      selectedCommunities: ['eSam']
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('renders toggle button', () => {
    render(<StatsPanel />);
    expect(screen.getByRole('button', { name: /Graf-statistik/i })).toBeDefined();
  });

  it('expands when toggle button is clicked', async () => {
    render(<StatsPanel />);

    const toggleButton = screen.getByRole('button', { name: /Graf-statistik/i });
    fireEvent.click(toggleButton);

    await waitFor(() => {
      expect(screen.getByText(/Översikt/i)).toBeDefined();
    });
  });

  it('displays local statistics', async () => {
    render(<StatsPanel />);

    const toggleButton = screen.getByRole('button', { name: /Graf-statistik/i });
    fireEvent.click(toggleButton);

    await waitFor(() => {
      // Check that it shows 2 displayed nodes (from the store)
      expect(screen.getByText(/Visade noder:/i)).toBeDefined();
      expect(screen.getByText('2')).toBeDefined(); // 2 nodes in store
    });
  });

  it('fetches and displays backend statistics', async () => {
    const { executeTool } = await import('../services/api');

    render(<StatsPanel />);

    const toggleButton = screen.getByRole('button', { name: /Graf-statistik/i });
    fireEvent.click(toggleButton);

    await waitFor(() => {
      // Check that executeTool was called
      expect(executeTool).toHaveBeenCalledWith('get_graph_stats', { communities: ['eSam'] });
    });

    await waitFor(() => {
      // Check that backend stats are displayed (17 total nodes from mock)
      expect(screen.getByText('17')).toBeDefined();
    });
  });

  it('displays nodes by type', async () => {
    render(<StatsPanel />);

    const toggleButton = screen.getByRole('button', { name: /Graf-statistik/i });
    fireEvent.click(toggleButton);

    await waitFor(() => {
      expect(screen.getByText(/Noder per typ/i)).toBeDefined();
      expect(screen.getByText(/Actor:/i)).toBeDefined();
      expect(screen.getByText(/Initiative:/i)).toBeDefined();
    });
  });

  it('displays nodes by community', async () => {
    render(<StatsPanel />);

    const toggleButton = screen.getByRole('button', { name: /Graf-statistik/i });
    fireEvent.click(toggleButton);

    await waitFor(() => {
      expect(screen.getByText(/Noder per community/i)).toBeDefined();
      expect(screen.getByText(/eSam:/i)).toBeDefined();
    });
  });

  it('shows loading state while fetching backend stats', async () => {
    const { executeTool } = await import('../services/api');
    executeTool.mockImplementation(() => new Promise(resolve => setTimeout(() => resolve({
      total_nodes: 17,
      total_edges: 11,
      nodes_by_type: {},
      nodes_by_community: {}
    }), 100)));

    render(<StatsPanel />);

    const toggleButton = screen.getByRole('button', { name: /Graf-statistik/i });
    fireEvent.click(toggleButton);

    await waitFor(() => {
      expect(screen.getByText(/Laddar statistik.../i)).toBeDefined();
    });
  });

  it('handles backend error gracefully', async () => {
    const { executeTool } = await import('../services/api');
    executeTool.mockRejectedValueOnce(new Error('Backend error'));

    render(<StatsPanel />);

    const toggleButton = screen.getByRole('button', { name: /Graf-statistik/i });
    fireEvent.click(toggleButton);

    await waitFor(() => {
      expect(screen.getByText(/Fel:/i)).toBeDefined();
    });
  });
});
