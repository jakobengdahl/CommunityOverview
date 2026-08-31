/**
 * Publish-timing scheduler for annotation changes (task-annotation-shared-
 * session-realtime's "accepted operation timing"), split out from App.jsx's
 * generic 1500ms autosave debounce (which stays as-is for graph-node
 * positions and the local session-restore bookkeeping that debounce also
 * serves). create/delete/style/geometry publish immediately — geometry
 * already notifies only at a release point (drag-stop, resize-end, a
 * rotation click), never continuously — while text edits coalesce on a
 * short debounce so a burst of keystrokes doesn't spam one publish per
 * character. A kind this scheduler does not recognise (including no kind at
 * all, from a call site nobody has classified) publishes immediately, the
 * safe default.
 *
 * Framework-agnostic (no React, no store import) so the timing decision is
 * unit-testable in isolation against fake timers, the same reason
 * `sessionSyncClient.js` keeps its op-batching logic framework-agnostic.
 * App.jsx wires `publish` to its `requestSessionSnapshot` call and keeps the
 * returned scheduler alive for a session's lifetime via a ref.
 */

export const DEFAULT_ANNOTATION_TEXT_DEBOUNCE_MS = 300;

/**
 * @param {Object} opts
 * @param {Function} opts.publish - Called (with no arguments) to actually
 *   publish the current annotation state.
 * @param {number} [opts.debounceMs] - Debounce window for 'text' changes.
 * @param {Function} [opts.setTimeoutFn]
 * @param {Function} [opts.clearTimeoutFn]
 * @returns {{schedule: (kind?: string) => void, clearPending: () => void}}
 */
export function createAnnotationChangeScheduler({
  publish,
  debounceMs = DEFAULT_ANNOTATION_TEXT_DEBOUNCE_MS,
  setTimeoutFn = setTimeout,
  clearTimeoutFn = clearTimeout,
} = {}) {
  let timer = null;

  function clearPending() {
    if (timer != null) {
      clearTimeoutFn(timer);
      timer = null;
    }
  }

  /**
   * Schedule a publish for the given operation kind. A later call before a
   * pending debounced publish fires replaces it (only the latest state is
   * ever published), matching a burst of keystrokes coalescing into one op.
   * @param {string} [kind] - 'create' | 'delete' | 'style' | 'text' | 'geometry'.
   */
  function schedule(kind) {
    clearPending();
    if (kind === 'text') {
      timer = setTimeoutFn(() => {
        timer = null;
        publish();
      }, debounceMs);
      return;
    }
    publish();
  }

  return { schedule, clearPending };
}
