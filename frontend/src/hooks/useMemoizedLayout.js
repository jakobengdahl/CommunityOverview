import { useMemo } from 'react';

// Cache for computed layouts
const layoutCache = new Map();
const MAX_CACHE_SIZE = 50;

/**
 * Generate a cache key from nodes and edges
 */
function generateCacheKey(nodes, edges) {
  const nodeIds = nodes.map(n => n.id).sort().join(',');
  const edgeIds = edges.map(e => `${e.source}-${e.target}`).sort().join(',');
  return `${nodeIds}|${edgeIds}`;
}

/**
 * Evict oldest entries if cache is too large
 */
function evictOldEntries() {
  if (layoutCache.size > MAX_CACHE_SIZE) {
    const keysToDelete = Array.from(layoutCache.keys()).slice(0, layoutCache.size - MAX_CACHE_SIZE);
    keysToDelete.forEach(key => layoutCache.delete(key));
  }
}

/**
 * Hook to memoize layout calculations with caching
 * @param {Array} nodes - React Flow nodes
 * @param {Array} edges - React Flow edges
 * @param {Function} layoutFn - Layout calculation function
 * @param {Array} dependencies - Additional dependencies for the memo
 * @returns {Array} Layouted nodes
 */
export function useMemoizedLayout(nodes, edges, layoutFn, dependencies = []) {
  return useMemo(() => {
    if (!nodes || nodes.length === 0) {
      return [];
    }

    // Generate cache key
    const cacheKey = generateCacheKey(nodes, edges);

    // Check cache
    if (layoutCache.has(cacheKey)) {
      console.log('[Layout] Cache hit for', nodes.length, 'nodes');
      return layoutCache.get(cacheKey);
    }

    // Compute layout
    console.log('[Layout] Computing layout for', nodes.length, 'nodes');
    const layoutedNodes = layoutFn(nodes, edges);

    // Store in cache
    layoutCache.set(cacheKey, layoutedNodes);
    evictOldEntries();

    return layoutedNodes;
  }, [nodes, edges, layoutFn, ...dependencies]);
}

/**
 * Clear the layout cache (useful for testing or memory management)
 */
export function clearLayoutCache() {
  layoutCache.clear();
}
