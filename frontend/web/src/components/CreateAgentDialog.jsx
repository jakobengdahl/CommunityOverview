import { useState, useEffect } from 'react';
import useGraphStore from '../store/graphStore';
import { useI18n } from '../i18n';
import './CreateSubscriptionDialog.css'; // Reuse the same styles

// The backend caps an agent name at 185 so "<name> - Subscription" fits the
// 200-character Node.name limit; a longer name fails the whole create.
const AGENT_NAME_MAX_LENGTH = 185;

/**
 * Dialog for creating an Agent node with its associated EventSubscription.
 *
 * When creating an Agent, we also create an EventSubscription that the agent
 * will use to receive events. The Agent node links to the subscription via
 * metadata.subscription_id.
 *
 * The agent runtime will start a background worker for enabled agents and
 * route matching events to the agent's LLM-based processing loop.
 */
export default function CreateAgentDialog({
  onClose,
  onSave,
  initialData,
  onViewRuns,
  onViewProposals,
}) {
  const { t } = useI18n();
  const schema = useGraphStore((state) => state.schema);
  const editingAgentId = initialData?.agent?.id || null;

  // Agent info
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [aliases, setAliases] = useState('');
  const [enabled, setEnabled] = useState(true);
  const [taskPrompt, setTaskPrompt] = useState('');

  // MCP Integrations
  const [availableIntegrations, setAvailableIntegrations] = useState([]);
  const [selectedIntegrations, setSelectedIntegrations] = useState([]);
  const [loadingIntegrations, setLoadingIntegrations] = useState(true);

  // Subscription settings (simplified - agent creates its own subscription)
  const [selectedNodeTypes, setSelectedNodeTypes] = useState([]);
  const [operations, setOperations] = useState({
    create: true,
    update: true,
    delete: false,
  });
  const [keywords, setKeywords] = useState('');
  const [webhookUrl, setWebhookUrl] = useState('');

  // Initialize form with initialData if provided
  useEffect(() => {
    if (initialData) {
      const { agent, subscription } = initialData;

      // Agent fields
      setName(agent.name || '');
      setDescription(agent.description || '');
      setAliases((agent.aliases || []).join(', '));

      if (agent.metadata) {
        // Try new structure first (flattened)
        if (agent.metadata.prompts || agent.metadata.mcp_integration_ids) {
          setEnabled(agent.metadata.enabled ?? true);
          setTaskPrompt(agent.metadata.prompts?.task_prompt || '');
          setSelectedIntegrations(agent.metadata.mcp_integration_ids || []);
        }
        // Fallback to legacy nested "agent" structure
        else if (agent.metadata.agent) {
          setEnabled(agent.metadata.agent.enabled ?? true);
          setTaskPrompt(agent.metadata.agent.task_prompt || '');
          if (agent.metadata.agent.mcp_integrations) {
            setSelectedIntegrations(agent.metadata.agent.mcp_integrations);
          }
        }
      }

      // Subscription fields
      if (subscription?.metadata) {
        const filters = subscription.metadata.filters || {};
        const delivery = subscription.metadata.delivery || {};

        if (filters.target?.node_types) {
          setSelectedNodeTypes(filters.target.node_types);
        }

        if (filters.operations) {
          setOperations({
            create: filters.operations.includes('create'),
            update: filters.operations.includes('update'),
            delete: filters.operations.includes('delete'),
          });
        }

        if (filters.keywords?.any) {
          setKeywords(filters.keywords.any.join(', '));
        }

        if (delivery.webhook_url && !delivery.webhook_url.startsWith('internal://')) {
          setWebhookUrl(delivery.webhook_url);
        }
      }
    }
  }, [initialData]);

  // Fetch available MCP integrations from server
  useEffect(() => {
    async function fetchIntegrations() {
      try {
        const response = await fetch('/agents/integrations');
        if (response.ok) {
          const data = await response.json();
          setAvailableIntegrations(data);

          // Select GRAPH by default if available (only if not editing)
          if (!initialData) {
            const graphIntegration = data.find((i) => i.id === 'GRAPH');
            if (graphIntegration) {
              setSelectedIntegrations(['GRAPH']);
            }
          }
        }
      } catch (error) {
        console.error('Failed to fetch MCP integrations:', error);
      } finally {
        setLoadingIntegrations(false);
      }
    }
    fetchIntegrations();
  }, [initialData]);

  // Get available node types from schema
  const nodeTypes = schema?.node_types
    ? Object.keys(schema.node_types).filter(
        (t) => !['SavedView', 'VisualizationView', 'EventSubscription', 'Agent'].includes(t)
      )
    : [];

  const handleToggleNodeType = (type) => {
    if (selectedNodeTypes.includes(type)) {
      setSelectedNodeTypes(selectedNodeTypes.filter((t) => t !== type));
    } else {
      setSelectedNodeTypes([...selectedNodeTypes, type]);
    }
  };

  const handleToggleOperation = (op) => {
    setOperations({ ...operations, [op]: !operations[op] });
  };

  const handleToggleIntegration = (integrationId) => {
    if (selectedIntegrations.includes(integrationId)) {
      setSelectedIntegrations(selectedIntegrations.filter((id) => id !== integrationId));
    } else {
      setSelectedIntegrations([...selectedIntegrations, integrationId]);
    }
  };

  const handleSubmit = (e) => {
    e.preventDefault();

    if (!name.trim()) {
      alert(t('agent_dialog.name_required'));
      return;
    }

    if (name.trim().length > AGENT_NAME_MAX_LENGTH) {
      alert(t('agent_dialog.name_too_long', { max: AGENT_NAME_MAX_LENGTH }));
      return;
    }

    if (!taskPrompt.trim()) {
      alert(t('agent_dialog.task_prompt_required'));
      return;
    }

    const aliasList = aliases
      .split(',')
      .map((a) => a.trim())
      .filter(Boolean);

    // Logic for Create vs Update
    if (initialData) {
      // UPDATE existing agent
      const agentId = initialData.agent.id;
      const subscriptionId = initialData.subscription?.id;

      // Update Agent Node
      const agentUpdates = {
        name: name.trim(),
        description: description.trim(),
        summary: `MCP agent with ${selectedIntegrations.length} integration(s)`,
        aliases: aliasList,
        metadata: {
          ...initialData.agent.metadata,
          subscription_id: subscriptionId, // Preserve or set ID
          // New structure
          enabled: enabled,
          prompts: {
            task_prompt: taskPrompt.trim(),
          },
          mcp_integration_ids: selectedIntegrations,
          // Remove legacy key if it exists to avoid confusion
          agent: undefined,
        },
      };

      // Update Subscription Node (if exists)
      let subscriptionUpdates = null;
      if (subscriptionId && initialData.subscription) {
        subscriptionUpdates = {
          name: `${name.trim()} - Subscription`,
          description: `Event subscription for agent: ${name.trim()}`,
          summary: `Listens to ${Object.entries(operations)
            .filter(([_, v]) => v)
            .map(([k]) => k)
            .join(', ')} events`,
          metadata: {
            ...initialData.subscription.metadata,
            filters: {
              target: {
                entity_kind: 'node',
                node_types: selectedNodeTypes,
              },
              operations: Object.entries(operations)
                .filter(([_, v]) => v)
                .map(([k]) => k),
              keywords: {
                any: keywords
                  .split(',')
                  .map((k) => k.trim())
                  .filter((k) => k),
              },
            },
            delivery: {
              ...initialData.subscription.metadata.delivery,
              webhook_url: webhookUrl.trim() || `internal://agent/${agentId}`,
            },
          },
        };
      }

      onSave({
        agentId,
        agentUpdates,
        subscriptionId,
        subscriptionUpdates,
      });
    } else {
      // CREATE new agent
      const idPrefix = 'agent-' + Date.now().toString(36);
      const subscriptionId = idPrefix + '-subscription';
      const agentId = idPrefix + '-node';

      // Create the EventSubscription node
      const subscriptionNode = {
        id: subscriptionId,
        name: `${name.trim()} - Subscription`,
        type: 'EventSubscription',
        description: `Event subscription for agent: ${name.trim()}`,
        summary: `Listens to ${Object.entries(operations)
          .filter(([_, v]) => v)
          .map(([k]) => k)
          .join(', ')} events`,
        metadata: {
          filters: {
            target: {
              entity_kind: 'node',
              node_types: selectedNodeTypes,
            },
            operations: Object.entries(operations)
              .filter(([_, v]) => v)
              .map(([k]) => k),
            keywords: {
              any: keywords
                .split(',')
                .map((k) => k.trim())
                .filter((k) => k),
            },
          },
          delivery: {
            webhook_url: webhookUrl.trim() || `internal://agent/${agentId}`,
            // Prevent loops: the runtime stamps the agent's own writes with
            // `agent:<agent node id>`, and the dispatcher matches it exactly.
            ignore_origins: [`agent:${agentId}`],
            ignore_session_ids: [],
          },
        },
      };

      // Create the Agent node
      const agentNode = {
        id: agentId,
        name: name.trim(),
        type: 'Agent',
        description: description.trim() || `AI agent: ${name.trim()}`,
        summary: `MCP agent with ${selectedIntegrations.length} integration(s)`,
        aliases: aliasList,
        metadata: {
          subscription_id: subscriptionId,
          enabled: enabled,
          prompts: {
            task_prompt: taskPrompt.trim(),
          },
          mcp_integration_ids: selectedIntegrations,
        },
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
    }
    onClose();
  };

  return (
    <div className="dialog-overlay" onClick={onClose}>
      <div className="dialog-content subscription-dialog" onClick={(e) => e.stopPropagation()}>
        <h2>{initialData ? t('agent_dialog.title_edit') : t('agent_dialog.title')}</h2>
        <p className="dialog-description">{t('agent_dialog.description')}</p>

        <form onSubmit={handleSubmit}>
          <div className="form-section">
            <h3>{t('agent_dialog.section_config')}</h3>

            <div className="form-group">
              <label className="checkbox-label" style={{ marginBottom: '0.75rem' }}>
                <input
                  type="checkbox"
                  checked={enabled}
                  onChange={(e) => setEnabled(e.target.checked)}
                />
                <strong>{t('agent_dialog.enabled_label')}</strong>
                <span style={{ marginLeft: '0.5rem', color: '#888', fontSize: '0.85rem' }}>
                  {t('agent_dialog.enabled_hint')}
                </span>
              </label>
            </div>

            <div className="form-group">
              <label htmlFor="agent-name">{t('agent_dialog.name_label')}</label>
              <input
                id="agent-name"
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder={t('agent_dialog.name_placeholder')}
                maxLength={AGENT_NAME_MAX_LENGTH}
                required
              />
            </div>

            <div className="form-group">
              <label htmlFor="agent-description">{t('agent_dialog.description_label')}</label>
              <textarea
                id="agent-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder={t('agent_dialog.description_placeholder')}
                rows={2}
              />
            </div>

            <div className="form-group">
              <label htmlFor="agent-aliases">{t('agent_dialog.aliases_label')}</label>
              <input
                id="agent-aliases"
                type="text"
                value={aliases}
                onChange={(e) => setAliases(e.target.value)}
                placeholder={t('agent_dialog.aliases_placeholder')}
              />
            </div>

            <div className="form-group">
              <label htmlFor="agent-task-prompt">{t('agent_dialog.task_prompt_label')}</label>
              <textarea
                id="agent-task-prompt"
                value={taskPrompt}
                onChange={(e) => setTaskPrompt(e.target.value)}
                placeholder={t('agent_dialog.task_prompt_placeholder')}
                rows={8}
                required
                style={{ fontFamily: 'monospace', fontSize: '0.85rem' }}
              />
              <small>{t('agent_dialog.task_prompt_help')}</small>
            </div>
          </div>

          <div className="form-section">
            <h3>{t('agent_dialog.section_integrations')}</h3>
            <p style={{ margin: '0 0 0.75rem 0', color: '#888', fontSize: '0.85rem' }}>
              {t('agent_dialog.integrations_help')}
            </p>

            {loadingIntegrations ? (
              <p style={{ color: '#888' }}>{t('agent_dialog.integrations_loading')}</p>
            ) : availableIntegrations.length === 0 ? (
              <p style={{ color: '#888' }}>{t('agent_dialog.integrations_empty')}</p>
            ) : (
              <div className="checkbox-group">
                {availableIntegrations.map((integration) => (
                  <label
                    key={integration.id}
                    className="checkbox-label"
                    style={{ marginBottom: '0.5rem' }}
                  >
                    <input
                      type="checkbox"
                      checked={selectedIntegrations.includes(integration.id)}
                      onChange={() => handleToggleIntegration(integration.id)}
                    />
                    <span>
                      <strong>{integration.id}</strong>
                      {integration.description && (
                        <span style={{ color: '#888', marginLeft: '0.5rem' }}>
                          - {integration.description}
                        </span>
                      )}
                    </span>
                  </label>
                ))}
              </div>
            )}
          </div>

          <div className="form-section">
            <h3>{t('agent_dialog.section_trigger')}</h3>

            <div className="form-group">
              <label>{t('agent_dialog.node_types_label')}</label>
              <div className="checkbox-group node-types-grid">
                {nodeTypes.map((type) => (
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
              <label>{t('agent_dialog.operations_label')}</label>
              <div className="checkbox-group">
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={operations.create}
                    onChange={() => handleToggleOperation('create')}
                  />
                  {t('agent_dialog.op_create')}
                </label>
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={operations.update}
                    onChange={() => handleToggleOperation('update')}
                  />
                  {t('agent_dialog.op_update')}
                </label>
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={operations.delete}
                    onChange={() => handleToggleOperation('delete')}
                  />
                  {t('agent_dialog.op_delete')}
                </label>
              </div>
            </div>

            <div className="form-group">
              <label htmlFor="agent-keywords">{t('agent_dialog.keywords_label')}</label>
              <input
                id="agent-keywords"
                type="text"
                value={keywords}
                onChange={(e) => setKeywords(e.target.value)}
                placeholder={t('agent_dialog.keywords_placeholder')}
              />
              <small>{t('agent_dialog.keywords_help')}</small>
            </div>
          </div>

          <div className="dialog-actions">
            {editingAgentId && onViewRuns && (
              <button
                type="button"
                className="btn-secondary"
                onClick={() => onViewRuns(editingAgentId)}
              >
                {t('agent_runs.view_runs')}
              </button>
            )}
            {editingAgentId && onViewProposals && (
              <button
                type="button"
                className="btn-secondary"
                onClick={() => onViewProposals(editingAgentId)}
              >
                {t('agent_proposals.view_proposals')}
              </button>
            )}
            <button type="button" className="btn-secondary" onClick={onClose}>
              {t('agent_dialog.cancel')}
            </button>
            <button type="submit" className="btn-primary">
              {initialData ? t('agent_dialog.submit_edit') : t('agent_dialog.submit')}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
