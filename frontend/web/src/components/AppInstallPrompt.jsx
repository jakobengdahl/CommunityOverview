import { useEffect, useState } from 'react';
import './AppInstallPrompt.css';

function isStandaloneDisplay() {
  return (
    window.matchMedia?.('(display-mode: standalone)').matches ||
    window.navigator?.standalone === true
  );
}

function AppInstallPrompt() {
  const [installPrompt, setInstallPrompt] = useState(null);
  const [installed, setInstalled] = useState(() => isStandaloneDisplay());

  useEffect(() => {
    function handleBeforeInstallPrompt(event) {
      event.preventDefault();
      setInstallPrompt(event);
    }

    function handleInstalled() {
      setInstallPrompt(null);
      setInstalled(true);
    }

    window.addEventListener('beforeinstallprompt', handleBeforeInstallPrompt);
    window.addEventListener('appinstalled', handleInstalled);

    return () => {
      window.removeEventListener('beforeinstallprompt', handleBeforeInstallPrompt);
      window.removeEventListener('appinstalled', handleInstalled);
    };
  }, []);

  if (installed || !installPrompt) {
    return null;
  }

  return (
    <button
      type="button"
      className="app-install-prompt"
      aria-label="Install app"
      onClick={async () => {
        const promptEvent = installPrompt;
        setInstallPrompt(null);
        try {
          await promptEvent.prompt?.();
        } catch {
          // The install prompt is optional browser UI; ignore dismissal or platform failures.
        }
      }}
    >
      Install app
    </button>
  );
}

export default AppInstallPrompt;
