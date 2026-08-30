// The two side effects a successful image-ingest REST response must trigger,
// extracted out of App.jsx's handleImageIngest so this exact sequence — the
// fix for the "upload never appears" bug — is unit-testable without mounting
// the whole app. See createSelfEchoDedup and SessionSyncClient.foldLocalOp
// for why each step exists, and App.jsx's applyRemoteOp (annotation_created/
// annotation_updated case) for why its return value gates foldLocalOp here.
//
// Reserving the annotation id for the self-echo dedup happens in the caller,
// *before* the ingest request is even sent (not here) — see
// handleImageIngest — so the reservation unconditionally predates the
// confirming SSE echo regardless of whether that echo or this REST response
// reaches this browser first.
//
// `applyRemoteOp` decides, via that same dedup, whether THIS call is the one
// that wins the race against the echo (see App.jsx). Only the winner should
// fold the op into the sync baseline: if the echo already won (arrived and
// applied first), it already folded the same op into the baseline itself
// (SessionSyncClient's own internal echo-fold guard) — folding it again here
// with this same, now possibly-stale creation-time content would risk
// reverting a move/resize made in the gap since.
// Returns whether the response actually carried an annotation to deliver.
// `false` means the round trip completed but produced nothing this browser can
// render — a 200 with a missing or malformed `annotation`. That case used to
// return here silently while the caller went on to mark the ingest delivered,
// so a user who picked a file got no image AND no error: the canvas simply
// never changed. It is a failure and the caller must say so.
//
// A `true` return says only "there was an annotation and it was handed to
// applyRemoteOp", NOT "this call is what rendered it". `applied` being falsy
// is an ordinary, healthy outcome — it means the confirming SSE echo won the
// race and already applied the same op — which is why it gates `foldLocalOp`
// (see below) and not the return value. Reporting a lost race as an error
// would fire on a perfectly successful upload.
export async function applyIngestedImageOptimistically({ annotation, applyRemoteOp, foldLocalOp }) {
  if (!annotation?.id) return false;
  const op = { op: 'annotation_created', annotation };
  const applied = await applyRemoteOp(op);
  if (applied) foldLocalOp?.(op);
  return true;
}
