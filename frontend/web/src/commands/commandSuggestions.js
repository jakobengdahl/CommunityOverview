import { useEffect, useMemo, useState } from 'react';
import * as api from '../services/api';
import { useI18n } from '../i18n';
import useGraphStore from '../store/graphStore';
import { useCommandRegistry, useCommandRegistryVersion } from './commandRegistry';

// docs/COMMAND_BOX_DESIGN.md, "Suggestion lifecycle and ranking".
export const MAX_SUGGESTIONS = 8;
export const NODE_QUERY_DEBOUNCE_MS = 150;
const MIN_PLAIN_QUERY_CHARS = 2;
const NO_TEMPLATES = [];

const TIER_EXACT = 0;
const TIER_PREFIX = 1;
const TIER_WORD_PREFIX = 2;
const TIER_SUBSTRING = 3;
const NO_MATCH = -1;

// Nodes, node types, then templates within a tier. Commands have their own set.
const KIND_ORDER = { node: 0, nodeType: 1, template: 2, command: 0 };

/**
 * Splits input into the command box's modes: `command` when the first
 * non-whitespace character is `/` (bare `/` lists every command), `plain` from
 * two non-whitespace characters, otherwise `idle`.
 */
export function parseCommandInput(text) {
  const value = text || '';
  const leading = value.trimStart();
  if (leading.startsWith('/')) {
    return { mode: 'command', query: leading.slice(1).trim() };
  }
  if (value.replace(/\s/g, '').length >= MIN_PLAIN_QUERY_CHARS) {
    return { mode: 'plain', query: value.trim() };
  }
  return { mode: 'idle', query: '' };
}

function textTier(text, query, locale) {
  const haystack = String(text || '').toLocaleLowerCase(locale);
  if (!haystack) return NO_MATCH;
  if (haystack === query) return TIER_EXACT;
  if (haystack.startsWith(query)) return TIER_PREFIX;
  let index = haystack.indexOf(query);
  if (index < 0) return NO_MATCH;
  // A word prefix is an occurrence starting at a word boundary; the query may
  // itself span several words or punctuation ('goal 2030', 'e-tj').
  while (index >= 0) {
    if (!/[\p{L}\p{N}]$/u.test(haystack.slice(0, index))) return TIER_WORD_PREFIX;
    index = haystack.indexOf(query, index + 1);
  }
  return TIER_SUBSTRING;
}

/** Best tier of the label and aliases for `query`, or -1 for no match. */
export function matchTier(label, aliases, query, locale) {
  const needle = query.toLocaleLowerCase(locale);
  let best = NO_MATCH;
  for (const text of [label, ...(aliases || [])]) {
    const tier = textTier(text, needle, locale);
    if (tier !== NO_MATCH && (best === NO_MATCH || tier < best)) best = tier;
  }
  return best;
}

/**
 * Ranks `{ kind, id, label, aliases? }` items for `query`: match tier, then
 * nodes before node types before templates, then locale-aware label, then id. An empty query
 * keeps every item in label/id order (bare `/`). No fuzzy match, no recency.
 */
export function rankSuggestions(items, query, locale, limit = MAX_SUGGESTIONS) {
  const collator = new Intl.Collator(locale);
  return items
    .map((item) => ({
      item,
      tier: query ? matchTier(item.label, item.aliases, query, locale) : TIER_EXACT,
    }))
    .filter(({ tier }) => tier !== NO_MATCH)
    .sort(
      (a, b) =>
        a.tier - b.tier ||
        KIND_ORDER[a.item.kind] - KIND_ORDER[b.item.kind] ||
        collator.compare(a.item.label, b.item.label) ||
        (a.item.id < b.item.id ? -1 : a.item.id > b.item.id ? 1 : 0)
    )
    .slice(0, limit)
    .map(({ item }) => item);
}

/** A command's translated label and aliases, as ranking reads them. */
export function commandSuggestion(command, t) {
  return {
    kind: 'command',
    id: command.id,
    label: t(command.labelKey, command.labelParams),
    aliases: [...(command.aliasKeys || []).map((key) => t(key)), ...(command.aliases || [])],
    command,
  };
}

/**
 * The graph search FloatingSearch runs, minus the types it never lists. Shared
 * so the search bar and the command box offer the same nodes for a query.
 */
export async function searchVisibleNodes(query, { limit = 10, federationDepth } = {}) {
  const result = await api.searchGraph(query, { limit, federationDepth });
  return (result.nodes || []).filter((n) => n.type !== 'Community');
}

/**
 * Suggestions for the command box's current `input`. Slash mode offers the
 * registry's available commands only, filtered immediately; plain mode offers
 * node types (`nodeTypes`: `{ id, label }`), nodes and readable templates once
 * the debounced searches settle, never commands. A superseded search's result is discarded.
 */
export function useCommandSuggestions(input, context, nodeTypes) {
  const { t } = useI18n();
  const registry = useCommandRegistry();
  const registryVersion = useCommandRegistryVersion(registry);
  const federationDepth = useGraphStore((s) => s.federationDepth);
  const { mode, query } = parseCommandInput(input);
  const enabled = registry && context.view === 'interactive' && mode !== 'idle';
  // A fresh identity also prevents a previous visit to the same query from resurfacing.
  const request = useMemo(() => (enabled ? {} : null), [enabled, mode, query, federationDepth]); // eslint-disable-line react-hooks/exhaustive-deps
  const [search, setSearch] = useState({
    request: null,
    nodes: [],
    failed: false,
  });
  const [templateSearch, setTemplateSearch] = useState({ request: null, templates: [] });

  useEffect(() => {
    if (!request) return undefined;
    let cancelled = false;
    const timer = setTimeout(
      () => {
        api.listTemplates().then(
          (result) =>
            !cancelled && setTemplateSearch({ request, templates: result.templates || [] }),
          () => !cancelled && setTemplateSearch({ request, templates: [] })
        );
        if (mode === 'plain') {
          searchVisibleNodes(query, { federationDepth }).then(
            (nodes) => !cancelled && setSearch({ request, nodes, failed: false }),
            () => !cancelled && setSearch({ request, nodes: [], failed: true })
          );
        }
      },
      mode === 'plain' ? NODE_QUERY_DEBOUNCE_MS : 0
    );
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [request, mode, query, federationDepth]);

  const templates = templateSearch.request === request ? templateSearch.templates : NO_TEMPLATES;
  useEffect(() => {
    if (!registry || mode !== 'command' || !request) return undefined;
    const cleanups = templates.map((template) =>
      registry.register({
        id: `template:insert:${template.template_id}`,
        labelKey: 'template.insert_command',
        labelParams: { name: template.name },
        aliases: ['template', 'mall', template.name],
        isAvailable: (ctx) => ctx.view === 'interactive',
        execute: (ctx) => {
          const insert = registry.get('template:insert');
          if (!insert) throw new Error('Template insertion unavailable');
          return insert.execute(ctx, template);
        },
      })
    );
    return () => cleanups.forEach((cleanup) => cleanup());
  }, [registry, mode, request, templates]);

  const { locale } = context;
  const commandItems = useMemo(() => {
    if (!registry || mode !== 'command') return [];
    const items = registry
      .list(context)
      .filter(
        (command) =>
          !command.id.startsWith('template:insert:') ||
          templates.some((template) => command.id === `template:insert:${template.template_id}`)
      )
      .map((command) => commandSuggestion(command, t));
    const listAllTemplates = templates.length && ['mall', 'template'].includes(query.toLowerCase());
    return rankSuggestions(items, query, locale, listAllTemplates ? Infinity : MAX_SUGGESTIONS);
    // registryVersion: a newly registered command must show without an edit.
  }, [registry, registryVersion, mode, query, context, locale, t, templates]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!registry || mode === 'idle') return { mode, items: [], status: 'idle' };
  if (mode === 'command') return { mode, items: commandItems, status: 'ready' };

  // Node types wait for the same debounced search as nodes, so the list
  // appears once and never reorders when the nodes arrive.
  const current = search.request === request;
  if (!current) return { mode, items: [], status: 'loading' };
  const typeItems = (nodeTypes || []).map((type) => ({ kind: 'nodeType', ...type }));
  const nodeItems = search.nodes.map((node) => ({
    kind: 'node',
    id: node.id,
    label: node.name,
    node,
  }));
  const templateItems = templates.map((template) => ({
    kind: 'template',
    id: template.template_id,
    label: template.name,
    template,
  }));
  const status = search.failed ? 'error' : 'ready';
  return {
    mode,
    items: rankSuggestions([...nodeItems, ...typeItems, ...templateItems], query, locale),
    status,
  };
}
