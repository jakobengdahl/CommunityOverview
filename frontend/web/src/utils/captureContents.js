/**
 * The sync baseline to capture from, or null when the session has no client.
 * A client holding only a placeholder fails the capture: its empty membership
 * is not the session's.
 */
export function readyBaseline(sync) {
  if (!sync) return null;
  if (!sync.hasCaptureBaseline()) throw new Error('Session state is not loaded yet');
  return sync.captureBaseline();
}

/**
 * Build legacy SavedView metadata from one sync baseline and the current
 * canvas. Without a baseline the store's members are captured, as before.
 */
export function buildCaptureContents({ baseline, members, nodes } = {}) {
  const node_ids = [...(baseline ? baseline.node_refs || [] : members || [])];
  const stored = baseline?.positions || {};
  const computed = new Map((nodes || []).map((node) => [node.id, node.position]));
  const positions = {};

  for (const id of node_ids) {
    const position = stored[id] ?? computed.get(id);
    if (position && Number.isFinite(position.x) && Number.isFinite(position.y)) {
      positions[id] = { x: position.x, y: position.y };
    }
  }
  return { node_ids, positions };
}

/** Read both sources only after the existing capture flush has completed. */
export async function captureContents(flushBeforeCapture, getCaptureState) {
  if (!flushBeforeCapture) return buildCaptureContents(getCaptureState?.());
  return flushBeforeCapture(() => buildCaptureContents(getCaptureState?.()));
}
