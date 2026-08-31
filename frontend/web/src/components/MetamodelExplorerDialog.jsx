import { useEffect, useMemo, useRef, useState } from 'react';
import {
  XLg,
  ZoomIn,
  ZoomOut,
  ArrowCounterclockwise,
  Diagram3Fill,
  Table,
} from 'react-bootstrap-icons';
import useGraphStore from '../store/graphStore';
import { useI18n } from '../i18n';
import { resolveColor, resolveIcon } from './FloatingToolbar';
import './MetamodelExplorerDialog.css';

// Pseudo node-type id for a relationship endpoint the schema leaves
// unconstrained on one side (source_types or target_types empty). It is never
// expanded into an edge to every real node type — that would render a rule
// nobody configured. It appears in the diagram only when at least one
// relationship type actually needs it.
const ANY_TYPE = '__any__';

const VIEWBOX_WIDTH = 600;
const VIEWBOX_HEIGHT = 520;
const CENTER_X = 300;
const CENTER_Y = 260;
const MIN_ZOOM = 0.5;
const MAX_ZOOM = 2.5;

function getTypeLabel(nodeType, schema, language) {
  const labels = schema?.node_types?.[nodeType]?.labels;
  if (labels && language && labels[language]) return labels[language];
  return nodeType;
}

function formatApplicability(types, anyLabel) {
  return Array.isArray(types) && types.length > 0 ? types.join(', ') : anyLabel;
}

function nodeRadius(typeName, stats) {
  if (typeName === ANY_TYPE) return 10;
  const count = stats?.nodes_by_type?.[typeName];
  if (typeof count !== 'number' || count <= 0) return 16;
  return Math.min(34, 16 + Math.sqrt(count) * 2.2);
}

// True iff this relationship type can draw at least one edge between
// currently-visible node types — i.e. it has some source that is visible (or
// unconstrained) AND some target that is visible (or unconstrained). A
// relationship with one endpoint hidden and one visible (e.g. bound to a
// system type that's currently hidden on one side) has no drawable edge and
// must not appear as if it did; a relationship left fully unconstrained is
// not tied to any specific node type, so it always qualifies.
function relationshipHasVisibleEdge(rt, visibleTypeNames) {
  const sources = rt.source_types.length > 0 ? rt.source_types : [ANY_TYPE];
  const targets = rt.target_types.length > 0 ? rt.target_types : [ANY_TYPE];
  const sourceVisible = sources.some((s) => s === ANY_TYPE || visibleTypeNames.has(s));
  const targetVisible = targets.some((t) => t === ANY_TYPE || visibleTypeNames.has(t));
  return sourceVisible && targetVisible;
}

function matchesFilter(nodeTypeEntry, filterText, language) {
  if (!filterText) return true;
  const needle = filterText.trim().toLowerCase();
  if (!needle) return true;
  const haystacks = [
    nodeTypeEntry.type,
    nodeTypeEntry.description || '',
    nodeTypeEntry.labels?.[language] || '',
  ];
  return haystacks.some((h) => h.toLowerCase().includes(needle));
}

/**
 * Read-only, interactive view of the effective graph schema: node types and
 * relationship types rendered as a pan/zoom network, with an accessible table
 * fallback. Reflects whatever `schema` the backend's config loader produced —
 * nothing here is hardcoded to a particular profile. Editing the metamodel is
 * out of scope for this view by design.
 */
function MetamodelExplorerDialog({ schema, stats, onClose }) {
  const { t, language } = useI18n();
  const storeSchema = useGraphStore((s) => s.schema);
  const effectiveSchema = schema ?? storeSchema;

  const dialogRef = useRef(null);
  const [view, setView] = useState('network');
  const [filterText, setFilterText] = useState('');
  const [showSystemTypes, setShowSystemTypes] = useState(false);
  const [selectedType, setSelectedType] = useState(null);
  const [transform, setTransform] = useState({ x: 0, y: 0, k: 1 });
  const dragState = useRef(null);
  const svgRef = useRef(null);

  useEffect(() => {
    dialogRef.current?.focus();
  }, []);

  useEffect(() => {
    const handleKey = (e) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handleKey);
    return () => document.removeEventListener('keydown', handleKey);
  }, [onClose]);

  const allNodeTypes = useMemo(
    () =>
      Object.entries(effectiveSchema?.node_types || {}).map(([name, config]) => ({
        type: name,
        ...config,
      })),
    [effectiveSchema]
  );

  const allRelationshipTypes = useMemo(
    () =>
      Object.entries(effectiveSchema?.relationship_types || {}).map(([name, config]) => ({
        type: name,
        description: config?.description || '',
        source_types: Array.isArray(config?.source_types) ? config.source_types : [],
        target_types: Array.isArray(config?.target_types) ? config.target_types : [],
      })),
    [effectiveSchema]
  );

  const visibleNodeTypes = useMemo(
    () =>
      allNodeTypes
        .filter((nt) => showSystemTypes || nt.category !== 'system')
        .filter((nt) => matchesFilter(nt, filterText, language)),
    [allNodeTypes, showSystemTypes, filterText, language]
  );

  const visibleTypeNames = useMemo(
    () => new Set(visibleNodeTypes.map((nt) => nt.type)),
    [visibleNodeTypes]
  );

  // A relationship type stays visible only if it can draw at least one edge
  // between the node types currently shown — the same test the network
  // view's edge-building loop applies below, so the table never lists a
  // relationship the diagram can't draw a single line for (e.g. one endpoint
  // hidden by the system-types toggle while the other stays visible).
  const visibleRelationshipTypes = useMemo(
    () =>
      allRelationshipTypes
        .filter((rt) => matchesFilter(rt, filterText, language))
        .filter((rt) => relationshipHasVisibleEdge(rt, visibleTypeNames)),
    [allRelationshipTypes, filterText, language, visibleTypeNames]
  );

  const configuredRelationships = useMemo(
    () =>
      allRelationshipTypes.filter((rt) => rt.source_types.length > 0 || rt.target_types.length > 0),
    [allRelationshipTypes]
  );
  const unconstrainedRelationships = useMemo(
    () =>
      visibleRelationshipTypes.filter(
        (rt) => rt.source_types.length === 0 && rt.target_types.length === 0
      ),
    [visibleRelationshipTypes]
  );

  // One edge per (source, target) pair a configured relationship type
  // actually declares. An empty side is rendered toward the shared ANY_TYPE
  // node rather than fanned out to every visible node type — the schema
  // did not say "every type", it said "unconstrained".
  const edges = useMemo(() => {
    const list = [];
    for (const rt of configuredRelationships) {
      const sources = rt.source_types.length > 0 ? rt.source_types : [ANY_TYPE];
      const targets = rt.target_types.length > 0 ? rt.target_types : [ANY_TYPE];
      for (const source of sources) {
        if (source !== ANY_TYPE && !visibleTypeNames.has(source)) continue;
        for (const target of targets) {
          if (target !== ANY_TYPE && !visibleTypeNames.has(target)) continue;
          list.push({
            id: `${rt.type}:${source}:${target}`,
            relationshipType: rt.type,
            description: rt.description,
            source,
            target,
            usesAny: source === ANY_TYPE || target === ANY_TYPE,
          });
        }
      }
    }
    return list;
  }, [configuredRelationships, visibleTypeNames]);

  const needsAnyNode = edges.some((e) => e.usesAny);

  const positions = useMemo(() => {
    const names = visibleNodeTypes.map((nt) => nt.type);
    const n = names.length;
    const radius = Math.min(230, 90 + n * 14);
    const map = {};
    names.forEach((name, i) => {
      const angle = (2 * Math.PI * i) / Math.max(n, 1) - Math.PI / 2;
      map[name] = {
        x: CENTER_X + radius * Math.cos(angle),
        y: CENTER_Y + radius * Math.sin(angle),
      };
    });
    if (needsAnyNode) {
      map[ANY_TYPE] = { x: CENTER_X, y: CENTER_Y };
    }
    return map;
  }, [visibleNodeTypes, needsAnyNode]);

  const selectedNodeConfig = useMemo(
    () => allNodeTypes.find((nt) => nt.type === selectedType) || null,
    [allNodeTypes, selectedType]
  );
  const relatedRelationships = useMemo(() => {
    if (!selectedType) return [];
    return allRelationshipTypes.filter(
      (rt) => rt.source_types.includes(selectedType) || rt.target_types.includes(selectedType)
    );
  }, [allRelationshipTypes, selectedType]);

  const clampZoom = (k) => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, k));
  const zoomBy = (delta) => setTransform((tr) => ({ ...tr, k: clampZoom(tr.k + delta) }));
  const resetView = () => setTransform({ x: 0, y: 0, k: 1 });

  const handleWheel = (e) => {
    const delta = e.deltaY > 0 ? -0.1 : 0.1;
    zoomBy(delta);
  };
  const handlePointerDown = (e) => {
    // Pointer events (rather than mouse-only) so touch dragging works too —
    // the canvas sets touch-action: none precisely so touch panning is
    // handled here instead of the browser's native scroll/pinch gesture.
    // Ignore a second pointer touching down mid-drag (e.g. a resting finger,
    // or a pinch attempt) rather than letting it hijack the single shared
    // drag state — same guard as BottomSheet.jsx's handlePointerDown.
    if (dragState.current) return;
    // Capturing keeps move/up events targeted here even if the pointer
    // strays outside the SVG mid-drag; not universally supported (falls
    // back to plain event delivery when unavailable, e.g. some test DOMs).
    try {
      e.currentTarget.setPointerCapture?.(e.pointerId);
    } catch {
      // Ignored — see comment above.
    }
    // CSS-pixel-to-viewBox-unit ratio, measured once per drag: the <svg> is
    // styled to fill a flexible container, so its rendered size rarely
    // matches the viewBox's own 600x520 units — a 1:1 use of clientX/Y deltas
    // would pan faster or slower than the cursor, and by a different amount
    // on each axis. Falls back to 1 when the rect isn't measurable (e.g. jsdom).
    const rect = svgRef.current?.getBoundingClientRect();
    const ratioX = rect?.width ? VIEWBOX_WIDTH / rect.width : 1;
    const ratioY = rect?.height ? VIEWBOX_HEIGHT / rect.height : 1;
    dragState.current = {
      pointerId: e.pointerId,
      startX: e.clientX,
      startY: e.clientY,
      origX: transform.x,
      origY: transform.y,
      ratioX,
      ratioY,
    };
  };
  const handlePointerMove = (e) => {
    if (!dragState.current || e.pointerId !== dragState.current.pointerId) return;
    const dx = (e.clientX - dragState.current.startX) * dragState.current.ratioX;
    const dy = (e.clientY - dragState.current.startY) * dragState.current.ratioY;
    setTransform((tr) => ({
      ...tr,
      x: dragState.current.origX + dx,
      y: dragState.current.origY + dy,
    }));
  };
  const handlePointerUp = (e) => {
    if (dragState.current && e.pointerId !== dragState.current.pointerId) return;
    dragState.current = null;
  };

  // Applied group transform: pans by `transform.x/y` while keeping zoom
  // anchored on the diagram's fixed center (CENTER_X/CENTER_Y) rather than
  // the viewBox's top-left corner — otherwise zooming in drifts the whole
  // diagram toward the bottom-right instead of scaling in place.
  const groupTransform = `translate(${CENTER_X * (1 - transform.k) + transform.x},${
    CENTER_Y * (1 - transform.k) + transform.y
  }) scale(${transform.k})`;

  const anyLabel = t('metamodel.any_type');

  return (
    <div className="mme-overlay" onClick={onClose}>
      <div
        ref={dialogRef}
        className="mme-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="mme-dialog-title"
        onClick={(e) => e.stopPropagation()}
        tabIndex={-1}
      >
        <div className="mme-header">
          <span id="mme-dialog-title" className="mme-title">
            {t('metamodel.title')}
          </span>
          <button className="mme-close" onClick={onClose} aria-label={t('metamodel.close')}>
            <XLg size={14} />
          </button>
        </div>

        <p className="mme-readonly-note">{t('metamodel.readonly_note')}</p>

        <div className="mme-toolbar">
          <div className="mme-tabs" role="tablist" aria-label={t('metamodel.title')}>
            <button
              role="tab"
              aria-selected={view === 'network'}
              className={`mme-tab${view === 'network' ? ' active' : ''}`}
              onClick={() => setView('network')}
            >
              <Diagram3Fill size={13} />
              <span>{t('metamodel.view_network')}</span>
            </button>
            <button
              role="tab"
              aria-selected={view === 'table'}
              className={`mme-tab${view === 'table' ? ' active' : ''}`}
              onClick={() => setView('table')}
            >
              <Table size={13} />
              <span>{t('metamodel.view_table')}</span>
            </button>
          </div>

          <input
            type="search"
            className="mme-filter-input"
            value={filterText}
            onChange={(e) => setFilterText(e.target.value)}
            placeholder={t('metamodel.filter_placeholder')}
            aria-label={t('metamodel.filter_aria_label')}
          />

          <label className="mme-system-toggle">
            <input
              type="checkbox"
              checked={showSystemTypes}
              onChange={(e) => setShowSystemTypes(e.target.checked)}
            />
            <span>{t('metamodel.show_system_types')}</span>
          </label>
        </div>

        {view === 'network' ? (
          <div className="mme-network-view">
            <div className="mme-network-canvas">
              {visibleNodeTypes.length === 0 ? (
                <div className="mme-empty">{t('metamodel.no_matches')}</div>
              ) : (
                <svg
                  ref={svgRef}
                  className="mme-svg"
                  viewBox={`0 0 ${VIEWBOX_WIDTH} ${VIEWBOX_HEIGHT}`}
                  role="img"
                  aria-label={t('metamodel.network_aria_label')}
                  onWheel={handleWheel}
                  onPointerDown={handlePointerDown}
                  onPointerMove={handlePointerMove}
                  onPointerUp={handlePointerUp}
                  onPointerCancel={handlePointerUp}
                  onPointerLeave={handlePointerUp}
                >
                  <defs>
                    <marker
                      id="mme-arrowhead"
                      viewBox="0 0 10 10"
                      refX="9"
                      refY="5"
                      markerWidth="6"
                      markerHeight="6"
                      orient="auto-start-reverse"
                    >
                      <path d="M0,0 L10,5 L0,10 z" className="mme-arrowhead-path" />
                    </marker>
                  </defs>
                  <g transform={groupTransform}>
                    {edges.map((edge) => {
                      const p1 = positions[edge.source];
                      const p2 = positions[edge.target];
                      if (!p1 || !p2) return null;
                      const midX = (p1.x + p2.x) / 2;
                      const midY = (p1.y + p2.y) / 2;
                      const highlighted =
                        selectedType &&
                        (edge.source === selectedType || edge.target === selectedType);
                      return (
                        <g
                          key={edge.id}
                          className={`mme-edge${highlighted ? ' mme-edge-highlighted' : ''}`}
                        >
                          <line
                            x1={p1.x}
                            y1={p1.y}
                            x2={p2.x}
                            y2={p2.y}
                            className={`mme-edge-line${edge.usesAny ? ' mme-edge-any' : ''}`}
                            markerEnd="url(#mme-arrowhead)"
                          />
                          <text x={midX} y={midY} className="mme-edge-label">
                            {edge.relationshipType}
                          </text>
                        </g>
                      );
                    })}

                    {visibleNodeTypes.map((nt) => {
                      const pos = positions[nt.type];
                      if (!pos) return null;
                      const r = nodeRadius(nt.type, stats);
                      const count = stats?.nodes_by_type?.[nt.type];
                      const label = getTypeLabel(nt.type, effectiveSchema, language);
                      return (
                        <g
                          key={nt.type}
                          transform={`translate(${pos.x},${pos.y})`}
                          className={`mme-node${selectedType === nt.type ? ' mme-node-selected' : ''}`}
                          tabIndex={0}
                          role="button"
                          aria-label={`${label}${typeof count === 'number' ? `, ${count}` : ''}`}
                          onClick={() => setSelectedType(nt.type)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' || e.key === ' ') {
                              e.preventDefault();
                              setSelectedType(nt.type);
                            }
                          }}
                        >
                          <circle r={r} fill={resolveColor(nt.type, effectiveSchema)} />
                          <text className="mme-node-label" y={r + 13}>
                            {label}
                          </text>
                        </g>
                      );
                    })}

                    {positions[ANY_TYPE] && (
                      <g
                        transform={`translate(${positions[ANY_TYPE].x},${positions[ANY_TYPE].y})`}
                        className="mme-node mme-node-any"
                      >
                        <circle r={10} className="mme-any-circle" />
                        <text className="mme-node-label" y={23}>
                          {anyLabel}
                        </text>
                      </g>
                    )}
                  </g>
                </svg>
              )}

              <div className="mme-zoom-controls">
                <button onClick={() => zoomBy(0.2)} aria-label={t('metamodel.zoom_in')}>
                  <ZoomIn size={14} />
                </button>
                <button onClick={() => zoomBy(-0.2)} aria-label={t('metamodel.zoom_out')}>
                  <ZoomOut size={14} />
                </button>
                <button onClick={resetView} aria-label={t('metamodel.reset_view')}>
                  <ArrowCounterclockwise size={14} />
                </button>
              </div>

              <div className="mme-legend">
                <span className="mme-legend-item">
                  <span className="mme-legend-line" /> {t('metamodel.legend_configured')}
                </span>
                {needsAnyNode && (
                  <span className="mme-legend-item">
                    <span className="mme-legend-line mme-legend-line-any" />{' '}
                    {t('metamodel.legend_any')}
                  </span>
                )}
              </div>
            </div>

            <aside className="mme-detail-panel" aria-label={t('metamodel.detail_panel_aria')}>
              {selectedNodeConfig ? (
                <>
                  <div className="mme-detail-header">
                    {(() => {
                      const Icon = resolveIcon(selectedNodeConfig.type, effectiveSchema);
                      return (
                        <Icon
                          size={16}
                          style={{ color: resolveColor(selectedNodeConfig.type, effectiveSchema) }}
                        />
                      );
                    })()}
                    <h4>{getTypeLabel(selectedNodeConfig.type, effectiveSchema, language)}</h4>
                  </div>
                  <p className="mme-detail-description">
                    {selectedNodeConfig.description || t('metamodel.no_description')}
                  </p>
                  <dl className="mme-detail-meta">
                    <div>
                      <dt>{t('metamodel.col_category')}</dt>
                      <dd>{selectedNodeConfig.category || 'domain'}</dd>
                    </div>
                    <div>
                      <dt>{t('metamodel.col_count')}</dt>
                      <dd>
                        {typeof stats?.nodes_by_type?.[selectedNodeConfig.type] === 'number'
                          ? stats.nodes_by_type[selectedNodeConfig.type]
                          : t('metamodel.count_unknown')}
                      </dd>
                    </div>
                    <div>
                      <dt>{t('metamodel.col_fields')}</dt>
                      <dd>{(selectedNodeConfig.fields || []).join(', ') || '—'}</dd>
                    </div>
                  </dl>
                  {relatedRelationships.length > 0 && (
                    <>
                      <div className="mme-detail-section-title">
                        {t('metamodel.related_relationships')}
                      </div>
                      <ul className="mme-detail-relations">
                        {relatedRelationships.map((rt) => (
                          <li key={rt.type}>
                            <strong>{rt.type}</strong>:{' '}
                            {formatApplicability(rt.source_types, anyLabel)} {'→'}{' '}
                            {formatApplicability(rt.target_types, anyLabel)}
                          </li>
                        ))}
                      </ul>
                    </>
                  )}
                </>
              ) : (
                <p className="mme-detail-hint">{t('metamodel.select_hint')}</p>
              )}
            </aside>
          </div>
        ) : (
          <div className="mme-table-view">
            {visibleNodeTypes.length === 0 && visibleRelationshipTypes.length === 0 ? (
              <div className="mme-empty">{t('metamodel.no_matches')}</div>
            ) : (
              <>
                <div className="mme-table-scroll">
                  <table className="mme-table">
                    <caption>{t('metamodel.node_types_table_caption')}</caption>
                    <thead>
                      <tr>
                        <th scope="col">{t('metamodel.col_type')}</th>
                        <th scope="col">{t('metamodel.col_label')}</th>
                        <th scope="col">{t('metamodel.col_category')}</th>
                        <th scope="col">{t('metamodel.col_description')}</th>
                        <th scope="col">{t('metamodel.col_fields')}</th>
                        <th scope="col">{t('metamodel.col_count')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {visibleNodeTypes.map((nt) => {
                        const count = stats?.nodes_by_type?.[nt.type];
                        return (
                          <tr key={nt.type}>
                            <th scope="row">
                              <span
                                className="mme-table-dot"
                                style={{ backgroundColor: resolveColor(nt.type, effectiveSchema) }}
                              />
                              {nt.type}
                            </th>
                            {/* This column is explicitly "Label (sv)" — the schema's labels
                                map only ever carries an sv translation (see METAMODEL.md), so
                                it always shows that, independent of the current UI language. */}
                            <td>{nt.labels?.sv || '—'}</td>
                            <td>{nt.category || 'domain'}</td>
                            <td>{nt.description || '—'}</td>
                            <td>{(nt.fields || []).join(', ') || '—'}</td>
                            <td>
                              {typeof count === 'number' ? count : t('metamodel.count_unknown')}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>

                <div className="mme-table-scroll">
                  <table className="mme-table">
                    <caption>{t('metamodel.relationship_types_table_caption')}</caption>
                    <thead>
                      <tr>
                        <th scope="col">{t('metamodel.col_type')}</th>
                        <th scope="col">{t('metamodel.col_source')}</th>
                        <th scope="col">{t('metamodel.col_target')}</th>
                        <th scope="col">{t('metamodel.col_description')}</th>
                        <th scope="col">{t('metamodel.col_count')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {visibleRelationshipTypes.map((rt) => {
                        const count = stats?.edges_by_type?.[rt.type];
                        return (
                          <tr key={rt.type}>
                            <th scope="row">{rt.type}</th>
                            <td>{formatApplicability(rt.source_types, anyLabel)}</td>
                            <td>{formatApplicability(rt.target_types, anyLabel)}</td>
                            <td>{rt.description || '—'}</td>
                            <td>
                              {typeof count === 'number' ? count : t('metamodel.count_unknown')}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </div>
        )}

        {view === 'network' && unconstrainedRelationships.length > 0 && (
          <div className="mme-unconstrained">
            <div className="mme-unconstrained-title">{t('metamodel.unconstrained_title')}</div>
            <ul>
              {unconstrainedRelationships.map((rt) => (
                <li key={rt.type}>
                  <strong>{rt.type}</strong>
                  {rt.description ? ` — ${rt.description}` : ''}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}

export default MetamodelExplorerDialog;
