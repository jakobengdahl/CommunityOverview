import { useI18n } from '../i18n';
import { useViewportMode } from '../hooks/useViewportMode';
import { useAppInstallPrompt } from '../pwa/useAppInstallPrompt';
import './AppInstallPrompt.css';

function AppInstallPrompt() {
  const { t } = useI18n();
  const { isMobile } = useViewportMode();
  const { canInstall, promptInstall } = useAppInstallPrompt();

  if (isMobile || !canInstall) {
    return null;
  }

  return (
    <button
      type="button"
      className="app-install-prompt"
      aria-label={t('app_install.install')}
      onClick={promptInstall}
    >
      {t('app_install.install')}
    </button>
  );
}

export default AppInstallPrompt;
