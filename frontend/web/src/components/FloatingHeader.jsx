import { useState, useRef, useEffect } from 'react';
import { List, Feather, Download } from 'react-bootstrap-icons';
import useGraphStore from '../store/graphStore';
import { useI18n } from '../i18n';
import './FloatingHeader.css';

const NODE_TYPE_COLORS = {
  Actor: '#3B82F6',
  Community: '#A855F7',
  Initiative: '#10B981',
  Capability: '#F97316',
  Resource: '#FBBF24',
  Legislation: '#EF4444',
  Theme: '#14B8A6',
  Goal: '#6366F1',
  Event: '#D946EF',
  Agent: '#EC4899',
  EventSubscription: '#8B5CF6',
  SavedView: '#6B7280',
};

function FloatingHeader({ stats, title = 'Community Graph View', onExportGraph }) {
  const { t } = useI18n();
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef(null);

  useEffect(() => {
    const handleClickOutside = (e) => {
      if (menuRef.current && !menuRef.current.contains(e.target)) {
        setMenuOpen(false);
      }
    };
    if (menuOpen) {
      // Use capture phase to catch clicks before ReactFlow stops propagation
      document.addEventListener('mousedown', handleClickOutside, true);
    }
    return () => document.removeEventListener('mousedown', handleClickOutside, true);
  }, [menuOpen]);

  return (
    <div className="floating-header" ref={menuRef}>
      <div className="floating-header-bar">
        <Feather size={18} className="floating-header-app-icon" />
        <button
          className="floating-header-hamburger"
          onClick={() => setMenuOpen(!menuOpen)}
          title="Menu"
        >
          <List size={20} />
        </button>
        <span className="floating-header-title">{title}</span>
      </div>

      {menuOpen && (
        <div className="floating-header-dropdown">
          {stats ? (
            <>
              <div className="floating-header-stats-summary">
                <div className="floating-header-stat">
                  <span className="floating-header-stat-value">{stats.total_nodes || 0}</span>
                  <span className="floating-header-stat-label">Nodes</span>
                </div>
                <div className="floating-header-stat">
                  <span className="floating-header-stat-value">{stats.total_edges || 0}</span>
                  <span className="floating-header-stat-label">Edges</span>
                </div>
              </div>

              {stats.nodes_by_type && Object.keys(stats.nodes_by_type).length > 0 && (
                <div className="floating-header-type-list">
                  <div className="floating-header-section-title">Nodes by type</div>
                  {Object.entries(stats.nodes_by_type)
                    .sort(([, a], [, b]) => b - a)
                    .map(([type, count]) => (
                      <div key={type} className="floating-header-type-row">
                        <span
                          className="floating-header-type-dot"
                          style={{ backgroundColor: NODE_TYPE_COLORS[type] || '#9CA3AF' }}
                        />
                        <span className="floating-header-type-name">{type}</span>
                        <span className="floating-header-type-count">{count}</span>
                      </div>
                    ))}
                </div>
              )}

              <div className="floating-header-section-divider" />
              <div className="floating-header-section-title">Admin</div>
              <button
                className="floating-header-menu-item"
                onClick={() => {
                  onExportGraph?.();
                  setMenuOpen(false);
                }}
              >
                <Download size={14} />
                <span>{t('menu.export_graph')}</span>
              </button>
            </>
          ) : (
            <div className="floating-header-placeholder">Loading stats...</div>
          )}
        </div>
      )}
    </div>
  );
}

export default FloatingHeader;
