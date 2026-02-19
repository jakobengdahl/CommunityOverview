import { useEffect } from 'react';
import useGraphStore from '../store/graphStore';
import { useI18n } from '../i18n';
import './NodeDetailDialog.css';

function NodeDetailDialog({ node, onClose, onEdit }) {
  const { getNodeColor } = useGraphStore();
  const { t } = useI18n();

  const data = node?.data || {};
  const nodeType = data.type || data.nodeType || '';
  const color = getNodeColor(nodeType);

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

          {data.metadata && Object.keys(data.metadata).length > 0 && (
            <div className="node-detail-section">
              <label>{t('detail.metadata')}</label>
              <div className="node-detail-metadata">
                {Object.entries(data.metadata)
                  .filter(([key]) => key !== 'identifier' && key !== 'node_ids' && key !== 'positions' && key !== 'edge_ids' && key !== 'edges' && key !== 'groups')
                  .map(([key, value]) => (
                    <div key={key} className="node-detail-meta-item">
                      <span className="node-detail-meta-key">{key}:</span>
                      <span className="node-detail-meta-value">
                        {typeof value === 'object' ? JSON.stringify(value) : String(value)}
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
