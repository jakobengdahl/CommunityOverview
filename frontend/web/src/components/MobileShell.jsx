import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Diagram3Fill,
  Search,
  PlusCircleFill,
  StickyFill,
  ChatDotsFill,
  List,
  XCircle,
} from 'react-bootstrap-icons';
import { useI18n } from '../i18n';
import useGraphStore from '../store/graphStore';
import { useSurfaceManager } from '../hooks/useSurfaceManager';
import BottomSheet from './BottomSheet';
import FloatingSearch from './FloatingSearch';
import FloatingToolbar from './FloatingToolbar';
import ChatPanel from './ChatPanel';
import SessionDrawer from './SessionDrawer';
import './MobileShell.css';

/**
 * MobileShell — the phone/narrow-viewport chrome (WAVE 1 of the mobile-gui
 * initiative): a compact top bar, the full-bleed canvas (rendered by App.jsx,
 * shared with DesktopShell), and a six-slot bottom navigation (Graph / Search
 * / Create / Annotate / Chat / Menu). App.jsx still owns all state and
 * handlers; this component owns only the mobile presentation and the mutual
 * exclusion of its surfaces, via useSurfaceManager
 * (frontend/web/src/hooks/useSurfaceManager.js).
 *
 * Panel content is reused, never forked:
 *  - Search and Create mount FloatingSearch / FloatingToolbar (variant="sheet")
 *    inside the shared BottomSheet primitive.
 *  - Annotate shares that same BottomSheet instance (see `sheetOpen` below)
 *    but cannot mount a host component the way Search/Create do: annotation
 *    creation (AnnotationToolbox) lives inside GraphCanvas, in the
 *    ui-graph-canvas package, and its onCreate/onDragCreate handlers close
 *    over ReactFlow state (screenToFlowPosition, the freehand-drawing mode)
 *    that only exists inside that component. So instead of forking a second
 *    toolbox here, this component renders an empty container div and hands
 *    its DOM node up to App.jsx (`onAnnotationSheetContainerChange`, a plain
 *    callback ref — App.jsx passes its state setter directly), which passes
 *    it into GraphCanvas as `annotationToolboxPortalContainer`. GraphCanvas
 *    then portals its own AnnotationToolbox instance (variant="sheet") into
 *    that node — same component, same creation handlers, only a different
 *    DOM location, the cross-package equivalent of FloatingToolbar's own
 *    variant="sheet" for graph-node creation. Graph-node creation
 *    (Create) and annotation creation (Annotate) stay two distinct nav
 *    slots and two distinct sheet contents, never merged into one - the
 *    task's own "keep them visually and behaviorally distinct" requirement.
 *    On mobile the always-on compact annotation strip GraphCanvas used to
 *    render unconditionally is gone entirely: this Annotate slot (and its
 *    sheet, while open) is now the only mobile entry point, so there is
 *    never more than one bottom surface competing for space with the nav
 *    below - see GraphCanvas.jsx's isCompact branch for the desktop-vs-mobile
 *    split.
 *  - Edit (task-annotation-responsive-bottom-toolbox) shares the same
 *    BottomSheet instance too, via `useSurfaceManager`'s pre-existing
 *    `'detail'` surface — unlike Search/Create/Annotate it has no bottom-nav
 *    slot of its own: it opens only contextually, when a node component deep
 *    inside GraphCanvas calls the opener this component hands up via
 *    `onDetailSheetControllerReady` (a `{open, close}` pair; see that prop's
 *    own comment for why a callback is needed here where the other sheet
 *    surfaces don't need one — they're opened by this component's own nav
 *    buttons, this one is opened from outside it). Its content container
 *    is handed up the same way Annotate's is
 *    (`onAnnotationEditSheetContainerChange`), portaled into by GraphCanvas's
 *    own `annotationEditSheetPortalContainer` prop.
 *  - Menu reuses SessionDrawer exactly as DesktopShell does. Its own slide-in
 *    overlay is an established, independently-tested surface in its own
 *    right, so it is driven directly by the surface manager's "menu" state
 *    rather than re-hosted inside BottomSheet — a deliberate scoping choice
 *    (see the PR description) rather than a smaller version of the same idea.
 *  - Chat also mounts inside the shared BottomSheet, via ChatPanel's
 *    variant="sheet" (ChatPanel.jsx) — the fixed-position floating panel and
 *    its minimized vertical bar are a desktop-only presentation; on mobile
 *    the Chat slot in the bottom nav is the equivalent affordance while
 *    closed. It gets its own BottomSheet instance (a second one alongside
 *    the search/create/annotate sheet below) because it is driven by the
 *    shared store's chatPanelOpen field rather than useSurfaceManager —
 *    ChatPanel's own controls (its header, its minimized bar on desktop) can
 *    flip that field directly, bypassing whatever this component calls to
 *    open it, so mutual exclusion with search/create/annotate/menu is
 *    enforced reactively below rather than only at this component's own call
 *    sites.
 */
function MobileShell({
  sessionId,
  onClear,
  sessions,
  currentSessionId,
  onNewSession,
  onConnectSession,
  onSelectSession,
  onRenameSession,
  onDeleteSession,
  onCopySessionLink,
  onCopyTriggerUrl,
  onOpenSettings,
  onOpenActivity,
  canvasLocked,
  onToggleLock,
  onEnterFullscreen,
  suspendEscape,
  onCreateNodeForType,
  onCreateAgent,
  onCreateSubscription,
  onSaveView,
  onCreateGroup,
  onCreateActiveKnowledgeCollection,
  llmAvailable,
  akcShortName,
  // A plain callback ref (App.jsx passes its state setter for this
  // directly): called with the annotate sheet's content DOM node while it is
  // mounted, and with null once it unmounts (the sheet closing, or switching
  // to a different surface). See the component doc comment above.
  onAnnotationSheetContainerChange,
  // The Edit surface's own content container — same callback-ref shape and
  // purpose as onAnnotationSheetContainerChange above, one slot down (the
  // `'detail'` surface rather than `'annotate'`).
  onAnnotationEditSheetContainerChange,
  // Called (via a memoized `{open, close}` object, so it fires once per real
  // change rather than every render) with the functions that open/close the
  // `'detail'` surface — GraphCanvas needs a way to ask THIS component's own
  // `useSurfaceManager` instance to open Edit from a node's button, and
  // unlike Search/Create/Annotate/Menu (all opened by this component's own
  // nav clicks) that request originates outside it, so it has to be handed
  // up rather than only down. See the component doc comment above.
  onDetailSheetControllerReady,
}) {
  const { t } = useI18n();
  const { chatPanelOpen, setChatPanelOpen, setChatPanelOpenTransient } = useGraphStore();
  const surface = useSurfaceManager();
  const [sheetSnapPoint, setSheetSnapPoint] = useState('half');
  const [chatSnapPoint, setChatSnapPoint] = useState('half');

  const sheetOpen =
    surface.isOpen('search') ||
    surface.isOpen('create') ||
    surface.isOpen('annotate') ||
    surface.isOpen('detail');

  // Destructured once so the callbacks below depend on these two plain
  // bindings — each individually stable, `useSurfaceManager` wraps them in
  // their own `useCallback([])` — rather than on the whole `surface` object,
  // which is a fresh literal every render. Depending on `surface` itself
  // would recreate `openDetailSheet` every render, which would recreate the
  // memoized controller below every render, which would re-fire the
  // ready-effect and re-run App.jsx's setState every render: an infinite
  // loop, not just a wasted render.
  const { open: openSurface, close: closeSurface } = surface;
  const openDetailSheet = useCallback(() => {
    setChatPanelOpenTransient(false);
    openSurface('detail');
  }, [openSurface, setChatPanelOpenTransient]);
  const detailSheetController = useMemo(
    () => ({ open: openDetailSheet, close: closeSurface }),
    [openDetailSheet, closeSurface]
  );
  useEffect(() => {
    onDetailSheetControllerReady?.(detailSheetController);
  }, [detailSheetController, onDetailSheetControllerReady]);

  // ChatPanel's own minimized bubble (its established touch target, reused
  // as-is per the "do not fork" rule) calls the store's toggleChatPanel
  // directly, bypassing openChat() below - so mutual exclusion with
  // search/create/menu has to be enforced here, reactively, rather than only
  // at the call sites this component controls.
  useEffect(() => {
    if (chatPanelOpen) {
      surface.close();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chatPanelOpen]);

  const closeAllSurfaces = () => {
    surface.close();
    setChatPanelOpenTransient(false);
  };

  const openSheet = (name) => {
    setChatPanelOpenTransient(false);
    surface.toggle(name);
  };

  const openChat = () => {
    if (chatPanelOpen) {
      setChatPanelOpen(false);
      return;
    }
    surface.close();
    setChatPanelOpen(true);
  };

  const openMenu = () => {
    setChatPanelOpenTransient(false);
    surface.toggle('menu');
  };

  const navItems = [
    {
      key: 'graph',
      label: t('mobile_nav.graph'),
      Icon: Diagram3Fill,
      active: !sheetOpen && !chatPanelOpen && !surface.isOpen('menu'),
      onClick: closeAllSurfaces,
    },
    {
      key: 'search',
      label: t('mobile_nav.search'),
      Icon: Search,
      active: surface.isOpen('search'),
      onClick: () => openSheet('search'),
    },
    {
      key: 'create',
      label: t('mobile_nav.create'),
      Icon: PlusCircleFill,
      active: surface.isOpen('create'),
      onClick: () => openSheet('create'),
    },
    {
      key: 'annotate',
      label: t('mobile_nav.annotate'),
      Icon: StickyFill,
      active: surface.isOpen('annotate'),
      onClick: () => openSheet('annotate'),
    },
    {
      key: 'chat',
      label: t('mobile_nav.chat'),
      Icon: ChatDotsFill,
      active: chatPanelOpen,
      onClick: openChat,
    },
    {
      key: 'menu',
      label: t('mobile_nav.menu'),
      Icon: List,
      active: surface.isOpen('menu'),
      onClick: openMenu,
    },
  ];

  return (
    <>
      <div className="mobile-shell-topbar">
        <span className="mobile-shell-topbar-title">{sessionId ? t('header.title') : ''}</span>
        <button
          className="mobile-shell-topbar-clear"
          onClick={onClear}
          aria-label={t('header.clear_canvas_aria')}
          title={t('header.clear_canvas_tooltip')}
        >
          <XCircle size={16} />
        </button>
      </div>

      <nav className="mobile-shell-bottomnav" aria-label={t('mobile_nav.bottom_nav')}>
        {navItems.map(({ key, label, Icon, active, onClick }) => (
          <button
            key={key}
            className={`mobile-shell-bottomnav-item${active ? ' active' : ''}`}
            onClick={onClick}
            aria-pressed={active}
            aria-label={label}
          >
            <Icon size={20} />
            <span className="mobile-shell-bottomnav-label">{label}</span>
          </button>
        ))}
      </nav>

      <BottomSheet
        isOpen={sheetOpen}
        snapPoint={sheetSnapPoint}
        onSnapPointChange={setSheetSnapPoint}
        onClose={surface.close}
        title={
          surface.isOpen('search')
            ? t('mobile_nav.search_panel_title')
            : surface.isOpen('create')
              ? t('mobile_nav.create_panel_title')
              : surface.isOpen('detail')
                ? t('mobile_nav.edit_panel_title')
                : t('mobile_nav.annotate_panel_title')
        }
      >
        {surface.isOpen('search') && <FloatingSearch variant="sheet" />}
        {surface.isOpen('create') && (
          <FloatingToolbar
            variant="sheet"
            onCreateNode={(nodeType) => {
              surface.close();
              onCreateNodeForType(nodeType);
            }}
            onCreateAgent={() => {
              surface.close();
              onCreateAgent();
            }}
            onCreateSubscription={() => {
              surface.close();
              onCreateSubscription();
            }}
            onSaveView={() => {
              surface.close();
              onSaveView();
            }}
            onCreateGroup={() => {
              surface.close();
              onCreateGroup();
            }}
            onCreateActiveKnowledgeCollection={() => {
              surface.close();
              onCreateActiveKnowledgeCollection();
            }}
          />
        )}
        {/* No component to mount here directly - see the doc comment above.
            This container is the portal target GraphCanvas's AnnotationToolbox
            renders into while it exists (i.e. while this surface is open). */}
        {surface.isOpen('annotate') && (
          <div
            ref={onAnnotationSheetContainerChange}
            className="mobile-shell-annotate-sheet-container"
            data-testid="mobile-annotate-sheet-container"
          />
        )}
        {/* The Edit surface's own portal target
            (task-annotation-responsive-bottom-toolbox) — same "no component
            to mount here directly" reasoning as Annotate above: the property
            editor lives inside whichever annotation node currently has it
            open (NoteNode/LabelNode/ArrowNode/GenericAnnotationNode/
            FreehandAnnotationNode, deep inside GraphCanvas), so this is only
            ever a portal target, never a host of its own content. */}
        {surface.isOpen('detail') && (
          <div
            ref={onAnnotationEditSheetContainerChange}
            className="mobile-shell-annotation-edit-sheet-container"
            data-testid="mobile-annotation-edit-sheet-container"
          />
        )}
      </BottomSheet>

      <SessionDrawer
        open={surface.isOpen('menu')}
        onClose={surface.close}
        sessions={sessions}
        currentSessionId={currentSessionId}
        onNewSession={onNewSession}
        onConnectSession={onConnectSession}
        onSelectSession={onSelectSession}
        onRenameSession={onRenameSession}
        onDeleteSession={onDeleteSession}
        onCopySessionLink={onCopySessionLink}
        onCopyTriggerUrl={onCopyTriggerUrl}
        onOpenSettings={onOpenSettings}
        onOpenActivity={() => {
          surface.close();
          onOpenActivity?.();
        }}
        canvasLocked={canvasLocked}
        onToggleLock={onToggleLock}
        onEnterFullscreen={onEnterFullscreen}
        suspendEscape={suspendEscape}
      />

      {llmAvailable && (
        <BottomSheet
          isOpen={chatPanelOpen}
          snapPoint={chatSnapPoint}
          onSnapPointChange={setChatSnapPoint}
          onClose={() => setChatPanelOpen(false)}
          title={t('mobile_nav.chat_panel_title')}
        >
          <ChatPanel variant="sheet" collectionShortName={akcShortName || undefined} />
        </BottomSheet>
      )}
    </>
  );
}

export default MobileShell;
