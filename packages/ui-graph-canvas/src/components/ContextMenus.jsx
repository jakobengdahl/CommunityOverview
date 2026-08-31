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
  if (key === 'ArrowDown' || key === 'ArrowRight') {
    return currentIndex < 0 ? 0 : (currentIndex + 1) % items.length;
  }
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

/**
 * ArrowUp/ArrowDown/Home/End roving navigation plus a Tab focus trap, for the
 * six annotation-kind context menus (NoteNode/LabelNode/ArrowNode/
 * GenericAnnotationNode/FreehandAnnotationNode/GroupNode). Reuses
 * `nextRovingIndex` — the same roving-index algorithm `useRootMenuKeyNav`
 * above uses for the graph-node/pane menu system — rather than a third
 * implementation (task-annotation-accessible-shared-controls, closing the
 * accessibility audit's "Arrow-key navigation between menu items" and "Focus
 * trap while open" gaps, both explicitly named MISSING for every annotation
 * kind there).
 *
 * Deliberately NOT `useRootMenuKeyNav` itself: that hook (and
 * `NodeContextMenu`/`MultiNodeContextMenu`, its callers) scope roving to
 * `[data-menu-item="root"]` because those menus nest `<Submenu>` panels with
 * their own, separately-roved `[data-menu-item="sub"]` items — a marker is
 * needed there to tell the two levels apart. None of the six annotation
 * menus has a submenu; every actionable element in one of their portals is a
 * real top-level `<button>`, so this operates on `button:not([disabled])`
 * directly and needs no markup changes across five files to add the marker.
 *
 * The focus trap is minimal by design: it does not fight Tab's natural
 * document-order movement between the menu's own buttons (already correct,
 * since they are real `<button>`s), only the two points where Tab would
 * otherwise leave the menu — wrapping Tab on the last item to the first, and
 * Shift+Tab on the first item to the last.
 */
export function useAnnotationMenuKeyNav(containerRef) {
  return useCallback(
    (event) => {
      const container = containerRef.current;
      if (!container) return;
      // Left/Right rove only in the horizontal property bar
      // (task-annotation-compact-property-bar), where they are the arrows a
      // user actually reaches for. They are deliberately NOT accepted in the
      // vertical menus this same hook also serves: ArrowRight already means
      // "open this submenu" there (see Submenu below), and roving on it too
      // would move focus off the trigger the submenu just opened from.
      const rovingKeys = container.classList.contains('graph-annotation-context-menu--bar')
        ? ['ArrowDown', 'ArrowUp', 'ArrowRight', 'ArrowLeft', 'Home', 'End']
        : ['ArrowDown', 'ArrowUp', 'Home', 'End'];
      if (rovingKeys.includes(event.key)) {
        const items = Array.from(container.querySelectorAll('button:not([disabled])'));
        if (items.length === 0) return;
        event.preventDefault();
        items[nextRovingIndex(items, document.activeElement, event.key)]?.focus();
        return;
      }
      if (event.key !== 'Tab') return;
      const items = Array.from(container.querySelectorAll('button:not([disabled])'));
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      }
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
 * Keep a root-level context menu on-screen: after it renders at the raw
 * click/long-press coordinates, clamp it back inside the viewport if it
 * would overflow the right or bottom edge (e.g. right-clicking a node near
 * the bottom-right corner). Mirrors the Submenu panel's own
 * getBoundingClientRect + before-paint correction below — but Submenu is
 * positioned relative to its trigger and flips to the opposite side via a
 * CSS class, while a root menu is positioned in raw viewport coordinates, so
 * here the fix is to adjust those coordinates directly instead. A layout
 * effect (not passive) so the correction lands before the browser paints the
 * newly (re)opened menu, the same guarantee Submenu's flip relies on.
 */
function useClampedMenuPosition(containerRef, menu) {
  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!menu || !container) return;
    const rect = container.getBoundingClientRect();
    const overflowX = rect.right - window.innerWidth;
    const overflowY = rect.bottom - window.innerHeight;
    if (overflowX > 0) {
      container.style.left = `${Math.max(0, menu.x - overflowX)}px`;
    }
    if (overflowY > 0) {
      container.style.top = `${Math.max(0, menu.y - overflowY)}px`;
    }
  }, [menu, containerRef]);
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
        aria-haspopup="true"
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

// The three attachable kinds the "Nearby object menu" contract entry point
// (docs/ANNOTATION_CONTRACT.md "Human authoring surfaces") offers from an
// existing node/annotation's own context menu. `arrow`/`line` is
// deliberately not offered here: arrow's own drag-to-attach editing already
// gives it a creation-adjacent docking preview via its selected endpoints,
// so this stays scoped to the one-click kinds that had no creation-time
// affordance at all. `vote_dot` used to be a member of this list — task-
// annotation-vote-dot-simplify removed it along with the rest of its
// attachment behaviour (see ATTACHABLE_OVERLAY_KINDS in utils/annotations.js):
// a vote dot is now a plain coloured dot with nothing to pre-wire an
// attachment to.
const NEARBY_ATTACH_KINDS = [
  { kind: 'label', labelKey: 'nearbyLabel' },
  { kind: 'icon', labelKey: 'nearbyIcon' },
  { kind: 'text', labelKey: 'nearbyText' },
];

/**
 * The "Nearby object menu" section shared by every context menu that offers
 * it (NodeContextMenu below, plus the annotation-kind menus in
 * NoteNode/LabelNode/ArrowNode/GenericAnnotationNode/FreehandAnnotationNode).
 * `onAttach(kind)` is called with one of NEARBY_ATTACH_KINDS' kinds; the
 * caller already knows which target id this menu is anchored to.
 *
 * `labels` uses the short `nearbyMenu`/`nearbyLabel`/`nearbyIcon`/
 * `nearbyText` keys — the same short-key convention
 * AnnotationContext's own `labels` object already uses for every other
 * annotation-menu string (e.g. `color` for `cml.annotationColor`). A caller
 * holding the flat `cml` object instead (NodeContextMenu, which is not an
 * annotation-node component and has no AnnotationContext to read from) maps
 * its `annotationNearby*` keys down to these short ones at the call site,
 * the same remapping GraphCanvas itself does when it builds
 * AnnotationContext's `labels` from `cml`.
 */
export function NearbyObjectMenuSection({ labels, onAttach }) {
  if (!onAttach) return null;
  return (
    <>
      <div className="context-menu-title">{labels.nearbyMenu}</div>
      <div className="context-menu-nearby">
        {NEARBY_ATTACH_KINDS.map(({ kind, labelKey }) => (
          <button
            key={kind}
            type="button"
            data-menu-item="root"
            className="context-menu-nearby-button"
            onClick={() => onAttach(kind)}
          >
            + {labels[labelKey]}
          </button>
        ))}
      </div>
    </>
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
  dimmedNodeIds = [],
  dimmedEdgeIds = [],
  graphEdges = [],
  onDimNodes,
  onRestoreNodes,
  onDimEdges,
  onRestoreEdges,
  onAttachNearby,
  onClose,
}) {
  const [containerRef, setContainerRef] = useMergedContainerRef(null);
  useMenuOpenFocus(containerRef, menu);
  useClampedMenuPosition(containerRef, menu);
  const handleRootKeyDown = useRootMenuKeyNav(containerRef);

  if (!menu) return null;
  const nodeId = menu.node.id;
  const isNodeDimmed = dimmedNodeIds.includes(nodeId);
  const incidentEdgeIds = graphEdges
    .filter((e) => e.source === nodeId || e.target === nodeId)
    .map((e) => e.id);
  const incidentEdgesDimmed =
    incidentEdgeIds.length > 0 && incidentEdgeIds.every((id) => dimmedEdgeIds.includes(id));
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
      {onDimNodes && onRestoreNodes && (
        <button
          type="button"
          data-menu-item="root"
          onClick={() => {
            (isNodeDimmed ? onRestoreNodes : onDimNodes)([nodeId]);
            onClose();
          }}
        >
          🔅 {isNodeDimmed ? cml.restoreNode : cml.dimNode}
        </button>
      )}
      {onDimEdges && onRestoreEdges && incidentEdgeIds.length > 0 && (
        <button
          type="button"
          data-menu-item="root"
          onClick={() => {
            (incidentEdgesDimmed ? onRestoreEdges : onDimEdges)(incidentEdgeIds);
            onClose();
          }}
        >
          🔅 {incidentEdgesDimmed ? cml.restoreIncidentEdges : cml.dimIncidentEdges}
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
      {onAttachNearby && (
        <>
          <div className="context-menu-separator"></div>
          <NearbyObjectMenuSection
            labels={{
              nearbyMenu: cml.annotationNearbyMenu,
              nearbyLabel: cml.annotationNearbyLabel,
              nearbyIcon: cml.annotationNearbyIcon,
              nearbyText: cml.annotationNearbyText,
            }}
            onAttach={(kind) => {
              onAttachNearby(menu.node.id, kind);
              onClose();
            }}
          />
        </>
      )}
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
  onHideSelection,
  onDelete,
  onDeleteMultiple,
  onDeleteSelection,
  selectNodesByType,
  onOrganize,
  onAlign,
  onDistribute,
  dimmedNodeIds = [],
  dimmedEdgeIds = [],
  graphEdges = [],
  onDimNodes,
  onRestoreNodes,
  onDimEdges,
  onRestoreEdges,
  onClose,
}) {
  const [containerRef, setContainerRef] = useMergedContainerRef(null);
  useMenuOpenFocus(containerRef, menu);
  useClampedMenuPosition(containerRef, menu);
  const handleRootKeyDown = useRootMenuKeyNav(containerRef);

  if (!menu) return null;
  const actionNodes = menu.actionNodes ?? menu.nodes;
  const nodeIds = actionNodes.map((n) => n.id);
  const nodeIdSet = new Set(nodeIds);
  const allNodesDimmed = nodeIds.length > 0 && nodeIds.every((id) => dimmedNodeIds.includes(id));
  const incidentEdgeIds = Array.from(
    new Set(
      graphEdges.filter((e) => nodeIdSet.has(e.source) || nodeIdSet.has(e.target)).map((e) => e.id)
    )
  );
  const incidentEdgesDimmed =
    incidentEdgeIds.length > 0 && incidentEdgeIds.every((id) => dimmedEdgeIds.includes(id));
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
  const alignItems = onAlign
    ? [
        { key: 'left', label: cml.alignLeft, onSelect: () => onAlign('left') },
        { key: 'centerX', label: cml.alignCenterHorizontal, onSelect: () => onAlign('centerX') },
        { key: 'right', label: cml.alignRight, onSelect: () => onAlign('right') },
        { key: 'top', label: cml.alignTop, onSelect: () => onAlign('top') },
        { key: 'centerY', label: cml.alignCenterVertical, onSelect: () => onAlign('centerY') },
        { key: 'bottom', label: cml.alignBottom, onSelect: () => onAlign('bottom') },
      ]
    : [];
  const distributeItems = onDistribute
    ? [
        {
          key: 'horizontal',
          label: cml.distributeHorizontal,
          onSelect: () => onDistribute('horizontal'),
        },
        {
          key: 'vertical',
          label: cml.distributeVertical,
          onSelect: () => onDistribute('vertical'),
        },
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
      {onShowOnly && nodeIds.length > 0 && (
        <button
          type="button"
          data-menu-item="root"
          onClick={() => {
            onShowOnly(nodeIds);
            onClose();
          }}
        >
          🔍 {cml.showOnly}
        </button>
      )}
      {nodeIds.length > 0 && (
        <button
          type="button"
          data-menu-item="root"
          onClick={() => {
            const types = actionNodes.map((n) => n.data?.nodeType || n.data?.type);
            selectNodesByType(types);
          }}
        >
          🎯 {cml.selectSameType}
        </button>
      )}
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
      {onAlign && (
        <Submenu
          label={`📐 ${cml.align}`}
          ariaLabel={cml.align}
          items={alignItems}
          panelClassName="align-list"
          resetKey={menu}
        />
      )}
      {onDistribute && (
        <Submenu
          label={`↔️ ${cml.distribute}`}
          ariaLabel={cml.distribute}
          items={distributeItems}
          panelClassName="distribute-list"
          resetKey={menu}
        />
      )}
      {(onHideSelection || onHideMultiple || onHide) && (
        <button
          type="button"
          data-menu-item="root"
          onClick={() => {
            if (onHideSelection) {
              onHideSelection();
            } else if (onHideMultiple) {
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
      {onDimNodes && onRestoreNodes && (
        <button
          type="button"
          data-menu-item="root"
          onClick={() => {
            (allNodesDimmed ? onRestoreNodes : onDimNodes)(nodeIds);
            onClose();
          }}
        >
          🔅 {allNodesDimmed ? cml.restoreSelected : cml.dimSelected}
        </button>
      )}
      {onDimEdges && onRestoreEdges && incidentEdgeIds.length > 0 && (
        <button
          type="button"
          data-menu-item="root"
          onClick={() => {
            (incidentEdgesDimmed ? onRestoreEdges : onDimEdges)(incidentEdgeIds);
            onClose();
          }}
        >
          🔅 {incidentEdgesDimmed ? cml.restoreIncidentEdges : cml.dimIncidentEdges}
        </button>
      )}
      {(onDeleteSelection || onDeleteMultiple || onDelete) && (
        <>
          <div className="context-menu-separator"></div>
          <button
            type="button"
            data-menu-item="root"
            className="context-menu-danger"
            onClick={() => {
              if (onDeleteSelection) {
                onDeleteSelection();
              } else if (onDeleteMultiple) {
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
  dimmedEdgeIds = [],
  onDimEdges,
  onRestoreEdges,
  onClose,
}) {
  const [containerRef, setContainerRef] = useMergedContainerRef(null);
  useMenuOpenFocus(containerRef, menu);
  useClampedMenuPosition(containerRef, menu);
  const handleRootKeyDown = useRootMenuKeyNav(containerRef);

  if (!menu) return null;
  // menu.edgeIds carries the whole multi-selection when the right-clicked
  // edge is part of one (see GraphCanvas's onEdgeContextMenu); otherwise
  // just this edge.
  const edgeIds = menu.edgeIds && menu.edgeIds.length > 0 ? menu.edgeIds : [menu.edge.id];
  const allEdgesDimmed = edgeIds.every((id) => dimmedEdgeIds.includes(id));

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
      {onDimEdges && onRestoreEdges && (
        <button
          type="button"
          data-menu-item="root"
          onClick={() => {
            (allEdgesDimmed ? onRestoreEdges : onDimEdges)(edgeIds);
            onClose();
          }}
        >
          🔅 {allEdgesDimmed ? cml.restoreEdge : cml.dimEdge}
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
  useClampedMenuPosition(containerRef, menu);
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
