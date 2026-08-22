import { useState, useEffect } from 'react';
import useGraphStore from '../store/graphStore';
import * as api from '../services/api';
import SubtypeInput from './SubtypeInput';
import './CreateNodeDialog.css';

// Fields always shown via dedicated form controls — never repeated as extra fields
const BASE_FIELDS = new Set([
  'name',
  'description',
  'summary',
  'tags',
  'subtypes',
  'aliases',
  'metadata',
]);

const FIELD_LABELS = {
  start_date: 'Start date',
  end_date: 'End date',
  effective_date: 'Effective date',
  target_date: 'Target date',
  identifier: 'Resource link (URL)',
  repo: 'Repository URL',
};

function formatFieldLabel(field) {
  return FIELD_LABELS[field] || field.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

function CreateNodeDialog({ nodeType, onClose, onSave }) {
  const { getNodeColor, getNodeTypeConfig } = useGraphStore();
  const color = getNodeColor(nodeType);
  const typeConfig = getNodeTypeConfig(nodeType);
  const extraFields = (typeConfig?.fields || []).filter((f) => !BASE_FIELDS.has(f));

  const [formData, setFormData] = useState({
    name: '',
    description: '',
    summary: '',
    tags: '',
    aliases: '',
    ...Object.fromEntries(extraFields.map((f) => [f, ''])),
  });
  const [subtypes, setSubtypes] = useState([]);
  const [existingSubtypes, setExistingSubtypes] = useState([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  // Fetch existing subtypes for this node type
  useEffect(() => {
    api
      .getSubtypes(nodeType)
      .then((data) => {
        setExistingSubtypes(data.subtypes?.[nodeType] || []);
      })
      .catch(() => {});
  }, [nodeType]);

  const handleChange = (e) => {
    const { name, value } = e.target;
    setFormData((prev) => ({ ...prev, [name]: value }));
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!formData.name.trim()) return;

    setSaving(true);
    setError(null);

    try {
      const node = {
        name: formData.name.trim(),
        type: nodeType,
        description: formData.description.trim(),
        summary: formData.summary.trim(),
        tags: formData.tags
          .split(',')
          .map((t) => t.trim())
          .filter(Boolean),
        aliases: formData.aliases
          .split(',')
          .map((a) => a.trim())
          .filter(Boolean),
      };

      if (subtypes.length > 0) {
        node.subtypes = subtypes;
      }

      // Add extra fields if they have values
      for (const field of extraFields) {
        if (formData[field]?.trim()) {
          node[field] = formData[field].trim();
        }
      }

      const result = await api.addNodes([node], []);

      if (result.added_node_ids && result.added_node_ids.length > 0) {
        const createdNode = { ...node, id: result.added_node_ids[0] };
        onSave?.(createdNode);
      }

      onClose();
    } catch (err) {
      console.error('Error creating node:', err);
      setError(err.message || 'Could not create node');
    } finally {
      setSaving(false);
    }
  };

  useEffect(() => {
    const handleEsc = (e) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handleEsc);
    return () => document.removeEventListener('keydown', handleEsc);
  }, [onClose]);

  return (
    <div className="create-node-overlay" onClick={onClose}>
      <div className="create-node-dialog" onClick={(e) => e.stopPropagation()}>
        <header className="create-node-header">
          <div className="create-node-header-title">
            <span className="create-node-type-dot" style={{ backgroundColor: color }} />
            <h2>Create {nodeType}</h2>
          </div>
          <button className="close-button" onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>

        <form onSubmit={handleSubmit}>
          <div className="form-group">
            <label htmlFor="create-name">Name *</label>
            <input
              type="text"
              id="create-name"
              name="name"
              value={formData.name}
              onChange={handleChange}
              placeholder={`Enter ${nodeType.toLowerCase()} name...`}
              required
              autoFocus
            />
          </div>

          <div className="form-group">
            <SubtypeInput
              value={subtypes}
              onChange={setSubtypes}
              existingSubtypes={existingSubtypes}
            />
          </div>

          <div className="form-group">
            <label htmlFor="create-description">Description</label>
            <textarea
              id="create-description"
              name="description"
              value={formData.description}
              onChange={handleChange}
              rows={3}
              placeholder="Optional description..."
            />
          </div>

          <div className="form-group">
            <label htmlFor="create-summary">Summary</label>
            <input
              type="text"
              id="create-summary"
              name="summary"
              value={formData.summary}
              onChange={handleChange}
              placeholder="Short summary (max 300 chars)..."
              maxLength={300}
            />
          </div>

          <div className="form-group">
            <label htmlFor="create-tags">Tags (comma-separated)</label>
            <input
              type="text"
              id="create-tags"
              name="tags"
              value={formData.tags}
              onChange={handleChange}
              placeholder="tag1, tag2, tag3"
            />
          </div>

          <div className="form-group">
            <label htmlFor="create-aliases">Aliases / synonyms (comma-separated)</label>
            <input
              type="text"
              id="create-aliases"
              name="aliases"
              value={formData.aliases}
              onChange={handleChange}
              placeholder="alternative name, abbreviation, synonym"
            />
          </div>

          {extraFields.map((field) => {
            const isDateField = field.includes('date');
            const useDateTime = isDateField && nodeType === 'Event';
            const label = formatFieldLabel(field);
            return (
              <div className="form-group" key={field}>
                <label htmlFor={`create-${field}`}>{label}</label>
                <input
                  type={useDateTime ? 'datetime-local' : isDateField ? 'date' : 'text'}
                  id={`create-${field}`}
                  name={field}
                  value={formData[field] || ''}
                  onChange={handleChange}
                  placeholder={isDateField ? '' : `Enter ${label.toLowerCase()}...`}
                />
              </div>
            );
          })}

          {error && <div className="create-node-error">{error}</div>}

          <div className="form-actions">
            <button type="button" className="secondary" onClick={onClose}>
              Cancel
            </button>
            <button
              type="submit"
              className="primary"
              disabled={saving || !formData.name.trim()}
              style={{ backgroundColor: color }}
            >
              {saving ? 'Creating...' : `Create ${nodeType}`}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default CreateNodeDialog;
