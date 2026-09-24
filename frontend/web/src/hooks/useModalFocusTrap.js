import { useCallback, useEffect, useRef } from 'react';

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function getFocusableElements(container) {
  if (!container) return [];
  return Array.from(container.querySelectorAll(FOCUSABLE_SELECTOR));
}

/**
 * useModalFocusTrap - the modal focus contract shared by BottomSheet and the
 * mobile SessionDrawer overlay: while `active`, body scroll is locked, focus
 * moves into `containerRef` (its first focusable descendant, else the
 * container itself), and on deactivation both the previous overflow value
 * and the previously focused element are restored.
 *
 * Returns `trapTabKey(event)`, which callers invoke from their own keydown
 * handler: it wraps Tab / Shift+Tab at the ends of the container and pulls
 * focus back in when it has escaped. Escape is left to the caller, because
 * the two consumers close differently (BottomSheet listens on its own
 * element; SessionDrawer listens on the document and peels menus first).
 */
export function useModalFocusTrap(containerRef, active) {
  const lastFocusedRef = useRef(null);

  // Restores whatever value was there before, so a modal opened while some
  // other overlay already locked scroll doesn't clobber that lock on close.
  useEffect(() => {
    if (!active || typeof document === 'undefined') return undefined;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [active]);

  useEffect(() => {
    if (!active) return undefined;
    lastFocusedRef.current = typeof document !== 'undefined' ? document.activeElement : null;

    const focusable = getFocusableElements(containerRef.current);
    (focusable[0] || containerRef.current)?.focus();

    return () => {
      const toRestore = lastFocusedRef.current;
      if (toRestore && typeof toRestore.focus === 'function' && document.contains(toRestore)) {
        toRestore.focus();
      }
    };
  }, [active, containerRef]);

  return useCallback(
    (event) => {
      if (event.key !== 'Tab') return;

      const focusables = getFocusableElements(containerRef.current);
      if (focusables.length === 0) {
        event.preventDefault();
        return;
      }

      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const activeElement = document.activeElement;

      if (event.shiftKey && activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && activeElement === last) {
        event.preventDefault();
        first.focus();
      } else if (!focusables.includes(activeElement)) {
        // Focus escaped the container (e.g. programmatic blur) - pull it back in.
        event.preventDefault();
        first.focus();
      }
    },
    [containerRef]
  );
}
