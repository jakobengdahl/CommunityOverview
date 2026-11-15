import { useState } from 'react';
import { useI18n } from '../i18n';
import * as api from '../services/api';
import MarkdownMessage from './MarkdownMessage';
import './PendingActionCard.css';

/**
 * A write the server refused after file content entered the conversation
 * (ADR 0010 §5). Shows the tool and arguments exactly as the server recorded
 * them; only this button runs it, through the confirmation route. The route
 * answers with an explicit outcome (no assistant turn): a failure shows its
 * message; a successful run shows its message and hands the raw tool result to
 * onConfirmed, so the caller applies its side effects (e.g. confirmed nodes
 * join the view). conversationId is the key of the conversation whose turn
 * recorded the action; the server refuses a confirmation from any other.
 * Dismissing sends nothing; the token expires. The outcome (dismissed, done,
 * failed) goes to onSettled so the owner stores it as action.settled: a card
 * remounted from the stored message (e.g. a reopened mobile sheet) then shows
 * that outcome instead of offering Confirm again.
 */
function PendingActionCard({ action, conversationId, disabled = false, onConfirmed, onSettled }) {
  const { t } = useI18n();
  const [settled, setSettled] = useState(action.settled ?? null);
  const [busy, setBusy] = useState(false);

  const settle = (key, params, reply = null) => {
    const outcome = { key, params, reply };
    setSettled(outcome);
    onSettled?.(outcome);
  };

  const confirm = async () => {
    setBusy(true);
    try {
      const response = await api.confirmChatAction(action.token, conversationId);
      if (response?.error_code === 'confirmation_unknown') settle('chat.pending_action_unknown');
      else if (response?.error_code === 'access_denied') settle('chat.pending_action_denied');
      // Anything but an explicit ok is a failure.
      else if (response?.outcome !== 'ok')
        settle(
          response?.message ? 'chat.pending_action_failed' : 'chat.pending_action_failed_no_reason',
          response?.message ? { message: response.message } : undefined
        );
      else {
        settle('chat.pending_action_done', undefined, response.message || null);
        // The action already ran; a failing view update must not report it as failed.
        try {
          await onConfirmed?.(response.result);
        } catch (err) {
          console.error('[PendingActionCard] Failed to apply the confirmed result:', err);
        }
      }
    } catch (err) {
      settle('chat.pending_action_failed', { message: err.message });
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
      {settled ? (
        <div className="pending-action-status" role="status">
          <p>{t(settled.key, settled.params)}</p>
          {settled.reply && <MarkdownMessage>{settled.reply}</MarkdownMessage>}
        </div>
      ) : (
        <div className="pending-action-buttons">
          <button type="button" onClick={confirm} disabled={disabled || busy}>
            {t('chat.pending_action_confirm')}
          </button>
          <button
            type="button"
            onClick={() => settle('chat.pending_action_dismissed')}
            disabled={busy}
          >
            {t('chat.pending_action_dismiss')}
          </button>
        </div>
      )}
    </div>
  );
}

export default PendingActionCard;
