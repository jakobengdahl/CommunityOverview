import { useCallback, useState } from 'react';

// Every valid mobile surface, plus the closed state. Keeping the list here
// (rather than letting callers pass arbitrary strings) is what lets
// mutual-exclusion be enforced for real: open() rejects anything not in this
// set instead of silently tracking an unknown surface as "the" open one.
export const SURFACES = ['none', 'search', 'create', 'annotate', 'chat', 'menu', 'detail'];

/**
 * useSurfaceManager - tracks which single mobile surface (bottom sheet,
 * panel, etc.) is currently open. Opening one surface always closes
 * whichever other surface was open, so at most one is ever open at once.
 *
 * This hook owns no rendering - it is the shared state a future App.jsx
 * wiring would read to decide which BottomSheet instance is mounted.
 */
export function useSurfaceManager(initialSurface = 'none') {
  const [openSurface, setOpenSurface] = useState(
    SURFACES.includes(initialSurface) ? initialSurface : 'none'
  );

  const open = useCallback((surface) => {
    if (!SURFACES.includes(surface)) {
      throw new Error(`useSurfaceManager: unknown surface "${surface}"`);
    }
    setOpenSurface(surface);
  }, []);

  const close = useCallback(() => {
    setOpenSurface('none');
  }, []);

  const toggle = useCallback((surface) => {
    if (!SURFACES.includes(surface)) {
      throw new Error(`useSurfaceManager: unknown surface "${surface}"`);
    }
    setOpenSurface((current) => (current === surface ? 'none' : surface));
  }, []);

  const isOpen = useCallback((surface) => openSurface === surface, [openSurface]);

  return {
    openSurface,
    isOpen,
    open,
    close,
    toggle,
  };
}
