import useGraphStore from '../store/graphStore';
import { useI18n } from '../i18n';
import './StagedEditNotice.css';

/**
 * In a staged session, says in the edit dialog itself that the edit is held
 * for review and does not reach the graph yet
 * (docs/SESSION_OVERLAY_CONTRACT.md §17). Renders nothing in a direct session.
 */
function StagedEditNotice() {
  const { t } = useI18n();
  const writeMode = useGraphStore((state) => state.sessionWriteMode);
  if (writeMode !== 'staged') return null;
  return (
    <p className="staged-edit-notice" role="note" data-testid="staged-edit-notice">
      {t('staged.edit_notice')}
    </p>
  );
}

export default StagedEditNotice;
