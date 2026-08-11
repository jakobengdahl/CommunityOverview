import { useState, useEffect } from 'react';
import useGraphStore from '../store/graphStore';
import { useI18n } from '../i18n';
import EntityHistoryView from './EntityHistoryView';
import './EditNodeDialog.css';

const EDGE_MIN_THICKNESS = 1;
const EDGE_MAX_THICKNESS = 12;
const DEFAULT_THICKNESS = 2;

// Keep this in sync with resolveEdgeVisuals() in the ui-graph-canvas package,
// which normalizes and clamps the same attributes at render time.
function clampThickness(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return DEFAULT_THICKNESS;
  return Math.min(EDGE_MAX_THICKNESS, Math.max(EDGE_MIN_THICKNESS, Math.round(n)));
}

function EditEdgeDialog({ edge, nodes, onClose, onSave, onDelete }) {
  const { getRelationshipTypes } = useGraphStore();
  const { t } = useI18n();
  const [view, setView] = useState('details');
  const [formData, setFormData] = useState({
    type: '',
    label: '',
  });
  const [appearance, setAppearance] = useState({
    direction: 'none',
    useColor: false,
    color: '#666666',
    thickness: 2,
    arrow: 'closed',
    animated: false,
  });

  const relationshipTypes = getRelationshipTypes?.() || [];

  // Find source and target node names for display
  const sourceNode = nodes?.find((n) => n.id === edge?.source);
  const targetNode = nodes?.find((n) => n.id === edge?.target);
  const sourceName = sourceNode?.name || sourceNode?.data?.name || edge?.source || '';
  const targetName = targetNode?.name || targetNode?.data?.name || edge?.target || '';

  useEffect(() => {
    if (edge) {
      setFormData({
        type: edge.type || edge.label || '',
        label: edge.label || '',
      });
      const meta = edge.metadata && typeof edge.metadata === 'object' ? edge.metadata : {};
      const hasColor = typeof meta.color === 'string' && meta.color.trim() !== '';
      // Normalize direction the same way resolveEdgeVisuals does so the dialog
      // reflects (rather than silently discards) externally-set values.
      const direction =
        typeof meta.direction === 'string' ? meta.direction.trim().toLowerCase() : '';
      setAppearance({
        direction: ['forward', 'backward', 'both'].includes(direction) ? direction : 'none',
        useColor: hasColor,
        color: hasColor ? meta.color.trim() : '#666666',
        thickness: clampThickness(meta.thickness),
        arrow: meta.arrow === 'open' ? 'open' : 'closed',
        animated: meta.animated === true || meta.pulse === true,
      });
    }
  }, [edge]);

  const handleChange = (e) => {
    const { name, value } = e.target;
    setFormData((prev) => ({ ...prev, [name]: value }));
  };

  const handleAppearanceChange = (e) => {
    const { name, value, type, checked } = e.target;
    setAppearance((prev) => ({
      ...prev,
      [name]: type === 'checkbox' ? checked : value,
    }));
  };

  const handleSubmit = (e) => {
    e.preventDefault();
    // Merge onto existing metadata, but only store non-default visual keys so an
    // unrelated edit doesn't stamp defaults onto a previously-plain edge, and so
    // clearing an attribute actually removes it.
    const baseMeta =
      edge && edge.metadata && typeof edge.metadata === 'object' ? { ...edge.metadata } : {};

    if (appearance.direction && appearance.direction !== 'none') {
      baseMeta.direction = appearance.direction;
    } else {
      delete baseMeta.direction;
    }

    if (appearance.arrow === 'open') {
      baseMeta.arrow = 'open';
    } else {
      delete baseMeta.arrow;
    }

    const thickness = clampThickness(appearance.thickness);
    if (thickness !== DEFAULT_THICKNESS) {
      baseMeta.thickness = thickness;
    } else {
      delete baseMeta.thickness;
    }

    if (appearance.useColor) {
      baseMeta.color = appearance.color;
    } else {
      delete baseMeta.color;
    }

    if (appearance.animated) {
      baseMeta.animated = true;
    } else {
      // Clear both aliases so unchecking reliably stops the animation even for
      // edges that were pulsed via the external `pulse` flag.
      delete baseMeta.animated;
      delete baseMeta.pulse;
    }

    onSave({
      type: formData.type || null,
      label: formData.label,
      metadata: baseMeta,
    });
  };

  const handleDelete = () => {
    if (onDelete) {
      onDelete(edge.id);
    }
  };

  useEffect(() => {
    const handleEsc = (e) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handleEsc);
    return () => document.removeEventListener('keydown', handleEsc);
  }, [onClose]);

  return (
    <div className="edit-dialog-overlay" onClick={onClose}>
      <div className="edit-dialog" onClick={(e) => e.stopPropagation()}>
        <header className="edit-dialog-header">
          <div className="edit-dialog-header-title">
            <h2>{t('edit_edge.title')}</h2>
          </div>
          <button className="close-button" onClick={onClose}>
            x
          </button>
        </header>

        <div className="node-detail-tabs" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={view === 'details'}
            className={`node-detail-tab${view === 'details' ? ' active' : ''}`}
            onClick={() => setView('details')}
          >
            {t('detail.tab_details')}
          </button>
          {edge?.id && (
            <button
              type="button"
              role="tab"
              aria-selected={view === 'history'}
              className={`node-detail-tab${view === 'history' ? ' active' : ''}`}
              onClick={() => setView('history')}
            >
              {t('history.view_history')}
            </button>
          )}
        </div>

        {view === 'history' ? (
          <div style={{ padding: '16px' }}>
            <EntityHistoryView entityKind="edge" entityId={edge.id} />
          </div>
        ) : (
          <form onSubmit={handleSubmit}>
            <div className="form-group">
              <label>{t('edit_edge.connection')}</label>
              <div style={{ color: '#aaa', fontSize: '0.9rem', padding: '8px 0' }}>
                {sourceName} &rarr; {targetName}
              </div>
            </div>

            <div className="form-group">
              <label htmlFor="edge-type">{t('edit_edge.type')}</label>
              <select id="edge-type" name="type" value={formData.type} onChange={handleChange}>
                <option value="">{t('edit_edge.no_type')}</option>
                {relationshipTypes.map((rt) => (
                  <option key={rt.type || rt} value={rt.type || rt}>
                    {rt.type || rt}
                    {rt.description ? ` - ${rt.description}` : ''}
                  </option>
                ))}
              </select>
            </div>

            <div className="form-group">
              <label htmlFor="edge-label">{t('edit_edge.label_field')}</label>
              <input
                type="text"
                id="edge-label"
                name="label"
                value={formData.label}
                onChange={handleChange}
                placeholder={t('edit_edge.label_placeholder')}
              />
            </div>

            <div className="form-group">
              <label>{t('edit_edge.appearance')}</label>

              <div className="form-group">
                <label htmlFor="edge-direction">{t('edit_edge.direction')}</label>
                <select
                  id="edge-direction"
                  name="direction"
                  value={appearance.direction}
                  onChange={handleAppearanceChange}
                >
                  <option value="none">{t('edit_edge.direction_none')}</option>
                  <option value="forward">{t('edit_edge.direction_forward')}</option>
                  <option value="backward">{t('edit_edge.direction_backward')}</option>
                  <option value="both">{t('edit_edge.direction_both')}</option>
                </select>
              </div>

              <div className="form-group">
                <label htmlFor="edge-arrow">{t('edit_edge.arrow_style')}</label>
                <select
                  id="edge-arrow"
                  name="arrow"
                  value={appearance.arrow}
                  onChange={handleAppearanceChange}
                >
                  <option value="closed">{t('edit_edge.arrow_closed')}</option>
                  <option value="open">{t('edit_edge.arrow_open')}</option>
                </select>
              </div>

              <div className="form-group">
                <label htmlFor="edge-thickness">
                  {t('edit_edge.thickness')} ({appearance.thickness}px)
                </label>
                <input
                  type="range"
                  id="edge-thickness"
                  name="thickness"
                  min="1"
                  max="12"
                  step="1"
                  value={appearance.thickness}
                  onChange={handleAppearanceChange}
                />
              </div>

              <div className="form-group">
                <label>
                  <input
                    type="checkbox"
                    name="useColor"
                    checked={appearance.useColor}
                    onChange={handleAppearanceChange}
                  />{' '}
                  {t('edit_edge.custom_color')}
                </label>
                {appearance.useColor && (
                  <input
                    type="color"
                    name="color"
                    value={appearance.color}
                    onChange={handleAppearanceChange}
                    aria-label={t('edit_edge.color')}
                  />
                )}
              </div>

              <div className="form-group">
                <label>
                  <input
                    type="checkbox"
                    name="animated"
                    checked={appearance.animated}
                    onChange={handleAppearanceChange}
                  />{' '}
                  {t('edit_edge.animate')}
                </label>
              </div>
            </div>

            <div className="form-actions">
              {onDelete && (
                <button
                  type="button"
                  className="secondary"
                  style={{ color: '#ef4444', borderColor: '#ef4444', marginRight: 'auto' }}
                  onClick={handleDelete}
                >
                  {t('context_menu.delete')}
                </button>
              )}
              <button type="button" className="secondary" onClick={onClose}>
                {t('common.cancel')}
              </button>
              <button type="submit" className="primary">
                {t('common.save')}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

export default EditEdgeDialog;
