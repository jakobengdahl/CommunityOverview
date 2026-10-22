import { useI18n } from '../i18n';
import './WriteModeBar.css';

/**
 * The active session's write mode, shown over the canvas where the user edits
 * (docs/SESSION_OVERLAY_CONTRACT.md §17).
 *
 * A staged session gets a banner that cannot be mistaken for direct editing:
 * edits stay out of the graph until merged, and how many are pending. Where
 * the backend can stage, a direct session says so too, with the switch to
 * staged. Where it cannot, nothing is shown and direct editing looks as it
 * always did.
 */
function WriteModeBar({
  writeMode,
  stagedAvailable,
  pendingCount = 0,
  diffUnavailable = false,
  onReview,
  onSwitchToStaged,
  onRetryMode,
}) {
  const { t } = useI18n();
  if (writeMode === 'unknown' && stagedAvailable) {
    // A linked session whose mode has not loaded: it may be staged, so edits
    // are held until it has.
    return (
      <div
        className="write-mode-bar write-mode-bar--unknown"
        role="status"
        data-testid="write-mode-unknown"
      >
        <span className="write-mode-bar-text">{t('staged.mode_unknown')}</span>
        <button type="button" className="write-mode-bar-link" onClick={onRetryMode}>
          {t('staged.mode_retry')}
        </button>
      </div>
    );
  }
  if (writeMode === 'staged') {
    return (
      <div
        className="write-mode-bar write-mode-bar--staged"
        role="status"
        title={t('staged.mode_staged_hint')}
        data-testid="write-mode-staged"
      >
        <span className="write-mode-bar-label">{t('staged.mode_staged')}</span>
        <span className="write-mode-bar-text">{t('staged.mode_staged_short')}</span>
        <span className="write-mode-bar-count" data-testid="staged-pending-count">
          {diffUnavailable
            ? t('staged.pending_unknown')
            : t('staged.pending_count', { count: pendingCount })}
        </span>
        <button type="button" className="write-mode-bar-button" onClick={onReview}>
          {t('staged.review')}
        </button>
      </div>
    );
  }
  if (!stagedAvailable || writeMode !== 'direct') return null;
  return (
    <div
      className="write-mode-bar write-mode-bar--direct"
      title={t('staged.mode_direct_hint')}
      data-testid="write-mode-direct"
    >
      <span className="write-mode-bar-label">{t('staged.mode_direct')}</span>
      <button type="button" className="write-mode-bar-link" onClick={onSwitchToStaged}>
        {t('staged.switch_to_staged')}
      </button>
    </div>
  );
}

export default WriteModeBar;
