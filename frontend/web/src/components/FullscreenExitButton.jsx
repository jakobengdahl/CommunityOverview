import { useEffect, useRef } from 'react';
import { FullscreenExit } from 'react-bootstrap-icons';

function FullscreenExitButton({ onExit, label }) {
  const buttonRef = useRef(null);
  useEffect(() => buttonRef.current?.focus(), []);

  return (
    <button
      ref={buttonRef}
      type="button"
      className="fullscreen-canvas-exit"
      onClick={onExit}
      aria-label={label}
      title={label}
    >
      <FullscreenExit size={18} aria-hidden="true" />
    </button>
  );
}

export default FullscreenExitButton;
