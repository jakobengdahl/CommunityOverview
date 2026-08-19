import { useCallback, useEffect, useRef, useState } from 'react';
import { useI18n } from '../i18n';
import './BottomSheet.css';

// Ordered low-to-high; index arithmetic in the drag handler and tests relies
// on this order, not just membership.
export const SNAP_POINTS = ['peek', 'half', 'full'];

const SNAP_HEIGHTS = {
  peek: 0.16,
  half: 0.5,
  full: 0.92,
};

const DRAG_THRESHOLD_PX = 60;

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

function isReducedMotionSupported() {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function';
}

// Mirrors the useMediaQuery pattern in useViewportMode.js: jsdom has no
// matchMedia, so this must degrade to "no reduced motion" rather than throw.
function usePrefersReducedMotion() {
  const [reduced, setReduced] = useState(() => {
    if (!isReducedMotionSupported()) return false;
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  });

  useEffect(() => {
    if (!isReducedMotionSupported()) return undefined;
    const mql = window.matchMedia('(prefers-reduced-motion: reduce)');
    const handleChange = (event) => setReduced(event.matches);
    mql.addEventListener('change', handleChange);
    return () => mql.removeEventListener('change', handleChange);
  }, []);

  return reduced;
}

function getFocusableElements(container) {
  if (!container) return [];
  return Array.from(container.querySelectorAll(FOCUSABLE_SELECTOR)).filter(
    (el) => !el.hasAttribute('data-focus-guard')
  );
}

/**
 * BottomSheet - reusable mobile sheet primitive with three snap points
 * (peek / half / full), a drag handle, a backdrop scrim, a focus trap,
 * Escape-to-close and body-scroll-lock while open.
 *
 * This is an unwired primitive: it owns no app state about *which* surface
 * is open (see useSurfaceManager for that) and is not mounted from App.jsx
 * yet - callers control `isOpen`/`snapPoint` and render whatever content
 * they like as children.
 */
function BottomSheet({
  isOpen,
  snapPoint = 'half',
  onSnapPointChange,
  onClose,
  title,
  closeLabel,
  dragHandleLabel,
  children,
  className = '',
}) {
  const { t } = useI18n();
  const resolvedCloseLabel = closeLabel ?? t('bottom_sheet.close');
  const resolvedDragHandleLabel = dragHandleLabel ?? t('bottom_sheet.drag_handle');
  const prefersReducedMotion = usePrefersReducedMotion();

  const sheetRef = useRef(null);
  const lastFocusedRef = useRef(null);
  const dragStateRef = useRef(null);
  const [dragOffset, setDragOffset] = useState(0);

  // Body scroll lock while open - restores whatever value was there before,
  // so a sheet opened while some other overlay already locked scroll doesn't
  // clobber that lock on close.
  useEffect(() => {
    if (!isOpen || typeof document === 'undefined') return undefined;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [isOpen]);

  // Move focus into the sheet on open, restore it to whatever had focus
  // beforehand on close - the standard modal focus-management contract.
  useEffect(() => {
    if (!isOpen) return undefined;
    lastFocusedRef.current = typeof document !== 'undefined' ? document.activeElement : null;

    const focusable = getFocusableElements(sheetRef.current);
    (focusable[0] || sheetRef.current)?.focus();

    return () => {
      const toRestore = lastFocusedRef.current;
      if (toRestore && typeof toRestore.focus === 'function' && document.contains(toRestore)) {
        toRestore.focus();
      }
    };
  }, [isOpen]);

  const handleKeyDown = useCallback(
    (event) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        onClose?.();
        return;
      }
      if (event.key !== 'Tab') return;

      const focusables = getFocusableElements(sheetRef.current);
      if (focusables.length === 0) {
        event.preventDefault();
        return;
      }

      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement;

      if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      } else if (!focusables.includes(active)) {
        // Focus escaped the sheet (e.g. programmatic blur) - pull it back in.
        event.preventDefault();
        first.focus();
      }
    },
    [onClose]
  );

  const handlePointerDown = useCallback((event) => {
    dragStateRef.current = { startY: event.clientY };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  }, []);

  const handlePointerMove = useCallback((event) => {
    if (!dragStateRef.current) return;
    const delta = event.clientY - dragStateRef.current.startY;
    setDragOffset(Math.max(0, delta));
  }, []);

  const finishDrag = useCallback(
    (event) => {
      if (!dragStateRef.current) return;
      const delta = event.clientY - dragStateRef.current.startY;
      dragStateRef.current = null;
      setDragOffset(0);

      const currentIndex = SNAP_POINTS.indexOf(snapPoint);

      if (delta > DRAG_THRESHOLD_PX) {
        if (currentIndex <= 0) {
          onClose?.();
        } else {
          onSnapPointChange?.(SNAP_POINTS[currentIndex - 1]);
        }
      } else if (delta < -DRAG_THRESHOLD_PX) {
        const nextIndex = Math.min(SNAP_POINTS.length - 1, currentIndex + 1);
        if (nextIndex !== currentIndex) {
          onSnapPointChange?.(SNAP_POINTS[nextIndex]);
        }
      }
    },
    [snapPoint, onClose, onSnapPointChange]
  );

  if (!isOpen) return null;

  const heightRatio = SNAP_HEIGHTS[snapPoint] ?? SNAP_HEIGHTS.half;
  const sheetClassName = [
    'bottom-sheet',
    `bottom-sheet--${snapPoint}`,
    prefersReducedMotion ? 'bottom-sheet--no-motion' : '',
    className,
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <div className="bottom-sheet-scrim" onClick={onClose} data-testid="bottom-sheet-scrim">
      <div
        ref={sheetRef}
        className={sheetClassName}
        style={{
          height: `${heightRatio * 100}%`,
          transform: dragOffset ? `translateY(${dragOffset}px)` : undefined,
        }}
        role="dialog"
        aria-modal="true"
        aria-label={title || resolvedCloseLabel}
        tabIndex={-1}
        onClick={(event) => event.stopPropagation()}
        onKeyDown={handleKeyDown}
      >
        <div
          className="bottom-sheet-handle"
          role="slider"
          aria-label={resolvedDragHandleLabel}
          aria-valuemin={0}
          aria-valuemax={SNAP_POINTS.length - 1}
          aria-valuenow={SNAP_POINTS.indexOf(snapPoint)}
          aria-valuetext={snapPoint}
          // Deliberately not in the tab order (drag-only control): the focus
          // trap should land keyboard users on real sheet content, not on a
          // handle that isn't keyboard-operable yet.
          tabIndex={-1}
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={finishDrag}
          onPointerCancel={finishDrag}
        >
          <span className="bottom-sheet-handle-bar" aria-hidden="true" />
        </div>

        {title ? (
          <div className="bottom-sheet-header">
            <h2 className="bottom-sheet-title">{title}</h2>
            <button
              type="button"
              className="bottom-sheet-close"
              onClick={onClose}
              aria-label={resolvedCloseLabel}
            >
              &times;
            </button>
          </div>
        ) : null}

        <div className="bottom-sheet-content">{children}</div>
      </div>
    </div>
  );
}

export default BottomSheet;
