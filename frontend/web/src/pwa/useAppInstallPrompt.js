import { useEffect, useState } from 'react';

export const IOS_INSTALL_HINT_DISMISSED_KEY = 'app_install_ios_hint_dismissed';

let installPromptEvent = null;
let installed = false;
let listening = false;
const listeners = new Set();

function canUseWindow() {
  return typeof window !== 'undefined';
}

function isStandaloneDisplay() {
  if (!canUseWindow()) return false;
  return (
    window.matchMedia?.('(display-mode: standalone)').matches ||
    window.navigator?.standalone === true
  );
}

function isIosDevice() {
  if (!canUseWindow()) return false;
  const nav = window.navigator;
  const ua = nav?.userAgent || '';
  return /iPad|iPhone|iPod/.test(ua) || (nav?.platform === 'MacIntel' && nav?.maxTouchPoints > 1);
}

function isIosHintDismissed() {
  try {
    return window.localStorage.getItem(IOS_INSTALL_HINT_DISMISSED_KEY) === 'true';
  } catch {
    return false;
  }
}

function notify() {
  listeners.forEach((listener) => listener());
}

function snapshot() {
  installed = installed || isStandaloneDisplay();
  return {
    canInstall: Boolean(installPromptEvent) && !installed,
    installed,
    showIosInstallHint: isIosDevice() && !installed && !installPromptEvent && !isIosHintDismissed(),
  };
}

function ensureBrowserListeners() {
  if (!canUseWindow() || listening) return;
  listening = true;
  installed = isStandaloneDisplay();

  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault();
    installPromptEvent = event;
    notify();
  });

  window.addEventListener('appinstalled', () => {
    installPromptEvent = null;
    installed = true;
    notify();
  });
}

export function useAppInstallPrompt() {
  const [state, setState] = useState(() => snapshot());

  useEffect(() => {
    ensureBrowserListeners();
    const update = () => setState(snapshot());
    listeners.add(update);
    update();
    return () => listeners.delete(update);
  }, []);

  const promptInstall = async () => {
    const promptEvent = installPromptEvent;
    installPromptEvent = null;
    notify();
    try {
      await promptEvent?.prompt?.();
    } catch {
      // The install prompt is optional browser UI; ignore dismissal or platform failures.
    }
  };

  const dismissIosInstallHint = () => {
    try {
      window.localStorage.setItem(IOS_INSTALL_HINT_DISMISSED_KEY, 'true');
    } catch {
      // Ignore storage failures; the hint will just be eligible again next load.
    }
    notify();
  };

  return {
    ...state,
    promptInstall,
    dismissIosInstallHint,
  };
}
