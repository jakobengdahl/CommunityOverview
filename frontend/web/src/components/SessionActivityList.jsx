import { ArrowCounterclockwise } from 'react-bootstrap-icons';
import { useI18n } from '../i18n';
import { relativeTime, absoluteTime } from '../utils/history';
import { describeActivity, isUndoableRecord } from '../utils/sessionActivity';
import './SessionActivityList.css';

function actorFor(clientId, roster) {
  return (roster || []).find((m) => m.client_id === clientId);
}

/**
 * One session activity record: actor badge, human description, relative
 * time, and — only on the single record `findLatestUndoable` (see
 * ActivityDrawer.jsx) picked out for the current actor — the Undo action.
 * Presentational only, mirroring HistoryList.jsx's split from its hosting
 * drawer (data fetching/undo-invocation stay in ActivityDrawer.jsx).
 */
function SessionActivityEntry({
  record,
  isSelf,
  actor,
  isLatestUndoable,
  undoing,
  onUndo,
  nodesById,
  t,
  language,
}) {
  const { key, params } = describeActivity(record, {
    nodeName: (id) => nodesById?.[id]?.name,
  });
  const description = t(key, {
    ...params,
    // Interpolate the annotation-type phrase itself through i18n so it is
    // localized too, not left as its raw i18n key inside the sentence.
    type: params.type ? t(params.type) : undefined,
  });
  const rel = relativeTime(record.occurred_at);
  const abs = absoluteTime(record.occurred_at, language);
  // Prefer the roster's display name, but "You" is knowable from
  // `currentClientId` alone (isSelf) even before presence has echoed this
  // browser's own roster entry back — so self-attribution never regresses to
  // a raw client id just because the roster hasn't caught up yet. A
  // collaborator who has since left the roster still falls back to their raw
  // client id, which is the best identity left to show.
  const actorLabel = isSelf
    ? actor
      ? `${actor.display_name} (${t('presence.you')})`
      : t('presence.you')
    : (actor?.display_name ?? record.actor);

  return (
    <li className="session-activity-entry">
      <div className="session-activity-entry-head">
        <span
          className="session-activity-actor-dot"
          style={{ backgroundColor: actor?.color || '#666' }}
          aria-hidden="true"
        />
        <span className="session-activity-actor" title={record.actor}>
          {actorLabel}
        </span>
        <time className="session-activity-time" dateTime={record.occurred_at} title={abs}>
          {t(rel.key, { count: rel.count })}
        </time>
      </div>
      <div className="session-activity-desc">{description}</div>
      <div className="session-activity-foot">
        {record.undone && (
          <span className="session-activity-undone-badge">{t('history.session_undone_badge')}</span>
        )}
        {isLatestUndoable && !record.undone && (
          <button
            type="button"
            className="session-activity-undo-button"
            onClick={onUndo}
            disabled={undoing}
            aria-label={t('history.session_undo_aria')}
          >
            <ArrowCounterclockwise size={13} />
            <span>{undoing ? t('history.session_undoing') : t('history.session_undo')}</span>
          </button>
        )}
      </div>
    </li>
  );
}

/**
 * Session tab body: the per-session annotation/canvas activity log
 * (GET /sessions/{id}/activity, PR #423), newest first. Read-only aside from
 * the single Undo affordance on the current actor's own latest eligible
 * record — see `isUndoableRecord`/`findLatestUndoable` in
 * utils/sessionActivity.js, which this list's `latestUndoableId` prop is
 * computed from by the caller so both the toolbar button and this row agree
 * on which record undo actually targets.
 */
function SessionActivityList({
  records,
  currentClientId,
  roster,
  latestUndoableId,
  undoing,
  onUndo,
  nodesById,
}) {
  const { t, language } = useI18n();

  if (!records || records.length === 0) {
    return <div className="session-activity-empty">{t('history.session_empty')}</div>;
  }

  return (
    <ul className="session-activity-list">
      {records.map((record) => {
        const actor = actorFor(record.actor, roster);
        return (
          <SessionActivityEntry
            key={record.id}
            record={record}
            actor={actor}
            isSelf={record.actor === currentClientId}
            isLatestUndoable={record.id === latestUndoableId && isUndoableRecord(record)}
            undoing={undoing}
            onUndo={onUndo}
            nodesById={nodesById}
            t={t}
            language={language}
          />
        );
      })}
    </ul>
  );
}

export default SessionActivityList;
