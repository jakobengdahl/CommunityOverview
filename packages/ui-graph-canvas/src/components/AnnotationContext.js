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
 * mutation is refused because another client currently holds the
 * annotation's selection claim (leases are exclusive —
 * task-annotation-shared-session-realtime), so the host can surface the
 * attempt (e.g. a toast) rather than the change silently doing nothing.
 *
 * `attachNearby(targetId, kind)` is the "Nearby object menu" creation entry
 * point (docs/ANNOTATION_CONTRACT.md "Human authoring surfaces"): creates a
 * new label/icon/text pre-wired to attach to `targetId` (this
 * annotation, or the graph node/annotation a caller anchors the control to),
 * using the exact same `content.attachment` shape and resolve/follow
 * mechanism the post-creation drag-to-attach path uses. `vote_dot` is not
 * offered here (task-annotation-vote-dot-simplify): it is no longer an
 * attachable kind.
 */
export const AnnotationContext = createContext({
  notifyChange: () => {},
  notifyRemoteLockedAttempt: () => {},
  attachNearby: () => {},
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
    nearbyMenu: 'Add nearby',
    nearbyLabel: 'Label',
    nearbyIcon: 'Icon',
    nearbyText: 'Text',
  },
});
