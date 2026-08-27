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
export async function applyIngestedImageOptimistically({ annotation, applyRemoteOp, foldLocalOp }) {
  if (!annotation?.id) return;
  const op = { op: 'annotation_created', annotation };
  const applied = await applyRemoteOp(op);
  if (applied) foldLocalOp?.(op);
}
