import { useContext } from 'react';
import { useReactFlow } from 'reactflow';
import { AnnotationContext } from './AnnotationContext';
import { isRemoteLocked } from '../utils/annotations';
import { LAYER_BACKWARD, LAYER_FORWARD, resolveLayerZ } from '../utils/annotationLayers';

/**
 * The forward/back layer row shared by every annotation context menu
 * (note, label, arrow, freehand and the generic kinds). One implementation
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
          aria-label={labels.layerBackward}
          title={labels.layerBackward}
          onClick={() => onChangeLayer(LAYER_BACKWARD)}
        >
          ⤓
        </button>
        <button
          type="button"
          className="layer-button"
          aria-label={labels.layerForward}
          title={labels.layerForward}
          onClick={() => onChangeLayer(LAYER_FORWARD)}
        >
          ⤒
        </button>
      </div>
    </>
  );
}

/**
 * The layer-change handler behind the row above. Refuses while another
 * client holds this annotation's claim (surfacing the attempt, as every
 * other annotation mutation does), and stays silent when the step is a
 * no-op — an annotation already at the front has nothing to report.
 *
 * A layer change publishes as 'style': it is an envelope-field edit at a
 * release point, not a continuous gesture, so it wants the immediate
 * publish path rather than the debounced text one.
 */
export function useAnnotationLayer(id, data) {
  const { getNodes, setNodes } = useReactFlow();
  const { notifyChange, notifyRemoteLockedAttempt } = useContext(AnnotationContext);
  return (direction) => {
    if (isRemoteLocked(data)) {
      notifyRemoteLockedAttempt();
      return;
    }
    // Resolved against getNodes() rather than inside the setNodes updater:
    // an updater must stay pure (React may re-run it), so it is not a place
    // to decide whether to notify the host of a change.
    const z = resolveLayerZ(getNodes(), id, direction);
    if (z === null) return;
    setNodes((nds) => nds.map((n) => (n.id === id ? { ...n, zIndex: z } : n)));
    notifyChange('style');
  };
}

export { LAYER_BACKWARD, LAYER_FORWARD };
