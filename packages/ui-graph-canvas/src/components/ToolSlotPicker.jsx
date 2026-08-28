import { useEffect, useLayoutEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import './ToolSlotPicker.css';

/**
 * Fold-out list opened by a collapsed "tool slot" button (see
 * `useToolSlotSelection`). AnnotationToolbox's shape slot is the first
 * caller; task-annotation-icon-slot-and-visuals is meant to reuse this
 * component for its icon slot rather than growing a second, hand-copied
 * fold-out — the two should look and behave identically.
 *
 * Portalled to document.body (the toolbox's own stacking/clip context would
 * otherwise cut it off, the same reason AnnotationToolbox's hover tooltip is
 * portalled) and positioned above `anchorRef`'s element, centred on it — "a
 * fold-out above the toolbox" per the owner's spec; anchoring on the slot
 * itself, which sits inside the bottom-anchored toolbox, satisfies that
 * without needing a second ref just for vertical placement. Uses `top`
 * (the anchor's own top edge, minus a gap) together with the CSS
 * `translateY(-100%)`, the exact same pairing AnnotationToolbox's own hover
 * tooltip uses (see `showTip`) to sit flush above a point — pairing that
 * transform with `bottom` instead would double-offset the panel upward by
 * its own height on top of the intended gap.
 *
 * Dismisses on Escape or an outside click/tap and returns focus to
 * `returnFocusRef`'s element, the same convention ContextMenus.jsx's
 * `useMenuOpenFocus` uses for its own flyouts — including not stealing focus
 * back on close if it had already moved elsewhere on the page while the
 * picker was open (e.g. the outside click that closed it landed on a real
 * focusable control, not just the canvas background).
 */
export function ToolSlotPicker({
  anchorRef,
  returnFocusRef,
  options,
  currentKey,
  onSelect,
  onClose,
  ariaLabel,
}) {
  const panelRef = useRef(null);
  const focusMovedAwayRef = useRef(false);
  // Kept current via refs (not effect dependencies) so the outside-click
  // listener below is wired up once per mount rather than torn down and
  // re-added on every unrelated re-render of the host toolbox while the
  // picker happens to be open — `onSelect`/`onClose` are fresh inline
  // closures from the caller's render on every such render.
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  // Measures and positions the panel imperatively (rather than round-tripping
  // through React state) so the very first paint already shows it in the
  // right place, with no unpositioned/default-position frame in between —
  // and focuses the first option once it is actually placed.
  useLayoutEffect(() => {
    const anchor = anchorRef.current;
    const panel = panelRef.current;
    if (!anchor || !panel) return;
    const rect = anchor.getBoundingClientRect();
    panel.style.left = `${rect.left + rect.width / 2}px`;
    panel.style.top = `${rect.top - 8}px`;
    panel.querySelector('button')?.focus();
  }, [anchorRef]);

  // Tracks whether focus already moved somewhere else on the page while the
  // picker was open, so the close effect below doesn't yank it back out of
  // wherever the user actually clicked/tabbed to.
  useEffect(() => {
    focusMovedAwayRef.current = false;
    const handleFocusIn = (event) => {
      if (panelRef.current && !panelRef.current.contains(event.target)) {
        focusMovedAwayRef.current = true;
      }
    };
    document.addEventListener('focusin', handleFocusIn, true);
    return () => document.removeEventListener('focusin', handleFocusIn, true);
  }, []);

  // Returns focus to whatever opened the picker (the corner button,
  // typically) once it closes for any reason — Escape, an outside
  // click/tap, or picking an option — unless focus already moved away on
  // its own (see the effect above).
  useEffect(
    () => () => {
      if (focusMovedAwayRef.current) return;
      const target = returnFocusRef?.current;
      if (target && typeof target.focus === 'function' && document.contains(target)) {
        target.focus();
      }
    },
    [returnFocusRef]
  );

  useEffect(() => {
    const handleOutside = (event) => {
      if (panelRef.current?.contains(event.target)) return;
      if (anchorRef.current?.contains(event.target)) return;
      onCloseRef.current();
    };
    document.addEventListener('mousedown', handleOutside, true);
    return () => document.removeEventListener('mousedown', handleOutside, true);
  }, [anchorRef]);

  // Document-level, not the panel's own onKeyDown: focus can legitimately be
  // on the corner button that opened the picker rather than inside the panel
  // (e.g. a second click on an already-open picker's corner button re-focuses
  // it natively without remounting/refocusing the panel) — Escape has to
  // close the picker from there too, not only while focus is inside it.
  useEffect(() => {
    const handleKeyDown = (event) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      onCloseRef.current();
    };
    document.addEventListener('keydown', handleKeyDown, true);
    return () => document.removeEventListener('keydown', handleKeyDown, true);
  }, []);

  return createPortal(
    <div ref={panelRef} role="group" aria-label={ariaLabel} className="tool-slot-picker">
      {options.map((option) => (
        <button
          key={option.key}
          type="button"
          aria-pressed={option.key === currentKey}
          className={`tool-slot-picker-item${
            option.key === currentKey ? ' tool-slot-picker-item--current' : ''
          }`}
          onClick={() => onSelectRef.current(option.key)}
        >
          <span className="tool-slot-picker-item-glyph" aria-hidden="true">
            {option.glyph}
          </span>
          <span className="tool-slot-picker-item-label">{option.label}</span>
        </button>
      ))}
    </div>,
    document.body
  );
}

export default ToolSlotPicker;
