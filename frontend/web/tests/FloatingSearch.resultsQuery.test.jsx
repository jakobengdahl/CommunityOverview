import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import FloatingSearch from '../src/components/FloatingSearch';
import useGraphStore from '../src/store/graphStore';

vi.mock('../src/services/api', () => ({
  searchGraph: vi.fn(),
  getNodeDetails: vi.fn(),
  getRelatedNodes: vi.fn(),
}));

import * as api from '../src/services/api';

const ATTR = 'data-results-query';

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function renderSearch() {
  const { container } = render(<FloatingSearch />);
  return {
    root: container.querySelector('#guide-target-search'),
    input: screen.getByPlaceholderText('Search graph...'),
    user: userEvent.setup(),
  };
}

const actor = { id: 'n1', type: 'Actor', name: 'Alpha actor', metadata: {} };
const savedView = { id: 'v1', type: 'SavedView', name: 'Alpha view', metadata: { node_ids: [] } };

describe('FloatingSearch data-results-query', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useGraphStore.setState({
      nodes: [],
      edges: [],
      hiddenNodeIds: [],
      federationDepth: 1,
      stats: null,
    });
    api.getRelatedNodes.mockResolvedValue({ nodes: [], edges: [] });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('is absent while the search is pending and equals the query once its results render', async () => {
    const search = deferred();
    api.searchGraph.mockReturnValueOnce(search.promise);
    const { root, input, user } = renderSearch();

    await user.type(input, 'al');
    await waitFor(() => expect(api.searchGraph).toHaveBeenCalledWith('al', expect.anything()));
    expect(root).not.toHaveAttribute(ATTR);

    search.resolve({ nodes: [actor], edges: [] });
    await waitFor(() => expect(root).toHaveAttribute(ATTR, 'al'));
    expect(screen.getByText('Alpha actor')).toBeInTheDocument();
  });

  it('names the searched query, not the live input, when a stale search settles', async () => {
    const first = deferred();
    const second = deferred();
    api.searchGraph.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const { root, input, user } = renderSearch();

    await user.type(input, 'al');
    await waitFor(() => expect(api.searchGraph).toHaveBeenCalledWith('al', expect.anything()));
    await user.type(input, 'p');
    expect(input).toHaveValue('alp');
    await waitFor(() => expect(api.searchGraph).toHaveBeenCalledWith('alp', expect.anything()));

    first.resolve({ nodes: [actor], edges: [] });
    await waitFor(() => expect(screen.getByText('Alpha actor')).toBeInTheDocument());
    expect(root).toHaveAttribute(ATTR, 'al');

    second.resolve({ nodes: [], edges: [] });
    await waitFor(() => expect(root).toHaveAttribute(ATTR, 'alp'));
  });

  it('equals the query after a rejected search', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const search = deferred();
    api.searchGraph.mockReturnValueOnce(search.promise);
    const { root, input, user } = renderSearch();

    await user.type(input, 'zz');
    await waitFor(() => expect(api.searchGraph).toHaveBeenCalledWith('zz', expect.anything()));
    expect(root).not.toHaveAttribute(ATTR);

    search.reject(new Error('network down'));
    await waitFor(() => expect(root).toHaveAttribute(ATTR, 'zz'));
  });

  it('is removed after a result is picked', async () => {
    api.searchGraph.mockResolvedValueOnce({ nodes: [actor], edges: [] });
    const { root, input, user } = renderSearch();

    await user.type(input, 'al');
    await waitFor(() => expect(root).toHaveAttribute(ATTR, 'al'));

    await user.click(screen.getByText('Alpha actor'));
    await waitFor(() => expect(input).toHaveValue(''));
    expect(root).not.toHaveAttribute(ATTR);
  });

  it('is removed after a saved view is picked from results that outlived the query', async () => {
    const search = deferred();
    api.searchGraph.mockReturnValueOnce(search.promise);
    const { root, input, user } = renderSearch();

    await user.type(input, 'al');
    await waitFor(() => expect(api.searchGraph).toHaveBeenCalledWith('al', expect.anything()));
    // Emptying the input before the search settles means the pick's setQuery('') is a no-op,
    // so only the SavedView branch's own clear can remove the attribute.
    await user.clear(input);

    search.resolve({ nodes: [savedView], edges: [] });
    await waitFor(() => expect(root).toHaveAttribute(ATTR, 'al'));
    expect(input).toHaveValue('');

    await user.click(screen.getByText('Alpha view'));
    await waitFor(() => expect(screen.queryByText('Alpha view')).not.toBeInTheDocument());
    expect(root).not.toHaveAttribute(ATTR);
  });

  it('is removed when the query is shortened below two characters', async () => {
    api.searchGraph.mockResolvedValueOnce({ nodes: [actor], edges: [] });
    const { root, input, user } = renderSearch();

    await user.type(input, 'al');
    await waitFor(() => expect(root).toHaveAttribute(ATTR, 'al'));

    await user.type(input, '{Backspace}');
    await waitFor(() => expect(root).not.toHaveAttribute(ATTR));
    expect(input).toHaveValue('a');
  });
});
