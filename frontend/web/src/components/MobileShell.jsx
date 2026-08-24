import { useEffect, useState } from 'react';
import {
  Diagram3Fill,
  Search,
  PlusCircleFill,
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
 * shared with DesktopShell), and a five-slot bottom navigation (Graph / Search
 * / Create / Chat / Menu). App.jsx still owns all state and handlers; this
 * component owns only the mobile presentation and the mutual exclusion of its
 * surfaces, via useSurfaceManager (frontend/web/src/hooks/useSurfaceManager.js).
 *
 * Panel content is reused, never forked:
 *  - Search and Create mount FloatingSearch / FloatingToolbar (variant="sheet")
 *    inside the shared BottomSheet primitive.
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
 *    the search/create sheet below) because it is driven by the shared
 *    store's chatPanelOpen field rather than useSurfaceManager — ChatPanel's
 *    own controls (its header, its minimized bar on desktop) can flip that
 *    field directly, bypassing whatever this component calls to open it, so
 *    mutual exclusion with search/create/menu is enforced reactively below
 *    rather than only at this component's own call sites.
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
}) {
  const { t } = useI18n();
  const { chatPanelOpen, setChatPanelOpen, setChatPanelOpenTransient } = useGraphStore();
  const surface = useSurfaceManager();
  const [sheetSnapPoint, setSheetSnapPoint] = useState('half');
  const [chatSnapPoint, setChatSnapPoint] = useState('half');

  const sheetOpen = surface.isOpen('search') || surface.isOpen('create');

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
            : t('mobile_nav.create_panel_title')
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
