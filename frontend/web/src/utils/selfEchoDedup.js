// Tracks ids this browser has already applied optimistically ahead of their
// own confirming echo. Image ingest (App.jsx's handleImageIngest) is the
// motivating case: the REST response is applied to the canvas immediately,
// but this browser's own SSE subscription still receives the same op
// afterwards — attributed to a marker distinct from this browser's own
// client id specifically so it is not dropped as a self-authored echo (see
// backend/service/rest_api.py's ingest_session_image). Without this, that
// echo would reapply the annotation a second time, silently discarding any
// move/resize the user made in the gap between the optimistic paint and the
// echo's arrival.
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
    // A mark whose echo never arrives as a discrete op — the SSE stream
    // reconnects and resyncs wholesale instead (App.jsx's resyncFromServer),
    // or this browser navigates away from the annotation before it lands —
    // would otherwise linger forever and wrongly swallow a *later, unrelated*
    // update for the same id (a genuine remote edit reusing it). A full
    // resync already re-hydrates every annotation from server truth, making
    // any pending marks moot; call this there so none outlive their purpose.
    clear() {
      ids.clear();
    },
  };
}
