import { useState } from 'react';
import './CollectionForm.css';

/**
 * CollectionForm — renders an AI-authored input form (radio buttons, checkboxes,
 * sliders, dropdowns, text) inside a chat message and reports the answers on submit.
 *
 * Used by both the collection kiosk (CollectKioskView) and the full-app chat
 * (ChatPanel) when the assistant calls the `present_form` tool. The package has no
 * access to the host i18n system, so fixed UI strings are accepted via `labels`
 * with English defaults (same pattern as GraphCanvas' contextMenuLabels).
 *
 * Props:
 *   - form: { title?, description?, submit_label?, fields: [...] }
 *   - onSubmit(answers): answers is [{ field_id, label, type, value }]
 *   - disabled: lock the form (already submitted or chat busy)
 *   - submitted: render the read-only "answers sent" state
 *   - labels: { submit, submitted, requiredHint }
 */
const DEFAULT_LABELS = {
  submit: 'Submit',
  submitted: 'Response submitted',
  requiredHint: 'Please answer all required questions.',
};

function normalizeOption(opt) {
  if (opt && typeof opt === 'object') {
    return { value: opt.value ?? opt.label ?? '', label: opt.label ?? String(opt.value ?? '') };
  }
  return { value: opt, label: String(opt) };
}

function initialValue(field) {
  if (field.type === 'checkbox') return [];
  if (field.type === 'boolean') return false;
  if (field.type === 'slider') {
    const min = typeof field.min === 'number' ? field.min : 0;
    const max = typeof field.max === 'number' ? field.max : 100;
    return Math.round((min + max) / 2);
  }
  return '';
}

export default function CollectionForm({ form, onSubmit, disabled = false, submitted = false, labels }) {
  const L = { ...DEFAULT_LABELS, ...(labels || {}) };
  const fields = Array.isArray(form?.fields) ? form.fields : [];

  const [values, setValues] = useState(() => {
    const init = {};
    fields.forEach((f) => { init[f.id] = initialValue(f); });
    return init;
  });
  const [error, setError] = useState(false);

  const setValue = (id, value) => {
    setValues((prev) => ({ ...prev, [id]: value }));
  };

  const toggleCheckbox = (id, optValue) => {
    setValues((prev) => {
      const current = Array.isArray(prev[id]) ? prev[id] : [];
      const next = current.includes(optValue)
        ? current.filter((v) => v !== optValue)
        : [...current, optValue];
      return { ...prev, [id]: next };
    });
  };

  const isAnswered = (field) => {
    const v = values[field.id];
    if (field.type === 'checkbox') return Array.isArray(v) && v.length > 0;
    if (field.type === 'boolean' || field.type === 'slider' || field.type === 'number') {
      return v !== '' && v !== null && v !== undefined;
    }
    return v !== '' && v !== null && v !== undefined;
  };

  const handleSubmit = () => {
    if (disabled || submitted) return;
    const missing = fields.some((f) => f.required && !isAnswered(f));
    if (missing) {
      setError(true);
      return;
    }
    setError(false);
    const answers = fields.map((f) => ({
      field_id: f.id,
      label: f.label,
      type: f.type,
      value: values[f.id],
    }));
    onSubmit(answers);
  };

  const renderField = (field) => {
    const v = values[field.id];
    const lock = disabled || submitted;

    switch (field.type) {
      case 'textarea':
        return (
          <textarea
            className="cf-input cf-textarea"
            value={v}
            rows={3}
            placeholder={field.placeholder || ''}
            disabled={lock}
            onChange={(e) => setValue(field.id, e.target.value)}
          />
        );
      case 'number':
        return (
          <input
            type="number"
            className="cf-input"
            value={v}
            min={field.min}
            max={field.max}
            step={field.step}
            placeholder={field.placeholder || ''}
            disabled={lock}
            onChange={(e) => setValue(field.id, e.target.value === '' ? '' : Number(e.target.value))}
          />
        );
      case 'radio':
        return (
          <div className="cf-options">
            {(field.options || []).map(normalizeOption).map((opt, i) => (
              <label key={i} className="cf-option">
                <input
                  type="radio"
                  name={field.id}
                  checked={v === opt.value}
                  disabled={lock}
                  onChange={() => setValue(field.id, opt.value)}
                />
                <span>{opt.label}</span>
              </label>
            ))}
          </div>
        );
      case 'checkbox':
        return (
          <div className="cf-options">
            {(field.options || []).map(normalizeOption).map((opt, i) => (
              <label key={i} className="cf-option">
                <input
                  type="checkbox"
                  checked={Array.isArray(v) && v.includes(opt.value)}
                  disabled={lock}
                  onChange={() => toggleCheckbox(field.id, opt.value)}
                />
                <span>{opt.label}</span>
              </label>
            ))}
          </div>
        );
      case 'select':
        return (
          <select
            className="cf-input cf-select"
            value={v}
            disabled={lock}
            onChange={(e) => setValue(field.id, e.target.value)}
          >
            <option value="" disabled>—</option>
            {(field.options || []).map(normalizeOption).map((opt, i) => (
              <option key={i} value={opt.value}>{opt.label}</option>
            ))}
          </select>
        );
      case 'slider': {
        const min = typeof field.min === 'number' ? field.min : 0;
        const max = typeof field.max === 'number' ? field.max : 100;
        const step = typeof field.step === 'number' ? field.step : 1;
        return (
          <div className="cf-slider-row">
            <input
              type="range"
              className="cf-slider"
              min={min}
              max={max}
              step={step}
              value={v}
              disabled={lock}
              onChange={(e) => setValue(field.id, Number(e.target.value))}
            />
            <span className="cf-slider-value">{v}</span>
          </div>
        );
      }
      case 'boolean':
        return (
          <label className="cf-option">
            <input
              type="checkbox"
              checked={!!v}
              disabled={lock}
              onChange={(e) => setValue(field.id, e.target.checked)}
            />
            <span>{field.placeholder || ''}</span>
          </label>
        );
      case 'text':
      default:
        return (
          <input
            type="text"
            className="cf-input"
            value={v}
            placeholder={field.placeholder || ''}
            disabled={lock}
            onChange={(e) => setValue(field.id, e.target.value)}
          />
        );
    }
  };

  return (
    <div className={`collection-form${submitted ? ' cf-submitted' : ''}`}>
      {form?.title && <div className="cf-title">{form.title}</div>}
      {form?.description && <div className="cf-description">{form.description}</div>}

      {fields.map((field) => (
        <div key={field.id} className="cf-field">
          <label className="cf-label">
            {field.label}
            {field.required && <span className="cf-required" aria-hidden="true"> *</span>}
          </label>
          {renderField(field)}
        </div>
      ))}

      {error && <div className="cf-error">{L.requiredHint}</div>}

      <button
        type="button"
        className="cf-submit"
        onClick={handleSubmit}
        disabled={disabled || submitted}
      >
        {submitted ? L.submitted : (form?.submit_label || L.submit)}
      </button>
    </div>
  );
}
