import { useEffect, useRef } from 'react';
import { XLg } from 'react-bootstrap-icons';
import useGraphStore from '../store/graphStore';
import { resolveColor } from './FloatingToolbar';
import './NodeTypeStatsDialog.css';

function NodeTypeStatsDialog({ nodesByType, schema, onClose }) {
  const dialogRef = useRef(null);
  const schema = useGraphStore((s) => s.schema);

  useEffect(() => {
    dialogRef.current?.focus();
  }, []);

  // Document-level listener so Escape works even after focus leaves the dialog.
  useEffect(() => {
    const handle = (e) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handle);
    return () => document.removeEventListener('keydown', handle);
  }, [onClose]);

  const sorted = Object.entries(nodesByType).sort(([, a], [, b]) => b - a);
  const relationshipTypes = schema?.relationship_types
    ? Object.entries(schema.relationship_types).map(([type, config]) => ({
        type,
        description: config?.description || '',
        sourceTypes: Array.isArray(config?.source_types) ? config.source_types : [],
        targetTypes: Array.isArray(config?.target_types) ? config.target_types : [],
      }))
    : [];
  const formatRules = (types) => (types.length > 0 ? types.join(', ') : '*');

  return (
    <div className="nts-dialog-overlay" onClick={onClose}>
      <div
        ref={dialogRef}
        className="nts-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="nts-dialog-title"
        onClick={(e) => e.stopPropagation()}
        tabIndex={-1}
      >
        <div className="nts-dialog-header">
          <span id="nts-dialog-title" className="nts-dialog-title">
            Nodes by type
          </span>
          <button className="nts-dialog-close" onClick={onClose} aria-label="Close">
            <XLg size={14} />
          </button>
        </div>

        <div className="nts-dialog-body">
          <div className="nts-dialog-section-title">Node types</div>
          {sorted.map(([type, count]) => (
            <div key={type} className="nts-dialog-row">
              <span
                className="nts-dialog-dot"
                style={{ backgroundColor: resolveColor(type, schema) }}
              />
              <span className="nts-dialog-type">{type}</span>
              <span className="nts-dialog-count">{count}</span>
            </div>
          ))}
          {relationshipTypes.length > 0 && (
            <>
              <div className="nts-dialog-section-title nts-dialog-section-spaced">
                Relationship types
              </div>
              {relationshipTypes.map((rt) => (
                <div key={rt.type} className="nts-dialog-relationship-row">
                  <span className="nts-dialog-type">{rt.type}</span>
                  <span className="nts-dialog-rule">
                    {formatRules(rt.sourceTypes)} -&gt; {formatRules(rt.targetTypes)}
                  </span>
                  {rt.description && (
                    <span className="nts-dialog-description">{rt.description}</span>
                  )}
                </div>
              ))}
            </>
          )}
        </div>

        <div className="nts-dialog-footer">
          <button className="nts-dialog-ok" onClick={onClose}>
            OK
          </button>
        </div>
      </div>
    </div>
  );
}

export default NodeTypeStatsDialog;
