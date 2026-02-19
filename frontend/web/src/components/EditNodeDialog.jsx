import { useState, useEffect } from 'react';
import useGraphStore from '../store/graphStore';
import './EditNodeDialog.css';

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
  const { getNodeTypes, getNodeColor } = useGraphStore();
  const [formData, setFormData] = useState({
    name: '',
    type: '',
    description: '',
    summary: '',
    tags: '',
  });

  // Get node types from schema or use defaults
  const nodeTypes = getNodeTypes();
  const availableTypes = nodeTypes.length > 0
    ? nodeTypes.filter(t => !t.static) // Exclude static types like SavedView
    : DEFAULT_NODE_TYPES;

  useEffect(() => {
    if (node?.data) {
      setFormData({
        name: node.data.name || '',
        type: node.data.type || '',
        description: node.data.description || '',
        summary: node.data.summary || '',
        tags: (node.data.tags || []).join(', '),
      });
    }
  }, [node]);

  const handleChange = (e) => {
    const { name, value } = e.target;
    setFormData(prev => ({ ...prev, [name]: value }));
  };

  const handleSubmit = (e) => {
    e.preventDefault();
    onSave({
      name: formData.name,
      type: formData.type,
      description: formData.description,
      summary: formData.summary,
      tags: formData.tags.split(',').map(t => t.trim()).filter(Boolean),
    });
  };

  useEffect(() => {
    const handleEsc = (e) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', handleEsc);
    return () => document.removeEventListener('keydown', handleEsc);
  }, [onClose]);

  return (
    <div className="edit-dialog-overlay" onClick={onClose}>
      <div className="edit-dialog" onClick={e => e.stopPropagation()}>
        <header className="edit-dialog-header">
          <div className="edit-dialog-header-title">
            <span
              className="edit-dialog-type-dot"
              style={{ backgroundColor: getNodeColor(formData.type) }}
            />
            <h2>Edit {formData.type || 'Node'}</h2>
          </div>
          <button className="close-button" onClick={onClose}>×</button>
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
            <select
              id="type"
              name="type"
              value={formData.type}
              onChange={handleChange}
              required
            >
              <option value="">Select type...</option>
              {availableTypes.map(nodeType => (
                <option key={nodeType.type} value={nodeType.type}>
                  {nodeType.type}
                </option>
              ))}
            </select>
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
