import { List, Feather, XCircle } from 'react-bootstrap-icons';
import { useI18n } from '../i18n';
import './FloatingHeader.css';

const MAX_VISIBLE_DOTS = 5;

function initialFor(name) {
  const trimmed = (name || '').trim();
  return trimmed ? trimmed[0].toUpperCase() : '?';
}

/**
 * Presence roster: one coloured dot per connected collaborator (design 3.5).
 * Only shown once at least one other user is present so a solo session stays
 * uncluttered.
 */
function PresenceRoster({ roster, currentClientId, t }) {
  const members = Array.isArray(roster) ? roster : [];
  const others = members.filter(m => m.client_id !== currentClientId);
  if (others.length === 0) return null;

  // Show self first, then others; cap the visible dots and summarise the rest.
  const ordered = [
    ...members.filter(m => m.client_id === currentClientId),
    ...others,
  ];
  const visible = ordered.slice(0, MAX_VISIBLE_DOTS);
  const overflow = ordered.length - visible.length;

  return (
    <div
      className="floating-header-presence"
      aria-label={t('presence.collaborators')}
    >
      {visible.map(m => {
        const isSelf = m.client_id === currentClientId;
        const label = isSelf
          ? `${m.display_name} (${t('presence.you')})`
          : m.display_name;
        return (
          <span
            key={m.client_id}
            className={`floating-header-presence-dot${isSelf ? ' is-self' : ''}`}
            style={{ backgroundColor: m.color }}
            title={label}
          >
            {initialFor(m.display_name)}
          </span>
        );
      })}
      {overflow > 0 && (
        <span className="floating-header-presence-more" title={t('presence.collaborators')}>
          +{overflow}
        </span>
      )}
    </div>
  );
}

function FloatingHeader({ title, sessionId, roster, currentClientId, onClear, onToggleDrawer }) {
  const { t } = useI18n();
  const heading = title || t('header.title');
  return (
    <div className="floating-header" id="guide-target-header">
      <div className="floating-header-bar">
        <Feather size={18} className="floating-header-app-icon" />
        <button
          className="floating-header-hamburger"
          onClick={() => onToggleDrawer?.()}
          title={t('header.menu')}
        >
          <List size={20} />
        </button>
        <span className="floating-header-title">{heading}</span>
        {sessionId && (
          <span className="floating-header-session-id" title={t('header.session_id_tooltip')}>
            {sessionId}
          </span>
        )}
        <PresenceRoster roster={roster} currentClientId={currentClientId} t={t} />
        <button
          className="floating-header-clear"
          onClick={() => onClear?.()}
          title={t('header.clear_canvas_tooltip')}
          aria-label={t('header.clear_canvas_aria')}
        >
          <XCircle size={15} />
        </button>
      </div>
    </div>
  );
}

export default FloatingHeader;
