import { useState, useEffect } from 'react';
import './EditNodeDialog.css';

function EditNodeDialog({ node, onClose, onSave }) {
  const [formData, setFormData] = useState({
    name: '',
    type: '',
    description: '',
    summary: '',
    tags: '',
  });

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

  return (
    <div className="edit-dialog-overlay" onClick={onClose}>
      <div className="edit-dialog" onClick={e => e.stopPropagation()}>
        <header className="edit-dialog-header">
          <h2>Edit Node</h2>
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
              <option value="Actor">Actor</option>
              <option value="Community">Community</option>
              <option value="Initiative">Initiative</option>
              <option value="Capability">Capability</option>
              <option value="Resource">Resource</option>
              <option value="Legislation">Legislation</option>
              <option value="Theme">Theme</option>
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
