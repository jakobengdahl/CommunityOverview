/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { fireEvent, render, waitFor } from '@testing-library/react';
import FloatingSearch from './FloatingSearch';
import { COLOR_MAP } from './FloatingToolbar';

const mockUseGraphStore = vi.fn();

vi.mock('../store/graphStore', () => ({
  default: () => mockUseGraphStore(),
}));

vi.mock('../i18n', () => ({
  useI18n: () => ({ t: (key) => key, language: 'en' }),
}));

vi.mock('../services/api', () => ({
  searchGraph: vi.fn(),
}));

import * as api from '../services/api';

const SCHEMA = {
  node_types: {
    Questionnaire: { color: '#7C3AED' },
    Capability: { color: '#F59E0B' },
  },
};

function renderWithResults(nodes) {
  mockUseGraphStore.mockImplementation(() => ({
    nodes: [],
    hiddenNodeIds: [],
    addNodesToVisualization: vi.fn(),
    clearVisualization: vi.fn(),
    setFocusNodeId: vi.fn(),
    setPendingGroups: vi.fn(),
    federationDepth: 1,
    stats: null,
    schema: SCHEMA,
    guideSearchInput: null,
    clearGuideSearchInput: vi.fn(),
    requestCloseMenus: vi.fn(),
  }));
  api.searchGraph.mockResolvedValue({ nodes });

  const view = render(<FloatingSearch />);
  fireEvent.change(view.container.querySelector('input'), { target: { value: 'qu' } });
  return view;
}

afterEach(() => {
  vi.clearAllMocks();
});

describe('FloatingSearch result colors', () => {
  it('colors a custom node type from the schema instead of the neutral default', async () => {
    const { container } = renderWithResults([{ id: 'n1', name: 'Survey', type: 'Questionnaire' }]);

    await waitFor(() => expect(container.querySelector('.floating-search-result')).toBeTruthy());

    const dot = container.querySelector('.floating-search-result-dot');
    expect(dot.style.backgroundColor).toBe('rgb(124, 58, 237)'); // #7C3AED
  });

  it('uses the schema color for a legacy type the profile recolors, matching the toolbar', async () => {
    const { container } = renderWithResults([{ id: 'n2', name: 'Reporting', type: 'Capability' }]);

    await waitFor(() => expect(container.querySelector('.floating-search-result')).toBeTruthy());

    const dot = container.querySelector('.floating-search-result-dot');
    expect(dot.style.backgroundColor).toBe('rgb(245, 158, 11)'); // schema #F59E0B
    expect(COLOR_MAP.Capability).toBe('#F97316');
  });
});
