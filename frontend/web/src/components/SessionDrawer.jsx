import { useState, useEffect, useMemo, useRef } from 'react';
import {
  X,
  Feather,
  PlusCircle,
  Search,
  BoxArrowInRight,
  GearFill,
  PencilSquare,
  Trash,
} from 'react-bootstrap-icons';
import { useI18n } from '../i18n';
import './SessionDrawer.css';

/**
 * SessionDrawer — full-height panel docked to the left screen edge, opened
 * from the hamburger button. Works like a drop-down menu but sliding in from
 * the left. Hosts session navigation (new / search / connect / recent
 * sessions) and the entry point to the Settings dialog.
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
  onOpenSettings,
  suspendEscape = false,
}) {
  const { t } = useI18n();
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState('');
  const searchInputRef = useRef(null);

  // Escape closes the drawer — except while a dialog is stacked on top of it
  // (settings, connect, rename), where Escape belongs to that dialog.
  useEffect(() => {
    if (!open || suspendEscape) return;
    const handleKeyDown = (e) => {
      if (e.key === 'Escape') {
        // Stop the event here so GraphCanvas's own Escape handler doesn't
        // also clear the canvas selection when the drawer closes.
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener('keydown', handleKeyDown, true);
    return () => document.removeEventListener('keydown', handleKeyDown, true);
  }, [open, suspendEscape, onClose]);

  useEffect(() => {
    if (searchOpen) searchInputRef.current?.focus();
  }, [searchOpen]);

  const filteredSessions = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return sessions;
    return sessions.filter(s =>
      (s.name || '').toLowerCase().includes(q) || s.id.toLowerCase().includes(q)
    );
  }, [sessions, query]);

  return (
    <div className={`session-drawer${open ? ' open' : ''}`} aria-hidden={!open}>
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
              <span className="session-drawer-session-label">
                {session.name || session.id}
              </span>
              {session.name && (
                <span className="session-drawer-session-id">{session.id}</span>
              )}
            </button>
            <button
              className="session-drawer-session-rename"
              onClick={() => onRenameSession(session.id)}
              title={t('sessions.rename_session')}
              aria-label={t('sessions.rename_session')}
            >
              <PencilSquare size={13} />
            </button>
            <button
              className="session-drawer-session-delete"
              onClick={() => onDeleteSession?.(session.id)}
              title={t('sessions.delete_session')}
              aria-label={t('sessions.delete_session')}
            >
              <Trash size={13} />
            </button>
          </div>
        ))}
      </div>

      <div className="session-drawer-footer">
        <button className="session-drawer-item" onClick={onOpenSettings}>
          <GearFill size={15} />
          <span>{t('sessions.settings')}</span>
        </button>
      </div>
    </div>
  );
}

export default SessionDrawer;
