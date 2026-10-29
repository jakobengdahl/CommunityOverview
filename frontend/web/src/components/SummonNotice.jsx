import { useI18n } from '../i18n';

/**
 * "Someone brought you here" notice shown after a session summon moved this
 * viewer's camera, with Return back to where they were.
 */
function SummonNotice({ notice, onReturn, onDismiss }) {
  const { t } = useI18n();
  if (!notice) return null;
  const message = notice.summonedBy
    ? t('summon.notice', { name: notice.summonedBy })
    : t('summon.notice_anonymous');
  return (
    <div className="app-notification app-notification-info app-summon-notice" role="status">
      <span>{message}</span>
      {notice.returnTo && (
        <button type="button" className="app-summon-return" onClick={onReturn}>
          {t('summon.return')}
        </button>
      )}
      <button
        type="button"
        onClick={onDismiss}
        aria-label={t('common.close')}
        title={t('common.close')}
      >
        ×
      </button>
    </div>
  );
}

export default SummonNotice;
