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
 */
export function useVisualViewportInset() {
  const [inset, setInset] = useState(0);

  useEffect(() => {
    if (!isVisualViewportAvailable()) return undefined;
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
    };
  }, []);

  return inset;
}
