import { useState, useEffect } from 'react';
import useGraphStore from '../store/graphStore';
import * as api from '../services/api';
import SubtypeInput from './SubtypeInput';
import './EditNodeDialog.css';

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

// Default node types as fallback if schema not loaded
const DEFAULT_NODE_TYPES = [
  { type: 'Actor', description: 'Government agencies, organizations' },
  { type: 'Initiative', description: 'Projects, programs' },
  { type: 'Capability', description: 'Capabilities, skills' },
  { type: 'Resource', description: 'Reports, software, tools' },
  { type: 'Legislation', description: 'Laws, directives' },
  { type: 'Theme', description: 'Themes, strategies' },
  { type: 'Goal', description: 'Strategic objectives, targets' },
  { type: 'Event', description: 'Conferences, workshops, milestones' },
  { type: 'Data', description: 'Datasets, registers, APIs' },
  { type: 'Risk', description: 'Risks, threats, vulnerabilities' },
];

function EditNodeDialog({ node, onClose, onSave }) {
  const { getNodeTypes, getNodeColor, schema } = useGraphStore();
  const [formData, setFormData] = useState({
    name: '',
    type: '',
    description: '',
    summary: '',
    tags: '',
    aliases: '',
  });
  const [subtypes, setSubtypes] = useState([]);
  const [existingSubtypes, setExistingSubtypes] = useState([]);

  // Extra fields driven by the schema for the current node type
  const schemaFields = schema?.node_types?.[formData.type]?.fields || [];
  const extraFields = schemaFields.filter((f) => !BASE_FIELDS.has(f));

  // Get node types from schema or use defaults
  const nodeTypes = getNodeTypes();
  const availableTypes =
    nodeTypes.length > 0
      ? nodeTypes.filter((t) => !t.static) // Exclude static types like SavedView
      : DEFAULT_NODE_TYPES;

  useEffect(() => {
    if (node?.data) {
      const nodeType = node.data.type || '';
      const sf = schema?.node_types?.[nodeType]?.fields || [];
      const ef = sf.filter((f) => !BASE_FIELDS.has(f));
      // Extra fields are stored in node.metadata by the backend
      const extraData = Object.fromEntries(
        ef.map((f) => [f, node.data.metadata?.[f] ?? node.data[f] ?? ''])
      );
      setFormData({
        name: node.data.name || '',
        type: nodeType,
        description: node.data.description || '',
        summary: node.data.summary || '',
        tags: (node.data.tags || []).join(', '),
        aliases: (node.data.aliases || []).join(', '),
        ...extraData,
      });
      setSubtypes(node.data.subtypes || []);
    }
  }, [node, schema]);

  // Fetch existing subtypes when node type changes
  useEffect(() => {
    if (formData.type) {
      api
        .getSubtypes(formData.type)
        .then((data) => {
          setExistingSubtypes(data.subtypes?.[formData.type] || []);
        })
        .catch(() => {});
    }
  }, [formData.type]);

  const handleChange = (e) => {
    const { name, value } = e.target;
    setFormData((prev) => ({ ...prev, [name]: value }));
  };

  const handleSubmit = (e) => {
    e.preventDefault();
    const extraData = Object.fromEntries(extraFields.map((f) => [f, formData[f] ?? '']));
    onSave({
      name: formData.name,
      type: formData.type,
      description: formData.description,
      summary: formData.summary,
      tags: formData.tags
        .split(',')
        .map((t) => t.trim())
        .filter(Boolean),
      aliases: formData.aliases
        .split(',')
        .map((a) => a.trim())
        .filter(Boolean),
      subtypes,
      ...extraData,
    });
  };

  useEffect(() => {
    const handleEsc = (e) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handleEsc);
    return () => document.removeEventListener('keydown', handleEsc);
  }, [onClose]);

  return (
    <div className="edit-dialog-overlay" onClick={onClose}>
      <div className="edit-dialog" onClick={(e) => e.stopPropagation()}>
        <header className="edit-dialog-header">
          <div className="edit-dialog-header-title">
            <span
              className="edit-dialog-type-dot"
              style={{ backgroundColor: getNodeColor(formData.type) }}
            />
            <h2>Edit {formData.type || 'Node'}</h2>
          </div>
          <button className="close-button" onClick={onClose}>
            ×
          </button>
        </header>

        <form onSubmit={handleSubmit}>
          <div className="form-group">
            <label htmlFor="name">Name</label>
            <input
              type="text"
              id="name"
              name="name"
              value={formData.name}
              onChange={handleChange}
              required
            />
          </div>

          <div className="form-group">
            <label htmlFor="type">Type</label>
            <select id="type" name="type" value={formData.type} onChange={handleChange} required>
              <option value="">Select type...</option>
              {availableTypes.map((nodeType) => (
                <option key={nodeType.type} value={nodeType.type}>
                  {nodeType.type}
                </option>
              ))}
            </select>
          </div>

          <div className="form-group">
            <SubtypeInput
              value={subtypes}
              onChange={setSubtypes}
              existingSubtypes={existingSubtypes}
            />
          </div>

          <div className="form-group">
            <label htmlFor="description">Description</label>
            <textarea
              id="description"
              name="description"
              value={formData.description}
              onChange={handleChange}
              rows={4}
            />
          </div>

          <div className="form-group">
            <label htmlFor="summary">Summary</label>
            <input
              type="text"
              id="summary"
              name="summary"
              value={formData.summary}
              onChange={handleChange}
              placeholder="Short summary..."
            />
          </div>

          <div className="form-group">
            <label htmlFor="tags">Tags (comma-separated)</label>
            <input
              type="text"
              id="tags"
              name="tags"
              value={formData.tags}
              onChange={handleChange}
              placeholder="tag1, tag2, tag3"
            />
          </div>

          <div className="form-group">
            <label htmlFor="aliases">Aliases / synonyms (comma-separated)</label>
            <input
              type="text"
              id="aliases"
              name="aliases"
              value={formData.aliases}
              onChange={handleChange}
              placeholder="alternative name, abbreviation, synonym"
            />
          </div>

          {extraFields.map((field) => {
            const isDateField = field.includes('date');
            const useDateTime = isDateField && formData.type === 'Event';
            const label = formatFieldLabel(field);
            return (
              <div className="form-group" key={field}>
                <label htmlFor={`edit-${field}`}>{label}</label>
                <input
                  type={useDateTime ? 'datetime-local' : isDateField ? 'date' : 'text'}
                  id={`edit-${field}`}
                  name={field}
                  value={formData[field] ?? ''}
                  onChange={handleChange}
                  placeholder={isDateField ? '' : `Enter ${label.toLowerCase()}...`}
                />
              </div>
            );
          })}

          <div className="form-actions">
            <button type="button" className="secondary" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="primary">
              Save
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default EditNodeDialog;
