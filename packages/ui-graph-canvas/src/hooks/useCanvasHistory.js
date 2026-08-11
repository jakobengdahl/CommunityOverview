import { useCallback, useRef, useState } from 'react';

// How many position-move actions the undo stack keeps. Bounded so a long editing
// session cannot grow the history without limit; older entries are dropped.
const DEFAULT_HISTORY_LIMIT = 100;

/**
 * Undo/redo history for canvas node-position moves.
 *
 * This is the client-side realisation of the reversibility the visualization
 * layout contract (docs/MCP_VISUALIZATION_LAYOUT_CONTRACT.md §12) reserves for
 * layout moves: a move is reversed by writing the prior positions back. Each
 * recorded entry is the batch of nodes moved by one user action (a drag or an
 * organize arrangement), capturing each node's position `from` (before) and `to`
 * (after). Undo re-applies the `from` positions; redo re-applies the `to`
 * positions. Applying is left to the caller so the same setNodes + persistence
 * path used by a normal move is reused.
 *
 * Only position moves are in scope: hide/delete and annotation edits are owned
 * elsewhere (the host, or the annotation save round-trip) and are not recorded.
 *
 * Stacks live in refs so recording never re-binds the keyboard handler or
 * churns the canvas; the depth-derived `canUndo` / `canRedo` are mirrored into
 * state after each mutation so React sees them without the stacks themselves
 * being reactive (and without reading a ref during render).
 */
export function useCanvasHistory({ limit = DEFAULT_HISTORY_LIMIT } = {}) {
  const undoStack = useRef([]);
  const redoStack = useRef([]);
  const [flags, setFlags] = useState({ canUndo: false, canRedo: false });
  const bump = useCallback(() => {
    setFlags((prev) => {
      const canUndo = undoStack.current.length > 0;
      const canRedo = redoStack.current.length > 0;
      if (prev.canUndo === canUndo && prev.canRedo === canRedo) return prev;
      return { canUndo, canRedo };
    });
  }, []);

  // Record one user action. `moves` is an array of { id, from:{x,y}, to:{x,y} }.
  // No-op entries (a node whose position did not actually change, or malformed
  // moves) are dropped; recording a real move clears the redo stack, because a
  // new action invalidates the redo future.
  const record = useCallback(
    (moves) => {
      if (!Array.isArray(moves)) return;
      const changed = moves.filter(
        (m) => m && m.id != null && m.from && m.to && (m.from.x !== m.to.x || m.from.y !== m.to.y)
      );
      if (changed.length === 0) return;
      const entry = changed.map((m) => ({
        id: m.id,
        from: { x: m.from.x, y: m.from.y },
        to: { x: m.to.x, y: m.to.y },
      }));
      undoStack.current.push(entry);
      if (undoStack.current.length > limit) undoStack.current.shift();
      redoStack.current = [];
      bump();
    },
    [limit, bump]
  );

  // Pop the most recent action and return the positions to restore it to
  // (its `from` positions), or null if there is nothing to undo. The entry is
  // moved onto the redo stack.
  const undo = useCallback(() => {
    const entry = undoStack.current.pop();
    if (!entry) return null;
    redoStack.current.push(entry);
    bump();
    return entry.map((m) => ({ id: m.id, position: { x: m.from.x, y: m.from.y } }));
  }, [bump]);

  // Re-apply the most recently undone action and return its `to` positions, or
  // null if there is nothing to redo. The entry is moved back onto the undo
  // stack.
  const redo = useCallback(() => {
    const entry = redoStack.current.pop();
    if (!entry) return null;
    undoStack.current.push(entry);
    bump();
    return entry.map((m) => ({ id: m.id, position: { x: m.to.x, y: m.to.y } }));
  }, [bump]);

  const clear = useCallback(() => {
    if (undoStack.current.length === 0 && redoStack.current.length === 0) return;
    undoStack.current = [];
    redoStack.current = [];
    bump();
  }, [bump]);

  return {
    record,
    undo,
    redo,
    clear,
    canUndo: flags.canUndo,
    canRedo: flags.canRedo,
  };
}
