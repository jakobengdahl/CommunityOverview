import { useState, useEffect } from 'react';
import useGraphStore from '../store/graphStore';
import { executeTool } from '../services/api';
// import { DEMO_GRAPH_DATA } from '../services/demoData'; // REMOVED
import './StatsPanel.css';

function StatsPanel() {
  const { nodes, edges, selectedCommunities } = useGraphStore();
  const [isExpanded, setIsExpanded] = useState(false);
  const [backendStats, setBackendStats] = useState(null);
  const [isLoadingStats, setIsLoadingStats] = useState(false);
  const [statsError, setStatsError] = useState(null);

  // Fetch backend statistics when component mounts or communities change
  useEffect(() => {
    const fetchBackendStats = async () => {
      setIsLoadingStats(true);
      setStatsError(null);

      try {
        const args = selectedCommunities.length > 0
          ? { communities: selectedCommunities }
          : {};

        const result = await executeTool('get_graph_stats', args);
        setBackendStats(result);
      } catch (error) {
        console.error('Error fetching backend stats:', error);
        setStatsError(error.message);
      } finally {
        setIsLoadingStats(false);
      }
    };

    // Only fetch if panel is expanded or on first mount
    if (isExpanded || backendStats === null) {
      fetchBackendStats();
    }
  }, [selectedCommunities, isExpanded]);

  // Calculate statistics
  const calculateStats = () => {
    // Filter by selected communities
    let relevantNodes = nodes;
    if (selectedCommunities.length > 0) {
      relevantNodes = nodes.filter(node =>
        node.communities.some(comm => selectedCommunities.includes(comm))
      );
    }

    // Count nodes by type
    const nodesByType = {};
    relevantNodes.forEach(node => {
      nodesByType[node.type] = (nodesByType[node.type] || 0) + 1;
    });

    // Count nodes by community
    const nodesByCommunity = {};
    relevantNodes.forEach(node => {
      node.communities.forEach(comm => {
        nodesByCommunity[comm] = (nodesByCommunity[comm] || 0) + 1;
      });
    });

    return {
      totalNodes: relevantNodes.length,
      totalEdges: edges.length,
      nodesByType,
      nodesByCommunity,
      totalInDatabase: backendStats?.total_nodes || 0,
      totalEdgesInDatabase: backendStats?.total_edges || 0,
      backendNodesByType: backendStats?.nodes_by_type || {},
      backendNodesByCommunity: backendStats?.nodes_by_community || {}
    };
  };

  const stats = calculateStats();

  return (
    <div className="stats-panel">
      <button
        className="stats-toggle"
        onClick={() => setIsExpanded(!isExpanded)}
      >
        📊 Graph Stats {isExpanded ? '▼' : '▶'}
      </button>

      {isExpanded && (
        <div className="stats-content">
          {isLoadingStats && (
            <div className="stats-loading">Loading statistics...</div>
          )}

          {statsError && (
            <div className="stats-error">Error: {statsError}</div>
          )}

          <div className="stats-section">
            <h4>Overview</h4>
            <div className="stat-item">
              <span className="stat-label">Displayed nodes:</span>
              <span className="stat-value">{stats.totalNodes}</span>
            </div>
            <div className="stat-item">
              <span className="stat-label">Total in database:</span>
              <span className="stat-value">
                {isLoadingStats ? '...' : stats.totalInDatabase}
              </span>
            </div>
            <div className="stat-item">
              <span className="stat-label">Displayed edges:</span>
              <span className="stat-value">{stats.totalEdges}</span>
            </div>
            <div className="stat-item">
              <span className="stat-label">Total edges:</span>
              <span className="stat-value">
                {isLoadingStats ? '...' : stats.totalEdgesInDatabase}
              </span>
            </div>
          </div>

          {(Object.keys(stats.nodesByType).length > 0 || Object.keys(stats.backendNodesByType).length > 0) && (
            <div className="stats-section">
              <h4>Nodes by type</h4>
              {Object.keys(stats.nodesByType).length > 0 ? (
                Object.entries(stats.nodesByType)
                  .sort((a, b) => b[1] - a[1])
                  .map(([type, count]) => {
                    const totalInDb = stats.backendNodesByType[type] || 0;
                    return (
                      <div key={type} className="stat-item">
                        <span className="stat-label">{type}:</span>
                        <span className="stat-value">
                          {count}
                          {totalInDb > 0 && totalInDb !== count && (
                            <span className="stat-total"> / {totalInDb}</span>
                          )}
                        </span>
                      </div>
                    );
                  })
              ) : (
                <div className="stat-item">
                  <span className="stat-label">No nodes loaded yet</span>
                </div>
              )}
            </div>
          )}

          {(Object.keys(stats.nodesByCommunity).length > 0 || Object.keys(stats.backendNodesByCommunity).length > 0) && (
            <div className="stats-section">
              <h4>Nodes by community</h4>
              {Object.keys(stats.nodesByCommunity).length > 0 ? (
                Object.entries(stats.nodesByCommunity)
                  .sort((a, b) => b[1] - a[1])
                  .map(([community, count]) => {
                    const totalInDb = stats.backendNodesByCommunity[community] || 0;
                    return (
                      <div key={community} className="stat-item">
                        <span className="stat-label">{community}:</span>
                        <span className="stat-value">
                          {count}
                          {totalInDb > 0 && totalInDb !== count && (
                            <span className="stat-total"> / {totalInDb}</span>
                          )}
                        </span>
                      </div>
                    );
                  })
              ) : backendStats && Object.keys(stats.backendNodesByCommunity).length > 0 ? (
                Object.entries(stats.backendNodesByCommunity)
                  .sort((a, b) => b[1] - a[1])
                  .map(([community, count]) => (
                    <div key={community} className="stat-item">
                      <span className="stat-label">{community}:</span>
                      <span className="stat-value">{count} in database</span>
                    </div>
                  ))
              ) : (
                <div className="stat-item">
                  <span className="stat-label">No communities yet</span>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default StatsPanel;
