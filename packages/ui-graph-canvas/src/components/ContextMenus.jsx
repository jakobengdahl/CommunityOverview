import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';

/**
 * Presentational context-menu components for GraphCanvas.
 *
 * Each component owns only its own render + click wiring; the menu's open/close
 * state, position and the action callbacks stay in GraphCanvas, which passes the
 * current menu descriptor plus an `onClose` closer. A component returns null when
 * its menu is not open, so callers can render it unconditionally.
 *
 * Keyboard/focus contract shared by every menu below:
 * - Opening a menu moves focus to its first enabled item and remembers what was
 *   focused before, so closing it (Escape, an outside click, picking an action)
 *   restores focus there instead of stranding it on a removed element.
 * - ArrowUp/ArrowDown/Home/End rove focus between the menu's top-level items
 *   (`data-menu-item="root"`); a `<Submenu>` traps the same keys while its panel
 *   is open so the two levels never fight over focus.
 * - Every actionable element is a real `<button>` (native tab/click/Enter/Space
 *   support), so touch taps and assistive tech both work with no extra wiring —
 *   the only added affordance is bigger touch targets under `(pointer: coarse)`.
 */

const ROOT_ITEM_SELECTOR = '[data-menu-item="root"]:not([disabled])';

/** Reads the container ref lazily so callers can build the handler before mount. */
function focusableRootItems(containerRef) {
  const container = containerRef.current;
  if (!container) return [];
  return Array.from(container.querySelectorAll(ROOT_ITEM_SELECTOR));
}

/**
 * Moves focus into a just-opened menu and restores it to whatever had focus
 * beforehand once the menu closes (or unmounts).
 *
 * Keyed on the menu descriptor itself, not just "is a menu open" — GraphCanvas
 * retargets an already-open menu straight to a new node/edge (no intervening
 * close), which is a fresh descriptor object each time. Keying on a boolean
 * would miss that retarget, leaving focus on a button belonging to the
 * previous target that may no longer even be in the DOM (a different node
 * type can render fewer items).
 *
 * Restoring focus on close is skipped if focus already moved somewhere else
 * on the page while the menu was open (tracked via `focusin`) — e.g. the host
 * app closes menus as a side effect of the user focusing a search/chat input;
 * closing must not then yank focus back out of the field they just clicked.
 */
function useMenuOpenFocus(containerRef, menu) {
  const previousFocusRef = useRef(null);
  const focusMovedAwayRef = useRef(false);
  useEffect(() => {
    if (!menu) return undefined;
    previousFocusRef.current = document.activeElement;
    focusMovedAwayRef.current = false;
    const items = focusableRootItems(containerRef);
    items[0]?.focus();

    const handleFocusIn = (event) => {
      if (containerRef.current && !containerRef.current.contains(event.target)) {
        focusMovedAwayRef.current = true;
      }
    };
    document.addEventListener('focusin', handleFocusIn, true);

    return () => {
      document.removeEventListener('focusin', handleFocusIn, true);
      if (focusMovedAwayRef.current) return;
      const prev = previousFocusRef.current;
      if (prev && typeof prev.focus === 'function' && document.contains(prev)) {
        prev.focus();
      }
    };
  }, [menu, containerRef]);
}

/**
 * Given a list (in DOM order) and the arrow/Home/End key that fired, returns
 * the index to move focus to next, wrapping at either end. Shared by the
 * root-level roving nav and the Submenu panel's own roving nav below, so a
 * fix to the wrap-around/Home/End semantics only has to be made once.
 */
function nextRovingIndex(items, currentElement, key) {
  const currentIndex = items.indexOf(currentElement);
  if (key === 'Home') return 0;
  if (key === 'End') return items.length - 1;
  if (key === 'ArrowDown') return currentIndex < 0 ? 0 : (currentIndex + 1) % items.length;
  return currentIndex < 0 ? items.length - 1 : (currentIndex - 1 + items.length) % items.length;
}

/** ArrowUp/ArrowDown/Home/End roving navigation across a menu's top-level items. */
function useRootMenuKeyNav(containerRef) {
  return useCallback(
    (event) => {
      if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
      const items = focusableRootItems(containerRef);
      if (items.length === 0) return;
      event.preventDefault();
      items[nextRovingIndex(items, document.activeElement, event.key)]?.focus();
    },
    [containerRef]
  );
}

/** Combines an internal ref (used for focus/keynav) with an optional externally-owned ref. */
function useMergedContainerRef(externalRef) {
  const containerRef = useRef(null);
  const setRef = useCallback(
    (el) => {
      containerRef.current = el;
      if (!externalRef) return;
      if (typeof externalRef === 'function') externalRef(el);
      else externalRef.current = el;
    },
    [externalRef]
  );
  return [containerRef, setRef];
}

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

/**
 * A single flyout submenu used to group a large option set (edge types, layout
 * actions) behind one root-level trigger instead of listing every option at the
 * top level. Opens on click/Enter/Space/ArrowRight (not hover-only, so it works
 * the same on touch); Escape/ArrowLeft closes it and returns focus to the
 * trigger without closing the parent menu.
 *
 * `resetKey` should be the enclosing menu's descriptor object. GraphCanvas can
 * retarget an already-open menu straight to a different node/edge without an
 * intervening close (a new descriptor, same component instance), which would
 * otherwise leave a submenu that was open for the old target still expanded —
 * showing the new target's items but never actually opened for it. Collapse
 * whenever the identity changes.
 */
function Submenu({ label, ariaLabel, items, panelClassName, resetKey }) {
  const [open, setOpen] = useState(false);
  const [flip, setFlip] = useState({ x: false, y: false });
  const triggerRef = useRef(null);
  const panelRef = useRef(null);

  const close = useCallback(() => {
    setOpen(false);
    triggerRef.current?.focus();
  }, []);

  // Collapse if the enclosing menu retargets to a different node/edge (see
  // the `resetKey` doc above). Layout effect, not a passive one: this must
  // land before the browser paints the retargeted frame, or a submenu left
  // open for the previous target would flash on screen showing the new
  // target's items before collapsing. Guarded so a resetKey change that
  // finds the submenu already closed doesn't trigger a pointless extra
  // render.
  useLayoutEffect(() => {
    setOpen((wasOpen) => (wasOpen ? false : wasOpen));
  }, [resetKey]);

  // Focus the first enabled item as soon as the panel opens.
  useEffect(() => {
    if (!open) return undefined;
    const first = panelRef.current?.querySelector('[data-menu-item="sub"]:not([disabled])');
    first?.focus();
    return undefined;
  }, [open]);

  // Keep the panel on-screen: flip to the opposite side when it would overflow.
  useLayoutEffect(() => {
    if (!open || !panelRef.current) return;
    const rect = panelRef.current.getBoundingClientRect();
    setFlip({
      x: rect.right > window.innerWidth,
      y: rect.bottom > window.innerHeight,
    });
  }, [open]);

  // Dismiss on outside interaction, same convention as the other menus.
  useEffect(() => {
    if (!open) return undefined;
    const handleOutside = (event) => {
      if (panelRef.current?.contains(event.target)) return;
      if (triggerRef.current?.contains(event.target)) return;
      setOpen(false);
    };
    document.addEventListener('mousedown', handleOutside, true);
    return () => document.removeEventListener('mousedown', handleOutside, true);
  }, [open]);

  const handleTriggerKeyDown = useCallback(
    (event) => {
      if (event.key === 'ArrowRight' || event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        setOpen(true);
      } else if (open && (event.key === 'ArrowLeft' || event.key === 'Escape')) {
        event.preventDefault();
        event.stopPropagation();
        close();
      }
    },
    [open, close]
  );

  const handlePanelKeyDown = useCallback(
    (event) => {
      if (event.key === 'ArrowLeft' || event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        close();
        return;
      }
      if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
        event.preventDefault();
        event.stopPropagation();
        const focusable = Array.from(
          panelRef.current?.querySelectorAll('[data-menu-item="sub"]:not([disabled])') || []
        );
        if (focusable.length === 0) return;
        focusable[nextRovingIndex(focusable, document.activeElement, event.key)]?.focus();
      }
    },
    [close]
  );

  const panelClasses = [
    'graph-context-menu',
    'context-submenu-panel',
    panelClassName,
    flip.x ? 'context-submenu-panel-flip-x' : '',
    flip.y ? 'context-submenu-panel-flip-y' : '',
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <div className="context-submenu">
      <button
        ref={triggerRef}
        type="button"
        data-menu-item="root"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={ariaLabel || label}
        className="context-submenu-trigger"
        onClick={() => setOpen((o) => !o)}
        onKeyDown={handleTriggerKeyDown}
      >
        <span>{label}</span>
        <span className="context-submenu-caret" aria-hidden="true">
          ›
        </span>
      </button>
      {open && (
        <div
          ref={panelRef}
          aria-label={ariaLabel || label}
          className={panelClasses}
          onKeyDown={handlePanelKeyDown}
        >
          {items.map((item) => (
            <button
              key={item.key}
              type="button"
              data-menu-item="sub"
              disabled={item.disabled}
              aria-disabled={item.disabled || undefined}
              title={item.description || undefined}
              className={item.active ? 'edge-type-active' : ''}
              onClick={() => {
                if (item.disabled) return;
                item.onSelect();
                // Always return focus to this trigger, even when the item's
                // onSelect (e.g. the edge-type picker) also closes the whole
                // parent menu — that unmount happens on a later render, so
                // the trigger is still mounted right now. For actions where
                // the parent menu deliberately stays open (Organize), this is
                // what keeps focus from stranding on <body> after the button
                // it was on disappears.
                close();
              }}
            >
              {item.active ? '✓ ' : ''}
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
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
  const [containerRef, setContainerRef] = useMergedContainerRef(null);
  useMenuOpenFocus(containerRef, menu);
  const handleRootKeyDown = useRootMenuKeyNav(containerRef);

  if (!menu) return null;
  return (
    <div
      ref={setContainerRef}
      className="graph-context-menu node-context-menu"
      style={{ left: menu.x, top: menu.y }}
      onKeyDown={handleRootKeyDown}
    >
      {onEdit && (
        <button
          type="button"
          data-menu-item="root"
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
          type="button"
          data-menu-item="root"
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
          type="button"
          data-menu-item="root"
          onClick={() => {
            onExpand(menu.node.id, menu.node.data);
            onClose();
          }}
        >
          🔍 {cml.expand}
        </button>
      )}
      <button
        type="button"
        data-menu-item="root"
        onClick={() => {
          const nodeType = menu.node.data?.nodeType || menu.node.data?.type;
          selectNodesByType([nodeType]);
        }}
      >
        🎯 {cml.selectSameType}
      </button>
      {onSelectRelated && (
        <button type="button" data-menu-item="root" onClick={() => onSelectRelated(menu.node.id)}>
          🕸️ {cml.selectRelated}
        </button>
      )}
      {onViewHistory && (
        <button
          type="button"
          data-menu-item="root"
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
                  type="button"
                  data-menu-item="root"
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
                  type="button"
                  data-menu-item="root"
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
            type="button"
            data-menu-item="root"
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
  const [containerRef, setContainerRef] = useMergedContainerRef(null);
  useMenuOpenFocus(containerRef, menu);
  const handleRootKeyDown = useRootMenuKeyNav(containerRef);

  if (!menu) return null;
  const organizeItems = onOrganize
    ? [
        { key: 'tidy', label: cml.autoTidy, onSelect: () => onOrganize('tidy') },
        { key: 'cluster', label: cml.organizeCluster, onSelect: () => onOrganize('cluster') },
        {
          key: 'horizontal',
          label: cml.organizeHorizontal,
          onSelect: () => onOrganize('horizontal'),
        },
        { key: 'vertical', label: cml.organizeVertical, onSelect: () => onOrganize('vertical') },
        { key: 'tree', label: cml.organizeTree, onSelect: () => onOrganize('tree') },
      ]
    : [];

  return (
    <div
      ref={setContainerRef}
      className="graph-context-menu node-context-menu multi-node-context-menu"
      style={{ left: menu.x, top: menu.y }}
      onKeyDown={handleRootKeyDown}
    >
      <div className="context-menu-header">
        {cml.nodesSelected.replace('{count}', menu.nodes.length)}
      </div>
      {onShowOnly && (
        <button
          type="button"
          data-menu-item="root"
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
        type="button"
        data-menu-item="root"
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
          <Submenu
            label={`✨ ${cml.organize}`}
            ariaLabel={cml.organize}
            items={organizeItems}
            panelClassName="organize-list"
            resetKey={menu}
          />
        </>
      )}
      {(onHideMultiple || onHide) && (
        <button
          type="button"
          data-menu-item="root"
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
            type="button"
            data-menu-item="root"
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
  const [containerRef, setContainerRef] = useMergedContainerRef(null);
  useMenuOpenFocus(containerRef, menu);
  const handleRootKeyDown = useRootMenuKeyNav(containerRef);

  if (!menu) return null;

  const validRelationshipTypes = (relationshipTypes || []).filter(
    (rt) => rt && typeof rt.type === 'string' && rt.type.length > 0
  );
  const currentType = menu.edge.label || menu.edge.data?.type || '';
  const isGeneral = !currentType || currentType === 'RELATES_TO';

  // The type an edge already has is not a valid "change to" target — offering
  // it as a selectable choice would be a no-op dressed up as an action.
  // Picking a type closes the whole edge menu (matching the other actions
  // below), not just the submenu, so onSelect closes the root menu itself.
  const edgeTypeItems = validRelationshipTypes.length
    ? [
        {
          key: 'RELATES_TO',
          label: cml.generalConnection,
          active: isGeneral,
          disabled: isGeneral,
          onSelect: () => {
            onSetEdgeType(menu.edge.id, 'RELATES_TO');
            onClose();
          },
        },
        ...validRelationshipTypes
          .filter((rt) => rt.type !== 'RELATES_TO')
          .map((rt) => ({
            key: rt.type,
            label: rt.type,
            description: rt.description,
            active: currentType === rt.type,
            disabled: currentType === rt.type,
            onSelect: () => {
              onSetEdgeType(menu.edge.id, rt.type);
              onClose();
            },
          })),
      ]
    : [];

  return (
    <div
      ref={setContainerRef}
      className="graph-context-menu edge-context-menu"
      style={{ left: menu.x, top: menu.y }}
      onKeyDown={handleRootKeyDown}
    >
      <div className="context-menu-header">
        {menu.edge.label || menu.edge.data?.type || 'Connection'}
      </div>
      {onSetEdgeType && edgeTypeItems.length > 0 && (
        <>
          <Submenu
            label={cml.changeType}
            items={edgeTypeItems}
            panelClassName="edge-type-list"
            resetKey={menu}
          />
          <div className="context-menu-separator"></div>
        </>
      )}
      {onEditEdge && (
        <button
          type="button"
          data-menu-item="root"
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
          type="button"
          data-menu-item="root"
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
            type="button"
            data-menu-item="root"
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
  const [containerRef, setContainerRef] = useMergedContainerRef(menuRef);
  useMenuOpenFocus(containerRef, menu);
  const handleRootKeyDown = useRootMenuKeyNav(containerRef);

  if (!menu) return null;
  return (
    <div
      ref={setContainerRef}
      className="graph-context-menu pane-context-menu"
      style={{ left: menu.x, top: menu.y }}
      onKeyDown={handleRootKeyDown}
    >
      <button
        type="button"
        data-menu-item="root"
        onClick={() => createAnnotation('note', menu.flowPosition)}
      >
        📝 {cml.addNote}
      </button>
      <button
        type="button"
        data-menu-item="root"
        onClick={() => createAnnotation('label', menu.flowPosition)}
      >
        🏷️ {cml.addLabel}
      </button>
      <button
        type="button"
        data-menu-item="root"
        onClick={() => createAnnotation('arrow', menu.flowPosition)}
      >
        ➡️ {cml.addArrow}
      </button>
    </div>
  );
}
