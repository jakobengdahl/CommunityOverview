import { useEffect, useRef } from 'react';
import './ContextMenu.css';

function ContextMenu({ x, y, onClose, onAddRectangle }) {
  const menuRef = useRef(null);

  useEffect(() => {
    const handleClickOutside = (e) => {
      if (menuRef.current && !menuRef.current.contains(e.target)) {
        onClose();
      }
    };

    const handleEscape = (e) => {
      if (e.key === 'Escape') {
        onClose();
      }
    };

    document.addEventListener('mousedown', handleClickOutside);
    document.addEventListener('keydown', handleEscape);

    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
      document.removeEventListener('keydown', handleEscape);
    };
  }, [onClose]);

  return (
    <div
      ref={menuRef}
      className="context-menu"
      style={{ left: x, top: y }}
    >
      <div className="context-menu-header">Add Element</div>
      <button
        className="context-menu-item"
        onClick={() => {
          onAddRectangle();
          onClose();
        }}
      >
        <span className="context-menu-icon">▭</span>
        Rectangle
      </button>
      {/* Future additions can go here */}
      {/* <button className="context-menu-item">
        <span className="context-menu-icon">⬮</span>
        Circle
      </button> */}
    </div>
  );
}

export default ContextMenu;
