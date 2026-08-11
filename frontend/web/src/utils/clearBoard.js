/**
 * Decide what should happen when the user asks to clear the visualization,
 * given how protected the board is and where the request came from.
 *
 * Protection tiers (see the "Confirm before clearing a named or locked
 * visualization" task):
 *   - locked    → the board is guarded via the navigation-menu lock. A
 *                 keyboard esc-esc is ignored entirely; the clear button asks
 *                 for an emphatic confirmation.
 *   - named     → the session has a user-given name worth protecting; both
 *                 esc-esc and the button ask for a plain confirmation.
 *   - otherwise → an unnamed, unlocked scratch board clears immediately.
 *
 * @param {Object} params
 * @param {boolean} params.locked  Navigation-menu lock enabled for the board.
 * @param {boolean} params.named   Whether the current session has a name.
 * @param {'keyboard'|'button'} params.source  How the clear was triggered.
 * @returns {'noop'|'confirm-locked'|'confirm'|'clear'}
 */
export function decideClearAction({ locked, named, source }) {
  if (locked) {
    return source === 'keyboard' ? 'noop' : 'confirm-locked';
  }
  if (named) {
    return 'confirm';
  }
  return 'clear';
}
