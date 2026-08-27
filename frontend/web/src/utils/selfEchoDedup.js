// Resolves the race between two independent deliveries of the exact same
// image-ingest annotation to this browser: the direct optimistic apply
// (handleImageIngest applies the REST response to the canvas immediately)
// and this browser's own confirming SSE echo of the same op — broadcast
// under a marker distinct from this browser's own client id specifically so
// it is not dropped as a self-authored echo (see backend/service/rest_api.py's
// ingest_session_image). Either one can reach the canvas-apply code first:
// the server may broadcast the op before it finishes serializing the REST
// response, or the reverse. Whichever arrives first must render it;
// whichever arrives second must be a no-op, or the second one would either
// duplicate the annotation or clobber a move/resize made in between.
//
// `claim(id)` is called from the one shared place both deliveries actually
// apply an annotation (App.jsx's applyRemoteOp, for its
// annotation_created/annotation_updated case) — not from two separate call
// sites — so this module never needs to know which caller is "the echo" and
// which is "the optimistic apply"; it only needs to know it has seen `id`
// exactly once before.
//
// Ordinary annotations — anything this browser did not itself just image-
// ingest — are never marked pending at all, so `claim` always returns true
// for them: every other annotation kind, and every OTHER collaborator's own
// image upload, is completely unaffected by this module.
//
// This assumes a genuine third-party edit to the same id can never be
// delivered *between* this browser's own optimistic apply and its own echo
// of the same op — true today because the backend serializes writes per
// session (a single asyncio.Lock) and fans them out over one FIFO queue per
// subscriber (session_hub.py), so a collaborator's write can only be queued
// after this one's echo, never interleaved with it. A transport that dropped
// that per-subscriber ordering guarantee would need this module to key on
// something more than the annotation id alone.
export function createSelfEchoDedup() {
  const pending = new Set(); // ids marked before their POST, not yet claimed by either side
  const resolved = new Set(); // ids whose race the first arrival already won

  return {
    // Call before sending the ingest request, with the client-generated
    // annotation id — before either delivery can possibly reach `claim`.
    markPending(id) {
      if (typeof id === 'string' && id) pending.add(id);
    },

    // Returns true if THIS call should actually render the annotation.
    // - Not a raced id at all (never markPending'd, or already fully
    //   resolved by an earlier pair) → always true.
    // - First arrival for a raced id → consumes the pending mark, records
    //   that the race is now resolved, and returns true (render it).
    // - Second arrival for the same raced id → consumes the resolved mark
    //   and returns false (already rendered by the other delivery).
    claim(id) {
      if (typeof id !== 'string' || !id) return true;
      if (pending.delete(id)) {
        resolved.add(id);
        return true;
      }
      if (resolved.delete(id)) return false;
      return true;
    },

    // The request this id was reserved for never resulted in a server-side
    // write (the POST failed, or the caller bailed out before even sending
    // it — see handleImageIngest's `delivered` guard). Neither delivery is
    // ever coming for `id`, so nothing should be left waiting for it.
    forget(id) {
      if (typeof id !== 'string' || !id) return;
      pending.delete(id);
      resolved.delete(id);
    },

    // A pending or half-resolved race whose other half never arrives as a
    // discrete op — the SSE stream reconnects and resyncs wholesale instead
    // (App.jsx's resyncFromServer) — would otherwise linger forever, and
    // wrongly veto a later, unrelated update for the same id. A full resync
    // already re-hydrates every annotation from server truth, making any
    // pending state moot; call this there so none outlives its purpose.
    clear() {
      pending.clear();
      resolved.clear();
    },
  };
}
