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
  // Bumped whenever the canvas contents are replaced wholesale, which
  // establishes a new position baseline; the canvas discards its undo history
  // on the change. Additive paths (expand) leave the existing nodes where they
  // are, so their recorded moves stay valid and must not bump it.
  // The signal is per replacement, not per node: a node that survives into the
  // next result keeps its position on the canvas, and its move is dropped from
  // the history along with the rest. That costs an undo the user could still
  // have used, which is the cheaper error than restoring a coordinate from a
  // layout that is gone.
  const [canvasBaselineEpoch, setCanvasBaselineEpoch] = useState(0);

  // Check MCP availability on mount, then show the result of the tool call
  // that opened the widget (show_graph_widget) once the host delivers it.
  useEffect(() => {
    const available = mcp.isMCPAvailable();
    setMcpAvailable(available);
    if (!available) return undefined;

    let disconnect = () => {};
    let active = true;
    mcp
      .connectHost({
        onToolResult: (payload) => {
          if (!active || !Array.isArray(payload?.nodes)) return;
          setNodes(payload.nodes);
          setEdges(payload.edges || []);
          setCanvasBaselineEpoch((epoch) => epoch + 1);
        },
      })
      .then((fn) => {
        if (active) disconnect = fn;
        else fn();
      })
      .catch((err) => {
        console.error('Host connection failed:', err);
        if (active) setError(err.message);
      });
    return () => {
      active = false;
      disconnect();
    };
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
        setCanvasBaselineEpoch((epoch) => epoch + 1);
        setHighlightedNodeIds(result.nodes.map((n) => n.id));
        setTimeout(() => setHighlightedNodeIds([]), 3000);
      }
    } catch (err) {
      console.error('Search failed:', err);
      setError(err.message);
    } finally {
      setIsLoading(false);
    }
  }, []);

  const handleExpand = useCallback(
    async (nodeId, nodeData) => {
      setIsLoading(true);
      try {
        const result = await mcp.getRelatedNodes(nodeId);
        if (result.nodes) {
          const existingIds = new Set(nodes.map((n) => n.id));
          const newNodes = result.nodes.filter((n) => !existingIds.has(n.id));

          const existingEdgeIds = new Set(edges.map((e) => e.id));
          const newEdges = (result.edges || []).filter((e) => !existingEdgeIds.has(e.id));

          setNodes([...nodes, ...newNodes]);
          setEdges([...edges, ...newEdges]);
          setHighlightedNodeIds(newNodes.map((n) => n.id));
          setTimeout(() => setHighlightedNodeIds([]), 3000);
        }
      } catch (err) {
        console.error('Expand failed:', err);
        setError(err.message);
      } finally {
        setIsLoading(false);
      }
    },
    [nodes, edges]
  );

  const handleEdit = useCallback(
    (nodeId, nodeData) => {
      if (onNodeSelect) {
        onNodeSelect(nodeId, nodeData);
      }
    },
    [onNodeSelect]
  );

  // Show message if MCP not available
  if (!mcpAvailable) {
    return (
      <div className="widget-container">
        <div className="widget-error">
          <h3>MCP Not Available</h3>
          <p>
            This widget runs inside an MCP Apps host (such as Claude or ChatGPT), or with{' '}
            <code>window.openai.callTool</code>.
          </p>
          <p>Open it from the show_graph_widget tool in a compatible chat client.</p>
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
          canvasBaselineEpoch={canvasBaselineEpoch}
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

  // Not a <form>: a host iframe sandboxed without allow-forms blocks form
  // submission outright, so the submit event would never fire.
  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !isLoading) {
      e.preventDefault();
      onSearch(query);
    }
  };

  return (
    <div className="widget-search" role="search">
      <input
        type="text"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="Search graph..."
        disabled={isLoading}
      />
      <button type="button" onClick={() => onSearch(query)} disabled={isLoading || !query.trim()}>
        Search
      </button>
    </div>
  );
}

export default Widget;
