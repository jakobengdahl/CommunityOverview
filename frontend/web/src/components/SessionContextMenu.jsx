import { useEffect, useRef } from 'react';
import { ThreeDotsVertical } from 'react-bootstrap-icons';
import './SessionContextMenu.css';

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
 */
function SessionContextMenu({ open, onToggle, onClose, items, triggerLabel }) {
  const rootRef = useRef(null);

  // Close on a click anywhere outside the menu. The kebab button lives inside
  // rootRef, so clicking it is "inside" and never triggers this handler —
  // leaving onToggle free to close an already-open menu without reopening it.
  useEffect(() => {
    if (!open) return;
    const handlePointerDown = (e) => {
      if (rootRef.current && !rootRef.current.contains(e.target)) onClose();
    };
    document.addEventListener('mousedown', handlePointerDown);
    return () => document.removeEventListener('mousedown', handlePointerDown);
  }, [open, onClose]);

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
        <div className="session-context-menu-dropdown" role="menu">
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
