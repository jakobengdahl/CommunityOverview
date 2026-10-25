import { useEffect, useRef } from 'react';
import { useI18n } from '../i18n';
import './ConfirmDialog.css';
import './NewSessionDialog.css';

/**
 * Choose how a new session writes (docs/SESSION_OVERLAY_CONTRACT.md §5.1):
 * directly into the graph, or staged for review before anything reaches it.
 * Offered only where the backend reports staged sessions.
 */
function NewSessionDialog({ defaultMode = 'direct', onChoose, onCancel }) {
  const { t } = useI18n();
  const dialogRef = useRef(null);

  useEffect(() => {
    dialogRef.current?.focus();
  }, []);

  const option = (mode, title, hint) => (
    <button
      type="button"
      className={`new-session-option${mode === defaultMode ? ' is-default' : ''}`}
      onClick={() => onChoose(mode)}
      data-testid={`new-session-${mode}`}
    >
      <span className="new-session-option-title">{title}</span>
      <span className="new-session-option-hint">{hint}</span>
    </button>
  );

  return (
    <div className="confirm-dialog-overlay" onClick={onCancel}>
      <div
        ref={dialogRef}
        className="confirm-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="new-session-dialog-title"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === 'Escape') onCancel();
        }}
        tabIndex={-1}
      >
        <div className="confirm-dialog-header">
          <h3 id="new-session-dialog-title">{t('staged.new_session_title')}</h3>
        </div>
        <div className="confirm-dialog-content new-session-options">
          {option('direct', t('staged.new_direct'), t('staged.new_direct_hint'))}
          {option('staged', t('staged.new_staged'), t('staged.new_staged_hint'))}
        </div>
        <div className="confirm-dialog-actions">
          <button className="confirm-dialog-button cancel" onClick={onCancel}>
            {t('common.cancel')}
          </button>
        </div>
      </div>
    </div>
  );
}

export default NewSessionDialog;
