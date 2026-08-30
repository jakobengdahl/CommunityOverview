import { useContext } from 'react';
import { useReactFlow } from 'reactflow';
import { AnnotationContext } from './AnnotationContext';
import { isRemoteLocked, isAnnotationDraggable } from '../utils/annotations';
import { reorderNodesForParentChild } from './GraphCanvas';

// Small, fixed screen-space nudge so a freshly duplicated annotation lands
// next to its source rather than exactly on top of it (indistinguishable
// until dragged apart). Matches this codebase's other small, fixed UI
// offsets (e.g. ArrowNode's PAD) rather than trying to be proportional to
// the source's own size, which the generic types don't all have (icon/
// vote_dot have no box at all).
export const DUPLICATE_OFFSET = 24;

/**
 * The duplicate action shared by every annotation context menu that has one
 * (note, label, line, freehand and the generic kinds) — one implementation
 * rather than five, following AnnotationLayerControls' precedent. Unlike the
 * layer row, this renders in BOTH the locked and unlocked branches of every
 * caller's menu: the capability baseline is "a locked object remains
 * selectable but offers only unlock or copy", so duplicating (which never
 * mutates the locked source) is one of the two actions locking is supposed
 * to leave reachable, not one it withholds.
 */
export default function AnnotationDuplicateControl({ labels, onDuplicate }) {
  return (
    <button type="button" className="context-menu-duplicate" onClick={onDuplicate}>
      📋 {labels.duplicate}
    </button>
  );
}

/**
 * The duplicate handler behind the row above.
 *
 * Reads the live ReactFlow node (not just the `data` prop callers already
 * have) via `getNodes()`, because the source's `position`/`style` — needed
 * to place and size the copy — live on the node itself, not inside `data`.
 * Mirrors the backend `duplicate_annotation` MCP tool's own description
 * ("copies every field ... so the caller does not need to know the type's
 * payload shape"): the whole node is cloned, so this one hook works for
 * every kind (note/label/arrow/freehand/generic) without a per-kind branch,
 * the same way `unlock`'s per-component copies do but without needing one at
 * all.
 *
 * Two departures from a plain clone, both deliberate:
 *
 * - `position` is offset by `DUPLICATE_OFFSET` so the copy is a distinct,
 *   visible annotation rather than an exact overlap. Every kind's geometry
 *   here (arrow's `dx`/`dy`, freehand's `points`) is stored relative to the
 *   node's own `position` (their own doc comments), so translating only the
 *   position moves the whole shape without touching its content.
 * - `data.locked` is always forced to `false` on the copy, regardless of the
 *   source. Duplicating a locked source must never itself be locked: an
 *   attached/anchored source's binding is already dropped the moment it is
 *   locked (`set_annotation_lock`/`create_annotation`,
 *   dec-annotation-lock-semantics point 2), but forcing this unconditionally
 *   — rather than relying on that upstream invariant always holding — is
 *   what makes a locked-with-a-live-binding copy structurally impossible
 *   here too, the same class of bug PR #515/#517 fixed for lock itself.
 *   Everything else (attachment, anchors, colour, size, rotation, z) is
 *   copied verbatim; an unlocked, attached duplicate is an ordinary, valid
 *   state.
 *
 * Refuses on a remote edit lease like every other mutation here (task-
 * annotation-exclusive-edit-leases) — a duplicate does not touch the source,
 * but reading a mid-drag/mid-edit node into a copy would freeze whatever
 * half-finished state another actor's gesture happens to be in.
 */
export function useAnnotationDuplicate(id, data) {
  const { getNodes, setNodes } = useReactFlow();
  const { notifyChange, notifyRemoteLockedAttempt } = useContext(AnnotationContext);
  return () => {
    if (isRemoteLocked(data)) {
      notifyRemoteLockedAttempt();
      return;
    }
    const source = getNodes().find((n) => n.id === id);
    if (!source) return;
    // Neither remote marker belongs on a fresh id nothing has selected or
    // leased yet — strip both rather than just the (originally sole) claim
    // marker, or a duplicate taken while its source happened to carry a
    // stale `remoteLease` would render as remote-locked from its very first
    // frame, until the next reconcile effect corrects it.
    const { remoteSelection: _remoteSelection, remoteLease: _remoteLease, ...restData } =
      source.data || {};
    const newId = `${source.type}-${Date.now()}`;
    const position = source.position || { x: 0, y: 0 };
    const newData = { ...restData, locked: false };
    const newNode = {
      ...source,
      id: newId,
      selected: false,
      position: {
        x: position.x + DUPLICATE_OFFSET,
        y: position.y + DUPLICATE_OFFSET,
      },
      data: newData,
      // `...source` above carries its stale top-level `draggable` (false when
      // the source is locked, per `overlayToFlowNode`), but `data.locked` is
      // just forced to `false` two lines up — recompute `draggable` from the
      // copy's own (now-unlocked) data rather than inheriting the source's,
      // the same way every other unlock path here does (NoteNode, LabelNode,
      // FreehandAnnotationNode, GenericAnnotationNode's `isAnnotationDraggable`
      // call; ArrowNode's equivalent inline check). Left stale, a duplicate of
      // a locked annotation would report `data.locked: false` (full menu) yet
      // stay structurally undraggable until a page reload.
      draggable: isAnnotationDraggable({ ...source, data: newData }),
    };
    setNodes((nds) => reorderNodesForParentChild([...nds, newNode]));
    notifyChange('create');
  };
}
