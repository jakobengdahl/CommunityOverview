/**
 * Drop the browser out of the session it is on and into a fresh, empty one.
 *
 * Both delete paths end here: the user deleting the session they are working in,
 * and a collaborator's delete arriving over the realtime stream. Landing in a
 * new session is a session switch like any other, so the session-scoped UI
 * (assistant history, active experts, node-detail/edit overlays, selection) must
 * be reset alongside the canvas — clearing the canvas alone leaves the deleted
 * session's conversation and its open node dialogs sitting in a session where
 * those nodes do not exist. Keeping the sequence in one place is what stops the
 * two call sites from drifting apart again.
 *
 * The reset runs before the new id is adopted so nothing from the old session is
 * ever observable under the new one.
 *
 * @param {Object} params
 * @param {string} params.freshId  The newly generated session id to switch into.
 * @param {Function} params.clearVisualization  Store action: empty the canvas.
 * @param {Function} params.resetSessionScopedState  Store action: reset session-scoped UI.
 * @param {Function} params.setSessionId  Adopt the new session id.
 * @param {Function} params.reflectSessionUrl  Mirror the new id into the URL.
 */
export function dropIntoFreshSession({
  freshId,
  clearVisualization,
  resetSessionScopedState,
  setSessionId,
  reflectSessionUrl,
}) {
  clearVisualization();
  resetSessionScopedState();
  setSessionId(freshId);
  reflectSessionUrl(freshId);
}
