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
 */
export const AnnotationContext = createContext({
  notifyChange: () => {},
  notifyRemoteLockedAttempt: () => {},
  labels: {
    color: 'Colour',
    delete: 'Delete',
    unlock: 'Unlock',
    notePlaceholder: 'Note',
    labelPlaceholder: 'Label',
    textSize: 'Text size',
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
    voteValue: 'Value',
    voteValueDecrease: 'Decrease value',
    voteValueIncrease: 'Increase value',
  },
});
