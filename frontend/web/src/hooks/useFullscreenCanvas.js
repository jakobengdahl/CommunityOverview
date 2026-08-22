import { useCallback, useEffect, useState } from 'react';

function isTypingTarget(target) {
  return (
    target instanceof Element &&
    (target.matches('input, textarea, select') || target.isContentEditable)
  );
}

export function useFullscreenCanvas(rootRef) {
  const [nativeFullscreen, setNativeFullscreen] = useState(false);
  const [fallbackFocusMode, setFallbackFocusMode] = useState(false);

  const enter = useCallback(async () => {
    const root = rootRef.current;
    if (!root) return;
    if (typeof root.requestFullscreen === 'function') {
      try {
        await root.requestFullscreen();
        return;
      } catch {
        // Browser policy failures use the equivalent in-app mode.
      }
    }
    setFallbackFocusMode(true);
  }, [rootRef]);

  const exit = useCallback(async () => {
    setFallbackFocusMode(false);
    if (document.fullscreenElement && typeof document.exitFullscreen === 'function') {
      try {
        await document.exitFullscreen();
      } catch {
        // fullscreenchange remains authoritative if the browser refuses exit.
      }
    }
  }, []);

  useEffect(() => {
    const syncNativeState = () =>
      setNativeFullscreen(document.fullscreenElement === rootRef.current);
    const onKeyDown = (event) => {
      if (event.key === 'Escape' && fallbackFocusMode) {
        event.preventDefault();
        setFallbackFocusMode(false);
        return;
      }
      if (
        event.key.toLowerCase() === 'f' &&
        event.shiftKey &&
        (event.ctrlKey || event.metaKey) &&
        !isTypingTarget(event.target)
      ) {
        event.preventDefault();
        if (nativeFullscreen || fallbackFocusMode) exit();
        else enter();
      }
    };
    document.addEventListener('fullscreenchange', syncNativeState);
    document.addEventListener('keydown', onKeyDown);
    syncNativeState();
    return () => {
      document.removeEventListener('fullscreenchange', syncNativeState);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [enter, exit, fallbackFocusMode, nativeFullscreen, rootRef]);

  return {
    enterFullscreenCanvas: enter,
    exitFullscreenCanvas: exit,
    fullscreenCanvasActive: nativeFullscreen || fallbackFocusMode,
  };
}

export default useFullscreenCanvas;
