import { useContext } from 'react';
import { useReactFlow } from 'reactflow';
import { AnnotationContext } from './AnnotationContext';
import { isRemoteLocked } from '../utils/annotations';

// Same four steps `FreehandAnnotationNode.jsx`'s own, pre-existing
// `FREEHAND_OPACITY_LEVELS` picked (task-annotation-responsive-bottom-toolbox:
// opacity is now offered on every other kind too, via this shared control —
// freehand keeps its own separate implementation rather than being migrated
// onto this one in the same change, to avoid touching that kind's already-
// shipped, already-tested right-click editor as a side effect of adding the
// control everywhere else; the two are intentionally kept in sync by value).
export const OPACITY_LEVELS = [0.3, 0.5, 0.75, 1];

/**
 * The opacity row shared by every annotation context menu that has one
 * (note, label, line and the generic kinds — `text`/`shape`/`icon`/
 * `vote_dot`/`image`) — one implementation rather than five copies, following
 * `AnnotationLayerControls`/`AnnotationDuplicateControl`'s precedent. Renders
 * only in the unlocked branch of a caller's menu: locking withholds every
 * property edit except unlock/duplicate (the same reason the layer row hides
 * itself when locked), and `useAnnotationOpacity` below refuses a locked
 * write regardless, so a caller that forgets its own `locked` branch still
 * gets the right behaviour rather than a visibly present, silently inert
 * control.
 */
export default function AnnotationOpacityControl({ labels, opacity, onChangeOpacity }) {
  const current = Number.isFinite(opacity) ? opacity : 1;
  return (
    <>
      <div className="context-menu-title">{labels.opacity}</div>
      <div className="context-menu-sizes">
        {OPACITY_LEVELS.map((o) => (
          <button
            key={o}
            type="button"
            className={`size-button${current === o ? ' active' : ''}`}
            onClick={() => onChangeOpacity(o)}
          >
            {Math.round(o * 100)}%
          </button>
        ))}
      </div>
    </>
  );
}

/**
 * The opacity-change handler behind the row above. Mirrors
 * `useAnnotationLayer`'s shape: refuses on a remote edit lease or a
 * persisted `locked` flag, otherwise patches `data.opacity` and publishes a
 * 'style' change — an instant, single-shot property edit, the same
 * classification every other swatch/picker in these menus already uses.
 */
export function useAnnotationOpacity(id, data) {
  const { setNodes } = useReactFlow();
  const { notifyChange, notifyRemoteLockedAttempt } = useContext(AnnotationContext);
  return (next) => {
    if (isRemoteLocked(data)) {
      notifyRemoteLockedAttempt();
      return;
    }
    if (data?.locked) return;
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, opacity: next } } : n))
    );
    notifyChange('style');
  };
}
