import { useEffect, useRef, useState } from 'react';
import './ContextMenu.css';

function GroupContextMenu({ x, y, onClose, onChangeColor, onDelete }) {
  const menuRef = useRef(null);
  const [showColorPicker, setShowColorPicker] = useState(false);

  const colors = [
    { name: 'Blue', value: '#646cff' },
    { name: 'Purple', value: '#A855F7' },
    { name: 'Green', value: '#10B981' },
    { name: 'Red', value: '#EF4444' },
    { name: 'Orange', value: '#F59E0B' },
    { name: 'Pink', value: '#EC4899' },
    { name: 'Teal', value: '#14B8A6' },
    { name: 'Indigo', value: '#6366F1' },
  ];

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

  const handleColorSelect = (color) => {
    onChangeColor(color);
    onClose();
  };

  return (
    <div
      ref={menuRef}
      className="context-menu"
      style={{ left: x, top: y }}
    >
      <div className="context-menu-header">Gruppinställningar</div>

      <div className="context-menu-section-label">Välj Färg</div>
      <div className="color-picker-grid">
        {colors.map((color) => (
          <button
            key={color.value}
            className="color-picker-item"
            style={{ backgroundColor: color.value }}
            onClick={() => handleColorSelect(color.value)}
            title={color.name}
          />
        ))}
      </div>

      {onDelete && (
        <>
          <div className="context-menu-separator"></div>
          <button
            className="context-menu-item context-menu-item-danger"
            onClick={() => {
              if (window.confirm('Är du säker på att du vill ta bort denna grupp?')) {
                onDelete();
                onClose();
              }
            }}
          >
            <span className="context-menu-icon">🗑️</span>
            Ta bort grupp
          </button>
        </>
      )}
    </div>
  );
}

export default GroupContextMenu;
