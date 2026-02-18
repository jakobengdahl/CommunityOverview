import { useState, useEffect } from 'react';
import useGraphStore from '../store/graphStore';
import { useI18n } from '../i18n';
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
  const { t } = useI18n();

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
      alert(t('subscription_dialog.validation_error'));
      return;
    }

    // Build the subscription node
    const activeOps = Object.entries(operations).filter(([_, v]) => v).map(([k]) => k).join(', ');
    const subscriptionNode = {
      name: name.trim(),
      type: 'EventSubscription',
      description: description.trim() || t('subscription_dialog.webhook_description', { name }),
      summary: t('subscription_dialog.webhook_summary', { events: activeOps }),
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
        <h2>{t('subscription_dialog.title')}</h2>
        <p className="dialog-description">
          {t('subscription_dialog.description')}
        </p>

        <form onSubmit={handleSubmit}>
          <div className="form-section">
            <h3>{t('subscription_dialog.basic_info')}</h3>

            <div className="form-group">
              <label htmlFor="sub-name">{t('subscription_dialog.name_label')}</label>
              <input
                id="sub-name"
                type="text"
                value={name}
                onChange={e => setName(e.target.value)}
                placeholder={t('subscription_dialog.name_placeholder')}
                required
              />
            </div>

            <div className="form-group">
              <label htmlFor="sub-description">{t('subscription_dialog.description_label')}</label>
              <textarea
                id="sub-description"
                value={description}
                onChange={e => setDescription(e.target.value)}
                placeholder={t('subscription_dialog.description_placeholder')}
                rows={2}
              />
            </div>
          </div>

          <div className="form-section">
            <h3>{t('subscription_dialog.filters')}</h3>

            <div className="form-group">
              <label>{t('subscription_dialog.node_types_label')}</label>
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
              <label>{t('subscription_dialog.operations_label')}</label>
              <div className="checkbox-group">
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={operations.create}
                    onChange={() => handleToggleOperation('create')}
                  />
                  {t('subscription_dialog.op_create')}
                </label>
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={operations.update}
                    onChange={() => handleToggleOperation('update')}
                  />
                  {t('subscription_dialog.op_update')}
                </label>
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={operations.delete}
                    onChange={() => handleToggleOperation('delete')}
                  />
                  {t('subscription_dialog.op_delete')}
                </label>
              </div>
            </div>

            <div className="form-group">
              <label htmlFor="sub-keywords">{t('subscription_dialog.keywords_label')}</label>
              <input
                id="sub-keywords"
                type="text"
                value={keywords}
                onChange={e => setKeywords(e.target.value)}
                placeholder={t('subscription_dialog.keywords_placeholder')}
              />
              <small>{t('subscription_dialog.keywords_hint')}</small>
            </div>
          </div>

          <div className="form-section">
            <h3>{t('subscription_dialog.delivery')}</h3>

            <div className="form-group">
              <label htmlFor="sub-webhook">{t('subscription_dialog.webhook_label')}</label>
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
              <label htmlFor="sub-ignore-origins">{t('subscription_dialog.ignore_origins_label')}</label>
              <input
                id="sub-ignore-origins"
                type="text"
                value={ignoreOrigins}
                onChange={e => setIgnoreOrigins(e.target.value)}
                placeholder={t('subscription_dialog.ignore_origins_placeholder')}
              />
              <small>{t('subscription_dialog.ignore_origins_hint')}</small>
            </div>
          </div>

          <div className="dialog-actions">
            <button type="button" className="btn-secondary" onClick={onClose}>
              {t('subscription_dialog.cancel')}
            </button>
            <button type="submit" className="btn-primary">
              {t('subscription_dialog.submit')}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
