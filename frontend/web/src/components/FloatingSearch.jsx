import { useState, useEffect, useRef, useCallback } from 'react';
import { Search } from 'react-bootstrap-icons';
import useGraphStore from '../store/graphStore';
import { ICON_MAP, COLOR_MAP } from './FloatingToolbar';
import * as api from '../services/api';
import './FloatingSearch.css';

function FloatingSearch() {
  const {
    nodes: vizNodes,
    hiddenNodeIds,
    addNodesToVisualization,
    clearVisualization,
    setFocusNodeId,
  } = useGraphStore();

  const [query, setQuery] = useState('');
  const [results, setResults] = useState([]);
  const [isLoading, setIsLoading] = useState(false);
  const [showDropdown, setShowDropdown] = useState(false);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const inputRef = useRef(null);
  const containerRef = useRef(null);
  const debounceRef = useRef(null);

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
        const result = await api.searchGraph(query, { limit: 10 });
        const nodes = (result.nodes || []).filter(
          n => n.type !== 'Community' && n.type !== 'VisualizationView'
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
  }, [query]);

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

  const selectResult = useCallback(async (node) => {
    // SavedView: clear canvas and load the saved view's nodes
    if (node.type === 'SavedView') {
      try {
        const nodeIds = node.metadata?.node_ids || [];
        if (nodeIds.length > 0) {
          clearVisualization();
          const details = await Promise.all(
            nodeIds.map(id => api.getNodeDetails(id).catch(() => null))
          );
          const loadedNodes = details.filter(d => d?.success).map(d => d.node);
          if (loadedNodes.length > 0) {
            addNodesToVisualization(loadedNodes, []);
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

    const existsInViz = vizNodes.some(n => n.id === node.id);
    const isHidden = hiddenNodeIds.includes(node.id);

    if (existsInViz && !isHidden) {
      setFocusNodeId(node.id);
    } else if (existsInViz && isHidden) {
      const { toggleNodeVisibility } = useGraphStore.getState();
      toggleNodeVisibility(node.id);
      setTimeout(() => setFocusNodeId(node.id), 100);
    } else {
      addNodesToVisualization([node], []);
      setTimeout(() => setFocusNodeId(node.id), 100);
    }

    setQuery('');
    setResults([]);
    setShowDropdown(false);
  }, [vizNodes, hiddenNodeIds, addNodesToVisualization, clearVisualization, setFocusNodeId]);

  const handleKeyDown = (e) => {
    if (!showDropdown) return;

    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setSelectedIndex(prev => Math.min(prev + 1, results.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setSelectedIndex(prev => Math.max(prev - 1, 0));
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
    <div className="floating-search" ref={containerRef}>
      <div className="floating-search-bar">
        <Search size={16} className="floating-search-icon" />
        <input
          ref={inputRef}
          type="text"
          className="floating-search-input"
          placeholder="Search graph..."
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={handleKeyDown}
          onFocus={() => {
            if (results.length > 0) setShowDropdown(true);
          }}
        />
        {isLoading && <div className="floating-search-spinner" />}
      </div>

      {showDropdown && results.length > 0 && (
        <div className="floating-search-dropdown">
          {results.map((node, index) => {
            const Icon = ICON_MAP[node.type];
            const color = COLOR_MAP[node.type] || '#9CA3AF';
            const isInViz = vizNodes.some(n => n.id === node.id) && !hiddenNodeIds.includes(node.id);

            return (
              <button
                key={node.id}
                className={`floating-search-result ${index === selectedIndex ? 'selected' : ''}`}
                onClick={() => selectResult(node)}
                onMouseEnter={() => setSelectedIndex(index)}
              >
                <span
                  className="floating-search-result-dot"
                  style={{ backgroundColor: color }}
                />
                {Icon && <Icon size={14} style={{ color, flexShrink: 0 }} />}
                <span className="floating-search-result-name">{node.name}</span>
                <span
                  className="floating-search-result-type"
                  style={{ color }}
                >
                  {node.type}
                </span>
                {isInViz && (
                  <span className="floating-search-result-badge">in view</span>
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default FloatingSearch;
