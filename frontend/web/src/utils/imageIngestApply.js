// The two side effects a successful image-ingest REST response must trigger,
// extracted out of App.jsx's handleImageIngest so this exact sequence — the
// fix for the "upload never appears" bug — is unit-testable without mounting
// the whole app. See createSelfEchoDedup and SessionSyncClient.foldLocalOp
// for why each step exists.
//
// Marking the annotation id for the self-echo dedup happens in the caller,
// *before* the ingest request is even sent (not here) — see
// handleImageIngest — so the mark unconditionally predates the confirming
// SSE echo regardless of whether that echo or this REST response reaches
// this browser first.
export function applyIngestedImageOptimistically({ annotation, applyRemoteOp, foldLocalOp }) {
  if (!annotation?.id) return;
  const op = { op: 'annotation_created', annotation };
  applyRemoteOp(op);
  foldLocalOp?.(op);
}
