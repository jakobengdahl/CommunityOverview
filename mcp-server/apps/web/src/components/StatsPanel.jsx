import './StatsPanel.css';

function StatsPanel({ stats }) {
  if (!stats) return null;

  return (
    <div className="stats-panel">
      <div className="stat">
        <span className="stat-value">{stats.total_nodes || 0}</span>
        <span className="stat-label">Nodes</span>
      </div>
      <div className="stat">
        <span className="stat-value">{stats.total_edges || 0}</span>
        <span className="stat-label">Edges</span>
      </div>
      {stats.nodes_by_type && (
        <div className="stat-types">
          {Object.entries(stats.nodes_by_type).slice(0, 4).map(([type, count]) => (
            <span key={type} className="stat-type">
              {type}: {count}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

export default StatsPanel;
