import { useEffect, useLayoutEffect, useRef } from 'react';
import { ThreeDotsVertical } from 'react-bootstrap-icons';
import './SessionContextMenu.css';

function positionMenu(anchor, width, height, viewportWidth, viewportHeight) {
  const margin = 8;
  const clamp = (value, size, viewport) =>
    Math.max(margin, Math.min(value, viewport - size - margin));
  const left = anchor.left + width > viewportWidth - margin ? anchor.right - width : anchor.left;
  const top =
    anchor.bottom + height > viewportHeight - margin ? anchor.top - height : anchor.bottom;
  return { left: clamp(left, width, viewportWidth), top: clamp(top, height, viewportHeight) };
}

/**
 * SessionContextMenu — a compact kebab menu of actions for a single session
 * row. The action list is passed in as `items`, so the surface stays
 * extensible: post-v1 controls (write-mode, unmerged-change, merge) are added
 * by pushing more descriptors, not by rewriting the drawer or the canvas.
 *
 * The menu is controlled by its parent (one open menu at a time), which also
 * owns Escape handling so it can decide between closing the menu and closing
 * the drawer behind it.
 *
 * @param {boolean} open           Whether this menu is expanded.
 * @param {() => void} onToggle    Toggle open/closed (kebab button click).
 * @param {() => void} onClose     Request close (item chosen or click outside).
 * @param {Array<{key: string, label: string, icon?: React.ReactNode,
 *   onClick: () => void, danger?: boolean, disabled?: boolean}>} items
 * @param {string} triggerLabel    Accessible label for the kebab button.
 * @param {{x: number, y: number} | null} pointerPosition
 *   When set, positions the dropdown at the given coordinates using
 *   `position: fixed` so a context-menu (right-click / long-press) can
 *   open the same surface at the pointer rather than below the kebab.
 * @param {{x: number, y: number, width: number, height: number} | null} kebabAnchor
 *   When set (kebab-triggered open), positions the dropdown using this rect
 *   instead of measuring the trigger button.  Takes precedence over pointerPosition when
 *   present; ignored when the menu is closed.
 */
function SessionContextMenu({
  open,
  onToggle,
  onClose,
  items,
  triggerLabel,
  pointerPosition,
  kebabAnchor,
}) {
  const rootRef = useRef(null);

  // Close on a click anywhere outside the menu. The kebab button lives inside
  // rootRef, so clicking it is "inside" and never triggers this handler —
  // leaving onToggle free to close an already-open menu without reopening it.
  useEffect(() => {
    if (!open) return;
    const handlePointerDown = (e) => {
      if (rootRef.current && !rootRef.current.contains(e.target)) onClose();
    };
    document.addEventListener('pointerdown', handlePointerDown);
    return () => document.removeEventListener('pointerdown', handlePointerDown);
  }, [open, onClose]);

  // Both opening methods use viewport coordinates, measured before paint.
  useLayoutEffect(() => {
    if (!open) return;
    const menu = rootRef.current?.querySelector('.session-context-menu-dropdown');
    if (!menu) return;
    const button = kebabAnchor || rootRef.current.querySelector('button').getBoundingClientRect();
    const anchor =
      kebabAnchor || !pointerPosition
        ? {
            left: button.x,
            right: button.x + button.width,
            top: button.y,
            bottom: button.y + button.height,
          }
        : {
            left: pointerPosition.x,
            right: pointerPosition.x,
            top: pointerPosition.y,
            bottom: pointerPosition.y,
          };
    const { width, height } = menu.getBoundingClientRect();
    const position = positionMenu(anchor, width, height, window.innerWidth, window.innerHeight);
    menu.style.left = `${position.left}px`;
    menu.style.top = `${position.top}px`;
  }, [open, pointerPosition, kebabAnchor]);

  return (
    <div className="session-context-menu" ref={rootRef}>
      <button
        type="button"
        className={`session-context-menu-trigger${open ? ' open' : ''}`}
        onClick={onToggle}
        title={triggerLabel}
        aria-label={triggerLabel}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <ThreeDotsVertical size={15} />
      </button>
      {open && (
        <div
          className={`session-context-menu-dropdown${pointerPosition && !kebabAnchor ? ' pointer-triggered' : ''}`}
          role="menu"
        >
          {items.map((item) => (
            <button
              key={item.key}
              type="button"
              role="menuitem"
              className={`session-context-menu-item${item.danger ? ' danger' : ''}`}
              disabled={item.disabled}
              onClick={() => {
                onClose();
                item.onClick();
              }}
            >
              {item.icon}
              <span>{item.label}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export default SessionContextMenu;
