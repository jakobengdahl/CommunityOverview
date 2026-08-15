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

/**
 * React to a `session_deleted` broadcast for the session this client is on: the
 * session is gone for everyone, so this client lands in its own fresh session
 * (design D11). Lives here rather than inline in the sync-handler map so it is
 * covered by the same tests as the local delete it mirrors — the two paths had
 * drifted once, leaving this one resetting the canvas but not the session-scoped
 * UI.
 *
 * A delete we issued ourselves is ignored: the local delete path has already
 * moved this client into a fresh session, and re-running here would strand it in
 * a second one.
 *
 * @param {Object} params
 * @param {string} params.deletedBy  Client id that issued the delete, per the broadcast.
 * @param {string} params.clientId   This browser's client id.
 * @param {string} params.sessionId  The session that was deleted (the active one).
 * @param {Function} params.generateSessionId  Mint the id to land in.
 * @param {Function} params.removeSession  Drop the session from the local recents list.
 * @param {Function} params.clearVisualization  Store action: empty the canvas.
 * @param {Function} params.resetSessionScopedState  Store action: reset session-scoped UI.
 * @param {Function} params.setSessionId  Adopt the new session id.
 * @param {Function} params.reflectSessionUrl  Mirror the new id into the URL.
 * @returns {boolean} Whether this client was moved into a fresh session.
 */
export function receiveRemoteSessionDeleted({
  deletedBy,
  clientId,
  sessionId,
  generateSessionId,
  removeSession,
  clearVisualization,
  resetSessionScopedState,
  setSessionId,
  reflectSessionUrl,
}) {
  if (deletedBy && deletedBy === clientId) return false;
  removeSession(sessionId);
  dropIntoFreshSession({
    freshId: generateSessionId(),
    clearVisualization,
    resetSessionScopedState,
    setSessionId,
    reflectSessionUrl,
  });
  return true;
}
