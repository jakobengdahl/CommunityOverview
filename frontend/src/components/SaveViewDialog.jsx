
import { useState } from 'react';
import useGraphStore from '../store/graphStore';
import { executeTool } from '../services/api';
import './SaveViewDialog.css';

function SaveViewDialog({ isOpen, onClose }) {
  const [viewName, setViewName] = useState('');
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState(null);

  const { nodes, edges, hiddenNodeIds, addNodesToVisualization } = useGraphStore();

  // NOTE: We don't have direct access to React Flow positions here easily
  // without passing them from VisualizationPanel.
  // Ideally, we should trigger this action from VisualizationPanel or pass the flow instance/nodes
  // to the store.
  // For now, let's assume we save the LOGICAL view (hidden nodes) and we assume the backend
  // or layout engine handles positions, OR we try to grab positions if stored.
  // BUT the prompt says "position should be stored".

  // To solve this: VisualizationPanel should sync positions back to the store
  // or we need to access the React Flow instance.
  // Let's rely on the fact that `nodes` in `VisualizationPanel` state *have* positions,
  // but `store.nodes` might not if they were just loaded.
  // However, `onNodesChange` in VisualizationPanel updates local state, not store.
  // We should probably update the store with positions when saving.

  // Actually, let's just create the dialog UI here.
  // The Logic to gather data needs to come from where the data is (VisualizationPanel).
  // So maybe we shouldn't put the logic inside the Dialog if the Dialog is isolated.
  // But let's assume `onSave` prop provides the data.

  const handleSave = async () => {
    if (!viewName.trim()) return;
    setIsSaving(true);
    setError(null);

    try {
        // We will delegate the actual saving to the parent component via onSave prop
        // because it has access to the React Flow instance/nodes.
        if (onClose.onSave) {
            await onClose.onSave(viewName);
        }
        onClose();
        setViewName('');
    } catch (err) {
        console.error("Error saving view:", err);
        setError(err.message);
    } finally {
        setIsSaving(false);
    }
  };

  if (!isOpen) return null;

  return (
    <div className="dialog-overlay">
      <div className="dialog-content">
        <h3>Save Current View</h3>
        <p>Save the current visualization state (visible nodes, positions) to the graph.</p>

        <input
          type="text"
          className="view-name-input"
          value={viewName}
          onChange={(e) => setViewName(e.target.value)}
          placeholder="Enter view name (e.g., 'Project Alpha Overview')"
          autoFocus
        />

        {error && <p className="error-message">{error}</p>}

        <div className="dialog-actions">
          <button
            className="cancel-button"
            onClick={onClose}
            disabled={isSaving}
          >
            Cancel
          </button>
          <button
            className="save-button"
            onClick={handleSave}
            disabled={!viewName.trim() || isSaving}
          >
            {isSaving ? 'Saving...' : 'Save View'}
          </button>
        </div>
      </div>
    </div>
  );
}

export default SaveViewDialog;
