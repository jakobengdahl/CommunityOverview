// The three side effects a successful image-ingest REST response must
// trigger, extracted out of App.jsx's handleImageIngest so this exact
// sequence — the fix for the "upload never appears" bug — is unit-testable
// without mounting the whole app. See createSelfEchoDedup and
// SessionSyncClient.foldLocalOp for why each step exists.
//
// Order matters: `applyRemoteOp` must run before `dedup.markApplied`, or this
// very call would see its own id as already marked and skip applying it (the
// dedup's `shouldSkip` is consulted from inside `applyRemoteOp`'s own
// annotation_created/updated handling).
export function applyIngestedImageOptimistically({ annotation, applyRemoteOp, foldLocalOp, dedup }) {
  if (!annotation?.id) return;
  const op = { op: 'annotation_created', annotation };
  applyRemoteOp(op);
  foldLocalOp?.(op);
  dedup.markApplied(annotation.id);
}
