/** Build legacy SavedView metadata from one sync baseline and the current canvas. */
export function buildCaptureContents({ baseline, nodes } = {}) {
  const node_ids = [...(baseline?.node_refs || [])];
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
