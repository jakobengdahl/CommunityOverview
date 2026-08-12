/**
 * Presentational context-menu components for GraphCanvas.
 *
 * Each component owns only its own render + click wiring; the menu's open/close
 * state, position and the action callbacks stay in GraphCanvas, which passes the
 * current menu descriptor plus an `onClose` closer. A component returns null when
 * its menu is not open, so callers can render it unconditionally.
 */

/**
 * Build a URL from a template string, substituting {field} or [field] tokens
 * with URI-encoded values from the node's data object. Returns null if the
 * template is not a valid http/https URL after substitution.
 */
export function buildContextMenuUrl(urlTemplate, nodeData) {
  if (typeof urlTemplate !== 'string') return null;
  const trimmed = urlTemplate.trim();
  if (!/^https?:\/\//i.test(trimmed)) return null;
  return trimmed.replace(/\{(\w+)\}|\[(\w+)\]/g, (_match, curlyKey, bracketKey) => {
    const key = curlyKey || bracketKey;
    const value = nodeData[key] ?? '';
    return encodeURIComponent(String(value));
  });
}

export function NodeContextMenu({
  menu,
  labels: cml,
  schema,
  onEdit,
  onHide,
  onExpand,
  onDelete,
  onContextMenuAction,
  selectNodesByType,
  onSelectRelated,
  onViewHistory,
  onClose,
}) {
  if (!menu) return null;
  return (
    <div className="graph-context-menu node-context-menu" style={{ left: menu.x, top: menu.y }}>
      {onEdit && (
        <button
          onClick={() => {
            onEdit(menu.node.id, menu.node.data);
            onClose();
          }}
        >
          ✏️ {cml.edit}
        </button>
      )}
      {onHide && (
        <button
          onClick={() => {
            onHide(menu.node.id);
            onClose();
          }}
        >
          👁️ {cml.hide}
        </button>
      )}
      {onExpand && (
        <button
          onClick={() => {
            onExpand(menu.node.id, menu.node.data);
            onClose();
          }}
        >
          🔍 {cml.expand}
        </button>
      )}
      <button
        onClick={() => {
          const nodeType = menu.node.data?.nodeType || menu.node.data?.type;
          selectNodesByType([nodeType]);
        }}
      >
        🎯 {cml.selectSameType}
      </button>
      {onSelectRelated && (
        <button onClick={() => onSelectRelated(menu.node.id)}>🕸️ {cml.selectRelated}</button>
      )}
      {onViewHistory && (
        <button
          onClick={() => {
            onViewHistory(menu.node.id, menu.node.data);
            onClose();
          }}
        >
          🕘 {cml.viewHistory}
        </button>
      )}
      {(() => {
        const nodeType = menu.node.data?.nodeType || menu.node.data?.type;
        const customItems = schema?.node_types?.[nodeType]?.context_menu;
        if (!Array.isArray(customItems) || customItems.length === 0) return null;
        const nodeData = menu.node.data || {};
        const items = customItems
          .map((item, idx) => {
            if (!item?.label || !item?.action) return null;
            if (item.action.type === 'open_url') {
              const url = buildContextMenuUrl(item.action.url, nodeData);
              if (!url) return null;
              return (
                <button
                  key={idx}
                  onClick={() => {
                    window.open(url, '_blank', 'noopener,noreferrer');
                    onClose();
                  }}
                >
                  {item.icon ? `${item.icon} ` : '🔗 '}
                  {item.label}
                </button>
              );
            }
            if (item.action.type === 'callback') {
              const actionName = item.action.name;
              if (!actionName || !onContextMenuAction) return null;
              return (
                <button
                  key={idx}
                  onClick={() => {
                    onContextMenuAction(actionName, menu.node.id, nodeData);
                    onClose();
                  }}
                >
                  {item.icon ? `${item.icon} ` : '⚡ '}
                  {item.label}
                </button>
              );
            }
            return null;
          })
          .filter(Boolean);
        if (items.length === 0) return null;
        return (
          <>
            {items}
            <div className="context-menu-separator"></div>
          </>
        );
      })()}
      {onDelete && (
        <>
          <div className="context-menu-separator"></div>
          <button
            className="context-menu-danger"
            onClick={() => {
              onDelete(menu.node.id);
              onClose();
            }}
          >
            🗑️ {cml.delete}
          </button>
        </>
      )}
    </div>
  );
}

export function MultiNodeContextMenu({
  menu,
  labels: cml,
  onShowOnly,
  onHide,
  onHideMultiple,
  onDelete,
  onDeleteMultiple,
  selectNodesByType,
  onOrganize,
  onClose,
}) {
  if (!menu) return null;
  return (
    <div
      className="graph-context-menu node-context-menu multi-node-context-menu"
      style={{ left: menu.x, top: menu.y }}
    >
      <div className="context-menu-header">
        {cml.nodesSelected.replace('{count}', menu.nodes.length)}
      </div>
      {onShowOnly && (
        <button
          onClick={() => {
            const nodeIds = menu.nodes.map((n) => n.id);
            onShowOnly(nodeIds);
            onClose();
          }}
        >
          🔍 {cml.showOnly}
        </button>
      )}
      <button
        onClick={() => {
          const types = menu.nodes.map((n) => n.data?.nodeType || n.data?.type);
          selectNodesByType(types);
        }}
      >
        🎯 {cml.selectSameType}
      </button>
      {onOrganize && (
        <>
          <div className="context-menu-separator"></div>
          <div className="context-menu-subheader">{cml.organize}</div>
          <button onClick={() => onOrganize('tidy')}>✨ {cml.autoTidy}</button>
          <button onClick={() => onOrganize('cluster')}>▦ {cml.organizeCluster}</button>
          <button onClick={() => onOrganize('horizontal')}>↔️ {cml.organizeHorizontal}</button>
          <button onClick={() => onOrganize('vertical')}>↕️ {cml.organizeVertical}</button>
          <button onClick={() => onOrganize('tree')}>🌳 {cml.organizeTree}</button>
          <div className="context-menu-separator"></div>
        </>
      )}
      {(onHideMultiple || onHide) && (
        <button
          onClick={() => {
            const nodeIds = menu.nodes.map((n) => n.id);
            if (onHideMultiple) {
              onHideMultiple(nodeIds);
            } else if (onHide) {
              nodeIds.forEach((id) => onHide(id));
            }
            onClose();
          }}
        >
          👁️ {cml.hideAll}
        </button>
      )}
      {(onDeleteMultiple || onDelete) && (
        <>
          <div className="context-menu-separator"></div>
          <button
            className="context-menu-danger"
            onClick={() => {
              const nodeIds = menu.nodes.map((n) => n.id);
              if (onDeleteMultiple) {
                onDeleteMultiple(nodeIds);
              } else if (onDelete) {
                nodeIds.forEach((id) => onDelete(id));
              }
              onClose();
            }}
          >
            🗑️ {cml.deleteAll}
          </button>
        </>
      )}
    </div>
  );
}

export function EdgeContextMenu({
  menu,
  labels: cml,
  relationshipTypes,
  onSetEdgeType,
  onEditEdge,
  onHideEdge,
  onDeleteEdge,
  onClose,
}) {
  if (!menu) return null;
  return (
    <div className="graph-context-menu edge-context-menu" style={{ left: menu.x, top: menu.y }}>
      <div className="context-menu-header">
        {menu.edge.label || menu.edge.data?.type || 'Connection'}
      </div>
      {onSetEdgeType &&
        relationshipTypes.length > 0 &&
        (() => {
          const currentType = menu.edge.label || menu.edge.data?.type || '';
          const isGeneral = !currentType || currentType === 'RELATES_TO';
          const setType = (type) => {
            onSetEdgeType(menu.edge.id, type);
            onClose();
          };
          return (
            <>
              <div className="context-menu-subheader">{cml.changeType}</div>
              <div className="edge-type-list">
                <button
                  className={isGeneral ? 'edge-type-active' : ''}
                  onClick={() => setType('RELATES_TO')}
                >
                  {isGeneral ? '✓ ' : ''}
                  {cml.generalConnection}
                </button>
                {relationshipTypes
                  .filter((rt) => rt.type !== 'RELATES_TO')
                  .map((rt) => (
                    <button
                      key={rt.type}
                      title={rt.description || undefined}
                      className={currentType === rt.type ? 'edge-type-active' : ''}
                      onClick={() => setType(rt.type)}
                    >
                      {currentType === rt.type ? '✓ ' : ''}
                      {rt.type}
                    </button>
                  ))}
              </div>
              <div className="context-menu-separator"></div>
            </>
          );
        })()}
      {onEditEdge && (
        <button
          onClick={() => {
            onEditEdge(menu.edge.id, menu.edge);
            onClose();
          }}
        >
          ✏️ {cml.edit}
        </button>
      )}
      {onHideEdge && (
        <button
          onClick={() => {
            onHideEdge(menu.edge.id);
            onClose();
          }}
        >
          👁️ {cml.hide}
        </button>
      )}
      {onDeleteEdge && (
        <>
          <div className="context-menu-separator"></div>
          <button
            className="context-menu-danger"
            onClick={() => {
              onDeleteEdge(menu.edge.id);
              onClose();
            }}
          >
            🗑️ {cml.delete}
          </button>
        </>
      )}
    </div>
  );
}

export function PaneContextMenu({ menu, labels: cml, menuRef, createAnnotation }) {
  if (!menu) return null;
  return (
    <div
      ref={menuRef}
      className="graph-context-menu pane-context-menu"
      style={{ left: menu.x, top: menu.y }}
    >
      <button onClick={() => createAnnotation('note', menu.flowPosition)}>📝 {cml.addNote}</button>
      <button onClick={() => createAnnotation('label', menu.flowPosition)}>
        🏷️ {cml.addLabel}
      </button>
      <button onClick={() => createAnnotation('arrow', menu.flowPosition)}>
        ➡️ {cml.addArrow}
      </button>
    </div>
  );
}
