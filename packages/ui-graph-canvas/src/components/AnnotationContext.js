import { createContext } from 'react';

/**
 * Shared context for annotation nodes (note, label, arrow). GraphCanvas provides
 * `notifyChange` (called after an annotation is edited, recoloured or deleted so
 * the host can persist the session) and `labels` (English-default UI strings, per
 * the package's props-with-defaults i18n rule — these node components are rendered
 * by ReactFlow's `nodeTypes` map and cannot receive props directly).
 *
 * `notifyChange` takes an optional `kind` — one of 'create' | 'delete' | 'style' |
 * 'text' | 'geometry' — naming the accepted operation-timing category the change
 * belongs to (task-annotation-shared-session-realtime's slice_scope). The host
 * publishes create/delete/style/geometry immediately (geometry changes already
 * fire only at a release point — drag-stop, resize-end, a rotation click — never
 * continuously) and debounces text edits briefly so a burst of keystrokes
 * coalesces into one op. Omitting `kind` is treated as an immediate publish, the
 * safe default for a call site nobody has classified yet.
 *
 * `notifyRemoteLockedAttempt` is called instead of `notifyChange` when a
 * mutation is refused because another client currently holds a live *edit
 * lease* on the annotation (task-annotation-exclusive-edit-leases —
 * first-actual-editor-wins, never a mere selection), so the host can
 * surface the attempt (e.g. a toast) rather than the change silently doing
 * nothing.
 *
 * `beginEditing(elementIds)` / `endEditing(elementIds)` are the edit-lease
 * acquire/release pair every real edit-start entry point calls — opening a
 * text field, beginning a geometry gesture, opening a property editor,
 * starting a bulk mutation or undo — never on mere selection.
 * `beginEditing` resolves `{granted, denied}` (`denied` maps a refused id to
 * the display name of whoever holds it); a caller checks `denied[id]` before
 * proceeding and calls `notifyRemoteLockedAttempt()` if refused. Backed by
 * `sessionSyncClient.beginEditing`/`endEditing` (see that module for the
 * first-actual-editor-wins acquisition semantics); the default below fails
 * open (grants locally) for a context consumer with no host wired up (e.g.
 * a bare unit test), matching that module's own fail-open reasoning for "no
 * live connection yet".
 *
 * `attachNearby(targetId, kind)` is the "Nearby object menu" creation entry
 * point (docs/ANNOTATION_CONTRACT.md "Human authoring surfaces"): creates a
 * new label/icon/text pre-wired to attach to `targetId` (this
 * annotation, or the graph node/annotation a caller anchors the control to),
 * using the exact same `content.attachment` shape and resolve/follow
 * mechanism the post-creation drag-to-attach path uses. `vote_dot` is not
 * offered here (task-annotation-vote-dot-simplify): it is no longer an
 * attachable kind.
 *
 * `editSheet` (task-annotation-responsive-bottom-toolbox) is the contextual
 * "Edit" surface's mobile-sheet wiring, consumed by
 * `hooks/useAnnotationEditTrigger.js` rather than read directly by node
 * components:
 *   - `capable`: true only on a compact/integrated host that has wired the
 *     mobile edit sheet (GraphCanvas's `onRequestAnnotationEditSheet` prop) —
 *     mirrors exactly how `annotationToolboxPortalContainer`'s presence gates
 *     the creation toolbox's own sheet variant. False everywhere else (desktop,
 *     or a compact host with no BottomSheet to portal into, e.g.
 *     `frontend/widget`), so the Edit button there opens the same floating
 *     menu the right-click path already renders, just anchored to the button.
 *   - `container`: the host DOM node to portal the open menu's content into
 *     while `capable` and the sheet is actually open; null otherwise (before
 *     the request has been acted on, or once closed).
 *   - `requestOpen`/`requestClose`: ask the host to open/close its mobile
 *     edit sheet (bound to `useSurfaceManager`'s `'detail'` surface in
 *     `frontend/web`'s `MobileShell`/`App.jsx`). The default below is a
 *     no-op, `capable: false` context — the same "no host wired up" fail-safe
 *     every other AnnotationContext field already defaults to.
 */
export const AnnotationContext = createContext({
  notifyChange: () => {},
  notifyRemoteLockedAttempt: () => {},
  beginEditing: async (elementIds) => ({ granted: elementIds || [], denied: {} }),
  endEditing: () => {},
  attachNearby: () => {},
  editSheet: { capable: false, container: null, requestOpen: () => {}, requestClose: () => {} },
  labels: {
    color: 'Colour',
    fill: 'Fill',
    border: 'Border',
    transparent: 'Transparent',
    delete: 'Delete',
    unlock: 'Unlock',
    duplicate: 'Duplicate',
    notePlaceholder: 'Note',
    labelPlaceholder: 'Label',
    textSize: 'Text size',
    textAlign: 'Alignment',
    alignTop: 'Top',
    alignMiddle: 'Middle',
    alignBottom: 'Bottom',
    alignLeft: 'Left',
    alignCenter: 'Center',
    alignRight: 'Right',
    fontFamily: 'Font',
    fontDefault: 'Default',
    fontFamilySerif: 'Serif',
    fontFamilyMonospace: 'Monospace',
    fontFamilyCursive: 'Cursive',
    arrowStartHead: 'Start arrowhead',
    arrowEndHead: 'End arrowhead',
    shape: 'Shape',
    rotation: 'Rotation',
    rotateLeft: 'Rotate left 15°',
    rotateRight: 'Rotate right 15°',
    rotateReset: 'Reset rotation',
    freehandColor: 'Colour',
    freehandWidth: 'Stroke width',
    freehandSmoothing: 'Smoothing',
    freehandOpacity: 'Opacity',
    layer: 'Layer',
    layerFront: 'Bring to front',
    layerBack: 'Send to back',
    groupLayer: 'Group order',
    groupLayerFront: 'Bring forward',
    groupLayerBack: 'Send backward',
    nearbyMenu: 'Add nearby',
    nearbyLabel: 'Label',
    nearbyIcon: 'Icon',
    nearbyText: 'Text',
    opacity: 'Opacity',
    editAnnotation: 'Edit',
    // Accessible-name kind words (task-annotation-accessible-shared-controls) —
    // see utils/annotations.js's computeAnnotationAriaLabel.
    ariaKindNote: 'Sticky note',
    ariaKindLabel: 'Label',
    ariaKindText: 'Text',
    ariaKindShape: 'shape',
    ariaKindIcon: 'icon',
    ariaKindVoteDot: 'Vote dot',
    ariaKindImage: 'Image',
    ariaKindArrow: 'Arrow',
    ariaKindFreehand: 'Freehand stroke',
    ariaKindGroup: 'Group',
    // Non-drag geometry (task-annotation-accessible-shared-controls).
    width: 'Width',
    height: 'Height',
    applySize: 'Apply size',
    // Non-drag "Attach to…" target-tap mode.
    attachTo: 'Attach to…',
    attachToCancel: 'Cancel attaching',
    detach: 'Detach',
    attachToHint: 'Choose a target to attach to — Escape to cancel',
  },
});
