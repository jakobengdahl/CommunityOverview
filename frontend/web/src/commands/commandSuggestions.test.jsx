import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import * as api from '../services/api';
import { CommandRegistryContext, createCommandRegistry } from './commandRegistry';
import {
  MAX_SUGGESTIONS,
  NODE_QUERY_DEBOUNCE_MS,
  parseCommandInput,
  rankSuggestions,
  useCommandSuggestions,
} from './commandSuggestions';

vi.mock('../services/api', () => ({ searchGraph: vi.fn() }));
vi.mock('../i18n', () => ({ useI18n: () => ({ t: (key) => key }) }));
vi.mock('../store/graphStore', () => ({
  default: (selector) => selector({ federationDepth: 1 }),
}));

const ids = (items) => items.map((item) => `${item.kind}:${item.id}`);

describe('parseCommandInput', () => {
  it.each([
    ['  /new', { mode: 'command', query: 'new' }],
    ['/', { mode: 'command', query: '' }],
    ['a / b', { mode: 'plain', query: 'a / b' }],
    [' a ', { mode: 'idle', query: '' }],
    ['ab', { mode: 'plain', query: 'ab' }],
  ])('%j parses as %j', (input, expected) => {
    expect(parseCommandInput(input)).toEqual(expected);
  });
});

describe('rankSuggestions', () => {
  it('ranks by match tier first, so a weak node match never beats a stronger type match', () => {
    const items = [
      { kind: 'node', id: 'n1', label: 'Strategic goal' }, // word prefix
      { kind: 'node', id: 'n2', label: 'Subgoals' }, // substring
      { kind: 'nodeType', id: 'Goal', label: 'Goal' }, // exact
      { kind: 'node', id: 'n3', label: 'Goals 2030' }, // prefix
      { kind: 'node', id: 'n4', label: 'GOAL' }, // exact, case-insensitive
      { kind: 'node', id: 'n5', label: 'Gaol' }, // no fuzzy match
    ];
    expect(ids(rankSuggestions(items, 'goal', 'en'))).toEqual([
      'node:n4',
      'nodeType:Goal',
      'node:n3',
      'node:n1',
      'node:n2',
    ]);
  });

  it('matches aliases, breaks ties by locale-aware label then id, and caps the list', () => {
    const alias = {
      kind: 'command',
      id: 'sub',
      label: 'Create subscription',
      aliases: ['Webhook'],
    };
    expect(ids(rankSuggestions([alias], 'webhook', 'en'))).toEqual(['command:sub']);

    const sv = [
      { kind: 'node', id: 'b', label: 'Ö' },
      { kind: 'node', id: 'a', label: 'Ä' },
      { kind: 'node', id: 'c', label: 'Ä' },
    ];
    expect(ids(rankSuggestions(sv, '', 'sv'))).toEqual(['node:a', 'node:c', 'node:b']);

    const many = Array.from({ length: 12 }, (_, i) => ({ kind: 'node', id: `n${i}`, label: 'x' }));
    expect(rankSuggestions(many, 'x', 'en')).toHaveLength(MAX_SUGGESTIONS);
  });
});

describe('useCommandSuggestions', () => {
  const context = { view: 'interactive', locale: 'en' };
  const nodeTypes = [{ id: 'Goal', label: 'Goal' }];
  let registry;
  const wrapper = ({ children }) => (
    <CommandRegistryContext.Provider value={registry}>{children}</CommandRegistryContext.Provider>
  );

  beforeEach(() => {
    vi.useFakeTimers();
    registry = createCommandRegistry();
    registry.register({
      id: 'goal-command',
      labelKey: 'Goal command',
      isAvailable: () => true,
      execute: async () => {},
    });
    api.searchGraph.mockReset();
  });
  afterEach(() => vi.useRealTimers());

  const settle = (ms) => act(async () => vi.advanceTimersByTimeAsync(ms));

  it('plain text yields debounced nodes and node types only; slash yields commands only', async () => {
    api.searchGraph.mockResolvedValue({
      nodes: [
        { id: 'n1', name: 'Goals 2030', type: 'Goal' },
        { id: 'c1', name: 'Goal community', type: 'Community' },
      ],
    });
    const { result, rerender } = renderHook(
      ({ input }) => useCommandSuggestions(input, context, nodeTypes),
      { wrapper, initialProps: { input: 'goal' } }
    );
    await settle(NODE_QUERY_DEBOUNCE_MS - 1);
    expect(api.searchGraph).not.toHaveBeenCalled();
    expect(result.current.status).toBe('loading');

    await settle(1);
    expect(api.searchGraph).toHaveBeenCalledWith('goal', { limit: 10, federationDepth: 1 });
    expect(ids(result.current.items)).toEqual(['nodeType:Goal', 'node:n1']);

    rerender({ input: '/goal' });
    expect(ids(result.current.items)).toEqual(['command:goal-command']);
    await settle(NODE_QUERY_DEBOUNCE_MS);
    expect(api.searchGraph).toHaveBeenCalledTimes(1);
  });

  it('discards a superseded search and offers nothing without a registry', async () => {
    let resolveFirst;
    api.searchGraph
      .mockImplementationOnce(() => new Promise((resolve) => (resolveFirst = resolve)))
      .mockResolvedValueOnce({ nodes: [{ id: 'n2', name: 'Goal B', type: 'Goal' }] });
    const { result, rerender } = renderHook(
      ({ input }) => useCommandSuggestions(input, context, nodeTypes),
      { wrapper, initialProps: { input: 'goal a' } }
    );
    await settle(NODE_QUERY_DEBOUNCE_MS);
    rerender({ input: 'goal b' });
    await act(async () => resolveFirst({ nodes: [{ id: 'n1', name: 'Goal A', type: 'Goal' }] }));
    expect(ids(result.current.items)).toEqual([]);

    await settle(NODE_QUERY_DEBOUNCE_MS);
    expect(ids(result.current.items)).toEqual(['node:n2']);

    registry = null;
    const kiosk = renderHook(() => useCommandSuggestions('/', context, nodeTypes), { wrapper });
    expect(kiosk.result.current.items).toEqual([]);
  });
});
