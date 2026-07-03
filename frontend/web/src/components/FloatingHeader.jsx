import { List, Feather, XCircle } from 'react-bootstrap-icons';
import './FloatingHeader.css';

function FloatingHeader({ title = 'Community Graph View', sessionId, onClear, onToggleDrawer }) {
  return (
    <div className="floating-header" id="guide-target-header">
      <div className="floating-header-bar">
        <Feather size={18} className="floating-header-app-icon" />
        <button
          className="floating-header-hamburger"
          onClick={() => onToggleDrawer?.()}
          title="Menu"
        >
          <List size={20} />
        </button>
        <span className="floating-header-title">{title}</span>
        {sessionId && (
          <span className="floating-header-session-id" title="Session ID — share with an external AI to connect it to this window">
            {sessionId}
          </span>
        )}
        <button
          className="floating-header-clear"
          onClick={() => onClear?.()}
          title="Clear canvas — remove all nodes and edges (or press Esc twice)"
          aria-label="Clear canvas"
        >
          <XCircle size={15} />
        </button>
      </div>
    </div>
  );
}

export default FloatingHeader;
