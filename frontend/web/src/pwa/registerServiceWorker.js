function isLocalhost(hostname) {
  return hostname === 'localhost' || hostname === '127.0.0.1' || hostname === '[::1]';
}

export function registerAppServiceWorker({
  navigator: targetNavigator = globalThis.navigator,
  window: targetWindow = globalThis.window,
} = {}) {
  if (!targetNavigator?.serviceWorker || !targetWindow?.location) {
    return;
  }

  const { protocol, hostname } = targetWindow.location;
  if (protocol !== 'https:' && !(protocol === 'http:' && isLocalhost(hostname))) {
    return;
  }

  targetNavigator.serviceWorker.register('/service-worker.js', { scope: '/' }).catch(() => {
    // Service worker registration is an enhancement; keep the app usable if it fails.
  });
}
