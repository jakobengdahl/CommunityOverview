import { useState, useEffect, useCallback, useRef } from 'react';
import { useI18n } from '../i18n';
import * as api from '../services/api';
import HistoryList from './HistoryList';
import './EntityHistoryView.css';

const PAGE_SIZE = 25;

/**
 * Read-only history for a single node or edge. Fetches through the shared API
 * client and renders the reusable HistoryList with load-more pagination.
 *
 * @param {'node'|'edge'} entityKind
 * @param {string} entityId
 */
function EntityHistoryView({ entityKind, entityId }) {
  const { t } = useI18n();
  const [entries, setEntries] = useState([]);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const requestSeq = useRef(0);

  const fetchPage = useCallback(
    (nextOffset) => {
      const opts = { limit: PAGE_SIZE, offset: nextOffset };
      return entityKind === 'edge'
        ? api.getEdgeHistory(entityId, opts)
        : api.getNodeHistory(entityId, opts);
    },
    [entityKind, entityId]
  );

  const loadPage = useCallback(
    async (nextOffset, replace) => {
      const seq = ++requestSeq.current;
      setLoading(true);
      setError(false);
      try {
        const result = await fetchPage(nextOffset);
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
    },
    [fetchPage]
  );

  useEffect(() => {
    setEntries([]);
    setOffset(0);
    setHasMore(false);
    loadPage(0, true);
  }, [loadPage]);

  const isEmpty = !loading && !error && entries.length === 0;

  return (
    <div className="entity-history">
      {error && <div className="entity-history-error">{t('history.error')}</div>}
      {isEmpty && <div className="entity-history-empty">{t('history.no_entity_history')}</div>}
      {loading && entries.length === 0 && (
        <div className="entity-history-loading">{t('history.loading')}</div>
      )}
      {entries.length > 0 && <HistoryList entries={entries} />}
      {hasMore && !error && (
        <button
          className="entity-history-load-more"
          onClick={() => loadPage(offset, false)}
          disabled={loading}
        >
          {loading ? t('history.loading') : t('history.load_more')}
        </button>
      )}
    </div>
  );
}

export default EntityHistoryView;
