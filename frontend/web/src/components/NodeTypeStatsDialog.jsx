import { useEffect, useRef } from 'react';
import { XLg } from 'react-bootstrap-icons';
import { COLOR_MAP } from './FloatingToolbar';
import './NodeTypeStatsDialog.css';

function NodeTypeStatsDialog({ nodesByType, onClose }) {
  const dialogRef = useRef(null);

  useEffect(() => {
    dialogRef.current?.focus();
  }, []);

  const handleKeyDown = (e) => {
    if (e.key === 'Escape') onClose();
  };

  const sorted = Object.entries(nodesByType).sort(([, a], [, b]) => b - a);

  return (
    <div className="nts-dialog-overlay" onClick={onClose}>
      <div
        ref={dialogRef}
        className="nts-dialog"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={handleKeyDown}
        tabIndex={-1}
      >
        <div className="nts-dialog-header">
          <span className="nts-dialog-title">Nodes by type</span>
          <button className="nts-dialog-close" onClick={onClose} aria-label="Close">
            <XLg size={14} />
          </button>
        </div>

        <div className="nts-dialog-body">
          {sorted.map(([type, count]) => (
            <div key={type} className="nts-dialog-row">
              <span
                className="nts-dialog-dot"
                style={{ backgroundColor: COLOR_MAP[type] || '#9CA3AF' }}
              />
              <span className="nts-dialog-type">{type}</span>
              <span className="nts-dialog-count">{count}</span>
            </div>
          ))}
        </div>

        <div className="nts-dialog-footer">
          <button className="nts-dialog-ok" onClick={onClose}>OK</button>
        </div>
      </div>
    </div>
  );
}

export default NodeTypeStatsDialog;
