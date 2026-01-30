import { useState, useCallback } from 'react';
import useGraphStore from '../store/graphStore';
import * as api from '../services/api';
import './SearchPanel.css';

function SearchPanel() {
  const [query, setQuery] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const { updateVisualization, addNodesToVisualization } = useGraphStore();

  const handleSearch = useCallback(async (e) => {
    e.preventDefault();
    if (!query.trim()) return;

    setIsLoading(true);
    try {
      const result = await api.searchGraph(query);
      if (result.nodes && result.nodes.length > 0) {
        updateVisualization(result.nodes, result.edges || [], result.nodes.map(n => n.id));
      }
    } catch (error) {
      console.error('Search error:', error);
    } finally {
      setIsLoading(false);
    }
  }, [query, updateVisualization]);

  const handleAddToGraph = useCallback(async () => {
    if (!query.trim()) return;

    setIsLoading(true);
    try {
      const result = await api.searchGraph(query);
      if (result.nodes && result.nodes.length > 0) {
        addNodesToVisualization(result.nodes, result.edges || []);
      }
    } catch (error) {
      console.error('Search error:', error);
    } finally {
      setIsLoading(false);
    }
  }, [query, addNodesToVisualization]);

  const handleExportAll = useCallback(async () => {
    setIsLoading(true);
    try {
      const result = await api.exportGraph();
      updateVisualization(result.nodes, result.edges, []);
    } catch (error) {
      console.error('Export error:', error);
    } finally {
      setIsLoading(false);
    }
  }, [updateVisualization]);

  return (
    <div className="search-panel">
      <h2>Search</h2>
      <form onSubmit={handleSearch}>
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search nodes..."
          disabled={isLoading}
        />
        <div className="search-buttons">
          <button type="submit" disabled={isLoading || !query.trim()}>
            {isLoading ? 'Searching...' : 'Search'}
          </button>
          <button
            type="button"
            onClick={handleAddToGraph}
            disabled={isLoading || !query.trim()}
            className="secondary"
          >
            Add to Graph
          </button>
        </div>
      </form>

      <div className="search-actions">
        <button onClick={handleExportAll} disabled={isLoading} className="export-button">
          📥 Load All Nodes
        </button>
      </div>
    </div>
  );
}

export default SearchPanel;
