import { useState, useCallback, useEffect } from 'react';
import { GraphCanvas } from '@community-graph/ui-graph-canvas';
import '@community-graph/ui-graph-canvas/styles';
import * as mcp from './mcpClient';

/**
 * Graph Widget - Embeddable graph visualization using MCP tools
 *
 * Props:
 * - initialQuery: Optional initial search query
 * - onNodeSelect: Callback when a node is selected
 */
function Widget({ initialQuery = '', onNodeSelect }) {
  const [nodes, setNodes] = useState([]);
  const [edges, setEdges] = useState([]);
  const [highlightedNodeIds, setHighlightedNodeIds] = useState([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState(null);
  const [mcpAvailable, setMcpAvailable] = useState(false);

  // Check MCP availability on mount
  useEffect(() => {
    setMcpAvailable(mcp.isMCPAvailable());
  }, []);

  // Run initial query if provided
  useEffect(() => {
    if (initialQuery && mcpAvailable) {
      handleSearch(initialQuery);
    }
  }, [initialQuery, mcpAvailable]);

  const handleSearch = useCallback(async (query) => {
    if (!query.trim()) return;

    setIsLoading(true);
    setError(null);

    try {
      const result = await mcp.searchGraph(query);
      if (result.nodes) {
        setNodes(result.nodes);
        setEdges(result.edges || []);
        setHighlightedNodeIds(result.nodes.map(n => n.id));
        setTimeout(() => setHighlightedNodeIds([]), 3000);
      }
    } catch (err) {
      console.error('Search failed:', err);
      setError(err.message);
    } finally {
      setIsLoading(false);
    }
  }, []);

  const handleExpand = useCallback(async (nodeId, nodeData) => {
    setIsLoading(true);
    try {
      const result = await mcp.getRelatedNodes(nodeId);
      if (result.nodes) {
        const existingIds = new Set(nodes.map(n => n.id));
        const newNodes = result.nodes.filter(n => !existingIds.has(n.id));

        const existingEdgeIds = new Set(edges.map(e => e.id));
        const newEdges = (result.edges || []).filter(e => !existingEdgeIds.has(e.id));

        setNodes([...nodes, ...newNodes]);
        setEdges([...edges, ...newEdges]);
        setHighlightedNodeIds(newNodes.map(n => n.id));
        setTimeout(() => setHighlightedNodeIds([]), 3000);
      }
    } catch (err) {
      console.error('Expand failed:', err);
      setError(err.message);
    } finally {
      setIsLoading(false);
    }
  }, [nodes, edges]);

  const handleEdit = useCallback((nodeId, nodeData) => {
    if (onNodeSelect) {
      onNodeSelect(nodeId, nodeData);
    }
  }, [onNodeSelect]);

  // Show message if MCP not available
  if (!mcpAvailable) {
    return (
      <div className="widget-container">
        <div className="widget-error">
          <h3>MCP Not Available</h3>
          <p>
            This widget requires MCP tools to be available via{' '}
            <code>window.openai.callTool</code>.
          </p>
          <p>
            Please ensure you're running this widget in a compatible environment.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="widget-container">
      <div className="widget-header">
        <SearchBar onSearch={handleSearch} isLoading={isLoading} />
      </div>

      {error && (
        <div className="widget-error-banner">
          {error}
          <button onClick={() => setError(null)}>×</button>
        </div>
      )}

      <div className="widget-canvas">
        <GraphCanvas
          nodes={nodes}
          edges={edges}
          highlightedNodeIds={highlightedNodeIds}
          hiddenNodeIds={[]}
          onExpand={handleExpand}
          onEdit={handleEdit}
        />
      </div>

      {isLoading && (
        <div className="widget-loading">
          <span>Loading...</span>
        </div>
      )}
    </div>
  );
}

function SearchBar({ onSearch, isLoading }) {
  const [query, setQuery] = useState('');

  const handleSubmit = (e) => {
    e.preventDefault();
    onSearch(query);
  };

  return (
    <form className="widget-search" onSubmit={handleSubmit}>
      <input
        type="text"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search graph..."
        disabled={isLoading}
      />
      <button type="submit" disabled={isLoading || !query.trim()}>
        Search
      </button>
    </form>
  );
}

export default Widget;
