import { useEffect, useRef } from 'react';
import './ContextMenu.css';

function NodeContextMenu({ x, y, node, onClose, onEdit, onHide, onDelete, onExpand, onShowInNewView }) {
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

  const handleDelete = () => {
    if (window.confirm(`Är du säker på att du vill ta bort "${node.data.label}"? Detta tar bort noden permanent från grafen.`)) {
      onDelete(node.id);
      onClose();
    }
  };

  return (
    <div
      ref={menuRef}
      className="context-menu"
      style={{ left: x, top: y }}
    >
      <div className="context-menu-header">{node.data.label}</div>

      <button
        className="context-menu-item"
        onClick={() => {
          onEdit(node);
          onClose();
        }}
      >
        <span className="context-menu-icon">✏️</span>
        Redigera
      </button>

      <button
        className="context-menu-item"
        onClick={() => {
          onHide(node.id);
          onClose();
        }}
      >
        <span className="context-menu-icon">👁️</span>
        Dölj
      </button>

      <button
        className="context-menu-item"
        onClick={() => {
          onExpand(node.id);
          onClose();
        }}
      >
        <span className="context-menu-icon">🔍</span>
        Expandera
      </button>

      <button
        className="context-menu-item"
        onClick={() => {
          onShowInNewView(node.id);
          onClose();
        }}
      >
        <span className="context-menu-icon">🎯</span>
        Visa i ny visualisering
      </button>

      <div className="context-menu-separator"></div>

      <button
        className="context-menu-item context-menu-item-danger"
        onClick={handleDelete}
      >
        <span className="context-menu-icon">🗑️</span>
        Ta bort
      </button>
    </div>
  );
}

export default NodeContextMenu;
