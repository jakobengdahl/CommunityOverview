import { Profiler } from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
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
  const commits = [];
  const { container } = render(
    <Profiler
      id="floating-search"
      onRender={() => {
        const root = document.querySelector('#guide-target-search');
        commits.push({
          attr: root.getAttribute(ATTR),
          value: root.querySelector('input').value,
        });
      }}
    >
      <FloatingSearch />
    </Profiler>
  );
  return {
    root: container.querySelector('#guide-target-search'),
    input: screen.getByPlaceholderText('Search graph...'),
    user: userEvent.setup(),
    commits,
  };
}

// The query effect also clears the attribute once the input is empty, one commit later,
// so only a per-commit check can tell whether the pick itself cleared it.
function expectNoCommitWithEmptyInputAndAttribute(commits, fromIndex) {
  const offending = commits.slice(fromIndex).filter((c) => c.value === '' && c.attr !== null);
  expect(offending).toEqual([]);
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

  it('names the searched query, not the live input, while a newer search is pending', async () => {
    const first = deferred();
    const second = deferred();
    api.searchGraph.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const { root, input, user } = renderSearch();

    await user.type(input, 'al');
    await waitFor(() => expect(api.searchGraph).toHaveBeenCalledWith('al', expect.anything()));
    first.resolve({ nodes: [actor], edges: [] });
    await waitFor(() => expect(root).toHaveAttribute(ATTR, 'al'));

    await user.type(input, 'p');
    expect(input).toHaveValue('alp');
    await waitFor(() => expect(api.searchGraph).toHaveBeenCalledWith('alp', expect.anything()));
    expect(screen.getByText('Alpha actor')).toBeInTheDocument();
    expect(root).toHaveAttribute(ATTR, 'al');

    second.resolve({ nodes: [], edges: [] });
    await waitFor(() => expect(root).toHaveAttribute(ATTR, 'alp'));
  });

  it('discards a search that settles after the query moved on', async () => {
    const first = deferred();
    const second = deferred();
    api.searchGraph.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const { root, input, user } = renderSearch();

    await user.type(input, 'al');
    await waitFor(() => expect(api.searchGraph).toHaveBeenCalledWith('al', expect.anything()));
    await user.type(input, 'p');
    await waitFor(() => expect(api.searchGraph).toHaveBeenCalledWith('alp', expect.anything()));

    await act(async () => {
      first.resolve({ nodes: [actor], edges: [] });
      await first.promise;
    });
    expect(screen.queryByText('Alpha actor')).not.toBeInTheDocument();
    expect(root).not.toHaveAttribute(ATTR);

    second.resolve({ nodes: [], edges: [] });
    await waitFor(() => expect(root).toHaveAttribute(ATTR, 'alp'));
    expect(screen.queryByText('Alpha actor')).not.toBeInTheDocument();
  });

  it('discards a search that settles after the input was emptied', async () => {
    const search = deferred();
    api.searchGraph.mockReturnValueOnce(search.promise);
    const { root, input, user } = renderSearch();

    await user.type(input, 'al');
    await waitFor(() => expect(api.searchGraph).toHaveBeenCalledWith('al', expect.anything()));
    await user.clear(input);

    await act(async () => {
      search.resolve({ nodes: [actor], edges: [] });
      await search.promise;
    });
    expect(screen.queryByText('Alpha actor')).not.toBeInTheDocument();
    expect(root).not.toHaveAttribute(ATTR);
    expect(root.querySelector('.floating-search-spinner')).toBeNull();

    // Re-focusing must not resurrect the discarded results either.
    await user.click(input);
    expect(screen.queryByText('Alpha actor')).not.toBeInTheDocument();
  });

  it('discards a search that rejects after the input was emptied', async () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const search = deferred();
    api.searchGraph.mockReturnValueOnce(search.promise);
    const { root, input, user } = renderSearch();

    await user.type(input, 'zz');
    await waitFor(() => expect(api.searchGraph).toHaveBeenCalledWith('zz', expect.anything()));
    await user.clear(input);

    await act(async () => {
      search.reject(new Error('network down'));
      await search.promise.catch(() => {});
    });
    expect(root).not.toHaveAttribute(ATTR);
    expect(errorSpy).not.toHaveBeenCalledWith('Search error:', expect.anything());
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
    const first = deferred();
    const second = deferred();
    api.searchGraph.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const { root, input, user, commits } = renderSearch();

    await user.type(input, 'al');
    first.resolve({ nodes: [savedView], edges: [] });
    await waitFor(() => expect(root).toHaveAttribute(ATTR, 'al'));
    // 'al' results stay rendered while the 'alp' search is pending: they outlive their query.
    await user.type(input, 'p');
    await waitFor(() => expect(api.searchGraph).toHaveBeenCalledWith('alp', expect.anything()));
    expect(root).toHaveAttribute(ATTR, 'al');

    const pickedAt = commits.length;
    await user.click(screen.getByText('Alpha view'));
    await waitFor(() => expect(input).toHaveValue(''));
    expectNoCommitWithEmptyInputAndAttribute(commits, pickedAt);
    expect(root).not.toHaveAttribute(ATTR);

    await act(async () => {
      second.resolve({ nodes: [savedView], edges: [] });
      await second.promise;
    });
    expect(root).not.toHaveAttribute(ATTR);
    expect(screen.queryByText('Alpha view')).not.toBeInTheDocument();
  });

  it('is removed in the same commit that a node pick empties the input', async () => {
    api.searchGraph.mockResolvedValueOnce({ nodes: [actor], edges: [] });
    const { root, input, user, commits } = renderSearch();

    await user.type(input, 'al');
    await waitFor(() => expect(root).toHaveAttribute(ATTR, 'al'));

    const pickedAt = commits.length;
    await user.click(screen.getByText('Alpha actor'));
    await waitFor(() => expect(input).toHaveValue(''));
    expectNoCommitWithEmptyInputAndAttribute(commits, pickedAt);
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
