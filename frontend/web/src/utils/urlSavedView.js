import { captureCanvasScope } from './sessionScopedGraphEdits';
import useGraphStore from '../store/graphStore';

// Content replaced by applying a view. Selection and other transient UI state
// do not count as edits; node moves and canvas-local annotations use the epoch.
const VIEW_CONTENT_FIELDS = [
  'nodes',
  'edges',
  'hiddenNodeIds',
  'hiddenEdgeIds',
  'dimmedNodeIds',
  'dimmedEdgeIds',
  'edgeIntensity',
];

/**
 * Load the saved view named by a `?view=` URL parameter.
 *
 * When the URL also carried `?session=`, the view wins: it waits for the
 * session bootstrap load to settle (success or failure) and is applied on top,
 * so the outcome no longer depends on which reply lands last. The session scope
 * is captured only after that wait — the bootstrap load legitimately replaces
 * the canvas — and a view for a session the user has since left, whether they
 * left during the bootstrap or during the fetch, is dropped rather than written
 * into the new one.
 *
 * @param {Object} params
 * @param {string} params.name  Saved view name from the URL.
 * @param {Promise<unknown>|null} params.sessionBootstrap  The `?session=` load, if any.
 * @param {string} params.sessionId  The session the URL was opened into.
 * @param {Function} params.getCurrentSessionId  Reads the active session id.
 * @param {Function} params.getSavedView  API call: fetch one saved view.
 * @param {Function} [params.getCanvasEditEpoch]  Tracks canvas-local edits.
 * @param {Function} params.applyView  Applies a fetched view to the canvas.
 * @returns {Promise<boolean>} Whether the view was applied.
 */
export async function loadUrlSavedView({
  name,
  sessionBootstrap,
  sessionId,
  getCurrentSessionId,
  getSavedView,
  getCanvasEditEpoch = () => 0,
  applyView,
}) {
  // A failed bootstrap load only means "settled" here; its error is logged by
  // the load itself.
  await Promise.resolve(sessionBootstrap).catch(() => {});
  // The user may have moved to another session while the bootstrap was pending;
  // the epoch captured below would not show that, as it is taken after the move.
  if (getCurrentSessionId() !== sessionId) return false;
  const scope = captureCanvasScope();
  const canvas = useGraphStore.getState();
  const editEpoch = getCanvasEditEpoch();
  const result = await getSavedView(name);
  if (scope.canvasReplaced() || getCurrentSessionId() !== sessionId) return false;
  if (getCanvasEditEpoch() !== editEpoch) return false;
  const currentCanvas = useGraphStore.getState();
  if (VIEW_CONTENT_FIELDS.some((key) => currentCanvas[key] !== canvas[key])) return false;
  // Clearing a restore queue only acknowledges content the bootstrap already
  // supplied; queuing new content during the fetch must still invalidate it.
  if (
    ['pendingGroups', 'pendingAnnotations'].some(
      (key) => currentCanvas[key] != null && currentCanvas[key] !== canvas[key]
    )
  )
    return false;
  if (!result?.success || !result.nodes?.length) return false;
  applyView(result);
  return true;
}
