import { useState, useEffect, useCallback } from 'react';
import { ClockHistory, X, Trash, BoxArrowInUpRight, Eye } from 'react-bootstrap-icons';
import { useI18n } from '../i18n';
import useGraphStore from '../store/graphStore';
import { relativeTime } from '../utils/history';
import './NodeHistoryPanel.css';

/**
 * NodeHistoryPanel — a session-scoped, clickable trail of the nodes that were
 * added to the visualization or navigated to. Clicking a row re-centers the
 * canvas on that node (reusing the existing focusNodeId → setCenter primitive),
 * letting the user jump back through what happened.
 *
 * This is deliberately distinct from the backend graph-mutation log
 * (RecentActivityDrawer, which shows who changed what in the graph data) and
 * from the canvas position undo/redo (useCanvasHistory). It answers "what have
 * I been looking at / adding in this session — take me back to it".
 *
 * The toggle only appears once the trail has at least one entry, so it stays
 * out of the way on an empty canvas.
 */
function NodeHistoryPanel() {
  const { t } = useI18n();
  const navHistory = useGraphStore((s) => s.navHistory);
  const setFocusNodeId = useGraphStore((s) => s.setFocusNodeId);
  const clearNavHistory = useGraphStore((s) => s.clearNavHistory);
  const getNodeColor = useGraphStore((s) => s.getNodeColor);
  const [open, setOpen] = useState(false);

  // While the panel is open, Escape closes it (and is consumed so it does not
  // also trigger the canvas's clear-board / other Escape handlers).
  useEffect(() => {
    if (!open) return undefined;
    const onKeyDown = (e) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        setOpen(false);
      }
    };
    document.addEventListener('keydown', onKeyDown, true);
    return () => document.removeEventListener('keydown', onKeyDown, true);
  }, [open]);

  const handleJump = useCallback(
    (id) => {
      setFocusNodeId(id);
    },
    [setFocusNodeId]
  );

  if (navHistory.length === 0) return null;

  return (
    <div className="node-history">
      {open && (
        <div className="node-history-panel" role="dialog" aria-label={t('nav_history.title')}>
          <div className="node-history-header">
            <ClockHistory size={15} className="node-history-header-icon" />
            <span className="node-history-title">{t('nav_history.title')}</span>
            <button
              type="button"
              className="node-history-clear"
              onClick={clearNavHistory}
              title={t('nav_history.clear')}
              aria-label={t('nav_history.clear')}
            >
              <Trash size={14} />
            </button>
            <button
              type="button"
              className="node-history-close"
              onClick={() => setOpen(false)}
              title={t('nav_history.close')}
              aria-label={t('nav_history.close')}
            >
              <X size={18} />
            </button>
          </div>
          <ul className="node-history-list">
            {navHistory.map((entry) => {
              const rel = relativeTime(entry.at);
              const isAdded = entry.action === 'added';
              return (
                <li key={entry.id}>
                  <button
                    type="button"
                    className="node-history-item"
                    onClick={() => handleJump(entry.id)}
                    title={t('nav_history.jump_to', { name: entry.name })}
                  >
                    <span
                      className="node-history-dot"
                      style={{ backgroundColor: getNodeColor(entry.type) }}
                      aria-hidden="true"
                    />
                    <span className="node-history-item-main">
                      <span className="node-history-name">{entry.name}</span>
                      <span className="node-history-meta">
                        <span className={`node-history-action node-history-action-${entry.action}`}>
                          {isAdded ? (
                            <BoxArrowInUpRight size={11} aria-hidden="true" />
                          ) : (
                            <Eye size={11} aria-hidden="true" />
                          )}
                          {isAdded
                            ? t('nav_history.action_added')
                            : t('nav_history.action_visited')}
                        </span>
                        {entry.type && <span className="node-history-type">{entry.type}</span>}
                        <span className="node-history-time">
                          {t(rel.key, { count: rel.count })}
                        </span>
                      </span>
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      )}
      <button
        type="button"
        className={`node-history-toggle${open ? ' active' : ''}`}
        onClick={() => setOpen((v) => !v)}
        title={t('nav_history.title')}
        aria-label={t('nav_history.title')}
        aria-expanded={open}
      >
        <ClockHistory size={16} />
        <span className="node-history-toggle-label">{t('nav_history.title')}</span>
        <span className="node-history-count">{navHistory.length}</span>
      </button>
    </div>
  );
}

export default NodeHistoryPanel;
