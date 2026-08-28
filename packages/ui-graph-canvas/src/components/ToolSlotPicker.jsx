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
 * without needing a second ref just for vertical placement.
 *
 * Dismisses on Escape or an outside click/tap and returns focus to
 * `returnFocusRef`'s element, the same convention ContextMenus.jsx's
 * `Submenu` uses for its own flyout.
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
    panel.style.bottom = `${window.innerHeight - rect.top + 8}px`;
    panel.querySelector('button')?.focus();
  }, [anchorRef]);

  // Returns focus to whatever opened the picker (the corner button,
  // typically) once it closes for any reason — Escape, an outside
  // click/tap, or picking an option — mirrors useMenuOpenFocus in
  // ContextMenus.jsx.
  useEffect(
    () => () => {
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
      onClose();
    };
    document.addEventListener('mousedown', handleOutside, true);
    return () => document.removeEventListener('mousedown', handleOutside, true);
  }, [anchorRef, onClose]);

  const handleKeyDown = (event) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      onClose();
    }
  };

  return createPortal(
    <div ref={panelRef} role="group" aria-label={ariaLabel} className="tool-slot-picker" onKeyDown={handleKeyDown}>
      {options.map((option) => (
        <button
          key={option.key}
          type="button"
          aria-pressed={option.key === currentKey}
          className={`tool-slot-picker-item${
            option.key === currentKey ? ' tool-slot-picker-item--current' : ''
          }`}
          onClick={() => onSelect(option.key)}
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
