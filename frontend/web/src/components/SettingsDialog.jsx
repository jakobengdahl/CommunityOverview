import { useState, useEffect } from 'react';
import { createPortal } from 'react-dom';
import { Download, Map, Eye, ChatDotsFill, BoxArrowRight, X } from 'react-bootstrap-icons';
import useGraphStore from '../store/graphStore';
import { useI18n } from '../i18n';
import { getDisplayName, setDisplayName } from '../services/api';
import { resolveColor } from './FloatingToolbar';
import NodeTypeStatsDialog from './NodeTypeStatsDialog';
import './SettingsDialog.css';

const MAX_INLINE_TYPES = 5;

/**
 * SettingsDialog — hosts the settings that previously lived directly in the
 * hamburger dropdown: graph stats, view options, language, admin actions and
 * logout. Opened from the session drawer.
 */
function SettingsDialog({ stats, onExportGraph, onClose }) {
  const { t, language, setLanguage } = useI18n();
  const {
    showMinimap,
    setShowMinimap,
    nodePreviewEnabled,
    setNodePreviewEnabled,
    chatPanelOpen,
    toggleChatPanel,
    resetChatPanelToDefault,
    schema,
  } = useGraphStore();
  const [statsDialogOpen, setStatsDialogOpen] = useState(false);
  const [displayName, setDisplayNameState] = useState(() => getDisplayName() || '');

  useEffect(() => {
    const handleKeyDown = (e) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        if (statsDialogOpen) {
          setStatsDialogOpen(false);
        } else {
          onClose();
        }
      }
    };
    document.addEventListener('keydown', handleKeyDown, true);
    return () => document.removeEventListener('keydown', handleKeyDown, true);
  }, [onClose, statsDialogOpen]);

  return (
    <div className="settings-dialog-overlay" onClick={onClose}>
      <div className="settings-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="settings-dialog-header">
          <h3>{t('settings.title')}</h3>
          <button
            className="settings-dialog-close"
            onClick={onClose}
            aria-label={t('settings.close')}
          >
            <X size={20} />
          </button>
        </div>

        <div className="settings-dialog-content">
          {stats && (
            <>
              <div className="settings-dialog-stats-summary">
                <div className="settings-dialog-stat">
                  <span className="settings-dialog-stat-value">{stats.total_nodes || 0}</span>
                  <span className="settings-dialog-stat-label">{t('settings.nodes')}</span>
                </div>
                <div className="settings-dialog-stat">
                  <span className="settings-dialog-stat-value">{stats.total_edges || 0}</span>
                  <span className="settings-dialog-stat-label">{t('settings.edges')}</span>
                </div>
              </div>

              {stats.nodes_by_type && Object.keys(stats.nodes_by_type).length > 0 && (
                <div className="settings-dialog-type-list">
                  <div className="settings-dialog-type-list-header">
                    <span className="settings-dialog-section-title">
                      {t('settings.nodes_by_type')}
                    </span>
                    {Object.keys(stats.nodes_by_type).length > MAX_INLINE_TYPES && (
                      <button
                        className="settings-dialog-type-details-btn"
                        aria-haspopup="dialog"
                        onClick={() => setStatsDialogOpen(true)}
                      >
                        {t('settings.details')}
                      </button>
                    )}
                  </div>
                  {Object.entries(stats.nodes_by_type)
                    .sort(([, a], [, b]) => b - a)
                    .slice(0, MAX_INLINE_TYPES)
                    .map(([type, count]) => (
                      <div key={type} className="settings-dialog-type-row">
                        <span
                          className="settings-dialog-type-dot"
                          style={{ backgroundColor: resolveColor(type, schema) }}
                        />
                        <span className="settings-dialog-type-name">{type}</span>
                        <span className="settings-dialog-type-count">{count}</span>
                      </div>
                    ))}
                </div>
              )}

              <div className="settings-dialog-section-divider" />
            </>
          )}

          <div className="settings-dialog-section-title">{t('menu.view_section')}</div>
          <button
            className="settings-dialog-menu-item"
            onClick={() => setShowMinimap(!showMinimap)}
          >
            <Map size={14} />
            <span>{t('menu.show_minimap')}</span>
            <span className={`settings-dialog-toggle${showMinimap ? ' active' : ''}`} />
          </button>
          <button
            className="settings-dialog-menu-item"
            onClick={() => setNodePreviewEnabled(!nodePreviewEnabled)}
          >
            <Eye size={14} />
            <span>{t('menu.show_node_preview')}</span>
            <span className={`settings-dialog-toggle${nodePreviewEnabled ? ' active' : ''}`} />
          </button>
          <button className="settings-dialog-menu-item" onClick={() => toggleChatPanel()}>
            <ChatDotsFill size={14} />
            <span>{t('menu.show_assistant_panel')}</span>
            <span className={`settings-dialog-toggle${chatPanelOpen ? ' active' : ''}`} />
          </button>
          <button
            className="settings-dialog-type-details-btn settings-dialog-reset-btn"
            onClick={() => resetChatPanelToDefault()}
          >
            {t('menu.reset_assistant_panel_default')}
          </button>

          <div className="settings-dialog-section-divider" />
          <div className="settings-dialog-section-title">{t('settings.presence_section')}</div>
          <div className="settings-dialog-field">
            <label className="settings-dialog-field-label" htmlFor="settings-display-name">
              {t('settings.display_name')}
            </label>
            <input
              id="settings-display-name"
              className="settings-dialog-field-input"
              type="text"
              maxLength={40}
              value={displayName}
              placeholder={t('settings.display_name_placeholder')}
              onChange={(e) => {
                setDisplayNameState(e.target.value);
                setDisplayName(e.target.value);
              }}
            />
            <p className="settings-dialog-field-hint">{t('settings.display_name_hint')}</p>
          </div>

          <div className="settings-dialog-section-divider" />
          <div className="settings-dialog-section-title">{t('menu.language_section')}</div>
          <button className="settings-dialog-menu-item" onClick={() => setLanguage('en')}>
            <span>{t('menu.language_en')}</span>
            <span className={`settings-dialog-toggle${language === 'en' ? ' active' : ''}`} />
          </button>
          <button className="settings-dialog-menu-item" onClick={() => setLanguage('sv')}>
            <span>{t('menu.language_sv')}</span>
            <span className={`settings-dialog-toggle${language === 'sv' ? ' active' : ''}`} />
          </button>

          <div className="settings-dialog-section-divider" />
          <div className="settings-dialog-section-title">{t('settings.admin_section')}</div>
          <button className="settings-dialog-menu-item" onClick={() => onExportGraph?.()}>
            <Download size={14} />
            <span>{t('menu.export_graph')}</span>
          </button>

          {/* Logout is always shown — redirect is harmless regardless of
              AUTH_ENABLED. Backend routes /auth/logout and /logged-out are
              exempt from auth middleware so the user never gets stuck. */}
          <div className="settings-dialog-section-divider" />
          <button
            className="settings-dialog-menu-item settings-dialog-menu-item-logout"
            onClick={() => {
              window.location.href = '/auth/logout';
            }}
          >
            <BoxArrowRight size={14} />
            <span>{t('menu.logout')}</span>
          </button>
        </div>
      </div>

      {statsDialogOpen &&
        stats?.nodes_by_type &&
        createPortal(
          <NodeTypeStatsDialog
            nodesByType={stats.nodes_by_type}
            onClose={() => setStatsDialogOpen(false)}
          />,
          document.body
        )}
    </div>
  );
}

export default SettingsDialog;
