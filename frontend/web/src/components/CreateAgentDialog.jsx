import { useState, useEffect } from 'react';
import useGraphStore from '../store/graphStore';
import './CreateSubscriptionDialog.css'; // Reuse the same styles

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
export default function CreateAgentDialog({ onClose, onSave, initialData }) {
  const schema = useGraphStore((state) => state.schema);

  // Agent info
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [enabled, setEnabled] = useState(true);
  const [taskPrompt, setTaskPrompt] = useState('');

  // MCP Integrations
  const [availableIntegrations, setAvailableIntegrations] = useState([]);
  const [selectedIntegrations, setSelectedIntegrations] = useState([]);
  const [loadingIntegrations, setLoadingIntegrations] = useState(true);

  // Skills
  const [availableSkillNodes, setAvailableSkillNodes] = useState([]);
  const [selectedSkillNodeIds, setSelectedSkillNodeIds] = useState([]);
  const [skillsUrls, setSkillsUrls] = useState(''); // comma-separated URLs
  const [loadingSkills, setLoadingSkills] = useState(true);

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

      if (agent.metadata) {
        // Try new structure first (flattened)
        if (agent.metadata.prompts || agent.metadata.mcp_integration_ids) {
          setEnabled(agent.metadata.enabled ?? true);
          setTaskPrompt(agent.metadata.prompts?.task_prompt || '');
          setSelectedIntegrations(agent.metadata.mcp_integration_ids || []);
          setSelectedSkillNodeIds(agent.metadata.skill_node_ids || []);
          setSkillsUrls((agent.metadata.skills_urls || []).join(', '));
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
            const graphIntegration = data.find(i => i.id === 'GRAPH');
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

  // Fetch available Skill nodes from the graph
  useEffect(() => {
    async function fetchSkillNodes() {
      try {
        const response = await fetch('/agents/skills');
        if (response.ok) {
          setAvailableSkillNodes(await response.json());
        }
      } catch (error) {
        console.error('Failed to fetch Skill nodes:', error);
      } finally {
        setLoadingSkills(false);
      }
    }
    fetchSkillNodes();
  }, []);

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

  const handleToggleIntegration = (integrationId) => {
    if (selectedIntegrations.includes(integrationId)) {
      setSelectedIntegrations(selectedIntegrations.filter(id => id !== integrationId));
    } else {
      setSelectedIntegrations([...selectedIntegrations, integrationId]);
    }
  };

  const handleToggleSkillNode = (nodeId) => {
    if (selectedSkillNodeIds.includes(nodeId)) {
      setSelectedSkillNodeIds(selectedSkillNodeIds.filter(id => id !== nodeId));
    } else {
      setSelectedSkillNodeIds([...selectedSkillNodeIds, nodeId]);
    }
  };

  const handleSubmit = (e) => {
    e.preventDefault();

    if (!name.trim()) {
      alert('Agent name is required');
      return;
    }

    if (!taskPrompt.trim()) {
      alert('Task prompt is required');
      return;
    }

    // Logic for Create vs Update
    if (initialData) {
      // UPDATE existing agent
      const agentId = initialData.agent.id;
      const subscriptionId = initialData.subscription?.id;

      const parsedSkillsUrls = skillsUrls.split(',').map(u => u.trim()).filter(u => u);

      // Update Agent Node
      const agentUpdates = {
        name: name.trim(),
        description: description.trim(),
        summary: `MCP agent with ${selectedIntegrations.length} integration(s)`,
        metadata: {
          ...initialData.agent.metadata,
          subscription_id: subscriptionId,
          enabled: enabled,
          prompts: {
            task_prompt: taskPrompt.trim(),
          },
          mcp_integration_ids: selectedIntegrations,
          skills_urls: parsedSkillsUrls,
          skill_node_ids: selectedSkillNodeIds,
          agent: undefined,
        },
      };

      // Update Subscription Node (if exists)
      let subscriptionUpdates = null;
      if (subscriptionId && initialData.subscription) {
        subscriptionUpdates = {
          name: `${name} - Subscription`,
          description: `Event subscription for agent: ${name}`,
          summary: `Listens to ${Object.entries(operations).filter(([_, v]) => v).map(([k]) => k).join(', ')} events`,
          metadata: {
            ...initialData.subscription.metadata,
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
        subscriptionUpdates
      });

    } else {
      // CREATE new agent
      const idPrefix = 'agent-' + Date.now().toString(36);
      const subscriptionId = idPrefix + '-subscription';
      const agentId = idPrefix + '-node';

      // Create the EventSubscription node
      const subscriptionNode = {
        id: subscriptionId,
        name: `${name} - Subscription`,
        type: 'EventSubscription',
        description: `Event subscription for agent: ${name}`,
        summary: `Listens to ${Object.entries(operations).filter(([_, v]) => v).map(([k]) => k).join(', ')} events`,
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
            webhook_url: webhookUrl.trim() || `internal://agent/${idPrefix}`,
            ignore_origins: [`agent:${idPrefix}`], // Prevent loops - ignore events from this agent
            ignore_session_ids: [],
          },
        },
      };

      const parsedSkillsUrls = skillsUrls.split(',').map(u => u.trim()).filter(u => u);

      // Create the Agent node
      const agentNode = {
        id: agentId,
        name: name.trim(),
        type: 'Agent',
        description: description.trim() || `AI agent: ${name}`,
        summary: `MCP agent with ${selectedIntegrations.length} integration(s)`,
        metadata: {
          subscription_id: subscriptionId,
          enabled: enabled,
          prompts: {
            task_prompt: taskPrompt.trim(),
          },
          mcp_integration_ids: selectedIntegrations,
          skills_urls: parsedSkillsUrls,
          skill_node_ids: selectedSkillNodeIds,
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
      <div className="dialog-content subscription-dialog" onClick={e => e.stopPropagation()}>
        <h2>{initialData ? 'Edit Agent' : 'Create Agent'}</h2>
        <p className="dialog-description">
          An agent listens to graph changes and processes events using an LLM with MCP tools.
        </p>

        <form onSubmit={handleSubmit}>
          <div className="form-section">
            <h3>Agent Configuration</h3>

            <div className="form-group">
              <label className="checkbox-label" style={{ marginBottom: '0.75rem' }}>
                <input
                  type="checkbox"
                  checked={enabled}
                  onChange={(e) => setEnabled(e.target.checked)}
                />
                <strong>Enabled</strong>
                <span style={{ marginLeft: '0.5rem', color: '#888', fontSize: '0.85rem' }}>
                  (agent will start processing events when enabled)
                </span>
              </label>
            </div>

            <div className="form-group">
              <label htmlFor="agent-name">Name *</label>
              <input
                id="agent-name"
                type="text"
                value={name}
                onChange={e => setName(e.target.value)}
                placeholder="e.g. 'Content Enrichment Agent'"
                required
              />
            </div>

            <div className="form-group">
              <label htmlFor="agent-description">Description</label>
              <textarea
                id="agent-description"
                value={description}
                onChange={e => setDescription(e.target.value)}
                placeholder="Optional description of the agent's purpose"
                rows={2}
              />
            </div>

            <div className="form-group">
              <label htmlFor="agent-task-prompt">Task Prompt *</label>
              <textarea
                id="agent-task-prompt"
                value={taskPrompt}
                onChange={e => setTaskPrompt(e.target.value)}
                placeholder={`Describe what the agent should do when receiving events. Example:

When a new Initiative node is created:
1. Use web search to find relevant information
2. Enrich the node description with key facts
3. Add relevant tags based on the content`}
                rows={8}
                required
                style={{ fontFamily: 'monospace', fontSize: '0.85rem' }}
              />
              <small>
                This prompt tells the agent how to process events. The agent receives the base
                system prompt plus this task-specific prompt.
              </small>
            </div>
          </div>

          <div className="form-section">
            <h3>MCP Integrations</h3>
            <p style={{ margin: '0 0 0.75rem 0', color: '#888', fontSize: '0.85rem' }}>
              Select which tool integrations this agent can use
            </p>

            {loadingIntegrations ? (
              <p style={{ color: '#888' }}>Loading integrations...</p>
            ) : availableIntegrations.length === 0 ? (
              <p style={{ color: '#888' }}>No MCP integrations configured on the server</p>
            ) : (
              <div className="checkbox-group">
                {availableIntegrations.map(integration => (
                  <label key={integration.id} className="checkbox-label" style={{ marginBottom: '0.5rem' }}>
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
            <h3>Skills</h3>
            <p style={{ margin: '0 0 0.75rem 0', color: '#888', fontSize: '0.85rem' }}>
              Skills extend the agent with structured instructions loaded from SKILL.md files or Skill nodes in the graph.
            </p>

            <div className="form-group">
              <label htmlFor="agent-skills-urls">Skills URLs (comma-separated)</label>
              <input
                id="agent-skills-urls"
                type="text"
                value={skillsUrls}
                onChange={e => setSkillsUrls(e.target.value)}
                placeholder="https://example.com/SKILL.md, https://github.com/org/repo"
              />
              <small>URLs to SKILL.md files or GitHub repos to load skills from at startup.</small>
            </div>

            <div className="form-group">
              <label>Skill Nodes (from graph)</label>
              {loadingSkills ? (
                <p style={{ color: '#888', fontSize: '0.85rem' }}>Loading skill nodes...</p>
              ) : availableSkillNodes.length === 0 ? (
                <p style={{ color: '#888', fontSize: '0.85rem' }}>No Skill nodes found in the graph</p>
              ) : (
                <div className="checkbox-group">
                  {availableSkillNodes.map(skill => (
                    <label key={skill.id} className="checkbox-label" style={{ marginBottom: '0.5rem' }}>
                      <input
                        type="checkbox"
                        checked={selectedSkillNodeIds.includes(skill.id)}
                        onChange={() => handleToggleSkillNode(skill.id)}
                      />
                      <span>
                        <strong>{skill.name}</strong>
                        {skill.description && (
                          <span style={{ color: '#888', marginLeft: '0.5rem' }}>
                            — {skill.description}
                          </span>
                        )}
                      </span>
                    </label>
                  ))}
                </div>
              )}
            </div>
          </div>

          <div className="form-section">
            <h3>Event Trigger (what events the agent listens to)</h3>

            <div className="form-group">
              <label>Node Types (empty = all)</label>
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
              <label>Operations</label>
              <div className="checkbox-group">
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={operations.create}
                    onChange={() => handleToggleOperation('create')}
                  />
                  Create
                </label>
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={operations.update}
                    onChange={() => handleToggleOperation('update')}
                  />
                  Update
                </label>
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={operations.delete}
                    onChange={() => handleToggleOperation('delete')}
                  />
                  Delete
                </label>
              </div>
            </div>

            <div className="form-group">
              <label htmlFor="agent-keywords">Keywords (comma-separated, optional)</label>
              <input
                id="agent-keywords"
                type="text"
                value={keywords}
                onChange={e => setKeywords(e.target.value)}
                placeholder="Leave empty to match all events"
              />
              <small>Optional: only trigger for events containing these keywords. Leave empty to match all events matching the node type and operation filters above.</small>
            </div>
          </div>

          <div className="dialog-actions">
            <button type="button" className="btn-secondary" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="btn-primary">
              {initialData ? 'Save Changes' : 'Create Agent'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
