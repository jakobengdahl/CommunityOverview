import { useState, useRef, useEffect } from 'react';
import { List, Feather, Download, Map, BoxArrowRight } from 'react-bootstrap-icons';
import useGraphStore from '../store/graphStore';
import { useI18n } from '../i18n';
import { COLOR_MAP } from './FloatingToolbar';
import './FloatingHeader.css';

function FloatingHeader({ stats, title = 'Community Graph View', onExportGraph }) {
  const { t } = useI18n();
  const { showMinimap, setShowMinimap } = useGraphStore();
  const [menuOpen, setMenuOpen] = useState(false);
  const [dropdownLeft, setDropdownLeft] = useState(null);
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

  // Calculate dropdown left position to avoid overlapping the toolbar
  useEffect(() => {
    if (menuOpen) {
      const toolbar = document.querySelector('.floating-toolbar');
      if (toolbar) {
        const rect = toolbar.getBoundingClientRect();
        setDropdownLeft(rect.right + 8);
      }
    }
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
        <div className="floating-header-dropdown" style={dropdownLeft ? { left: dropdownLeft } : undefined}>
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
                          style={{ backgroundColor: COLOR_MAP[type] || '#9CA3AF' }}
                        />
                        <span className="floating-header-type-name">{type}</span>
                        <span className="floating-header-type-count">{count}</span>
                      </div>
                    ))}
                </div>
              )}

              <div className="floating-header-section-divider" />
              <div className="floating-header-section-title">{t('menu.view_section') || 'View'}</div>
              <button
                className="floating-header-menu-item"
                onClick={() => setShowMinimap(!showMinimap)}
              >
                <Map size={14} />
                <span>{t('menu.show_minimap') || 'Show minimap'}</span>
                <span className={`floating-header-toggle${showMinimap ? ' active' : ''}`} />
              </button>

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

          {/* Logout is always shown — redirect is harmless regardless of
              AUTH_ENABLED. Backend routes /auth/logout and /logged-out are
              exempt from auth middleware so the user never gets stuck. */}
          <div className="floating-header-section-divider" />
          <button
            className="floating-header-menu-item floating-header-menu-item-logout"
            onClick={() => {
              setMenuOpen(false);
              window.location.href = '/auth/logout';
            }}
          >
            <BoxArrowRight size={14} />
            <span>{t('menu.logout')}</span>
          </button>
        </div>
      )}
    </div>
  );
}

export default FloatingHeader;
