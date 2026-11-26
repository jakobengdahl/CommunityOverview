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
export function currentAppearance(node, schema, shapeOverrides, typeShapes, savedShapes = {}) {
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
    savedShapes,
    shapeOverrides,
    typeShapes,
  })({ ...node, type });
  const explicitBuiltin = selection?.builtin === true && Object.keys(selection).length === 1;
  return { selection, source: resolved || explicitBuiltin ? level : 'fallback' };
}

// The selection the draft starts from: a picked reference, the built-in card
// once the draft's saved entry is deleted, or else the original selection.
const startOf = (original, edits) =>
  edits.ref ? { ref: edits.ref } : edits.builtin ? { builtin: true } : original;

/**
 * The draft as a pure function of the original selection and the user's
 * explicit edits: no edit, or edits that land on the starting definition,
 * give back the starting selection itself; any other shape edit gives an
 * inline copy. `resolve(selection)` is the canvas resolver for this node.
 */
export function appearanceDraft(original, edits, resolve) {
  const selection = startOf(original, edits);
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

/**
 * A key no entry has used: keys are never reused, since a deleted key keeps
 * its tombstone (NODE_SHAPES.md § Resolution and deletion).
 */
export function freshShapeKey(name) {
  const slug = name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^[^a-z]+|-+$/g, '')
    .slice(0, 50);
  return `${slug || 'shape'}-${crypto.randomUUID().slice(0, 8)}`;
}

/**
 * The fetched library with this dialog's own committed writes laid over it,
 * so a saved entry previews before the session's fetch catches up; the
 * higher revision of an entry wins and a deletion removes it. A fetch read at
 * or after a write's revision already holds that write, or its deletion
 * elsewhere, so the write no longer applies.
 */
export function mergeSavedShapes(fetched, written, fetchedRevision = null) {
  const merged = { ...(fetched || {}) };
  for (const [key, entry] of Object.entries(written)) {
    if (fetchedRevision != null && fetchedRevision >= entry.revision) continue;
    if (merged[key] && merged[key].revision > entry.revision) continue;
    if (entry.deleted) delete merged[key];
    else merged[key] = entry;
  }
  return merged;
}

/**
 * The shape edits in `current` made since `sent` was written to the library:
 * the rest are now the entry's content, not the draft's own changes.
 */
function editsSince(sent, current) {
  const changed = Object.fromEntries(
    SHAPE_EDITS.filter(
      (key) => key !== 'areas' && Object.hasOwn(current, key) && current[key] !== sent[key]
    ).map((key) => [key, current[key]])
  );
  const areas = Object.fromEntries(
    Object.entries(current.areas || {}).filter(([area, value]) => value !== sent.areas?.[area])
  );
  return Object.keys(areas).length ? { ...changed, areas } : changed;
}

const savedOf = (selection) =>
  typeof selection?.ref === 'string' && selection.ref.startsWith('saved:')
    ? selection.ref.slice('saved:'.length)
    : '';

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
  savedShapes,
  savedShapesRevision = null,
  library,
  onApply,
  onDraftChange,
  onClose,
  error,
}) {
  const { t } = useI18n();
  const ref = useRef(null);
  const opener = useRef(null);
  const overlay = useRef(null);
  const type = node.nodeType || node.type;
  const entity = { ...node, type };
  const [written, setWritten] = useState({});
  const saved = mergeSavedShapes(savedShapes, written, savedShapesRevision);
  const [original] = useState(() =>
    currentAppearance(node, schema, shapeOverrides, typeShapes, saved)
  );
  const [edits, setEdits] = useState({});
  // The saved entry the draft started from, as read then. The draft resolves
  // against this copy and edit or delete send its revision, so a library
  // change since is a conflict to review, never a silent rebase.
  const [base, setBase] = useState(() => {
    const key = savedOf(original.selection);
    return Object.hasOwn(saved, key) ? { ...saved[key], key } : null;
  });
  const [tab, setTab] = useState(0);
  const [scope, setScope] = useState('node');
  const [status, setStatus] = useState({ message: '', slot: 0 });
  const [theme, setTheme] = useState('light');
  const [longText, setLongText] = useState(false);
  const [shapeName, setShapeName] = useState('');
  // The edit or delete awaiting confirmation, with the usage it warned of
  // and the entry and content it was started for.
  const [pending, setReview] = useState(null);
  const [libraryBusy, setLibraryBusy] = useState(false);
  const [libraryStatus, setLibraryStatus] = useState(null);
  const tabs = ['shape', 'text', 'apply'];
  const savedKey = savedOf(startOf(original.selection, edits));
  const savedEntry = Object.hasOwn(saved, savedKey) ? saved[savedKey] : null;
  const pinned = base?.key === savedKey ? base : null;
  // A library that arrives after open is the start of a draft not yet edited.
  if (!pinned && savedEntry && !SHAPE_EDITS.some((key) => Object.hasOwn(edits, key)))
    setBase({ ...savedEntry, key: savedKey });
  // A pin without a revision is a draft edited before its entry was known:
  // it starts from the built-in card, and the entry arriving is a conflict.
  const unknownStart = pinned?.revision == null;
  // Only a known library can show a conflict: a failed fetch is no evidence.
  const conflict = Boolean(
    savedShapes != null &&
    pinned &&
    (savedEntry ? savedEntry.revision !== pinned.revision : !unknownStart)
  );
  const pinnedLibrary = () => {
    if (!pinned) return saved;
    const { [pinned.key]: _latest, ...rest } = saved;
    return unknownStart ? rest : { ...rest, [pinned.key]: pinned };
  };
  const resolve = (selection) =>
    createNodeShapeResolver({
      nodeTypes: schema?.node_types,
      nodeShapes: schema?.node_shapes,
      savedShapes: pinnedLibrary(),
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
  // Any draft change after the warning ends the review, so a confirm never
  // writes content or a target the warning was not shown for.
  const review = pending?.edits === edits ? pending : null;
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
    // A saved start not yet read is pinned as unknown, so a library arriving
    // later never moves these edits onto the entry unreviewed.
    if (!pinned && savedKey && !savedEntry) setBase({ key: savedKey, revision: null });
    setEdits((previous) => {
      const next = { ...previous, ...patch };
      if (patch.areas) next.areas = { ...previous.areas, ...patch.areas };
      return next;
    });
    const next = { ...edits, ...patch, areas: { ...edits.areas, ...patch.areas } };
    // Only the preview claim depends on validity; a placement reset happens
    // regardless and is always announced.
    if (message !== 'preview_updated' || appearanceDraft(original.selection, next, resolve).valid)
      announce(message);
    else setStatus({ message: '', slot: 0 });
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
  function libraryFailed(error) {
    setReview(null);
    if (error?.status === 409) library.refresh?.();
    setLibraryStatus(
      error?.status === 409
        ? 'library_conflict'
        : error?.status === 403
          ? 'library_forbidden'
          : 'library_failed'
    );
  }
  // A library write commits on its own: the session changes only on Apply.
  async function libraryWrite(write, message) {
    setLibraryBusy(true);
    try {
      const entry = await write();
      setWritten((previous) => ({ ...previous, [entry.key]: entry }));
      setReview(null);
      setLibraryStatus(message);
      return entry;
    } catch (error) {
      libraryFailed(error);
      return null;
    } finally {
      setLibraryBusy(false);
    }
  }
  async function saveNew(name) {
    const sent = edits;
    const entry = await libraryWrite(
      () => library.create(freshShapeKey(name), name, structuredClone(shown)),
      'library_saved'
    );
    // Edits made while the request ran stay on top of the saved entry.
    if (entry) {
      setEdits((current) => ({ ...editsSince(sent, current), ref: `saved:${entry.key}` }));
      setBase(entry);
      setShapeName('');
    }
  }
  // Edit and delete first show the current usage, then commit only if
  // neither the entry nor its usage changed since that warning.
  async function startReview(action) {
    const started = { action, key: savedKey, name: pinned.name, edits };
    if (action === 'edit') started.definition = structuredClone(shown);
    setLibraryBusy(true);
    try {
      const { revision, usage, template_usage: templateUsage = 0 } = await library.usage(savedKey);
      if (revision !== pinned.revision) libraryFailed({ status: 409 });
      else {
        setLibraryStatus(null);
        setReview({ ...started, revision, usage, templateUsage });
      }
    } catch (error) {
      libraryFailed(error?.status === 404 ? { status: 409 } : error);
    } finally {
      setLibraryBusy(false);
    }
  }
  async function confirmReview() {
    const { action, key, name, definition, revision, usage, templateUsage } = review;
    if (action === 'delete') {
      const entry = await libraryWrite(
        () => library.remove(key, revision, usage, templateUsage),
        'library_deleted'
      );
      // Its users, this draft included, now show the built-in card; the
      // draft's own changes stay on top of it.
      if (entry) {
        setBase(null);
        setEdits(({ ref: _ref, ...rest }) => ({ ...rest, builtin: true }));
      }
      return;
    }
    const entry = await libraryWrite(
      () => library.update(key, name, definition, revision, usage, templateUsage),
      'library_updated'
    );
    if (entry) {
      setEdits((current) => ({ ...editsSince(review.edits, current), ref: `saved:${key}` }));
      setBase(entry);
    }
  }
  // The user's explicit answer to a conflict: keep the draft's own changes
  // over the latest entry, or over the built-in card once it is deleted.
  function reviewLatest() {
    setReview(null);
    setLibraryStatus(null);
    setBase(savedEntry ? { ...savedEntry, key: savedKey } : null);
    // As after a delete here: dropping the ref alone would fall back to the
    // original selection, which may be another live entry.
    if (!savedEntry) setEdits(({ ref: _ref, ...rest }) => ({ ...rest, builtin: true }));
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
    <div className="mme-overlay appearance-overlay" ref={overlay} onChange={onDraftChange}>
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
                  <select
                    value={shown.base}
                    disabled={libraryBusy}
                    onChange={(e) => changeBase(e.target.value)}
                  >
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
                      disabled={libraryBusy}
                      onChange={(e) => {
                        if (Object.hasOwn(schema.node_shapes, e.target.value)) {
                          setEdits({ ref: `config:${e.target.value}` });
                          setBase(null);
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
                {Object.keys(saved).length > 0 && (
                  <label>
                    {t('node_appearance.saved_shape')}
                    <select
                      value={savedOf(draft.selection)}
                      disabled={libraryBusy}
                      onChange={(e) => {
                        if (Object.hasOwn(saved, e.target.value)) {
                          setEdits({ ref: `saved:${e.target.value}` });
                          setBase({ ...saved[e.target.value], key: e.target.value });
                          announce('preview_updated');
                        }
                      }}
                    >
                      <option value="">{t('node_appearance.choose_shape')}</option>
                      {Object.entries(saved).map(([key, entry]) => (
                        <option key={key} value={key}>
                          {entry.name}
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
                    disabled={libraryBusy}
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
                    disabled={libraryBusy}
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
                    disabled={libraryBusy}
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
                {library && (
                  <fieldset className="appearance-library">
                    <legend>{t('node_appearance.library')}</legend>
                    <label>
                      {t('node_appearance.shape_name')}
                      <input
                        value={shapeName}
                        maxLength={128}
                        onChange={(e) => setShapeName(e.target.value)}
                      />
                    </label>
                    <button
                      disabled={!valid || libraryBusy || !shapeName.trim()}
                      onClick={() => saveNew(shapeName.trim())}
                    >
                      {t('node_appearance.save_named')}
                    </button>
                    {pinned && (!unknownStart || conflict) && (
                      <>
                        <p>
                          {t('node_appearance.saved_source', {
                            name: pinned.name ?? savedEntry.name,
                          })}
                        </p>
                        {conflict && (
                          <div role="alert">
                            <p>{t('node_appearance.library_stale')}</p>
                            <button onClick={reviewLatest}>
                              {t('node_appearance.review_latest')}
                            </button>
                          </div>
                        )}
                        {savedEntry && (
                          <button
                            disabled={!valid || libraryBusy || conflict || !draft.selection.inline}
                            onClick={() => startReview('edit')}
                          >
                            {t('node_appearance.edit_saved')}
                          </button>
                        )}
                        <button
                          disabled={!valid || libraryBusy}
                          onClick={() =>
                            saveNew(
                              shapeName.trim() ||
                                t('node_appearance.copy_name', {
                                  name: pinned.name ?? savedEntry.name,
                                })
                            )
                          }
                        >
                          {t('node_appearance.save_copy')}
                        </button>
                        {savedEntry && (
                          <button
                            disabled={libraryBusy || conflict}
                            onClick={() => startReview('delete')}
                          >
                            {t('node_appearance.delete_saved')}
                          </button>
                        )}
                      </>
                    )}
                    {review && (
                      <div role="alert">
                        <p>
                          {t(`node_appearance.${review.action}_warning`, {
                            count: review.usage,
                            templates: review.templateUsage,
                          })}
                        </p>
                        <button disabled={libraryBusy || conflict} onClick={confirmReview}>
                          {t(`node_appearance.confirm_${review.action}`)}
                        </button>
                        <button onClick={() => setReview(null)}>{t('node_appearance.back')}</button>
                      </div>
                    )}
                    {libraryStatus && <p role="status">{t(`node_appearance.${libraryStatus}`)}</p>}
                  </fieldset>
                )}
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
          {error && (
            <p role="alert">
              {t(error === 'failed' ? 'sessions.change_not_saved' : `node_appearance.${error}`)}
            </p>
          )}
          <button onClick={onClose}>{t('node_appearance.cancel')}</button>
          {/* A bare reference would apply the changed entry the preview does not show. */}
          <button
            disabled={!valid || (conflict && Boolean(draft.selection.ref))}
            onClick={() => onApply(scope, structuredClone(draft.selection))}
          >
            {t('node_appearance.apply')}
          </button>
        </div>
      </div>
    </div>
  );
}
