import { useEffect, useRef } from 'react';
import './ContextMenu.css';

function ContextMenu({ x, y, onClose, onAddGroup, onSaveView }) {
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
      <div className="context-menu-header">Visualisering</div>

      {onSaveView && (
        <button
          className="context-menu-item"
          onClick={() => {
            onSaveView();
            onClose();
          }}
        >
          <span className="context-menu-icon">💾</span>
          Skapa visualisering
        </button>
      )}

      <button
        className="context-menu-item"
        onClick={() => {
          onAddGroup();
          onClose();
        }}
      >
        <span className="context-menu-icon">📁</span>
        Skapa grupp
      </button>
    </div>
  );
}

export default ContextMenu;
