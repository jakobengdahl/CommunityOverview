import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import * as api from '../services/api';
import { useI18n } from '../i18n';
import ConfirmDialog from './ConfirmDialog';
import {
  allowedChoices,
  buildMergeRequest,
  conflictingFields,
  dependants,
  dependencyClosure,
  entryKey,
  formatValue,
  itemKey,
  itemLabel,
  mergeOutcomeUnknown,
  nextMergeAttempt,
  parseManualValue,
  reviewDrift,
  unresolvedKeys,
} from '../utils/stagedReview';
import './StagedReviewDialog.css';

function newMergeId() {
  return crypto.randomUUID();
}

/**
 * Review, merge and discard a staged session's changes
 * (docs/SESSION_OVERLAY_CONTRACT.md §10–§12, §17).
 *
 * `items` is the diff as the server last answered it for this session; it
 * follows live changes. A merge or discard always names the entries the user
 * ticked — never `all` — and goes only after a fresh read of the diff shows
 * those entries exactly as they were on screen, so nothing is merged or
 * dropped that the user did not see.
 */
function StagedReviewDialog({
  sessionId,
  items,
  diffError,
  onRefresh,
  onMerged,
  onDiscarded,
  onClose,
  canvasName,
}) {
  const { t } = useI18n();
  const [selected, setSelected] = useState(() => new Set());
  const [resolutions, setResolutions] = useState({});
  const [manualText, setManualText] = useState({});
  const [expanded, setExpanded] = useState(() => new Set());
  const [blockedKeys, setBlockedKeys] = useState(() => new Set());
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null);
  const [confirmDiscard, setConfirmDiscard] = useState(null);
  const attemptRef = useRef(null);
  const sessionRef = useRef(sessionId);
  const dialogRef = useRef(null);

  useEffect(() => {
    dialogRef.current?.focus();
  }, []);

  // The dialog belongs to one session; a switch closes it rather than
  // carrying a selection over.
  useEffect(() => {
    if (sessionRef.current !== sessionId) onClose();
  }, [sessionId, onClose]);

  const byKey = useMemo(() => new Map(items.map((item) => [itemKey(item), item])), [items]);

  // Entries merged, discarded or restaged elsewhere leave the selection.
  const liveSelected = useMemo(
    () => new Set([...selected].filter((key) => byKey.has(key))),
    [selected, byKey]
  );

  const labelOf = useCallback((item) => itemLabel(item, canvasName), [canvasName]);
  const labelOfRef = useCallback(
    (ref) => {
      const item = byKey.get(entryKey(ref.kind, ref.entity_id));
      return item ? labelOf(item) : ref.entity_id;
    },
    [byKey, labelOf]
  );

  const toggle = (key) => {
    setSelected((prev) => {
      const next = new Set([...prev].filter((k) => byKey.has(k)));
      if (next.has(key)) {
        next.delete(key);
        dependants(items, key).forEach((k) => next.delete(k));
        return next;
      }
      return dependencyClosure(items, [...next, key]);
    });
  };

  const selectAll = () => setSelected(new Set(items.map(itemKey)));
  const selectNone = () => setSelected(new Set());

  const setChoice = (item, choice) => {
    const key = itemKey(item);
    setResolutions((prev) => ({ ...prev, [key]: { choice } }));
    if (choice === 'manual' && !manualText[key]) {
      const seeded = {};
      for (const field of conflictingFields(item)) {
        seeded[field] = formatValue(item.fields[field]?.staged);
      }
      setManualText((prev) => ({ ...prev, [key]: seeded }));
    }
  };

  // Resolutions with every manual value parsed; null when one does not parse.
  const resolvedForRequest = () => {
    const out = {};
    for (const [key, resolution] of Object.entries(resolutions)) {
      if (resolution.choice !== 'manual') {
        out[key] = resolution;
        continue;
      }
      const item = byKey.get(key);
      if (!item) continue;
      const values = {};
      for (const field of conflictingFields(item)) {
        const parsed = parseManualValue(manualText[key]?.[field] ?? '', item.fields[field]?.staged);
        if (!parsed.ok) {
          setMessage({ kind: 'error', text: t('staged.manual_invalid', { field }) });
          return null;
        }
        values[field] = parsed.value;
      }
      out[key] = { choice: 'manual', values };
    }
    return out;
  };

  // Read the diff again and check the chosen entries are what the user saw.
  const freshUnchanged = async (keys) => {
    const fresh = await onRefresh();
    if (!fresh) {
      setMessage({ kind: 'error', text: t('staged.refresh_failed') });
      return null;
    }
    const drift = reviewDrift(items, fresh, keys);
    if (drift.length) {
      setMessage({ kind: 'warning', text: t('staged.review_drift', { count: drift.length }) });
      return null;
    }
    return fresh;
  };

  const merge = async ({ retried = false } = {}) => {
    const keys = liveSelected;
    if (!keys.size) return;
    const resolved = resolvedForRequest();
    if (!resolved) return;
    const unresolved = unresolvedKeys(items, keys, resolved);
    if (unresolved.length) {
      setBlockedKeys(new Set(unresolved));
      setMessage({
        kind: 'warning',
        text: t('staged.needs_decision', { count: unresolved.length }),
      });
      return;
    }
    setBusy(true);
    setMessage(null);
    try {
      const fresh = await freshUnchanged(keys);
      if (!fresh) return;
      const request = buildMergeRequest(fresh, keys, resolved);
      const attempt = nextMergeAttempt(attemptRef.current, request, newMergeId);
      attemptRef.current = attempt;
      let result;
      try {
        result = await api.mergeStagedChanges(sessionId, { merge_id: attempt.mergeId, ...request });
      } catch (error) {
        if (mergeOutcomeUnknown(error)) {
          // It may have landed: the retry must replay this merge id.
          attempt.outcomeUnknown = true;
          setMessage({ kind: 'error', text: t('staged.merge_unknown') });
          return;
        }
        attemptRef.current = null;
        await handleMergeRefusal(error, retried);
        return;
      }
      attemptRef.current = null;
      setSelected(new Set());
      setResolutions({});
      setManualText({});
      setBlockedKeys(new Set());
      const count = (result?.merged || []).length;
      setMessage({
        kind: 'success',
        text: result?.replayed ? t('staged.merge_replayed') : t('staged.merged', { count }),
      });
      onMerged?.(result);
    } finally {
      setBusy(false);
    }
  };

  const handleMergeRefusal = async (error, retried) => {
    const data = error.data || {};
    switch (error.code) {
      case 'merge_blocked': {
        const blocking = (data.items || []).map(itemKey);
        setBlockedKeys(new Set(blocking));
        await onRefresh();
        setMessage({
          kind: 'warning',
          text: t('staged.merge_blocked', { count: blocking.length }),
        });
        return;
      }
      case 'merge_raced':
        // Something changed underneath. Retry once when the chosen entries
        // still read as reviewed; otherwise the user looks again.
        if (!retried) {
          setBusy(false);
          await merge({ retried: true });
          return;
        }
        await onRefresh();
        setMessage({ kind: 'warning', text: t('staged.merge_raced') });
        return;
      case 'merge_incomplete': {
        const missing = (data.missing || [])
          .map((ref) => entryKey(ref.kind, ref.entity_id))
          .filter((key) => byKey.has(key));
        setSelected((prev) => dependencyClosure(items, [...prev, ...missing]));
        setMessage({
          kind: 'warning',
          text: t('staged.merge_incomplete', { count: (data.missing || []).length }),
        });
        return;
      }
      default:
        setMessage({ kind: 'error', text: error.message || t('staged.merge_failed') });
    }
  };

  const requestDiscard = (keys) => {
    if (keys.size) setConfirmDiscard(keys);
  };

  const discard = async () => {
    const keys = confirmDiscard;
    setConfirmDiscard(null);
    if (!keys?.size) return;
    setBusy(true);
    setMessage(null);
    try {
      const fresh = await freshUnchanged(keys);
      if (!fresh) return;
      const entries = fresh
        .filter((item) => keys.has(itemKey(item)))
        .map((item) => ({ kind: item.kind, entity_id: item.entity_id }));
      const result = await api.discardStagedChanges(sessionId, entries);
      setSelected(new Set());
      setMessage({
        kind: 'success',
        text: t('staged.discarded', { count: (result?.removed_entries || []).length }),
      });
      onDiscarded?.(result);
    } catch (error) {
      setMessage({ kind: 'error', text: error.message || t('staged.discard_failed') });
    } finally {
      setBusy(false);
    }
  };

  const toggleExpanded = (key) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const renderFields = (item) => {
    const fields = Object.entries(item.fields || {});
    if (!fields.length) return null;
    return (
      <table className="staged-review-fields">
        <thead>
          <tr>
            <th>{t('staged.field')}</th>
            <th>{t('staged.value_base')}</th>
            <th>{t('staged.value_staged')}</th>
            <th>{t('staged.value_main')}</th>
          </tr>
        </thead>
        <tbody>
          {fields.map(([name, diff]) => (
            <tr key={name} className={diff?.conflict ? 'is-conflict' : ''}>
              <th scope="row">
                {name}
                {diff?.conflict && (
                  <span className="staged-review-conflict-mark">{t('staged.conflict_mark')}</span>
                )}
              </th>
              <td>{formatValue(diff?.base)}</td>
              <td>{formatValue(diff?.staged)}</td>
              <td>{formatValue(diff?.main)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  };

  const renderResolution = (item) => {
    const choices = allowedChoices(item);
    if (!choices.length) return null;
    const key = itemKey(item);
    const current = resolutions[key]?.choice || '';
    return (
      <div className="staged-review-resolution">
        <label>
          {t('staged.resolution')}
          <select
            value={current}
            onChange={(e) => setChoice(item, e.target.value)}
            data-testid={`resolution-${key}`}
          >
            <option value="" disabled>
              {t('staged.choose')}
            </option>
            {choices.map((choice) => (
              <option key={choice} value={choice}>
                {t(`staged.choice.${choice}`)}
              </option>
            ))}
          </select>
        </label>
        {current === 'manual' &&
          conflictingFields(item).map((field) => (
            <label key={field} className="staged-review-manual">
              {field}
              <input
                type="text"
                value={manualText[key]?.[field] ?? ''}
                onChange={(e) =>
                  setManualText((prev) => ({
                    ...prev,
                    [key]: { ...prev[key], [field]: e.target.value },
                  }))
                }
                data-testid={`manual-${key}-${field}`}
              />
            </label>
          ))}
      </div>
    );
  };

  const renderItem = (item) => {
    const key = itemKey(item);
    const isOpen = expanded.has(key) || item.status !== 'clean';
    return (
      <li
        key={key}
        className={`staged-review-item status-${item.status}${blockedKeys.has(key) ? ' is-blocked' : ''}`}
        data-testid={`staged-item-${key}`}
      >
        <div className="staged-review-item-head">
          <input
            type="checkbox"
            checked={liveSelected.has(key)}
            onChange={() => toggle(key)}
            aria-label={t('staged.select_entry', { name: labelOf(item) })}
            disabled={busy}
          />
          <button
            type="button"
            className="staged-review-item-name"
            onClick={() => toggleExpanded(key)}
          >
            {labelOf(item)}
          </button>
          <span className="staged-review-badge">{t(`staged.kind.${item.kind}`)}</span>
          <span className={`staged-review-badge action-${item.action}`}>
            {t(`staged.action.${item.action}`)}
          </span>
          <span className={`staged-review-badge status-badge-${item.status}`}>
            {t(`staged.status.${item.status}`)}
          </span>
        </div>
        {isOpen && (
          <div className="staged-review-item-body">
            <p className="staged-review-status-hint">{t(`staged.status_hint.${item.status}`)}</p>
            {renderFields(item)}
            {item.depends_on?.length > 0 && (
              <p className="staged-review-deps">
                {t('staged.depends_on')} {item.depends_on.map(labelOfRef).join(', ')}
              </p>
            )}
            {item.unstaged_edges?.length > 0 && (
              <p className="staged-review-deps">
                {t('staged.unstaged_edges', { count: item.unstaged_edges.length })}{' '}
                {item.unstaged_edges.join(', ')}
              </p>
            )}
            {(item.staged_by || item.staged_at) && (
              <p className="staged-review-meta">
                {t('staged.staged_by', { by: item.staged_by || '?', at: item.staged_at || '' })}
              </p>
            )}
            {renderResolution(item)}
          </div>
        )}
      </li>
    );
  };

  return (
    <>
      <div className="confirm-dialog-overlay" onClick={busy ? undefined : onClose}>
        <div
          ref={dialogRef}
          className="staged-review-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="staged-review-title"
          onClick={(e) => e.stopPropagation()}
          onKeyDown={(e) => {
            if (e.key === 'Escape' && !busy && !confirmDiscard) onClose();
          }}
          tabIndex={-1}
          data-testid="staged-review-dialog"
        >
          <div className="staged-review-header">
            <h3 id="staged-review-title">{t('staged.review_title')}</h3>
            <span className="staged-review-count">
              {t('staged.pending_count', { count: items.length })}
            </span>
            <button type="button" onClick={() => onRefresh()} disabled={busy}>
              {t('staged.refresh')}
            </button>
          </div>
          <p className="staged-review-intro">{t('staged.review_intro')}</p>
          {diffError && (
            <p className="staged-review-message kind-error">{t('staged.diff_unavailable')}</p>
          )}
          {message && (
            <p className={`staged-review-message kind-${message.kind}`} role="status">
              {message.text}
            </p>
          )}
          {items.length === 0 ? (
            <p className="staged-review-empty">{t('staged.nothing_pending')}</p>
          ) : (
            <>
              <div className="staged-review-select">
                <button type="button" onClick={selectAll} disabled={busy}>
                  {t('staged.select_all')}
                </button>
                <button type="button" onClick={selectNone} disabled={busy}>
                  {t('staged.select_none')}
                </button>
              </div>
              <ul className="staged-review-list">{items.map(renderItem)}</ul>
            </>
          )}
          <div className="staged-review-actions">
            <button
              type="button"
              className="confirm-dialog-button danger"
              onClick={() => requestDiscard(liveSelected)}
              disabled={busy || liveSelected.size === 0}
            >
              {t('staged.discard_selected', { count: liveSelected.size })}
            </button>
            <button
              type="button"
              className="confirm-dialog-button cancel"
              onClick={onClose}
              disabled={busy}
            >
              {t('common.close')}
            </button>
            <button
              type="button"
              className="confirm-dialog-button primary"
              onClick={() => merge()}
              disabled={busy || liveSelected.size === 0}
              data-testid="staged-merge"
            >
              {t('staged.merge_selected', { count: liveSelected.size })}
            </button>
          </div>
        </div>
      </div>
      {confirmDiscard && (
        <ConfirmDialog
          title={t('staged.discard_title')}
          message={t('staged.discard_message', { count: confirmDiscard.size })}
          confirmText={t('staged.discard_confirm')}
          cancelText={t('common.cancel')}
          confirmStyle="danger"
          onConfirm={discard}
          onCancel={() => setConfirmDiscard(null)}
        />
      )}
    </>
  );
}

export default StagedReviewDialog;
