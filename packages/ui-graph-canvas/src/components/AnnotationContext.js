import { createContext } from 'react';

/**
 * Shared context for annotation nodes (note, label, arrow). GraphCanvas provides
 * `notifyChange` (called after an annotation is edited, recoloured or deleted so
 * the host can persist the session) and `labels` (English-default UI strings, per
 * the package's props-with-defaults i18n rule — these node components are rendered
 * by ReactFlow's `nodeTypes` map and cannot receive props directly).
 */
export const AnnotationContext = createContext({
  notifyChange: () => {},
  labels: {
    color: 'Colour',
    delete: 'Delete',
    notePlaceholder: 'Note',
    labelPlaceholder: 'Label',
  },
});
