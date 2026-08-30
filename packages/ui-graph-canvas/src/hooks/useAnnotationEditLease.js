import { useContext, useEffect, useRef } from 'react';
import { AnnotationContext } from '../components/AnnotationContext';

/**
 * Shared edit-lease gate for an annotation's right-click property editor or
 * rename input (task-annotation-exclusive-edit-leases: opening the context
 * menu — or, for GroupNode, its rename input — is the "editing starts"
 * moment for every color/fill/border/rotation/etc. setter it holds open;
 * those setters already refuse on `isRemoteLocked(data)` individually, same
 * as before this task).
 *
 * Purely reactive to `isOpen` (the caller's own `Boolean(contextMenu)` or
 * `isEditing`) rather than an acquire-then-open call the caller awaits: menu
 * open and rename-start are both optimistic UI everywhere else in this
 * codebase (no round trip before the user sees a response — matching
 * `useEditableText`'s own `startEditing`, and `sessionSyncClient.
 * setLocalSelection`'s fire-and-forget claim), so this acquires in the
 * background the instant `isOpen` becomes true and releases the instant it
 * goes false — covering every way the menu/input already closes (an outside
 * click/Escape via the caller's own dismiss effect, or an action handler
 * like `remove`/`unlock` calling `setContextMenu(null)` itself) without any
 * of those call sites needing to know about leases at all. A denial while
 * open is not surfaced here — the individual mutation handlers' own
 * `isRemoteLocked`/`notifyRemoteLockedAttempt` guard is what a user actually
 * hits if they try to act after losing the race, and the server remains the
 * authoritative backstop regardless (LeaseConflict).
 *
 * One implementation shared by NoteNode, LabelNode, ArrowNode, GroupNode,
 * FreehandAnnotationNode and GenericAnnotationNode, so all six use the exact
 * same trigger definition rather than six hand-copied ones.
 */
export function useAnnotationEditLease(id, isOpen) {
  const { beginEditing, endEditing } = useContext(AnnotationContext);
  const heldRef = useRef(false);
  const wasOpenRef = useRef(false);

  useEffect(() => {
    if (isOpen && !wasOpenRef.current) {
      if (beginEditing) {
        beginEditing([id]).then(({ denied } = {}) => {
          if (!denied?.[id]) heldRef.current = true;
        });
      } else {
        heldRef.current = true;
      }
    } else if (!isOpen && wasOpenRef.current && heldRef.current) {
      heldRef.current = false;
      endEditing?.([id]);
    }
    wasOpenRef.current = isOpen;
  }, [isOpen, id, beginEditing, endEditing]);

  // Release on unmount too (annotation deleted while its menu/input was open).
  useEffect(
    () => () => {
      if (heldRef.current) {
        heldRef.current = false;
        endEditing?.([id]);
      }
    },
    [id, endEditing]
  );
}
