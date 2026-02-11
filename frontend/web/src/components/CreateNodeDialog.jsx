import { useState } from 'react';
import useGraphStore from '../store/graphStore';
import * as api from '../services/api';
import './CreateNodeDialog.css';

// Fields that certain node types have beyond the basic set
const TYPE_EXTRA_FIELDS = {
  Initiative: ['start_date', 'end_date'],
  Resource: ['identifier'],
  Legislation: ['effective_date'],
  Goal: ['target_date'],
  Event: ['start_date', 'end_date'],
};

const FIELD_LABELS = {
  start_date: 'Start date',
  end_date: 'End date',
  effective_date: 'Effective date',
  target_date: 'Target date',
  identifier: 'Identifier (URL/URI)',
};

function CreateNodeDialog({ nodeType, onClose, onSave }) {
  const { getNodeColor } = useGraphStore();
  const color = getNodeColor(nodeType);
  const extraFields = TYPE_EXTRA_FIELDS[nodeType] || [];

  const [formData, setFormData] = useState({
    name: '',
    description: '',
    summary: '',
    tags: '',
    ...Object.fromEntries(extraFields.map(f => [f, ''])),
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  const handleChange = (e) => {
    const { name, value } = e.target;
    setFormData(prev => ({ ...prev, [name]: value }));
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
        tags: formData.tags.split(',').map(t => t.trim()).filter(Boolean),
      };

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

  return (
    <div className="create-node-overlay" onClick={onClose}>
      <div className="create-node-dialog" onClick={e => e.stopPropagation()}>
        <header className="create-node-header">
          <div className="create-node-header-title">
            <span
              className="create-node-type-dot"
              style={{ backgroundColor: color }}
            />
            <h2>Create {nodeType}</h2>
          </div>
          <button className="close-button" onClick={onClose}>×</button>
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
              placeholder="Short summary (max 100 chars)..."
              maxLength={100}
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

          {extraFields.map(field => (
            <div className="form-group" key={field}>
              <label htmlFor={`create-${field}`}>{FIELD_LABELS[field] || field}</label>
              <input
                type={field.includes('date') ? 'date' : 'text'}
                id={`create-${field}`}
                name={field}
                value={formData[field]}
                onChange={handleChange}
                placeholder={field.includes('date') ? '' : `Enter ${FIELD_LABELS[field] || field}...`}
              />
            </div>
          ))}

          {error && (
            <div className="create-node-error">{error}</div>
          )}

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
