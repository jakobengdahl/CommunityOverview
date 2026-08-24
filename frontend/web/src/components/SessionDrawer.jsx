import { useState, useEffect, useMemo, useRef } from 'react';
import {
  X,
  Feather,
  PlusCircle,
  Search,
  BoxArrowInRight,
  GearFill,
  PencilSquare,
  Link45deg,
  BroadcastPin,
  Trash,
  ClockHistory,
  LockFill,
  UnlockFill,
  ArrowsFullscreen,
} from 'react-bootstrap-icons';
import { useI18n } from '../i18n';
import { useViewportMode } from '../hooks/useViewportMode';
import SessionContextMenu from './SessionContextMenu';
import './SessionDrawer.css';

/**
 * SessionDrawer — full-height panel opened from the hamburger button (desktop)
 * or the Menu slot (mobile). Hosts session navigation (new / search / connect
 * / recent sessions) and the entry point to the Settings dialog.
 *
 * On a mobile-width viewport it becomes a full-width overlay sheet with a
 * scrim behind it, closable by tapping the scrim, instead of the docked
 * 280px pane desktop uses — see SessionDrawer.css for the `--mobile` variant.
 */
function SessionDrawer({
  open,
  onClose,
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
  canvasLocked = false,
  onToggleLock,
  onEnterFullscreen,
  suspendEscape = false,
}) {
  const { t } = useI18n();
  const { isMobile } = useViewportMode();
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [openMenuId, setOpenMenuId] = useState(null);
  const [wasOpen, setWasOpen] = useState(open);
  const searchInputRef = useRef(null);

  // Only one per-session menu is open at a time; drop it when the drawer closes
  // so it doesn't reappear on the next open. Reset during render (React's
  // recommended pattern for deriving from a prop change) rather than in an effect.
  if (open !== wasOpen) {
    setWasOpen(open);
    if (!open) setOpenMenuId(null);
  }

  // Escape closes the drawer — except while a dialog is stacked on top of it
  // (settings, connect, rename), where Escape belongs to that dialog. An open
  // per-session menu is peeled off first, keeping the drawer in place.
  useEffect(() => {
    if (!open || suspendEscape) return;
    const handleKeyDown = (e) => {
      if (e.key === 'Escape') {
        // Stop the event here so GraphCanvas's own Escape handler doesn't
        // also clear the canvas selection when the drawer closes.
        e.stopPropagation();
        if (openMenuId) {
          setOpenMenuId(null);
          return;
        }
        onClose();
      }
    };
    document.addEventListener('keydown', handleKeyDown, true);
    return () => document.removeEventListener('keydown', handleKeyDown, true);
  }, [open, suspendEscape, openMenuId, onClose]);

  useEffect(() => {
    if (searchOpen) searchInputRef.current?.focus();
  }, [searchOpen]);

  const filteredSessions = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return sessions;
    return sessions.filter(
      (s) => (s.name || '').toLowerCase().includes(q) || s.id.toLowerCase().includes(q)
    );
  }, [sessions, query]);

  return (
    <>
      {isMobile && open && (
        <div
          className="session-drawer-scrim"
          onClick={onClose}
          data-testid="session-drawer-scrim"
          aria-hidden="true"
        />
      )}
      <div
        className={`session-drawer${open ? ' open' : ''}${isMobile ? ' session-drawer--mobile' : ''}`}
        aria-hidden={!open}
      >
        <div className="session-drawer-header">
          <Feather size={18} className="session-drawer-app-icon" />
          <span className="session-drawer-title">{t('sessions.title')}</span>
          <button
            className="session-drawer-close"
            onClick={onClose}
            title={t('sessions.close')}
            aria-label={t('sessions.close')}
          >
            <X size={20} />
          </button>
        </div>

        <div className="session-drawer-actions">
          <button className="session-drawer-item" onClick={onNewSession}>
            <PlusCircle size={15} />
            <span>{t('sessions.new_session')}</span>
          </button>
          <button
            className={`session-drawer-item${searchOpen ? ' active' : ''}`}
            onClick={() => {
              setSearchOpen(!searchOpen);
              if (searchOpen) setQuery('');
            }}
          >
            <Search size={15} />
            <span>{t('sessions.search_sessions')}</span>
          </button>
          {searchOpen && (
            <input
              ref={searchInputRef}
              type="text"
              className="session-drawer-search-input"
              placeholder={t('sessions.search_placeholder')}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          )}
          <button className="session-drawer-item" onClick={onConnectSession}>
            <BoxArrowInRight size={15} />
            <span>{t('sessions.connect_session')}</span>
          </button>
        </div>

        <div className="session-drawer-section-title">{t('sessions.recent_sessions')}</div>

        <div className="session-drawer-list">
          {filteredSessions.length === 0 && (
            <div className="session-drawer-empty">
              {query ? t('sessions.no_matches') : t('sessions.no_sessions')}
            </div>
          )}
          {filteredSessions.map((session) => (
            <div
              key={session.id}
              className={`session-drawer-session${session.id === currentSessionId ? ' current' : ''}`}
            >
              <button
                className="session-drawer-session-name"
                onClick={() => onSelectSession(session.id)}
                title={session.name ? `${session.name} (${session.id})` : session.id}
              >
                <span className="session-drawer-session-label">{session.name || session.id}</span>
                {session.name && <span className="session-drawer-session-id">{session.id}</span>}
              </button>
              <SessionContextMenu
                open={openMenuId === session.id}
                onToggle={() => setOpenMenuId((cur) => (cur === session.id ? null : session.id))}
                onClose={() => setOpenMenuId(null)}
                triggerLabel={t('sessions.session_menu')}
                items={[
                  {
                    key: 'rename',
                    label: t('sessions.rename_session'),
                    icon: <PencilSquare size={14} />,
                    onClick: () => onRenameSession(session.id),
                  },
                  {
                    key: 'copy-link',
                    label: t('sessions.copy_link'),
                    icon: <Link45deg size={16} />,
                    onClick: () => onCopySessionLink?.(session.id),
                  },
                  // Only the session this browser is live on can receive pulses,
                  // so the trigger URL is offered on the current session only.
                  ...(session.id === currentSessionId && onCopyTriggerUrl
                    ? [
                        {
                          key: 'copy-trigger-url',
                          label: t('sessions.copy_trigger_url'),
                          icon: <BroadcastPin size={15} />,
                          onClick: () => onCopyTriggerUrl(session.id),
                        },
                      ]
                    : []),
                  {
                    key: 'delete',
                    label: t('sessions.delete_session'),
                    icon: <Trash size={14} />,
                    danger: true,
                    onClick: () => onDeleteSession?.(session.id),
                  },
                ]}
              />
            </div>
          ))}
        </div>

        <div className="session-drawer-footer">
          <button className="session-drawer-item" onClick={() => onEnterFullscreen?.()}>
            <ArrowsFullscreen size={15} />
            <span>{t('fullscreen.enter')}</span>
          </button>
          <button
            className={`session-drawer-item${canvasLocked ? ' active' : ''}`}
            onClick={() => onToggleLock?.()}
            aria-pressed={canvasLocked}
          >
            {canvasLocked ? <LockFill size={15} /> : <UnlockFill size={15} />}
            <span>{canvasLocked ? t('sessions.unlock_canvas') : t('sessions.lock_canvas')}</span>
          </button>
          {onOpenActivity && (
            <button className="session-drawer-item" onClick={onOpenActivity}>
              <ClockHistory size={15} />
              <span>{t('history.open')}</span>
            </button>
          )}
          <button className="session-drawer-item" onClick={onOpenSettings}>
            <GearFill size={15} />
            <span>{t('sessions.settings')}</span>
          </button>
        </div>
      </div>
    </>
  );
}

export default SessionDrawer;
