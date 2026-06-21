import { useState, useEffect, useRef, useCallback } from 'react';
import './SubtypeInput.css';

/**
 * SubtypeInput - Autocomplete input for node subtypes.
 *
 * Features:
 * - Shows existing subtypes for the given node type on focus
 * - Filters suggestions as user types
 * - Enter selects top suggestion
 * - Comma adds current text as a new subtype
 * - Case normalization: matches existing casing
 * - Multiple subtypes displayed as removable chips
 */
function SubtypeInput({ value = [], onChange, existingSubtypes = [], label = 'Subtypes' }) {
  const [inputValue, setInputValue] = useState('');
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const inputRef = useRef(null);
  const wrapperRef = useRef(null);

  // Filter suggestions based on current input, excluding already-selected values
  const filteredSuggestions = existingSubtypes.filter(s => {
    const alreadySelected = value.some(v => v.toLowerCase() === s.toLowerCase());
    if (alreadySelected) return false;
    if (!inputValue.trim()) return true;
    return s.toLowerCase().includes(inputValue.toLowerCase());
  });

  // Reset selection index when suggestions change
  useEffect(() => {
    setSelectedIndex(0);
  }, [inputValue, existingSubtypes.length]);

  // Close suggestions on outside click
  useEffect(() => {
    const handleClickOutside = (e) => {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target)) {
        setShowSuggestions(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  // Normalize case: if an existing subtype matches case-insensitively, use existing casing
  const normalizeCase = useCallback((text) => {
    const trimmed = text.trim();
    if (!trimmed) return '';
    const match = existingSubtypes.find(s => s.toLowerCase() === trimmed.toLowerCase());
    return match || trimmed;
  }, [existingSubtypes]);

  const addSubtype = useCallback((text) => {
    const normalized = normalizeCase(text);
    if (!normalized) return;
    // Don't add duplicates (case-insensitive)
    if (value.some(v => v.toLowerCase() === normalized.toLowerCase())) return;
    onChange([...value, normalized]);
    setInputValue('');
    setShowSuggestions(true);
  }, [value, onChange, normalizeCase]);

  const removeSubtype = useCallback((index) => {
    onChange(value.filter((_, i) => i !== index));
  }, [value, onChange]);

  const handleKeyDown = (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      if (filteredSuggestions.length > 0 && showSuggestions) {
        addSubtype(filteredSuggestions[selectedIndex] || filteredSuggestions[0]);
      } else if (inputValue.trim()) {
        addSubtype(inputValue);
      }
    } else if (e.key === ',') {
      e.preventDefault();
      if (inputValue.trim()) {
        addSubtype(inputValue);
      }
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      setSelectedIndex(i => Math.min(i + 1, filteredSuggestions.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setSelectedIndex(i => Math.max(i - 1, 0));
    } else if (e.key === 'Backspace' && !inputValue && value.length > 0) {
      removeSubtype(value.length - 1);
    } else if (e.key === 'Escape') {
      setShowSuggestions(false);
    }
  };

  const handleChange = (e) => {
    setInputValue(e.target.value);
    setShowSuggestions(true);
  };

  const handleFocus = () => {
    setShowSuggestions(true);
  };

  return (
    <div className="subtype-input-wrapper" ref={wrapperRef}>
      <label className="subtype-label">{label}</label>
      <div className="subtype-input-container" onClick={() => inputRef.current?.focus()}>
        {value.map((subtype, index) => (
          <span key={index} className="subtype-chip">
            {subtype}
            <button
              type="button"
              className="subtype-chip-remove"
              onClick={(e) => { e.stopPropagation(); removeSubtype(index); }}
            >
              ×
            </button>
          </span>
        ))}
        <input
          ref={inputRef}
          type="text"
          className="subtype-text-input"
          value={inputValue}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          onFocus={handleFocus}
          placeholder={value.length === 0 ? 'Type to add...' : ''}
        />
      </div>
      {showSuggestions && filteredSuggestions.length > 0 && (
        <ul className="subtype-suggestions">
          {filteredSuggestions.map((suggestion, index) => (
            <li
              key={suggestion}
              className={`subtype-suggestion-item ${index === selectedIndex ? 'selected' : ''}`}
              onMouseDown={(e) => { e.preventDefault(); addSubtype(suggestion); }}
              onMouseEnter={() => setSelectedIndex(index)}
            >
              {suggestion}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default SubtypeInput;
