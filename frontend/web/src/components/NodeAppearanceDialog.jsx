import { useEffect, useRef, useState } from 'react';
import {
  createNodeShapeResolver,
  nodeShapeAreaValues,
  nodeShapeGeometry,
  nodeShapeBandPath,
  nodeShapeTextBox,
} from '@community-graph/ui-graph-canvas';
import { useI18n } from '../i18n';
import './MetamodelExplorerDialog.css';
import './NodeAppearanceDialog.css';

const BASES = ['rectangle', 'rectangle-flat', 'triangle-up', 'triangle-down'];
const RECT = ['top', 'middle', 'bottom'].flatMap((v) =>
  ['left', 'centre', 'right'].map((h) => `${v}-${h}`)
);
const placements = (base) => (base.startsWith('triangle') ? ['centre', 'bottom'] : RECT);
const DEFAULT = {
  version: 1,
  base: 'rectangle',
  scale: 1,
  placement: 'top-left',
  areas: { first: 'type', second: 'name', third: 'summary' },
};
const AREAS = ['first', 'second', 'third'];

export function appearanceOperation(scope, node, selection, revisions, operationId) {
  const type = node.nodeType || node.type;
  const common = { version: 1, selection, operation_id: operationId };
  return scope === 'node'
    ? {
        ...common,
        op: 'node_shape_set',
        node_id: node.id,
        base_revision: revisions[`node:${node.id}`] || 0,
      }
    : {
        ...common,
        op: 'type_shape_set',
        type,
        clear_node_id: node.id,
        base_revision: revisions[`type:${type}`] || 0,
        clear_base_revision: revisions[`node:${node.id}`] || 0,
      };
}

export default function NodeAppearanceDialog({
  node,
  schema,
  shapeOverrides,
  typeShapes,
  nodes,
  onApply,
  onClose,
}) {
  const { t } = useI18n();
  const ref = useRef(null);
  const opener = useRef(null);
  const type = node.nodeType || node.type;
  const entity = { ...node, type };
  const resolved = createNodeShapeResolver({
    nodeTypes: schema?.node_types,
    nodeShapes: schema?.node_shapes,
    shapeOverrides,
    typeShapes,
  })(entity);
  const source = Object.hasOwn(shapeOverrides, node.id)
    ? 'node'
    : Object.hasOwn(typeShapes, type)
      ? 'type'
      : schema?.node_types?.[type]?.shape
        ? 'config'
        : 'builtin';
  const [draft, setDraft] = useState(() => structuredClone(resolved?.definition || DEFAULT));
  const [scale, setScale] = useState(() => String((resolved?.definition || DEFAULT).scale));
  const [configRef, setConfigRef] = useState('');
  const [builtin, setBuiltin] = useState(() => !resolved);
  const [tab, setTab] = useState(0);
  const [scope, setScope] = useState('node');
  const [status, setStatus] = useState({ message: '', slot: 0 });
  const [theme, setTheme] = useState('light');
  const [longText, setLongText] = useState(false);
  const tabs = ['shape', 'text', 'apply'];
  const valid =
    scale !== '' &&
    Number.isFinite(Number(scale)) &&
    Number(scale) >= 0.1 &&
    Number(scale) <= 10 &&
    placements(draft.base).includes(draft.placement);
  const fields = [
    ...new Set([
      'type',
      'name',
      'summary',
      ...(schema?.node_types?.[type]?.fields || []).filter((f) => f !== 'metadata'),
    ]),
  ];
  const values = nodeShapeAreaValues(draft, entity, fields);
  const retained = nodes.filter(
    (n) =>
      n.id !== node.id && (n.nodeType || n.type) === type && Object.hasOwn(shapeOverrides, n.id)
  );
  const geometry = nodeShapeGeometry(draft.base, valid ? Number(scale) : 1);
  const box = nodeShapeTextBox(geometry, draft.placement);
  const triangle = draft.base.startsWith('triangle');
  const [vertical, horizontal] = triangle
    ? [draft.placement, 'centre']
    : draft.placement.split('-');
  const alignment = { left: 'flex-start', centre: 'center', right: 'flex-end' }[horizontal];
  useEffect(() => {
    opener.current = document.activeElement;
    ref.current?.focus();
    const keepFocus = (event) => {
      if (ref.current && !ref.current.contains(event.target)) ref.current.focus();
    };
    document.addEventListener('focusin', keepFocus);
    return () => {
      document.removeEventListener('focusin', keepFocus);
      opener.current?.focus();
    };
  }, []);
  function announce(message) {
    setStatus((previous) => ({
      message: t(`node_appearance.${message}`),
      slot: 1 - previous.slot,
    }));
  }
  function update(patch, message = 'preview_updated') {
    setConfigRef('');
    setBuiltin(false);
    setDraft((d) => ({ ...d, ...patch }));
    announce(message);
  }
  function changeBase(base) {
    const reset = !placements(base).includes(draft.placement);
    update(
      { base, ...(reset ? { placement: placements(base)[0] } : {}) },
      reset ? 'placement_reset' : 'preview_updated'
    );
  }
  function handleKey(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      onClose();
    }
    if (e.key === 'Tab') {
      const items = [
        ...ref.current.querySelectorAll(
          'button:not(:disabled):not([tabindex="-1"]), input:not(:disabled), select:not(:disabled), [tabindex="0"]'
        ),
      ];
      const first = items[0],
        last = items[items.length - 1];
      if (
        e.shiftKey &&
        (document.activeElement === first || document.activeElement === ref.current)
      ) {
        e.preventDefault();
        last?.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first?.focus();
      }
    }
  }
  function tabKey(e) {
    let next = tab;
    if (e.key === 'ArrowRight') next = (tab + 1) % tabs.length;
    else if (e.key === 'ArrowLeft') next = (tab + tabs.length - 1) % tabs.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = tabs.length - 1;
    else return;
    e.preventDefault();
    setTab(next);
    ref.current.querySelectorAll('[role="tab"]')[next]?.focus();
  }
  return (
    <div className="mme-overlay appearance-overlay" onKeyDown={handleKey}>
      <div
        className="mme-dialog appearance-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="appearance-title"
        tabIndex={-1}
        ref={ref}
      >
        <header className="mme-header">
          <div>
            <h2 id="appearance-title" className="mme-title">
              {t('node_appearance.title')}
            </h2>
            <p>
              {node.name} · {type} · {t(`node_appearance.source_${source}`)}
            </p>
          </div>
          <button className="mme-close" onClick={onClose} aria-label={t('node_appearance.cancel')}>
            ×
          </button>
        </header>
        <div className="mme-toolbar">
          <div
            className="mme-tabs"
            role="tablist"
            aria-label={t('node_appearance.title')}
            onKeyDown={tabKey}
          >
            {tabs.map((key, i) => (
              <button
                key={key}
                role="tab"
                id={`appearance-tab-${key}`}
                aria-controls="appearance-panel"
                aria-selected={tab === i}
                tabIndex={tab === i ? 0 : -1}
                className={`mme-tab ${tab === i ? 'active' : ''}`}
                onClick={() => setTab(i)}
              >
                {t(`node_appearance.${key}`)}
              </button>
            ))}
          </div>
        </div>
        <div className="appearance-body">
          <div
            role="tabpanel"
            id="appearance-panel"
            aria-labelledby={`appearance-tab-${tabs[tab]}`}
            className="appearance-fields"
          >
            {tab === 0 && (
              <>
                <label>
                  {t('node_appearance.base')}
                  <select value={draft.base} onChange={(e) => changeBase(e.target.value)}>
                    {BASES.map((base) => (
                      <option key={base} value={base}>
                        {t(`node_appearance.base_${base}`)}
                      </option>
                    ))}
                  </select>
                </label>
                {Object.keys(schema?.node_shapes || {}).length > 0 && (
                  <label>
                    {t('node_appearance.config_shape')}
                    <select
                      value={configRef}
                      onChange={(e) => {
                        const definition = schema.node_shapes[e.target.value];
                        if (definition) {
                          setConfigRef(e.target.value);
                          setBuiltin(false);
                          setDraft(structuredClone(definition));
                          setScale(String(definition.scale));
                          announce('preview_updated');
                        }
                      }}
                    >
                      <option value="">{t('node_appearance.choose_shape')}</option>
                      {Object.keys(schema.node_shapes).map((name) => (
                        <option key={name} value={name}>
                          {name}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
                <p>{t('node_appearance.config_readonly')}</p>
                <label>
                  {t('node_appearance.scale')}
                  <input
                    type="number"
                    min="0.1"
                    max="10"
                    step="0.1"
                    value={scale}
                    onChange={(e) => {
                      setConfigRef('');
                      setBuiltin(false);
                      setScale(e.target.value);
                      announce('preview_updated');
                    }}
                  />
                </label>
                <label>
                  {t('node_appearance.scale_slider')}
                  <input
                    type="range"
                    min="0.1"
                    max="10"
                    step="0.1"
                    value={valid ? Number(scale) : 1}
                    onChange={(e) => {
                      setConfigRef('');
                      setBuiltin(false);
                      setScale(e.target.value);
                      announce('preview_updated');
                    }}
                  />
                </label>
              </>
            )}
            {tab === 1 && (
              <>
                <label>
                  {t('node_appearance.placement')}
                  <select
                    value={draft.placement}
                    onChange={(e) => update({ placement: e.target.value })}
                  >
                    {placements(draft.base).map((p) => (
                      <option key={p} value={p}>
                        {t(`node_appearance.placement_${p}`)}
                      </option>
                    ))}
                  </select>
                </label>
                {AREAS.map((area) => (
                  <label key={area}>
                    {t(`node_appearance.area_${area}`)}
                    <select
                      value={draft.areas[area] || ''}
                      onChange={(e) =>
                        update({ areas: { ...draft.areas, [area]: e.target.value || null } })
                      }
                    >
                      <option value="">{t('node_appearance.none')}</option>
                      {fields.map((field) => (
                        <option key={field} value={field}>
                          {field}
                        </option>
                      ))}
                    </select>
                  </label>
                ))}
              </>
            )}
            {tab === 2 && (
              <>
                <fieldset>
                  <legend>{t('node_appearance.scope')}</legend>
                  {['node', 'type'].map((value) => (
                    <label key={value}>
                      <input
                        type="radio"
                        name="appearance-scope"
                        checked={scope === value}
                        onChange={() => setScope(value)}
                      />{' '}
                      {t(`node_appearance.scope_${value}`)}
                    </label>
                  ))}
                </fieldset>
              </>
            )}
            {!valid && <p role="alert">{t('node_appearance.invalid')}</p>}
          </div>
          <section
            className={`appearance-preview ${theme}`}
            aria-label={t('node_appearance.preview')}
          >
            <div className="appearance-preview-controls">
              <label>
                {t('node_appearance.theme')}
                <select
                  value={theme}
                  onChange={(e) => {
                    setTheme(e.target.value);
                    announce('preview_updated');
                  }}
                >
                  <option value="light">{t('node_appearance.light')}</option>
                  <option value="dark">{t('node_appearance.dark')}</option>
                </select>
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={longText}
                  onChange={(e) => {
                    setLongText(e.target.checked);
                    announce('preview_updated');
                  }}
                />{' '}
                {t('node_appearance.long_text')}
              </label>
            </div>
            <div className="appearance-preview-stage" aria-hidden="true">
              <div
                className="appearance-preview-card"
                style={{
                  width: geometry.width,
                  height: geometry.height,
                  marginTop: triangle ? geometry.bandHeight : 0,
                }}
              >
                <svg
                  width={geometry.width}
                  height={geometry.height}
                  viewBox={`0 0 ${geometry.width} ${geometry.height}`}
                >
                  <path
                    d={geometry.path}
                    fill="var(--preview-surface)"
                    stroke="#777"
                    strokeWidth={2 * geometry.scale}
                  />
                  <path
                    d={nodeShapeBandPath(geometry)}
                    fill={schema?.node_types?.[type]?.color || '#646cff'}
                  />
                </svg>
                <div
                  className="appearance-preview-band"
                  style={{
                    top: triangle ? -geometry.bandHeight : 0,
                    height: geometry.bandHeight,
                    justifyContent: alignment,
                    fontSize: `${geometry.text.type}rem`,
                  }}
                >
                  {values.first || (draft.areas.first ? t('node_appearance.missing') : '')}
                </div>
                <div
                  className="appearance-preview-text"
                  style={{
                    left: box.x,
                    top: box.y,
                    width: box.width,
                    height: box.height,
                    alignItems: alignment,
                    justifyContent: {
                      top: 'flex-start',
                      middle: 'center',
                      centre: 'center',
                      bottom: 'flex-end',
                    }[vertical],
                    textAlign: horizontal === 'centre' ? 'center' : horizontal,
                  }}
                >
                  {AREAS.slice(1).map((area, index) => (
                    <div
                      key={area}
                      style={{
                        fontSize: `${index ? geometry.text.summary : geometry.text.title}rem`,
                      }}
                    >
                      {draft.areas[area] &&
                        (values[area]
                          ? longText
                            ? `${values[area]} ${values[area]} ${values[area]}`
                            : values[area]
                          : t('node_appearance.missing'))}
                    </div>
                  ))}
                </div>
              </div>
            </div>
            {AREAS.some((area) => draft.areas[area] && !values[area]) && (
              <p>{t('node_appearance.missing_warning')}</p>
            )}
          </section>
        </div>
        <div className="appearance-footer">
          {scope === 'type' && (
            <p>
              {t('node_appearance.retained')}:{' '}
              {retained.length
                ? retained.map((n) => n.name || n.id).join(', ')
                : t('node_appearance.none')}
            </p>
          )}
          <span>
            {[0, 1].map((slot) => (
              <span key={slot} role="status" aria-live="polite">
                {status.slot === slot ? status.message : ''}
              </span>
            ))}
          </span>
          <button onClick={onClose}>{t('node_appearance.cancel')}</button>
          <button
            disabled={!valid}
            onClick={() =>
              onApply(
                scope,
                configRef
                  ? { ref: `config:${configRef}` }
                  : builtin
                    ? { builtin: true }
                    : { inline: { ...draft, scale: Number(scale) } }
              )
            }
          >
            {t('node_appearance.apply')}
          </button>
        </div>
      </div>
    </div>
  );
}
