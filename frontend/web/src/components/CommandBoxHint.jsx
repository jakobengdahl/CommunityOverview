import { useState } from 'react';
import { X } from 'react-bootstrap-icons';
import { useI18n } from '../i18n';
import useUserPreferencesStore from '../store/userPreferencesStore';

export const COMMAND_BOX_HINT_DISMISSED_KEY = 'command_box_hint_dismissed';

function readDismissed() {
  try {
    return window.localStorage.getItem(COMMAND_BOX_HINT_DISMISSED_KEY) === 'true';
  } catch {
    return false;
  }
}

/**
 * Explains the command box once the classic search and create surfaces are
 * hidden, until dismissed. It sits beside the composer, never over it, so it
 * never stands between the user and typing or sending.
 */
function CommandBoxHint() {
  const { t } = useI18n();
  const hideClassicSurfaces = useUserPreferencesStore(
    (state) => state.preferences.hideClassicSurfaces
  );
  const [dismissed, setDismissed] = useState(readDismissed);
  if (!hideClassicSurfaces || dismissed) return null;

  const dismiss = () => {
    setDismissed(true);
    try {
      window.localStorage.setItem(COMMAND_BOX_HINT_DISMISSED_KEY, 'true');
    } catch {
      // Not remembered then; it stays dismissed for this page.
    }
  };

  return (
    <div className="command-box-hint" role="note">
      <span>{t('chat.command_box_hint')}</span>
      <button
        type="button"
        className="command-box-hint-dismiss"
        onClick={dismiss}
        aria-label={t('chat.command_box_hint_dismiss')}
        title={t('chat.command_box_hint_dismiss')}
      >
        <X size={14} />
      </button>
    </div>
  );
}

export default CommandBoxHint;
