import { useState, useEffect, useCallback, useRef } from 'react';
import { X, ClockHistory, ArrowClockwise } from 'react-bootstrap-icons';
import { useI18n } from '../i18n';
import * as api from '../services/api';
import HistoryList from './HistoryList';
import './RecentActivityDrawer.css';

const PAGE_SIZE = 25;

/**
 * RecentActivityDrawer — full-height panel docked to the right screen edge,
 * showing the global graph mutation history newest-first with load-more
 * pagination. Read-only: it never mutates the graph.
 */
function RecentActivityDrawer({ open, onClose }) {
  const { t } = useI18n();
  const [entries, setEntries] = useState([]);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  // Guards against a stale in-flight request overwriting a newer refresh.
  const requestSeq = useRef(0);

  const loadPage = useCallback(async (nextOffset, replace) => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(false);
    try {
      const result = await api.getGraphHistory({ limit: PAGE_SIZE, offset: nextOffset });
      if (seq !== requestSeq.current) return;
      const page = result.entries || [];
      setEntries((prev) => (replace ? page : [...prev, ...page]));
      setOffset(nextOffset + page.length);
      setHasMore(page.length === PAGE_SIZE);
    } catch {
      if (seq !== requestSeq.current) return;
      setError(true);
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, []);

  // Refresh from the top each time the drawer is opened so it reflects the
  // latest activity without needing a manual reload.
  useEffect(() => {
    if (!open) return;
    setEntries([]);
    setOffset(0);
    setHasMore(false);
    loadPage(0, true);
  }, [open, loadPage]);

  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (e) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener('keydown', handleKeyDown, true);
    return () => document.removeEventListener('keydown', handleKeyDown, true);
  }, [open, onClose]);

  const isEmpty = !loading && !error && entries.length === 0;

  return (
    <div className={`activity-drawer${open ? ' open' : ''}`} aria-hidden={!open}>
      <div className="activity-drawer-header">
        <ClockHistory size={17} className="activity-drawer-icon" />
        <span className="activity-drawer-title">{t('history.panel_title')}</span>
        <button
          className="activity-drawer-refresh"
          onClick={() => loadPage(0, true)}
          title={t('history.refresh')}
          aria-label={t('history.refresh')}
          disabled={loading}
        >
          <ArrowClockwise size={15} />
        </button>
        <button
          className="activity-drawer-close"
          onClick={onClose}
          title={t('history.close')}
          aria-label={t('history.close')}
        >
          <X size={20} />
        </button>
      </div>

      <div className="activity-drawer-body">
        {error && (
          <div className="activity-drawer-error">{t('history.error')}</div>
        )}
        {isEmpty && (
          <div className="activity-drawer-empty">{t('history.empty')}</div>
        )}
        {entries.length > 0 && <HistoryList entries={entries} />}
        {loading && entries.length === 0 && (
          <div className="activity-drawer-loading">{t('history.loading')}</div>
        )}
        {hasMore && !error && (
          <button
            className="activity-drawer-load-more"
            onClick={() => loadPage(offset, false)}
            disabled={loading}
          >
            {loading ? t('history.loading') : t('history.load_more')}
          </button>
        )}
      </div>
    </div>
  );
}

export default RecentActivityDrawer;
