import { useState, useEffect, useRef } from 'react';
import './InputDialog.css';

/**
 * InputDialog - Modal for text input
 */
function InputDialog({
  title = 'Enter value',
  label = '',
  placeholder = '',
  defaultValue = '',
  confirmText = 'Save',
  cancelText = 'Cancel',
  loadingText = 'Saving...',
  isLoading = false,
  allowEmpty = false,
  onConfirm,
  onCancel,
}) {
  const [value, setValue] = useState(defaultValue);
  const inputRef = useRef(null);

  // Focus input on mount
  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  const canSubmit = (allowEmpty || value.trim()) && !isLoading;

  // Handle keyboard events
  const handleKeyDown = (e) => {
    if (e.key === 'Escape' && !isLoading) {
      onCancel();
    } else if (e.key === 'Enter' && canSubmit) {
      onConfirm(value.trim());
    }
  };

  const handleSubmit = () => {
    if (canSubmit) {
      onConfirm(value.trim());
    }
  };

  return (
    <div className="input-dialog-overlay" onClick={onCancel}>
      <div className="input-dialog" onClick={(e) => e.stopPropagation()} onKeyDown={handleKeyDown}>
        <div className="input-dialog-header">
          <h3>{title}</h3>
        </div>

        <div className="input-dialog-content">
          {label && <label className="input-dialog-label">{label}</label>}
          <input
            ref={inputRef}
            type="text"
            className="input-dialog-input"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={placeholder}
            disabled={isLoading}
          />
        </div>

        <div className="input-dialog-actions">
          <button className="input-dialog-button cancel" onClick={onCancel} disabled={isLoading}>
            {cancelText}
          </button>
          <button
            className="input-dialog-button primary"
            onClick={handleSubmit}
            disabled={!canSubmit}
          >
            {isLoading ? loadingText : confirmText}
          </button>
        </div>
      </div>
    </div>
  );
}

export default InputDialog;
