import { useEffect, useState } from 'react';

// jsdom (the test environment) has no visualViewport implementation, and
// several real browsers predate the API too - this must degrade to "no
// keyboard inset" rather than throw, same pattern as useViewportMode's
// matchMedia guard.
function isVisualViewportAvailable() {
  return typeof window !== 'undefined' && window.visualViewport != null;
}

/**
 * useVisualViewportInset - tracks how many pixels of the layout viewport's
 * bottom edge are currently covered by an on-screen keyboard (or any other
 * visualViewport-shrinking overlay), via the visualViewport API.
 *
 * Returns 0 when the API is unavailable (desktop browsers, older mobile
 * browsers, jsdom) - a graceful no-op rather than an error, since keyboard
 * awareness is a progressive enhancement on top of the sheet already working.
 *
 * `enabled` (default true) skips subscribing entirely - pass false from a
 * caller that only needs this value in one of several render paths (e.g.
 * ChatPanel's desktop "floating" variant, which never reads the result) so
 * every resize/scroll on that path doesn't force a re-render for a value
 * nothing consumes.
 */
export function useVisualViewportInset(enabled = true) {
  const [inset, setInset] = useState(0);

  useEffect(() => {
    if (!enabled || !isVisualViewportAvailable()) return undefined;
    const vv = window.visualViewport;

    const update = () => {
      // window.innerHeight is the layout viewport (stable across keyboard
      // show/hide on most browsers); visualViewport shrinks and offsets when
      // the keyboard opens. The gap between them is the keyboard height.
      const keyboardInset = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
      setInset(keyboardInset);
    };

    update();
    vv.addEventListener('resize', update);
    vv.addEventListener('scroll', update);
    return () => {
      vv.removeEventListener('resize', update);
      vv.removeEventListener('scroll', update);
      // Reset on the way out (enabled flipping to false, or unmount) so a
      // caller that later re-enables starts clean rather than carrying a
      // stale inset from before the subscription was torn down.
      setInset(0);
    };
  }, [enabled]);

  return inset;
}
