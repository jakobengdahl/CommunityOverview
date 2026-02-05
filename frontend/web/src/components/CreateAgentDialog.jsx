import { useState } from 'react';
import useGraphStore from '../store/graphStore';
import './CreateSubscriptionDialog.css'; // Reuse the same styles

/**
 * Dialog for creating an Agent node with its associated EventSubscription.
 *
 * When creating an Agent, we also create an EventSubscription that the agent
 * will use to receive events. The Agent node links to the subscription via
 * metadata.subscription_id.
 *
 * Note: The agent runtime is NOT implemented - this just creates the
 * configuration nodes in the graph.
 */
export default function CreateAgentDialog({ onClose, onSave }) {
  const schema = useGraphStore((state) => state.schema);

  // Agent info
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [agentType, setAgentType] = useState('mcp-agent');

  // Subscription settings (simplified - agent creates its own subscription)
  const [selectedNodeTypes, setSelectedNodeTypes] = useState([]);
  const [operations, setOperations] = useState({
    create: true,
    update: true,
    delete: false,
  });
  const [keywords, setKeywords] = useState('');
  const [webhookUrl, setWebhookUrl] = useState('');

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

    // Generate a unique ID prefix for linking the nodes
    const idPrefix = 'agent-' + Date.now().toString(36);
    const subscriptionId = idPrefix + '-subscription';
    const agentId = idPrefix + '-node';

    // Create the EventSubscription node
    const subscriptionNode = {
      id: subscriptionId,
      name: `${name} - Prenumeration`,
      type: 'EventSubscription',
      description: `Webhook-prenumeration för agent: ${name}`,
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
          ignore_origins: [`agent:${idPrefix}`], // Prevent loops - ignore events from this agent
          ignore_session_ids: [],
        },
      },
      communities: [],
    };

    // Create the Agent node
    const agentNode = {
      id: agentId,
      name: name.trim(),
      type: 'Agent',
      description: description.trim() || `AI-agent: ${name}`,
      summary: `Agent av typ ${agentType} (runtime ej implementerad)`,
      metadata: {
        subscription_id: subscriptionId,
        agent: {
          type: agentType,
          config: {
            // Placeholder for future agent configuration
          },
        },
      },
      communities: [],
    };

    // Save both nodes (and optionally a relationship edge)
    onSave({
      nodes: [subscriptionNode, agentNode],
      edges: [
        {
          source: agentNode.id,
          target: subscriptionNode.id,
          type: 'RELATES_TO',
        },
      ],
    });
    onClose();
  };

  return (
    <div className="dialog-overlay" onClick={onClose}>
      <div className="dialog-content subscription-dialog" onClick={e => e.stopPropagation()}>
        <h2>Skapa agent</h2>
        <p className="dialog-description">
          En agent lyssnar på grafändringar via en webhook-prenumeration.
          <br />
          <strong>OBS:</strong> Agentkörning är inte implementerad - detta skapar bara konfigurationsnoder.
        </p>

        <form onSubmit={handleSubmit}>
          <div className="form-section">
            <h3>Agent-information</h3>

            <div className="form-group">
              <label htmlFor="agent-name">Namn *</label>
              <input
                id="agent-name"
                type="text"
                value={name}
                onChange={e => setName(e.target.value)}
                placeholder="T.ex. 'Min analysagent'"
                required
              />
            </div>

            <div className="form-group">
              <label htmlFor="agent-description">Beskrivning</label>
              <textarea
                id="agent-description"
                value={description}
                onChange={e => setDescription(e.target.value)}
                placeholder="Valfri beskrivning av agentens syfte"
                rows={2}
              />
            </div>

            <div className="form-group">
              <label htmlFor="agent-type">Agenttyp</label>
              <select
                id="agent-type"
                value={agentType}
                onChange={e => setAgentType(e.target.value)}
                style={{
                  width: '100%',
                  padding: '0.5rem 0.75rem',
                  border: '1px solid #444',
                  borderRadius: '4px',
                  background: '#2a2a2a',
                  color: '#fff',
                  fontSize: '0.9rem',
                }}
              >
                <option value="mcp-agent">MCP Agent (placeholder)</option>
                <option value="webhook-processor">Webhook Processor (placeholder)</option>
              </select>
              <small>Agenttyp - runtime stöds inte ännu</small>
            </div>
          </div>

          <div className="form-section">
            <h3>Trigger (vilka events agenten lyssnar på)</h3>

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
              <label htmlFor="agent-keywords">Nyckelord (kommaseparerade)</label>
              <input
                id="agent-keywords"
                type="text"
                value={keywords}
                onChange={e => setKeywords(e.target.value)}
                placeholder="T.ex. 'AI, digitalisering'"
              />
            </div>
          </div>

          <div className="form-section">
            <h3>Webhook-endpoint</h3>

            <div className="form-group">
              <label htmlFor="agent-webhook">Webhook-URL *</label>
              <input
                id="agent-webhook"
                type="url"
                value={webhookUrl}
                onChange={e => setWebhookUrl(e.target.value)}
                placeholder="https://example.com/agent-webhook"
                required
              />
              <small>URL dit events skickas när de matchar filtren</small>
            </div>
          </div>

          <div className="dialog-actions">
            <button type="button" className="btn-secondary" onClick={onClose}>
              Avbryt
            </button>
            <button type="submit" className="btn-primary">
              Skapa agent
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
