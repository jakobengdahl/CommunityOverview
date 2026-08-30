import { useState, useEffect, useLayoutEffect, useRef, useCallback } from 'react';
import { Search } from 'react-bootstrap-icons';
import useGraphStore from '../store/graphStore';
import { resolveIcon, resolveColor } from './FloatingToolbar';
import * as api from '../services/api';
import './FloatingSearch.css';
import { useI18n } from '../i18n';
import { savedViewMetadataToCanvasMetadata } from '../utils/sessionAnnotations';

function FloatingSearch({ variant = 'floating' }) {
  const { t, language } = useI18n();
  const {
    nodes: vizNodes,
    hiddenNodeIds,
    addNodesToVisualization,
    clearVisualization,
    setFocusNodeId,
    setPendingGroups,
    setPendingAnnotations,
    federationDepth,
    stats,
    schema,
    guideSearchInput,
    clearGuideSearchInput,
    requestCloseMenus,
  } = useGraphStore();

  const [query, setQuery] = useState('');
  const [results, setResults] = useState([]);
  const [isLoading, setIsLoading] = useState(false);
  const [showDropdown, setShowDropdown] = useState(false);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const inputRef = useRef(null);
  const containerRef = useRef(null);
  const debounceRef = useRef(null);

  useLayoutEffect(() => {
    const search = containerRef.current;
    const header = document.querySelector('.floating-header');
    if (!search || !header) return;

    let frame;
    const safeAreaProbe = document.createElement('div');
    safeAreaProbe.style.cssText =
      'position:fixed;visibility:hidden;pointer-events:none;padding-left:env(safe-area-inset-left);padding-right:env(safe-area-inset-right)';
    document.body.appendChild(safeAreaProbe);

    const applyMeasurement = () => {
      const headerRect = header.getBoundingClientRect();
      const viewportWidth = document.documentElement.clientWidth || window.innerWidth;
      const probeStyle = getComputedStyle(safeAreaProbe);
      const safeLeft = Number.parseFloat(probeStyle.paddingLeft) || 0;
      const safeRight = Number.parseFloat(probeStyle.paddingRight) || 0;
      const edgeGap = 16;
      const chromeGap = 12;
      const maximumWidth = 400;
      const minimumUsableWidth = 240;
      const leftEdge = safeLeft + edgeGap;
      const rightEdge = viewportWidth - safeRight - edgeGap;
      const centeredLeft = (viewportWidth - maximumWidth) / 2;
      const left = Math.max(leftEdge, centeredLeft, headerRect.right + chromeGap);
      const width = Math.max(0, Math.min(maximumWidth, rightEdge - left));
      const stacked = viewportWidth <= 600 || width < minimumUsableWidth;

      const phoneStack = stacked && viewportWidth <= 600;
      const stackedWidth = phoneStack
        ? rightEdge - leftEdge
        : Math.min(maximumWidth, rightEdge - leftEdge);
      const stackedLeft = phoneStack
        ? leftEdge
        : Math.max(leftEdge, (viewportWidth - stackedWidth) / 2);

      search.dataset.stacked = String(stacked);
      search.style.setProperty(
        '--floating-search-left',
        `${Math.round(stacked ? stackedLeft : left)}px`
      );
      search.style.setProperty(
        '--floating-search-width',
        `${Math.round(stacked ? stackedWidth : width)}px`
      );
      search.style.setProperty('--floating-header-bottom', `${Math.ceil(headerRect.bottom)}px`);
    };
    const measure = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(applyMeasurement);
    };
    let positionTransitionActive = false;
    const measurePositionTransition = () => {
      if (!positionTransitionActive) return;
      applyMeasurement();
      frame = requestAnimationFrame(measurePositionTransition);
    };
    const handleTransitionRun = (event) => {
      if (event.propertyName !== 'left') return;
      positionTransitionActive = true;
      cancelAnimationFrame(frame);
      measurePositionTransition();
    };
    const handleTransitionEnd = (event) => {
      if (event.propertyName && event.propertyName !== 'left') return;
      positionTransitionActive = false;
      measure();
    };
    const handleTransitionCancel = (event) => {
      if (event.propertyName && event.propertyName !== 'left') return;
      positionTransitionActive = false;
      cancelAnimationFrame(frame);
    };

    const resizeObserver = new ResizeObserver(measure);
    resizeObserver.observe(header);
    const mutationObserver = new MutationObserver(measure);
    mutationObserver.observe(header, { childList: true, subtree: true, characterData: true });
    window.addEventListener('resize', measure);
    window.visualViewport?.addEventListener('resize', measure);
    header.addEventListener('transitionrun', handleTransitionRun);
    header.addEventListener('transitionend', handleTransitionEnd);
    header.addEventListener('transitioncancel', handleTransitionCancel);
    applyMeasurement();

    return () => {
      cancelAnimationFrame(frame);
      resizeObserver.disconnect();
      mutationObserver.disconnect();
      safeAreaProbe.remove();
      window.removeEventListener('resize', measure);
      window.visualViewport?.removeEventListener('resize', measure);
      positionTransitionActive = false;
      header.removeEventListener('transitionrun', handleTransitionRun);
      header.removeEventListener('transitionend', handleTransitionEnd);
      header.removeEventListener('transitioncancel', handleTransitionCancel);
    };
  }, []);

  const getTypeLabel = useCallback(
    (nodeType) => {
      const labels = schema?.node_types?.[nodeType]?.labels;
      if (labels && language && labels[language]) {
        return labels[language];
      }
      return nodeType;
    },
    [schema, language]
  );

  const graphDisplayNames = stats?.federation?.graph_display_names || {};
  const showGraphPrefix = Boolean(stats?.federation?.search_has_multiple_graphs);
  const effectiveMaxDepth = Math.max(1, stats?.federation?.max_selectable_depth || 1);

  const getResultLabel = useCallback(
    (node) => {
      if (!showGraphPrefix) {
        return node.name;
      }

      const originGraphId = node.metadata?.origin_graph_id;
      const originGraphName =
        node.metadata?.origin_graph_name ||
        (originGraphId ? graphDisplayNames[originGraphId] : null) ||
        graphDisplayNames.local ||
        'Local';

      return `${originGraphName}: ${node.name}`;
    },
    [graphDisplayNames, showGraphPrefix]
  );

  // Debounced search
  useEffect(() => {
    if (query.length < 2) {
      setResults([]);
      setShowDropdown(false);
      return;
    }

    clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(async () => {
      setIsLoading(true);
      try {
        const result = await api.searchGraph(query, { limit: 10, federationDepth });
        const nodes = (result.nodes || []).filter(
          (n) => n.type !== 'Community' && n.type !== 'VisualizationView'
        );
        setResults(nodes);
        setSelectedIndex(0);
        setShowDropdown(nodes.length > 0);
      } catch (err) {
        console.error('Search error:', err);
        setResults([]);
      } finally {
        setIsLoading(false);
      }
    }, 300);

    return () => clearTimeout(debounceRef.current);
  }, [query, federationDepth]);

  // Click outside to close
  useEffect(() => {
    const handleClickOutside = (e) => {
      if (containerRef.current && !containerRef.current.contains(e.target)) {
        setShowDropdown(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  // Global keyboard shortcut: / to focus search
  useEffect(() => {
    const handleGlobalKey = (e) => {
      if (e.key === '/' || (e.shiftKey && e.key === '7')) {
        const tag = e.target.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || e.target.isContentEditable) return;
        e.preventDefault();
        inputRef.current?.focus();
      }
    };
    document.addEventListener('keydown', handleGlobalKey);
    return () => document.removeEventListener('keydown', handleGlobalKey);
  }, []);

  // Guide: animate typing into search input
  useEffect(() => {
    if (!guideSearchInput) return;
    const { text, animated } = guideSearchInput;
    clearGuideSearchInput();

    if (!animated) {
      setQuery(text);
      return;
    }

    let i = 0;
    setQuery('');
    const interval = setInterval(() => {
      i++;
      setQuery(text.slice(0, i));
      if (i >= text.length) clearInterval(interval);
    }, 30);
    return () => clearInterval(interval);
  }, [guideSearchInput]); // eslint-disable-line react-hooks/exhaustive-deps

  const selectResult = useCallback(
    async (node) => {
      // SavedView: clear canvas and load the saved view's nodes with positions and edges
      if (node.type === 'SavedView') {
        try {
          const nodeIds = node.metadata?.node_ids || [];
          const positions = node.metadata?.positions || {};
          const savedEdges = node.metadata?.edges || [];
          const savedEdgeIds = new Set(node.metadata?.edge_ids || []);
          const savedViewAnnotations = savedViewMetadataToCanvasMetadata(node.metadata || {});
          if (nodeIds.length > 0) {
            clearVisualization();
            const details = await Promise.all(
              nodeIds.map((id) => api.getNodeDetails(id).catch(() => null))
            );
            const loadedNodes = details
              .filter((d) => d?.success)
              .map((d) => {
                const n = d.node;
                if (positions[n.id]) {
                  return { ...n, _savedPosition: positions[n.id] };
                }
                return n;
              });
            if (loadedNodes.length > 0) {
              let edgesToLoad = [];
              if (savedEdges.length > 0) {
                // Use saved edges directly
                edgesToLoad = savedEdges;
              } else {
                // Discover edges between loaded nodes
                const loadedIds = new Set(loadedNodes.map((n) => n.id));
                for (const d of details) {
                  if (d?.edges) {
                    const relevant = d.edges.filter(
                      (e) =>
                        loadedIds.has(e.source) &&
                        loadedIds.has(e.target) &&
                        (savedEdgeIds.size === 0 || savedEdgeIds.has(e.id))
                    );
                    edgesToLoad.push(...relevant);
                  }
                }
              }
              const edgeMap = new Map(edgesToLoad.map((e) => [e.id, e]));
              addNodesToVisualization(loadedNodes, Array.from(edgeMap.values()));

              // Restore groups if any were saved
              if (savedViewAnnotations.groups.length > 0) {
                setPendingGroups({
                  groups: savedViewAnnotations.groups,
                  parentIds: savedViewAnnotations.parentIds,
                });
              }
              if (savedViewAnnotations.annotations.length > 0) {
                setPendingAnnotations(savedViewAnnotations.annotations);
              }
            }
          }
        } catch (err) {
          console.error('Error loading saved view:', err);
        }
        setQuery('');
        setResults([]);
        setShowDropdown(false);
        return;
      }

      const existsInViz = vizNodes.some((n) => n.id === node.id);
      const isHidden = hiddenNodeIds.includes(node.id);

      if (existsInViz && !isHidden) {
        setFocusNodeId(node.id);
      } else if (existsInViz && isHidden) {
        const { toggleNodeVisibility } = useGraphStore.getState();
        toggleNodeVisibility(node.id);
        setTimeout(() => setFocusNodeId(node.id), 100);
      } else {
        // Add node and fetch edges connecting it to existing visualization nodes
        addNodesToVisualization([node], []);
        try {
          const related = await api.getRelatedNodes(node.id, { depth: 1 });
          if (related.edges && related.edges.length > 0) {
            const vizNodeIds = new Set(vizNodes.map((n) => n.id));
            vizNodeIds.add(node.id);
            const relevantEdges = related.edges.filter(
              (e) => vizNodeIds.has(e.source) && vizNodeIds.has(e.target)
            );
            if (relevantEdges.length > 0) {
              addNodesToVisualization([], relevantEdges);
            }
          }
        } catch (err) {
          console.error('Error loading edges for node:', err);
        }
        setTimeout(() => setFocusNodeId(node.id), 100);
      }

      setQuery('');
      setResults([]);
      setShowDropdown(false);
    },
    [
      vizNodes,
      hiddenNodeIds,
      addNodesToVisualization,
      clearVisualization,
      setFocusNodeId,
      setPendingGroups,
      setPendingAnnotations,
    ]
  );

  const handleKeyDown = (e) => {
    if (!showDropdown) return;

    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setSelectedIndex((prev) => Math.min(prev + 1, results.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setSelectedIndex((prev) => Math.max(prev - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (results[selectedIndex]) {
        selectResult(results[selectedIndex]);
      }
    } else if (e.key === 'Escape') {
      setShowDropdown(false);
      setQuery('');
      inputRef.current?.blur();
    }
  };

  return (
    <div
      className={`floating-search${variant === 'sheet' ? ' floating-search--sheet' : ''}`}
      id="guide-target-search"
      ref={containerRef}
    >
      <div className="floating-search-bar">
        <Search size={18} className="floating-search-icon" />
        <input
          ref={inputRef}
          type="text"
          className="floating-search-input"
          placeholder="Search graph..."
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={handleKeyDown}
          onFocus={() => {
            requestCloseMenus();
            if (results.length > 0) setShowDropdown(true);
          }}
        />
        {isLoading && <div className="floating-search-spinner" />}
      </div>
      {effectiveMaxDepth > 1 && (
        <div
          className="floating-search-depth-indicator"
          title={t('federation.depth_indicator_tooltip')}
        >
          {t('federation.depth_indicator', { current: federationDepth, max: effectiveMaxDepth })}
        </div>
      )}

      {showDropdown && results.length > 0 && (
        <div className="floating-search-dropdown">
          {results.map((node, index) => {
            const Icon = resolveIcon(node.type, schema);
            const color = resolveColor(node.type, schema);
            const isInViz =
              vizNodes.some((n) => n.id === node.id) && !hiddenNodeIds.includes(node.id);

            return (
              <button
                key={node.id}
                className={`floating-search-result ${index === selectedIndex ? 'selected' : ''}`}
                onClick={() => selectResult(node)}
                onMouseEnter={() => setSelectedIndex(index)}
              >
                <span className="floating-search-result-dot" style={{ backgroundColor: color }} />
                {Icon && <Icon size={14} style={{ color, flexShrink: 0 }} />}
                <span className="floating-search-result-name">{getResultLabel(node)}</span>
                <span className="floating-search-result-type" style={{ color }}>
                  {getTypeLabel(node.type)}
                </span>
                {isInViz && <span className="floating-search-result-badge">in view</span>}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default FloatingSearch;
