import { useState, useEffect } from 'react';
import useGraphStore from '../store/graphStore';
import './CreateSubscriptionDialog.css';

/**
 * Dialog for creating an EventSubscription node.
 *
 * EventSubscription nodes define webhook targets for graph mutation events.
 * Configuration is stored in metadata:
 * - filters: { target: { entity_kind, node_types }, operations, keywords }
 * - delivery: { webhook_url, ignore_origins, ignore_session_ids }
 */
export default function CreateSubscriptionDialog({ onClose, onSave }) {
  const schema = useGraphStore((state) => state.schema);

  // Basic info
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');

  // Filter settings
  const [selectedNodeTypes, setSelectedNodeTypes] = useState([]);
  const [operations, setOperations] = useState({
    create: true,
    update: true,
    delete: true,
  });
  const [keywords, setKeywords] = useState('');

  // Delivery settings
  const [webhookUrl, setWebhookUrl] = useState('');
  const [ignoreOrigins, setIgnoreOrigins] = useState('');

  // Get available node types from schema
  const nodeTypes = schema?.node_types
    ? Object.keys(schema.node_types).filter(t => !['SavedView', 'VisualizationView', 'EventSubscription', 'Agent'].includes(t))
    : [];

  const handleToggleNodeType = (type) => {
    if (selectedNodeTypes.includes(type)) {
      setSelectedNodeTypes(selectedNodeTypes.filter(t => t !== type));
    } else {
      setSelectedNodeTypes([...selectedNodeTypes, type]);
    }
  };

  const handleToggleOperation = (op) => {
    setOperations({ ...operations, [op]: !operations[op] });
  };

  const handleSubmit = (e) => {
    e.preventDefault();

    if (!name.trim() || !webhookUrl.trim()) {
      alert('Namn och webhook-URL krävs');
      return;
    }

    // Build the subscription node
    const subscriptionNode = {
      id: crypto.randomUUID(),
      name: name.trim(),
      type: 'EventSubscription',
      description: description.trim() || `Webhook-prenumeration: ${name}`,
      summary: `Lyssnar på ${Object.entries(operations).filter(([_, v]) => v).map(([k]) => k).join(', ')} events`,
      metadata: {
        filters: {
          target: {
            entity_kind: 'node',
            node_types: selectedNodeTypes,
          },
          operations: Object.entries(operations).filter(([_, v]) => v).map(([k]) => k),
          keywords: {
            any: keywords.split(',').map(k => k.trim()).filter(k => k),
          },
        },
        delivery: {
          webhook_url: webhookUrl.trim(),
          ignore_origins: ignoreOrigins.split(',').map(o => o.trim()).filter(o => o),
          ignore_session_ids: [],
        },
      },
      communities: [],
    };

    onSave(subscriptionNode);
    onClose();
  };

  return (
    <div className="dialog-overlay" onClick={onClose}>
      <div className="dialog-content subscription-dialog" onClick={e => e.stopPropagation()}>
        <h2>Skapa webhook-prenumeration</h2>
        <p className="dialog-description">
          En prenumeration skickar events till en webhook-URL när noder skapas, uppdateras eller tas bort.
        </p>

        <form onSubmit={handleSubmit}>
          <div className="form-section">
            <h3>Grundläggande information</h3>

            <div className="form-group">
              <label htmlFor="sub-name">Namn *</label>
              <input
                id="sub-name"
                type="text"
                value={name}
                onChange={e => setName(e.target.value)}
                placeholder="T.ex. 'Notifiera vid nya initiativ'"
                required
              />
            </div>

            <div className="form-group">
              <label htmlFor="sub-description">Beskrivning</label>
              <textarea
                id="sub-description"
                value={description}
                onChange={e => setDescription(e.target.value)}
                placeholder="Valfri beskrivning av prenumerationens syfte"
                rows={2}
              />
            </div>
          </div>

          <div className="form-section">
            <h3>Filter</h3>

            <div className="form-group">
              <label>Nodtyper (tom = alla)</label>
              <div className="checkbox-group node-types-grid">
                {nodeTypes.map(type => (
                  <label key={type} className="checkbox-label">
                    <input
                      type="checkbox"
                      checked={selectedNodeTypes.includes(type)}
                      onChange={() => handleToggleNodeType(type)}
                    />
                    {type}
                  </label>
                ))}
              </div>
            </div>

            <div className="form-group">
              <label>Operationer</label>
              <div className="checkbox-group">
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={operations.create}
                    onChange={() => handleToggleOperation('create')}
                  />
                  Skapa (create)
                </label>
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={operations.update}
                    onChange={() => handleToggleOperation('update')}
                  />
                  Uppdatera (update)
                </label>
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={operations.delete}
                    onChange={() => handleToggleOperation('delete')}
                  />
                  Ta bort (delete)
                </label>
              </div>
            </div>

            <div className="form-group">
              <label htmlFor="sub-keywords">Nyckelord (kommaseparerade)</label>
              <input
                id="sub-keywords"
                type="text"
                value={keywords}
                onChange={e => setKeywords(e.target.value)}
                placeholder="T.ex. 'AI, digitalisering, NIS2'"
              />
              <small>Matchar mot namn, beskrivning, sammanfattning och taggar</small>
            </div>
          </div>

          <div className="form-section">
            <h3>Leverans</h3>

            <div className="form-group">
              <label htmlFor="sub-webhook">Webhook-URL *</label>
              <input
                id="sub-webhook"
                type="url"
                value={webhookUrl}
                onChange={e => setWebhookUrl(e.target.value)}
                placeholder="https://example.com/webhook"
                required
              />
            </div>

            <div className="form-group">
              <label htmlFor="sub-ignore-origins">Ignorera origins (kommaseparerade)</label>
              <input
                id="sub-ignore-origins"
                type="text"
                value={ignoreOrigins}
                onChange={e => setIgnoreOrigins(e.target.value)}
                placeholder="T.ex. 'agent:my-agent, mcp'"
              />
              <small>För loopskydd: events från dessa origins skickas inte</small>
            </div>
          </div>

          <div className="dialog-actions">
            <button type="button" className="btn-secondary" onClick={onClose}>
              Avbryt
            </button>
            <button type="submit" className="btn-primary">
              Skapa prenumeration
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
