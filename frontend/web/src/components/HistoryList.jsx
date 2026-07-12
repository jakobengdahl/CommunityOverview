import { useI18n } from '../i18n';
import {
  relativeTime,
  absoluteTime,
  entityName,
  computeDiff,
  formatValue,
  isUpdate,
} from '../utils/history';
import './HistoryList.css';

/** Map a dotted event type ("node.create") to its i18n key segment. */
function eventKey(eventType) {
  const KNOWN = new Set([
    'node.create',
    'node.update',
    'node.delete',
    'edge.create',
    'edge.update',
    'edge.delete',
  ]);
  return KNOWN.has(eventType) ? eventType.replace('.', '_') : 'unknown';
}

/**
 * Compact before→after diff for an update entry, derived from the patch
 * (or the before/after snapshots when no patch is present).
 */
function HistoryDiff({ entry, t }) {
  const changes = computeDiff(entry);
  if (changes.length === 0) return null;
  return (
    <div className="history-diff">
      {changes.map(({ field, before, after }) => (
        <div key={field} className="history-diff-row">
          <span className="history-diff-field">{field}</span>
          <span className="history-diff-before" title={t('history.before')}>
            {formatValue(before)}
          </span>
          <span className="history-diff-arrow" aria-hidden="true">
            →
          </span>
          <span className="history-diff-after" title={t('history.after')}>
            {formatValue(after)}
          </span>
        </div>
      ))}
    </div>
  );
}

/**
 * One history record: event type, entity, relative + absolute time, origin and
 * an AI badge, plus a diff for updates.
 */
function HistoryEntry({ entry, t, language }) {
  const eventLabel = t(`history.event.${eventKey(entry.event_type)}`);
  const rel = relativeTime(entry.occurred_at);
  const abs = absoluteTime(entry.occurred_at, language);
  const name = entityName(entry);

  return (
    <li className="history-entry">
      <div className="history-entry-head">
        <span className={`history-event-type history-event-${entry.entity_kind}`}>
          {eventLabel}
        </span>
        {entry.is_ai_action && (
          <span className="history-ai-badge" title={t('history.ai_badge_title')}>
            {t('history.ai_badge')}
          </span>
        )}
        <time className="history-time" dateTime={entry.occurred_at} title={abs}>
          {t(rel.key, { count: rel.count })}
        </time>
      </div>
      <div className="history-entry-entity">
        {entry.entity_type && <span className="history-entity-type">{entry.entity_type}</span>}
        <span className="history-entity-name" title={entry.entity_id}>
          {name}
        </span>
      </div>
      {isUpdate(entry) && <HistoryDiff entry={entry} t={t} />}
      <div className="history-entry-meta">
        <span className="history-entry-abs">{abs}</span>
        {entry.event_origin && (
          <span className="history-entry-origin">
            {t('history.origin_label', { origin: entry.event_origin })}
          </span>
        )}
      </div>
    </li>
  );
}

/**
 * Read-only list of graph history records. Presentational only — data
 * fetching, pagination and error handling live in the hosting view.
 */
function HistoryList({ entries }) {
  const { t, language } = useI18n();
  if (!entries || entries.length === 0) {
    return <div className="history-empty">{t('history.empty')}</div>;
  }
  return (
    <ul className="history-list">
      {entries.map((entry) => (
        <HistoryEntry
          key={entry.event_id || `${entry.entity_id}-${entry.occurred_at}`}
          entry={entry}
          t={t}
          language={language}
        />
      ))}
    </ul>
  );
}

export default HistoryList;
