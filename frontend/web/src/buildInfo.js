/* global __APP_VERSION__, __APP_BUILD_SHA__ */
// Build-time metadata injected by Vite `define` (see vite.config.js); the
// typeof guards keep unit tests and non-Vite tooling from throwing.
export function getBuildInfo() {
  return {
    version: typeof __APP_VERSION__ === 'string' ? __APP_VERSION__ : '',
    sha: typeof __APP_BUILD_SHA__ === 'string' ? __APP_BUILD_SHA__ : '',
  };
}
