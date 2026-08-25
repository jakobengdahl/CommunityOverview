import { useContext } from 'react';
import { useReactFlow } from 'reactflow';
import { AnnotationContext } from './AnnotationContext';
import { isRemoteLocked } from '../utils/annotations';
import { LAYER_BACK, LAYER_FRONT, resolveLayerZ } from '../utils/annotationLayers';

/**
 * The layer row shared by every annotation context menu that has one
 * (note, label, line, freehand and the generic kinds). One implementation
 * rather than five copies: the arithmetic lives in utils/annotationLayers.js,
 * the refusal rules live in `useAnnotationLayer` below, and each calling
 * component only renders the row — so the control behaves identically
 * whichever annotation it is opened on.
 */
export default function AnnotationLayerControls({ labels, onChangeLayer }) {
  return (
    <>
      <div className="context-menu-title">{labels.layer}</div>
      <div className="context-menu-layer">
        <button
          type="button"
          className="layer-button"
          aria-label={labels.layerBack}
          title={labels.layerBack}
          onClick={() => onChangeLayer(LAYER_BACK)}
        >
          ⤓
        </button>
        <button
          type="button"
          className="layer-button"
          aria-label={labels.layerFront}
          title={labels.layerFront}
          onClick={() => onChangeLayer(LAYER_FRONT)}
        >
          ⤒
        </button>
      </div>
    </>
  );
}

/**
 * The layer-change handler behind the row above.
 *
 * Refuses on a persisted `locked` flag as well as on another client's live
 * claim. The four menus that have a locked branch already withhold the whole
 * row from a locked annotation, but ArrowNode has no such branch and opens
 * its menu whatever `locked` says — so the check lives here, where it covers
 * all five identically, rather than in each caller.
 *
 * Stays silent when the step is a no-op: an annotation already at the front
 * has nothing to publish.
 *
 * A layer change publishes as 'style': an envelope-field edit at a release
 * point, not a continuous gesture, so it wants the immediate publish path
 * rather than the debounced text one.
 */
export function useAnnotationLayer(id, data) {
  const { getNodes, setNodes } = useReactFlow();
  const { notifyChange, notifyRemoteLockedAttempt } = useContext(AnnotationContext);
  return (direction) => {
    if (isRemoteLocked(data)) {
      notifyRemoteLockedAttempt();
      return;
    }
    if (data?.locked) return;
    // Resolved against getNodes() rather than inside the setNodes updater:
    // an updater must stay pure (React may re-run it), so it is not a place
    // to decide whether to notify the host of a change.
    const z = resolveLayerZ(getNodes(), id, direction);
    if (z === null) return;
    setNodes((nds) => nds.map((n) => (n.id === id ? { ...n, zIndex: z } : n)));
    notifyChange('style');
  };
}
