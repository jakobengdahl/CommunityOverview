import { useState, useEffect } from 'react';
import { executeTool } from '../services/api';
import useGraphStore from '../store/graphStore';
import './NodeEditDialog.css';

function NodeEditDialog({ node, onClose, onSave }) {
  const { updateVisualization, nodes, edges } = useGraphStore();
  const [formData, setFormData] = useState({
    name: '',
    description: '',
    summary: '',
    communities: []
  });
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (node) {
      setFormData({
        name: node.name || '',
        description: node.description || '',
        summary: node.summary || '',
        communities: node.communities || []
      });
    }
  }, [node]);

  const handleChange = (field, value) => {
    setFormData(prev => ({ ...prev, [field]: value }));
  };

  const handleCommunitiesChange = (value) => {
    // Parse comma-separated string into array
    const communitiesArray = value.split(',').map(c => c.trim()).filter(c => c);
    setFormData(prev => ({ ...prev, communities: communitiesArray }));
  };

  const handleSave = async () => {
    setIsSaving(true);
    setError(null);

    try {
      const result = await executeTool('update_node', {
        node_id: node.id,
        updates: formData
      });

      if (result.success) {
        // Update the node in the store
        const updatedNodes = nodes.map(n =>
          n.id === node.id
            ? { ...n, ...result.node, label: result.node.name }
            : n
        );
        updateVisualization(updatedNodes, edges);

        if (onSave) {
          onSave(result.node);
        }
        onClose();
      } else {
        setError(result.error || 'Failed to update node');
      }
    } catch (err) {
      console.error('Error updating node:', err);
      setError(err.message || 'Failed to update node');
    } finally {
      setIsSaving(false);
    }
  };

  if (!node) return null;

  return (
    <div className="node-edit-overlay" onClick={onClose}>
      <div className="node-edit-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="node-edit-header">
          <h2>Edit Node</h2>
          <button className="close-button" onClick={onClose}>✕</button>
        </div>

        <div className="node-edit-body">
          {error && (
            <div className="error-message">
              ❌ {error}
            </div>
          )}

          <div className="form-group">
            <label htmlFor="node-type">Type</label>
            <input
              id="node-type"
              type="text"
              value={node.type || ''}
              disabled
              className="form-input disabled"
            />
            <span className="form-hint">Node type cannot be changed</span>
          </div>

          <div className="form-group">
            <label htmlFor="node-name">Name *</label>
            <input
              id="node-name"
              type="text"
              value={formData.name}
              onChange={(e) => handleChange('name', e.target.value)}
              className="form-input"
              placeholder="Enter node name"
            />
          </div>

          <div className="form-group">
            <label htmlFor="node-summary">Summary</label>
            <input
              id="node-summary"
              type="text"
              value={formData.summary}
              onChange={(e) => handleChange('summary', e.target.value)}
              className="form-input"
              placeholder="Short summary (1-2 sentences)"
            />
          </div>

          <div className="form-group">
            <label htmlFor="node-description">Description</label>
            <textarea
              id="node-description"
              value={formData.description}
              onChange={(e) => handleChange('description', e.target.value)}
              className="form-textarea"
              placeholder="Detailed description"
              rows={4}
            />
          </div>

          <div className="form-group">
            <label htmlFor="node-communities">Communities</label>
            <input
              id="node-communities"
              type="text"
              value={formData.communities.join(', ')}
              onChange={(e) => handleCommunitiesChange(e.target.value)}
              className="form-input"
              placeholder="eSam, Myndigheter, Officiell Statistik"
            />
            <span className="form-hint">Separate multiple communities with commas</span>
          </div>
        </div>

        <div className="node-edit-footer">
          <button className="cancel-button" onClick={onClose} disabled={isSaving}>
            Cancel
          </button>
          <button
            className="save-button"
            onClick={handleSave}
            disabled={isSaving || !formData.name.trim()}
          >
            {isSaving ? 'Saving...' : 'Save Changes'}
          </button>
        </div>
      </div>
    </div>
  );
}

export default NodeEditDialog;
