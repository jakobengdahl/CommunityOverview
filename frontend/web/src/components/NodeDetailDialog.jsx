import { useEffect } from 'react';
import useGraphStore from '../store/graphStore';
import { useI18n } from '../i18n';
import './NodeDetailDialog.css';

const BASE_FIELDS = new Set(['name', 'description', 'summary', 'tags', 'subtypes', 'metadata', 'identifier']);

const FIELD_LABELS = {
  identifier: 'Resource link (URL)',
  repo: 'Repository URL',
  start_date: 'Start date',
  end_date: 'End date',
  effective_date: 'Effective date',
  target_date: 'Target date',
};

function formatFieldLabel(field) {
  return FIELD_LABELS[field] || field.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

function asUrl(value) {
  if (typeof value !== 'string') return null;
  if (value.startsWith('http://') || value.startsWith('https://')) return value;
  if (value.startsWith('www.')) return `https://${value}`;
  return null;
}

function renderNodeTypePermissions(perms) {
  if (!perms || typeof perms !== 'object') return null;
  const allowed = Object.entries(perms)
    .filter(([, ops]) => ops && (ops.create || ops.update || ops.delete))
    .map(([nodeType, ops]) => {
      const ops_list = ['create', 'update', 'delete'].filter(op => ops[op]);
      return { nodeType, ops_list };
    });
  if (allowed.length === 0) {
    return <span style={{ color: '#888', fontSize: '0.85rem' }}>No operations permitted</span>;
  }
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.35rem', marginTop: '0.25rem' }}>
      {allowed.map(({ nodeType, ops_list }) => (
        <span key={nodeType} style={{
          display: 'inline-flex', alignItems: 'center', gap: '0.3rem',
          background: '#1e2533', border: '1px solid #2d3748', borderRadius: '4px',
          padding: '0.15rem 0.5rem', fontSize: '0.8rem',
        }}>
          <strong style={{ color: '#e2e8f0' }}>{nodeType}:</strong>
          <span style={{ color: '#94a3b8' }}>{ops_list.join(', ')}</span>
        </span>
      ))}
    </div>
  );
}

function NodeDetailDialog({ node, onClose, onEdit }) {
  const { getNodeColor, schema } = useGraphStore();
  const { t } = useI18n();

  const data = node?.data || {};
  const nodeType = data.type || data.nodeType || '';
  const color = getNodeColor(nodeType);

  // Schema-defined extra fields for this node type (stored in metadata by backend)
  const schemaFields = schema?.node_types?.[nodeType]?.fields || [];
  const extraFieldNames = schemaFields.filter(f => !BASE_FIELDS.has(f));
  const extraFields = extraFieldNames
    .map(f => ({ key: f, value: data.metadata?.[f] ?? data[f] ?? null }))
    .filter(({ value }) => value !== null && value !== '');

  // Keys from metadata that are NOT schema extra fields (raw system metadata)
  const extraFieldSet = new Set(extraFieldNames);
  const rawMetadataEntries = Object.entries(data.metadata || {})
    .filter(([k]) => !extraFieldSet.has(k) &&
      !['identifier', 'node_ids', 'positions', 'edge_ids', 'edges', 'groups'].includes(k));

  // Collect links from metadata or identifier field
  const identifier = data.identifier || data.metadata?.identifier || '';
  const hasLink = identifier && (
    identifier.startsWith('http://') ||
    identifier.startsWith('https://') ||
    identifier.startsWith('www.')
  );
  const linkUrl = hasLink
    ? (identifier.startsWith('www.') ? `https://${identifier}` : identifier)
    : null;

  useEffect(() => {
    const handleEsc = (e) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', handleEsc);
    return () => document.removeEventListener('keydown', handleEsc);
  }, [onClose]);

  return (
    <div className="node-detail-overlay" onClick={onClose}>
      <div className="node-detail-dialog" onClick={e => e.stopPropagation()}>
        <header className="node-detail-header">
          <div className="node-detail-header-title">
            <span
              className="node-detail-type-dot"
              style={{ backgroundColor: color }}
            />
            <div>
              <span className="node-detail-type-label" style={{ color }}>
                {nodeType}
              </span>
              <h2>{data.name || data.label || t('detail.unknown_node')}</h2>
            </div>
          </div>
          <button className="close-button" onClick={onClose}>&times;</button>
        </header>

        <div className="node-detail-body">
          {data.summary && (
            <div className="node-detail-section">
              <label>{t('detail.summary')}</label>
              <p className="node-detail-summary">{data.summary}</p>
            </div>
          )}

          {data.description && (
            <div className="node-detail-section">
              <label>{t('detail.description')}</label>
              <p className="node-detail-description">{data.description}</p>
            </div>
          )}

          {data.tags && data.tags.length > 0 && (
            <div className="node-detail-section">
              <label>{t('detail.tags')}</label>
              <div className="node-detail-tags">
                {data.tags.map((tag, i) => (
                  <span key={i} className="node-detail-tag">{tag}</span>
                ))}
              </div>
            </div>
          )}

          {linkUrl && (
            <div className="node-detail-section">
              <label>{t('node_fields.identifier')}</label>
              <a
                href={linkUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="node-detail-link"
              >
                {identifier}
              </a>
            </div>
          )}

          {identifier && !hasLink && (
            <div className="node-detail-section">
              <label>{t('node_fields.identifier')}</label>
              <p className="node-detail-text">{identifier}</p>
            </div>
          )}

          {extraFields.map(({ key, value }) => {
            const url = asUrl(value);
            return (
              <div key={key} className="node-detail-section">
                <label>{formatFieldLabel(key)}</label>
                {url ? (
                  <a href={url} target="_blank" rel="noopener noreferrer" className="node-detail-link">
                    {value}
                  </a>
                ) : (
                  <p className="node-detail-text">{String(value)}</p>
                )}
              </div>
            );
          })}

          {rawMetadataEntries.length > 0 && (
            <div className="node-detail-section">
              <label>{t('detail.metadata')}</label>
              <div className="node-detail-metadata">
                {rawMetadataEntries.map(([key, value]) => (
                    <div key={key} className="node-detail-meta-item">
                      <span className="node-detail-meta-key">{key}:</span>
                      <span className="node-detail-meta-value">
                        {key === 'node_type_permissions' && typeof value === 'object'
                          ? renderNodeTypePermissions(value)
                          : typeof value === 'object' ? JSON.stringify(value) : String(value)}
                      </span>
                    </div>
                  ))
                }
              </div>
            </div>
          )}
        </div>

        <div className="node-detail-actions">
          <button className="secondary" onClick={onClose}>
            {t('detail.close')}
          </button>
          {onEdit && (
            <button
              className="primary"
              onClick={() => onEdit(node.id, data)}
            >
              {t('detail.edit')}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

export default NodeDetailDialog;
