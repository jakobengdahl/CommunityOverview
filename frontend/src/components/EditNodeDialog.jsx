import { useState, useEffect, useRef } from 'react';
import './EditNodeDialog.css';

function EditNodeDialog({ node, onClose, onSave }) {
  const [formData, setFormData] = useState({
    name: '',
    type: '',
    description: '',
    summary: '',
    tags: '',
  });

  const dialogRef = useRef(null);

  useEffect(() => {
    // Initialize form with node data
    if (node && node.data) {
      const tagsArray = node.data.tags || [];
      const tagsString = Array.isArray(tagsArray) ? tagsArray.join(', ') : '';

      setFormData({
        name: node.data.label || node.data.name || '',
        type: node.data.nodeType || node.data.type || '',
        description: node.data.description || '',
        summary: node.data.summary || '',
        tags: tagsString,
      });
    }
  }, [node]);

  useEffect(() => {
    const handleClickOutside = (e) => {
      if (dialogRef.current && !dialogRef.current.contains(e.target)) {
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

  const handleSubmit = (e) => {
    e.preventDefault();

    // Parse tags from comma-separated string to array
    const tagsArray = formData.tags
      .split(',')
      .map(tag => tag.trim())
      .filter(tag => tag.length > 0);

    onSave({
      ...node,
      data: {
        ...node.data,
        name: formData.name,
        label: formData.name,
        type: formData.type,
        nodeType: formData.type,
        description: formData.description,
        summary: formData.summary,
        tags: tagsArray,
      }
    });
  };

  const handleChange = (field, value) => {
    setFormData(prev => ({
      ...prev,
      [field]: value
    }));
  };

  return (
    <div className="edit-node-overlay">
      <div ref={dialogRef} className="edit-node-dialog">
        <div className="edit-node-header">
          <h3>Redigera Nod</h3>
          <button
            className="edit-node-close"
            onClick={onClose}
            type="button"
          >
            ×
          </button>
        </div>

        <form onSubmit={handleSubmit} className="edit-node-form">
          <div className="form-group">
            <label htmlFor="name">Namn</label>
            <input
              id="name"
              type="text"
              value={formData.name}
              onChange={(e) => handleChange('name', e.target.value)}
              placeholder="Namn på noden"
              required
            />
          </div>

          <div className="form-group">
            <label htmlFor="type">Typ</label>
            <select
              id="type"
              value={formData.type}
              onChange={(e) => handleChange('type', e.target.value)}
              required
            >
              <option value="">Välj typ...</option>
              <option value="Actor">Actor</option>
              <option value="Community">Community</option>
              <option value="Initiative">Initiative</option>
              <option value="Challenge">Challenge</option>
              <option value="Goal">Goal</option>
              <option value="Insight">Insight</option>
              <option value="Resource">Resource</option>
              <option value="Topic">Topic</option>
            </select>
          </div>

          <div className="form-group">
            <label htmlFor="summary">Sammanfattning</label>
            <input
              id="summary"
              type="text"
              value={formData.summary}
              onChange={(e) => handleChange('summary', e.target.value)}
              placeholder="Kort sammanfattning"
            />
          </div>

          <div className="form-group">
            <label htmlFor="description">Beskrivning</label>
            <textarea
              id="description"
              value={formData.description}
              onChange={(e) => handleChange('description', e.target.value)}
              placeholder="Detaljerad beskrivning"
              rows={6}
            />
          </div>

          <div className="form-group">
            <label htmlFor="tags">Taggar</label>
            <input
              id="tags"
              type="text"
              value={formData.tags}
              onChange={(e) => handleChange('tags', e.target.value)}
              placeholder="AI, Maskininlärning, Öppen källkod"
            />
            <small className="form-hint">
              Separera taggar med kommatecken. Exempel: "AI, Maskininlärning, Öppen källkod"
            </small>
          </div>

          <div className="edit-node-actions">
            <button
              type="button"
              onClick={onClose}
              className="button-secondary"
            >
              Avbryt
            </button>
            <button
              type="submit"
              className="button-primary"
            >
              Spara
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default EditNodeDialog;
