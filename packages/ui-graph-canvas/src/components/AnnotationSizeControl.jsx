import { useContext, useState } from 'react';
import { useReactFlow } from 'reactflow';
import { AnnotationContext } from './AnnotationContext';
import { isRemoteLocked, nodeSize } from '../utils/annotations';

// Matches NodeResizer's own MIN_SIZE/minWidth/minHeight conventions across
// the kinds that use it (NoteNode: 120x80, GenericAnnotationNode's
// RESIZABLE_KINDS: 40x40, GroupNode: 200x150) — this shared control cannot
// know which caller it is in, so it enforces the smallest floor any of them
// already does, leaving each drag-resize's own tighter minimum as the real
// authority; this only stops the numeric input from committing something
// degenerate (0 or negative), not from being slightly smaller than one
// specific kind's own preferred floor.
const MIN_DIMENSION = 20;

/**
 * Non-drag width/height control (task-annotation-accessible-shared-controls,
 * closing the accessibility audit's "Non-drag resize | MISSING" row): two
 * real, keyboard/touch-operable number inputs plus an Apply button, offered
 * only by the kinds that already carry an explicit box size and a
 * NodeResizer drag handle — NoteNode, GenericAnnotationNode's `shape`/`image`
 * (RESIZABLE_KINDS), and GroupNode. `text`/`icon`/`vote_dot`/`arrow`/
 * `freehand` render at either a fixed intrinsic size or geometry with no box
 * at all (see docs/ANNOTATION_CONTRACT.md's Canvas rendering section) and so
 * have nothing for this control to change — they do not render it.
 *
 * Reads the CURRENT size once, at mount (a lazy `useState` initializer) —
 * this component only ever mounts while its caller's context menu is open
 * (every caller's own `contextMenu && <Portal>…` guard unmounts it on
 * close), so "on mount" already means "when the menu opens", with no extra
 * open/close plumbing needed. `getNode` (not the `data` prop) is the source
 * for the starting values because a box's width/height live in the
 * ReactFlow node's `style`, not in `data` — the same split `nodeSize()`
 * already reads.
 */
export default function AnnotationSizeControl({ id, data, labels }) {
  const { getNode, getNodes, setNodes } = useReactFlow();
  const { notifyChange, notifyRemoteLockedAttempt } = useContext(AnnotationContext);
  const [draft, setDraft] = useState(() => {
    // Prefers `getNode` (real ReactFlow, and every mock in this suite that
    // bothers to provide it); falls back to `getNodes().find(...)` for the
    // several existing test mocks that only ever provided the plural form
    // (`useReactFlow: () => ({ setNodes, getNodes, screenToFlowPosition })`,
    // written before this control existed). Never throws either way.
    const current =
      typeof getNode === 'function'
        ? getNode(id)
        : (typeof getNodes === 'function' ? getNodes() : []).find((n) => n.id === id);
    const size = nodeSize(current || {});
    return {
      width: Math.round(size.w) || MIN_DIMENSION,
      height: Math.round(size.h) || MIN_DIMENSION,
    };
  });

  const apply = () => {
    if (isRemoteLocked(data)) {
      notifyRemoteLockedAttempt();
      return;
    }
    if (data?.locked) return;
    const width = Math.max(MIN_DIMENSION, Math.round(Number(draft.width)) || MIN_DIMENSION);
    const height = Math.max(MIN_DIMENSION, Math.round(Number(draft.height)) || MIN_DIMENSION);
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, style: { ...n.style, width, height } } : n))
    );
    notifyChange('geometry');
  };

  const handleKeyDown = (e) => {
    // Enter applies without needing to Tab to the button — matches every
    // other single-value commit in these menus (rename inputs' own
    // onKeyDown pattern).
    if (e.key === 'Enter') {
      e.preventDefault();
      apply();
    }
  };

  return (
    <>
      <div className="context-menu-title">{labels.width}</div>
      <div className="context-menu-size-inputs">
        <input
          type="number"
          min={MIN_DIMENSION}
          className="context-menu-size-input nodrag"
          aria-label={labels.width}
          value={draft.width}
          onChange={(e) => setDraft((d) => ({ ...d, width: e.target.value }))}
          onKeyDown={handleKeyDown}
        />
        <span className="context-menu-size-separator" aria-hidden="true">
          ×
        </span>
        <input
          type="number"
          min={MIN_DIMENSION}
          className="context-menu-size-input nodrag"
          aria-label={labels.height}
          value={draft.height}
          onChange={(e) => setDraft((d) => ({ ...d, height: e.target.value }))}
          onKeyDown={handleKeyDown}
        />
        <button type="button" className="context-menu-apply-size" onClick={apply}>
          {labels.applySize}
        </button>
      </div>
    </>
  );
}
