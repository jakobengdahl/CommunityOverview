// Tracks ids this browser has already applied optimistically ahead of their
// own confirming echo. Image ingest (App.jsx's handleImageIngest) is the
// motivating case: the annotation id is generated client-side and marked
// *before* the ingest request is even sent, so the mark unconditionally
// exists before this browser's own SSE echo of the resulting op — attributed
// to a marker distinct from this browser's own client id specifically so it
// is not dropped as a self-authored echo (see backend/service/rest_api.py's
// ingest_session_image) — can possibly arrive, whichever of the REST response
// or the SSE broadcast this browser's own EventSource happens to receive
// first. Without this, that echo would reapply the annotation a second time,
// silently discarding any move/resize the user made in the gap between the
// optimistic paint and the echo's arrival.
//
// `shouldSkip` consumes the mark (delete-on-first-sight): only the one echo
// immediately following an optimistic apply is swallowed. A later update for
// the same id — a real remote edit, or this same annotation replaced by a
// fresh upload reusing its id — is not, since by then the mark is gone.
export function createSelfEchoDedup() {
  const ids = new Set();
  return {
    markApplied(id) {
      if (typeof id === 'string' && id) ids.add(id);
    },
    shouldSkip(id) {
      return typeof id === 'string' && id ? ids.delete(id) : false;
    },
    // The request this mark was set for never resulted in a server-side
    // write (the POST failed, or the caller bailed out before even sending
    // it — see handleImageIngest's `delivered` guard) — no echo is ever
    // coming for `id`, so there is nothing to wait for. Distinct from
    // `shouldSkip` only in intent: both delete the same entry, but this one
    // is called from the failure path, not from a genuine echo arriving.
    forget(id) {
      if (typeof id === 'string' && id) ids.delete(id);
    },
    // A mark whose echo never arrives as a discrete op — the SSE stream
    // reconnects and resyncs wholesale instead (App.jsx's resyncFromServer)
    // — would otherwise linger forever and wrongly swallow a *later,
    // unrelated* update for the same id (a genuine remote edit reusing it).
    // A full resync already re-hydrates every annotation from server truth,
    // making any pending marks moot; call this there so none outlive their
    // purpose.
    clear() {
      ids.clear();
    },
  };
}
