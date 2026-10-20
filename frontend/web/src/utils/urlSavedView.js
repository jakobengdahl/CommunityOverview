import { captureCanvasScope } from './sessionScopedGraphEdits';

/**
 * Load the saved view named by a `?view=` URL parameter.
 *
 * When the URL also carried `?session=`, the view wins: it waits for the
 * session bootstrap load to settle (success or failure) and is applied on top,
 * so the outcome no longer depends on which reply lands last. The session scope
 * is captured only after that wait — the bootstrap load legitimately replaces
 * the canvas — and a view fetched for a session the user has since left is
 * dropped rather than written into the new one.
 *
 * @param {Object} params
 * @param {string} params.name  Saved view name from the URL.
 * @param {Promise<unknown>|null} params.sessionBootstrap  The `?session=` load, if any.
 * @param {Function} params.getSavedView  API call: fetch one saved view.
 * @param {Function} params.applyView  Applies a fetched view to the canvas.
 * @returns {Promise<boolean>} Whether the view was applied.
 */
export async function loadUrlSavedView({ name, sessionBootstrap, getSavedView, applyView }) {
  // A failed bootstrap load is reported by its own caller; here it only means
  // "settled".
  await Promise.resolve(sessionBootstrap).catch(() => {});
  const scope = captureCanvasScope();
  const result = await getSavedView(name);
  if (scope.sessionChanged()) return false;
  if (!result?.success || !result.nodes?.length) return false;
  applyView(result);
  return true;
}
