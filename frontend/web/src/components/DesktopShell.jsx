import FloatingHeader from './FloatingHeader';
import FloatingSearch from './FloatingSearch';
import FloatingToolbar from './FloatingToolbar';
import ChatPanel from './ChatPanel';
import SessionDrawer from './SessionDrawer';

/**
 * DesktopShell — the desktop chrome: FloatingHeader, SessionDrawer,
 * FloatingSearch, FloatingToolbar and ChatPanel as floating overlays around
 * the canvas, exactly as App.jsx rendered them before the shell split. App.jsx
 * still owns all state and handlers; this component only owns which chrome is
 * on screen for a wide/mouse viewport (see MobileShell for the touch/narrow
 * counterpart, chosen by App.jsx via useViewportMode().isMobile).
 */
function DesktopShell({
  sessionId,
  roster,
  currentClientId,
  onClear,
  drawerOpen,
  onToggleDrawer,
  onCloseDrawer,
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
  return (
    <>
      <FloatingHeader
        sessionId={sessionId}
        roster={roster}
        currentClientId={currentClientId}
        onClear={onClear}
        onToggleDrawer={onToggleDrawer}
      />
      <SessionDrawer
        open={drawerOpen}
        onClose={onCloseDrawer}
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
        onOpenActivity={onOpenActivity}
        canvasLocked={canvasLocked}
        onToggleLock={onToggleLock}
        suspendEscape={suspendEscape}
      />
      <FloatingSearch />
      <FloatingToolbar
        onCreateNode={onCreateNodeForType}
        onCreateAgent={onCreateAgent}
        onCreateSubscription={onCreateSubscription}
        onSaveView={onSaveView}
        onCreateGroup={onCreateGroup}
        onCreateActiveKnowledgeCollection={onCreateActiveKnowledgeCollection}
      />
      {llmAvailable && <ChatPanel collectionShortName={akcShortName || undefined} />}
    </>
  );
}

export default DesktopShell;
