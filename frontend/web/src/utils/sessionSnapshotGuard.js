/**
 * Does this canvas hold anything worth persisting?
 *
 * Extracted out of App.jsx's `persistSessionSnapshot` so the exact decision
 * that caused the "annotations are not saved" bug is unit-testable without
 * mounting the whole app — the same reason `imageIngestApply.js` exists.
 *
 * The guard itself is deliberate and stays: an empty, never-edited session
 * must not register server-side (design D13/D14), or every visitor to a
 * fresh id would leave an empty session file behind. What was wrong was the
 * question it asked. It counted only the GRAPH store's nodes, but annotations
 * and group boxes live exclusively in ReactFlow's own node state and never
 * enter that store. A session used purely for annotating — a blank canvas
 * with notes and shapes on it and no graph nodes at all — therefore looked
 * empty on every single save, so nothing was ever written and the work
 * disappeared as soon as the session was switched away and back.
 *
 * Takes the counts rather than the objects so the caller's own shapes
 * (`viewData.annotations`, `viewData.groups`) stay its business.
 */
export function hasCanvasContent({ graphNodeCount = 0, annotationCount = 0, groupCount = 0 } = {}) {
  return graphNodeCount > 0 || annotationCount > 0 || groupCount > 0;
}

/**
 * Whether a snapshot should actually be written.
 *
 * Once a session exists server-side, an empty canvas is real content — the
 * user deleted everything and that deletion has to persist — so emptiness
 * stops being a reason to suppress. That is why `isMaterialized` overrides.
 */
export function shouldPersistSnapshot({ isMaterialized = false, ...content } = {}) {
  return isMaterialized || hasCanvasContent(content);
}
