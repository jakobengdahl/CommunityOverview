import { useState, useEffect } from 'react';

const MOBILE_QUERY = '(max-width: 768px)';
const COARSE_POINTER_QUERY = '(pointer: coarse)';

// jsdom (the test environment) has no matchMedia implementation, so this must
// degrade gracefully rather than throw and break every test that mounts App.
function isMatchMediaAvailable() {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function';
}

// Subscribes to a single media query and keeps `matches` in sync. isMobile and
// isCoarsePointer are independent signals (a desktop with a touchscreen can be
// coarse-pointer but not mobile-width, and vice versa) so each query gets its
// own subscription rather than being derived from one another.
function useMediaQuery(query) {
  const [matches, setMatches] = useState(() => {
    if (!isMatchMediaAvailable()) return false;
    return window.matchMedia(query).matches;
  });

  useEffect(() => {
    if (!isMatchMediaAvailable()) return undefined;

    // The lazy useState initializer above already captured this query's value
    // as of mount, so this effect only needs to subscribe for future changes.
    const mql = window.matchMedia(query);
    const handleChange = (event) => setMatches(event.matches);

    mql.addEventListener('change', handleChange);
    return () => mql.removeEventListener('change', handleChange);
  }, [query]);

  return matches;
}

export function useViewportMode() {
  const isMobile = useMediaQuery(MOBILE_QUERY);
  const isCoarsePointer = useMediaQuery(COARSE_POINTER_QUERY);
  const [width, setWidth] = useState(() =>
    typeof window !== 'undefined' ? window.innerWidth : 0
  );

  useEffect(() => {
    if (typeof window === 'undefined') return undefined;

    const handleResize = () => setWidth(window.innerWidth);
    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  return { isMobile, isCoarsePointer, width };
}
