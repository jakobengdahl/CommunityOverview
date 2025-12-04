import { useState } from 'react';
import useGraphStore from '../store/graphStore';
// import { DEMO_GRAPH_DATA } from '../services/demoData'; // REMOVED
import './StatsPanel.css';

function StatsPanel() {
  const { nodes, edges, selectedCommunities } = useGraphStore();
  const [isExpanded, setIsExpanded] = useState(false);

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
      totalInDatabase: 0 // Placeholder until backend stats integration
    };
  };

  const stats = calculateStats();

  return (
    <div className="stats-panel">
      <button
        className="stats-toggle"
        onClick={() => setIsExpanded(!isExpanded)}
      >
        📊 Graf-statistik {isExpanded ? '▼' : '▶'}
      </button>

      {isExpanded && (
        <div className="stats-content">
          <div className="stats-section">
            <h4>Översikt</h4>
            <div className="stat-item">
              <span className="stat-label">Visade noder:</span>
              <span className="stat-value">{stats.totalNodes}</span>
            </div>
            <div className="stat-item">
              <span className="stat-label">Totalt i databasen:</span>
              <span className="stat-value">{stats.totalInDatabase}</span>
            </div>
            <div className="stat-item">
              <span className="stat-label">Kopplingar:</span>
              <span className="stat-value">{stats.totalEdges}</span>
            </div>
          </div>

          {Object.keys(stats.nodesByType).length > 0 && (
            <div className="stats-section">
              <h4>Noder per typ</h4>
              {Object.entries(stats.nodesByType)
                .sort((a, b) => b[1] - a[1])
                .map(([type, count]) => (
                  <div key={type} className="stat-item">
                    <span className="stat-label">{type}:</span>
                    <span className="stat-value">{count}</span>
                  </div>
                ))}
            </div>
          )}

          {Object.keys(stats.nodesByCommunity).length > 0 && (
            <div className="stats-section">
              <h4>Noder per community</h4>
              {Object.entries(stats.nodesByCommunity)
                .sort((a, b) => b[1] - a[1])
                .map(([community, count]) => (
                  <div key={community} className="stat-item">
                    <span className="stat-label">{community}:</span>
                    <span className="stat-value">{count}</span>
                  </div>
                ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default StatsPanel;
