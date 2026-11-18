import { useEffect, useRef, useState } from 'react';
import {
  NodePreview,
  createNodeShapeResolver,
  nodeShapeAreaValues,
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
const SHAPE_EDITS = ['base', 'scale', 'placement', 'areas'];
// Attribute names with a translation; other declared fields are config keys.
const FIELD_LABELS = ['type', 'name', 'summary', 'description', 'tags'];

const sameDefinition = (a, b) =>
  a.version === b.version &&
  a.base === b.base &&
  a.scale === b.scale &&
  a.placement === b.placement &&
  AREAS.every((area) => a.areas[area] === b.areas[area]);

/**
 * The selection the node's look comes from and the source to report. A
 * selection the canvas cannot resolve falls back to the built-in card, so
 * the source is then 'fallback' rather than the level that holds it.
 */
export function currentAppearance(node, schema, shapeOverrides, typeShapes) {
  const type = node.nodeType || node.type;
  const configShape = Object.hasOwn(schema?.node_types || {}, type)
    ? schema.node_types[type]?.shape
    : null;
  let selection, level;
  if (Object.hasOwn(shapeOverrides, node.id))
    [selection, level] = [shapeOverrides[node.id], 'node'];
  else if (Object.hasOwn(typeShapes, type)) [selection, level] = [typeShapes[type], 'type'];
  else if (typeof configShape === 'string')
    [selection, level] = [{ ref: `config:${configShape}` }, 'config'];
  else return { selection: { builtin: true }, source: 'builtin' };
  const resolved = createNodeShapeResolver({
    nodeTypes: schema?.node_types,
    nodeShapes: schema?.node_shapes,
    shapeOverrides,
    typeShapes,
  })({ ...node, type });
  const explicitBuiltin = selection?.builtin === true && Object.keys(selection).length === 1;
  return { selection, source: resolved || explicitBuiltin ? level : 'fallback' };
}

/**
 * The draft as a pure function of the original selection and the user's
 * explicit edits: no edit, or edits that land on the starting definition,
 * give back the starting selection itself; any other shape edit gives an
 * inline copy. `resolve(selection)` is the canvas resolver for this node.
 */
export function appearanceDraft(original, edits, resolve) {
  const selection = edits.ref ? { ref: edits.ref } : original;
  const start = resolve(selection);
  const from = start || DEFAULT;
  const scaleText = edits.scale ?? String(from.scale);
  if (!SHAPE_EDITS.some((key) => Object.hasOwn(edits, key))) {
    return { selection, definition: start, from, shown: from, scaleText, valid: true };
  }
  const definition = {
    version: 1,
    base: edits.base ?? from.base,
    scale: Number(scaleText),
    placement: edits.placement ?? from.placement,
    areas: { ...from.areas, ...edits.areas },
  };
  const valid =
    scaleText.trim() !== '' &&
    Number.isFinite(definition.scale) &&
    definition.scale >= 0.1 &&
    definition.scale <= 10 &&
    placements(definition.base).includes(definition.placement);
  if (valid && start && sameDefinition(definition, start)) {
    return { selection, definition: start, from, shown: start, scaleText, valid };
  }
  // An invalid scale previews at the starting scale until it is fixed.
  const preview = valid ? definition : { ...definition, scale: from.scale };
  return {
    selection: { inline: definition },
    definition: preview,
    from,
    shown: definition,
    scaleText,
    valid,
  };
}

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
  color,
  shapeOverrides,
  typeShapes,
  nodes,
  onApply,
  onClose,
}) {
  const { t } = useI18n();
  const ref = useRef(null);
  const opener = useRef(null);
  const overlay = useRef(null);
  const type = node.nodeType || node.type;
  const entity = { ...node, type };
  const [original] = useState(() => currentAppearance(node, schema, shapeOverrides, typeShapes));
  const [edits, setEdits] = useState({});
  const [tab, setTab] = useState(0);
  const [scope, setScope] = useState('node');
  const [status, setStatus] = useState({ message: '', slot: 0 });
  const [theme, setTheme] = useState('light');
  const [longText, setLongText] = useState(false);
  const tabs = ['shape', 'text', 'apply'];
  const resolve = (selection) =>
    createNodeShapeResolver({
      nodeTypes: schema?.node_types,
      nodeShapes: schema?.node_shapes,
      shapeOverrides: { [node.id]: selection },
    })(entity)?.definition || null;
  const draft = appearanceDraft(original.selection, edits, resolve);
  const { shown, valid } = draft;
  const fields = [
    ...new Set([
      'type',
      'name',
      'summary',
      ...(schema?.node_types?.[type]?.fields || []).filter((f) => f !== 'metadata'),
    ]),
  ];
  const fieldLabel = (field) =>
    FIELD_LABELS.includes(field) ? t(`node_appearance.field_${field}`) : field;
  const unavailable = (area) => shown.areas[area] !== null && !fields.includes(shown.areas[area]);
  const stretch = (text) => (longText && text ? `${text} ${text} ${text}` : text);
  const values = nodeShapeAreaValues(shown, entity, fields);
  const previewValues = Object.fromEntries(
    AREAS.map((area) => [
      area,
      shown.areas[area] === null
        ? ''
        : unavailable(area)
          ? t('node_appearance.unavailable')
          : stretch(values[area]) || t('node_appearance.missing'),
    ])
  );
  const previewNode = {
    ...entity,
    name: stretch(entity.name),
    summary: stretch(entity.summary || entity.description?.slice(0, 100)),
  };
  const configName =
    typeof draft.selection?.ref === 'string' && draft.selection.ref.startsWith('config:')
      ? draft.selection.ref.slice('config:'.length)
      : '';
  const retained = nodes.filter(
    (n) =>
      n.id !== node.id && (n.nodeType || n.type) === type && Object.hasOwn(shapeOverrides, n.id)
  );
  useEffect(() => {
    opener.current = document.activeElement;
    const inerted = [];
    for (let element = overlay.current; element?.parentElement; element = element.parentElement) {
      for (const sibling of element.parentElement.children) {
        if (sibling !== element && !sibling.hasAttribute('inert')) {
          sibling.setAttribute('inert', '');
          inerted.push(sibling);
        }
      }
    }
    ref.current?.focus();
    const keepFocus = (event) => {
      if (ref.current && !ref.current.contains(event.target)) ref.current.focus();
    };
    document.addEventListener('focusin', keepFocus);
    return () => {
      document.removeEventListener('focusin', keepFocus);
      inerted.forEach((element) => element.removeAttribute('inert'));
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
    setEdits((previous) => {
      const next = { ...previous, ...patch };
      if (patch.areas) next.areas = { ...previous.areas, ...patch.areas };
      return next;
    });
    announce(message);
  }
  function changeBase(base) {
    const reset = !placements(base).includes(shown.placement);
    // The starting placement is preferred, so undoing a base change undoes
    // its placement reset too.
    const placement = placements(base).includes(draft.from.placement)
      ? draft.from.placement
      : placements(base)[0];
    update(
      { base, ...(reset ? { placement } : {}) },
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
  useEffect(() => {
    const isolateKeys = (event) => {
      event.stopPropagation();
      if (!ref.current?.contains(event.target)) {
        event.preventDefault();
        return;
      }
      handleKey(event);
      if (event.target.closest('[role="tablist"]')) tabKey(event);
    };
    window.addEventListener('keydown', isolateKeys, true);
    return () => window.removeEventListener('keydown', isolateKeys, true);
  });
  return (
    <div className="mme-overlay appearance-overlay" ref={overlay}>
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
              {node.name} · {type} · {t(`node_appearance.source_${original.source}`)}
            </p>
          </div>
          <button className="mme-close" onClick={onClose} aria-label={t('node_appearance.cancel')}>
            ×
          </button>
        </header>
        <div className="mme-toolbar">
          <div className="mme-tabs" role="tablist" aria-label={t('node_appearance.title')}>
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
                  <select value={shown.base} onChange={(e) => changeBase(e.target.value)}>
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
                      value={configName}
                      onChange={(e) => {
                        if (Object.hasOwn(schema.node_shapes, e.target.value)) {
                          setEdits({ ref: `config:${e.target.value}` });
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
                    value={draft.scaleText}
                    onChange={(e) => update({ scale: e.target.value })}
                  />
                </label>
                <label>
                  {t('node_appearance.scale_slider')}
                  <input
                    type="range"
                    min="0.1"
                    max="10"
                    step="0.1"
                    value={(draft.definition || shown).scale}
                    onChange={(e) => update({ scale: e.target.value })}
                  />
                </label>
              </>
            )}
            {tab === 1 && (
              <>
                <label>
                  {t('node_appearance.placement')}
                  <select
                    value={shown.placement}
                    onChange={(e) => update({ placement: e.target.value })}
                  >
                    {placements(shown.base).map((p) => (
                      <option key={p} value={p}>
                        {t(`node_appearance.placement_${p}`)}
                      </option>
                    ))}
                  </select>
                </label>
                {AREAS.map((area) => (
                  <div key={area}>
                    <label>
                      {t(`node_appearance.area_${area}`)}
                      <select
                        value={shown.areas[area] ?? ''}
                        aria-describedby={
                          unavailable(area) ? `appearance-unavailable-${area}` : undefined
                        }
                        onChange={(e) => update({ areas: { [area]: e.target.value || null } })}
                      >
                        <option value="">{t('node_appearance.none')}</option>
                        {unavailable(area) && (
                          <option value={shown.areas[area]}>
                            {fieldLabel(shown.areas[area])} ({t('node_appearance.unavailable')})
                          </option>
                        )}
                        {fields.map((field) => (
                          <option key={field} value={field}>
                            {fieldLabel(field)}
                          </option>
                        ))}
                      </select>
                    </label>
                    {unavailable(area) && (
                      <p id={`appearance-unavailable-${area}`} className="appearance-warning">
                        {t('node_appearance.unavailable_warning', {
                          field: fieldLabel(shown.areas[area]),
                        })}
                      </p>
                    )}
                  </div>
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
          <section className="appearance-preview" aria-label={t('node_appearance.preview')}>
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
            {/* The canvas container supplies the canvas background of the chosen theme. */}
            <div
              className={`graph-canvas-container graph-card-theme-${theme} appearance-preview-stage`}
              aria-hidden="true"
            >
              <NodePreview
                node={previewNode}
                color={color}
                definition={draft.definition}
                values={previewValues}
                theme={theme}
              />
            </div>
            {draft.definition &&
              AREAS.some(
                (area) => shown.areas[area] !== null && !unavailable(area) && !values[area]
              ) && <p>{t('node_appearance.missing_warning')}</p>}
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
            onClick={() => onApply(scope, structuredClone(draft.selection))}
          >
            {t('node_appearance.apply')}
          </button>
        </div>
      </div>
    </div>
  );
}
