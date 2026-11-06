import { useState } from 'react';
import { useI18n } from '../i18n';
import * as api from '../services/api';
import './PendingActionCard.css';

/**
 * A write the server refused after file content entered the conversation
 * (ADR 0010 §5). Shows the tool and arguments exactly as the server recorded
 * them; only this button runs it, through the confirmation route.
 */
function PendingActionCard({ action, disabled = false }) {
  const { t } = useI18n();
  const [status, setStatus] = useState(null);
  const [busy, setBusy] = useState(false);

  const confirm = async () => {
    setBusy(true);
    try {
      const result = await api.confirmChatAction(action.token);
      if (result?.error_code === 'confirmation_unknown')
        setStatus(t('chat.pending_action_unknown'));
      else if (result?.error_code === 'access_denied') setStatus(t('chat.pending_action_denied'));
      else if (result?.error || result?.success === false)
        setStatus(t('chat.pending_action_failed', { message: result.error || '' }));
      else setStatus(t('chat.pending_action_done'));
    } catch (err) {
      setStatus(t('chat.pending_action_failed', { message: err.message }));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="pending-action-card">
      <h4>{t('chat.pending_action_title')}</h4>
      <p className="pending-action-notice">{t('chat.pending_action_notice')}</p>
      <p>
        <strong>{t('chat.pending_action_tool')}</strong> <code>{action.tool}</code>
      </p>
      <pre className="pending-action-arguments">{JSON.stringify(action.arguments, null, 2)}</pre>
      {status ? (
        <p className="pending-action-status" role="status">
          {status}
        </p>
      ) : (
        <button type="button" onClick={confirm} disabled={disabled || busy}>
          {t('chat.pending_action_confirm')}
        </button>
      )}
    </div>
  );
}

export default PendingActionCard;
