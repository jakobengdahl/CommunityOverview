/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { act, fireEvent, render, waitFor } from '@testing-library/react';
import FloatingSearch from './FloatingSearch';
import { COLOR_MAP } from './FloatingToolbar';

const mockUseGraphStore = vi.fn();

vi.mock('../store/graphStore', () => ({
  default: () => mockUseGraphStore(),
}));

vi.mock('../i18n', () => ({
  useI18n: () => ({
    t: (key) =>
      ({
        'floating_search.placeholder': 'Search graph...',
        'floating_search.aria_label': 'Search',
        'floating_search.local_graph': 'Local',
        'floating_search.in_view_badge': 'in view',
        'mobile_nav.search_panel_title': 'Search',
        'federation.depth_indicator_tooltip': 'Search depth',
        'federation.depth_indicator': 'Depth',
      })[key] || key,
    language: 'en',
  }),
}));

vi.mock('../services/api', () => ({
  searchGraph: vi.fn(),
  getNodeDetails: vi.fn(),
  getRelatedNodes: vi.fn(),
}));

import * as api from '../services/api';

const SCHEMA = {
  node_types: {
    Questionnaire: { color: '#7C3AED' },
    Capability: { color: '#F59E0B' },
  },
};

let storeState;

function makeStore(overrides = {}) {
  storeState = {
    nodes: [],
    hiddenNodeIds: [],
    addNodesToVisualization: vi.fn(),
    clearVisualization: vi.fn(),
    setFocusNodeId: vi.fn(),
    setPendingGroups: vi.fn(),
    setPendingAnnotations: vi.fn(),
    federationDepth: 1,
    stats: null,
    schema: SCHEMA,
    guideSearchInput: null,
    clearGuideSearchInput: vi.fn(),
    requestCloseMenus: vi.fn(),
    ...overrides,
  };
  mockUseGraphStore.mockImplementation(() => storeState);
  return storeState;
}

async function renderWithResults(nodes) {
  makeStore();
  api.searchGraph.mockResolvedValue({ nodes });

  const view = render(<FloatingSearch />);
  fireEvent.change(view.container.querySelector('input'), { target: { value: 'qu' } });
  await waitFor(() => expect(api.searchGraph).toHaveBeenCalledWith('qu', expect.any(Object)));
  return view;
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

async function renderWithTimedResults(nodes) {
  api.searchGraph.mockResolvedValue({ nodes });

  const view = render(<FloatingSearch />);
  fireEvent.change(view.container.querySelector('input'), { target: { value: 'qu' } });
  await act(async () => {
    vi.advanceTimersByTime(300);
  });
  return view;
}

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe('FloatingSearch result colors', () => {
  it('colors a custom node type from the schema instead of the neutral default', async () => {
    const { container } = await renderWithResults([
      { id: 'n1', name: 'Survey', type: 'Questionnaire' },
    ]);

    await waitFor(() => expect(container.querySelector('.floating-search-result')).toBeTruthy());

    const dot = container.querySelector('.floating-search-result-dot');
    expect(dot.style.backgroundColor).toBe('rgb(124, 58, 237)'); // #7C3AED
  });

  it('uses the schema color for a legacy type the profile recolors, matching the toolbar', async () => {
    const { container } = await renderWithResults([
      { id: 'n2', name: 'Reporting', type: 'Capability' },
    ]);

    await waitFor(() => expect(container.querySelector('.floating-search-result')).toBeTruthy());

    const dot = container.querySelector('.floating-search-result-dot');
    expect(dot.style.backgroundColor).toBe('rgb(245, 158, 11)'); // schema #F59E0B
    expect(COLOR_MAP.Capability).toBe('#F97316');
  });
});

describe('FloatingSearch async behavior', () => {
  it('keeps the spinner visible when an older cancelled search resolves during a newer search', async () => {
    vi.useFakeTimers();
    makeStore();
    const first = deferred();
    const second = deferred();
    api.searchGraph.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);

    const { container } = render(<FloatingSearch />);
    const input = container.querySelector('input');

    fireEvent.change(input, { target: { value: 'al' } });
    await act(async () => {
      vi.advanceTimersByTime(300);
    });
    expect(container.querySelector('.floating-search-spinner')).toBeTruthy();

    fireEvent.change(input, { target: { value: 'alp' } });
    await act(async () => {
      vi.advanceTimersByTime(300);
    });
    expect(api.searchGraph).toHaveBeenCalledTimes(2);

    await act(async () => {
      first.resolve({ nodes: [{ id: 'old', name: 'Old', type: 'Capability' }] });
      await first.promise;
    });

    expect(container.querySelector('.floating-search-spinner')).toBeTruthy();

    await act(async () => {
      second.resolve({ nodes: [{ id: 'new', name: 'New', type: 'Capability' }] });
      await second.promise;
    });

    expect(container.querySelector('.floating-search-spinner')).toBeFalsy();
  });

  it('does not let a stale saved-view pick clear or replace a newer saved-view selection', async () => {
    vi.useFakeTimers();
    const store = makeStore();
    const staleDetails = deferred();
    const freshDetails = Promise.resolve({
      success: true,
      node: { id: 'fresh-node', name: 'Fresh node', type: 'Capability' },
      edges: [],
    });
    api.getNodeDetails.mockImplementation((id) =>
      id === 'stale-node' ? staleDetails.promise : freshDetails
    );

    const staleView = {
      id: 'stale-view',
      name: 'Stale view',
      type: 'SavedView',
      metadata: { node_ids: ['stale-node'] },
    };
    const freshView = {
      id: 'fresh-view',
      name: 'Fresh view',
      type: 'SavedView',
      metadata: {
        node_ids: ['fresh-node'],
        positions: { 'fresh-node': { x: 10, y: 20 } },
      },
    };
    const { container } = await renderWithTimedResults([staleView, freshView]);

    const buttons = container.querySelectorAll('.floating-search-result');
    fireEvent.click(buttons[0]);
    expect(store.clearVisualization).not.toHaveBeenCalled();

    fireEvent.click(buttons[1]);
    await act(async () => {
      await freshDetails;
    });

    expect(store.clearVisualization).toHaveBeenCalledTimes(1);
    expect(store.addNodesToVisualization).toHaveBeenCalledWith(
      [
        {
          id: 'fresh-node',
          name: 'Fresh node',
          type: 'Capability',
          _savedPosition: { x: 10, y: 20 },
        },
      ],
      []
    );

    await act(async () => {
      staleDetails.resolve({
        success: true,
        node: { id: 'stale-node', name: 'Stale node', type: 'Capability' },
        edges: [],
      });
      await staleDetails.promise;
    });

    expect(store.clearVisualization).toHaveBeenCalledTimes(1);
    expect(store.addNodesToVisualization).toHaveBeenCalledTimes(1);
  });
});
