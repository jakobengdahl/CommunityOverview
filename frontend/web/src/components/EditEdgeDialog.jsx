import { useState, useEffect } from 'react';
import useGraphStore from '../store/graphStore';
import './EditNodeDialog.css';

function EditEdgeDialog({ edge, nodes, onClose, onSave, onDelete }) {
  const { getRelationshipTypes } = useGraphStore();
  const [formData, setFormData] = useState({
    type: '',
    label: '',
  });

  const relationshipTypes = getRelationshipTypes?.() || [];

  // Find source and target node names for display
  const sourceNode = nodes?.find(n => n.id === edge?.source);
  const targetNode = nodes?.find(n => n.id === edge?.target);
  const sourceName = sourceNode?.name || sourceNode?.data?.name || edge?.source || '';
  const targetName = targetNode?.name || targetNode?.data?.name || edge?.target || '';

  useEffect(() => {
    if (edge) {
      setFormData({
        type: edge.type || edge.label || '',
        label: edge.label || '',
      });
    }
  }, [edge]);

  const handleChange = (e) => {
    const { name, value } = e.target;
    setFormData(prev => ({ ...prev, [name]: value }));
  };

  const handleSubmit = (e) => {
    e.preventDefault();
    onSave({
      type: formData.type || null,
      label: formData.label,
    });
  };

  const handleDelete = () => {
    if (onDelete) {
      onDelete(edge.id);
    }
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
            <h2>Edit Connection</h2>
          </div>
          <button className="close-button" onClick={onClose}>x</button>
        </header>

        <form onSubmit={handleSubmit}>
          <div className="form-group">
            <label>Connection</label>
            <div style={{ color: '#aaa', fontSize: '0.9rem', padding: '8px 0' }}>
              {sourceName} &rarr; {targetName}
            </div>
          </div>

          <div className="form-group">
            <label htmlFor="edge-type">Type</label>
            <select
              id="edge-type"
              name="type"
              value={formData.type}
              onChange={handleChange}
            >
              <option value="">No specific type</option>
              {relationshipTypes.map(rt => (
                <option key={rt.type || rt} value={rt.type || rt}>
                  {rt.type || rt}{rt.description ? ` - ${rt.description}` : ''}
                </option>
              ))}
            </select>
          </div>

          <div className="form-group">
            <label htmlFor="edge-label">Label</label>
            <input
              type="text"
              id="edge-label"
              name="label"
              value={formData.label}
              onChange={handleChange}
              placeholder="Optional label for this connection..."
            />
          </div>

          <div className="form-actions">
            {onDelete && (
              <button
                type="button"
                className="secondary"
                style={{ color: '#ef4444', borderColor: '#ef4444', marginRight: 'auto' }}
                onClick={handleDelete}
              >
                Delete
              </button>
            )}
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

export default EditEdgeDialog;
