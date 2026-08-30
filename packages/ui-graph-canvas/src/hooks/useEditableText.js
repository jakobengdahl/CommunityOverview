import { useCallback, useContext, useEffect, useRef, useState } from 'react';
import { useReactFlow } from 'reactflow';
import { AnnotationContext } from '../components/AnnotationContext';
import { isRemoteLocked } from '../utils/annotations';

/**
 * The double-click/blur/Escape/live-300ms-sync inline-text-edit state
 * machine shared by NoteNode, LabelNode and GenericAnnotationNode's `text`/
 * `shape` kinds (docs/ANNOTATION_CONTRACT.md's "300 ms text debounce" — the
 * host's scheduler, annotationChangeScheduler.js, debounces the 'text' kind,
 * so this hook's per-keystroke notifyChange('text') calls coalesce into one
 * publish regardless of how often they fire). Follows
 * AnnotationLayerControls.js's `useAnnotationLayer` as precedent: one
 * implementation of the commit/debounce/remote-lock contract rather than
 * three hand-copied ones that were already drifting (task-shared-editable-text-hook).
 *
 * `commitOnEnter` is the one real behavioural difference across the three
 * call sites: LabelNode's single-line `<input>` commits on Enter (a "submit"
 * gesture for one line of text); NoteNode's and GenericAnnotationNode's
 * `<textarea>` leave Enter alone so it inserts a newline, matching each
 * component's pre-extraction behaviour exactly.
 *
 * Returns the state and handlers a caller wires onto its own input element —
 * the element itself (textarea vs. input, styling, rows) stays with the
 * caller, since that is the part that actually differs between them.
 */
export function useEditableText(id, data, { commitOnEnter = false } = {}) {
  const [isEditing, setIsEditing] = useState(false);
  const [text, setText] = useState(data.text || '');
  const inputRef = useRef(null);
  const { setNodes } = useReactFlow();
  const { notifyChange, notifyRemoteLockedAttempt, beginEditing, endEditing } =
    useContext(AnnotationContext);
  const remoteLocked = isRemoteLocked(data);
  // The persisted flag, distinct from the remote lease above — see GroupNode's
  // equivalent `locked` (smallfix-locked-annotation-text-still-editable-by-doubleclick).
  const locked = Boolean(data?.locked);
  // Tracks whether *this* client currently holds the edit lease it acquired
  // in startEditing, so commitText/Escape/unmount know whether there is
  // anything of theirs to release (task-annotation-exclusive-edit-leases).
  // A ref, not state: read/written from event handlers and effects only,
  // never during render (see releaseSignal below for the one place render
  // needs to trigger a release without touching it directly).
  const leaseHeldRef = useRef(false);

  const releaseLease = useCallback(() => {
    if (leaseHeldRef.current) {
      leaseHeldRef.current = false;
      endEditing?.([id]);
    }
  }, [id, endEditing]);

  // Bumped (a state update, not a ref write) by the two render-phase
  // adjustments below when they need a lease release — refs are only ever
  // touched from an effect/handler, never during render itself, so the
  // actual releaseLease() call is deferred to the effect that watches this.
  const [releaseSignal, setReleaseSignal] = useState(0);
  useEffect(() => {
    if (releaseSignal > 0) releaseLease();
  }, [releaseSignal, releaseLease]);

  useEffect(() => {
    setText(data.text || '');
  }, [data.text]);

  useEffect(() => {
    if (isEditing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [isEditing]);

  // Release on unmount (annotation deleted/deselected mid-edit) — the only
  // exit path not already covered by commitText/handleKeyDown below.
  useEffect(() => releaseLease, [releaseLease]);

  // A lock can arrive from MCP or a collaborator while the editor is open.
  // Close it the moment that happens rather than leaving it open: this hook
  // also live-syncs on every keystroke (handleTextChange below), so leaving
  // the editor mounted would keep writing through a lock that has already
  // taken effect everywhere else. Adjusted during render rather than in an
  // effect — same reasoning, and the same pattern, as GroupNode's
  // `lockedWhenLastRendered` guard on its rename input. This is the
  // *persisted* lock — a deliberate, permanent state a user set — so
  // discarding the in-progress draft here is the existing, unchanged
  // behaviour; it is not the remote-lease case handled below.
  const [lockedWhenLastRendered, setLockedWhenLastRendered] = useState(locked);
  if (locked !== lockedWhenLastRendered) {
    setLockedWhenLastRendered(locked);
    if (locked && isEditing) {
      setReleaseSignal((s) => s + 1);
      setIsEditing(false);
      setText(data.text || '');
    }
  }

  // A remote edit lease can also appear while this client is mid-edit: this
  // client's own background lease renewal (sessionSyncClient's renew timer)
  // lost a race after a TTL gap, and someone else's acquisition won and was
  // broadcast. Unlike the persisted-lock branch above, the in-progress draft
  // is *not* discarded here — task-annotation-exclusive-edit-leases requires
  // a refused/interrupted edit to keep the user's local input. The editor
  // stays open showing the local `text`; handleTextChange's own remoteLocked
  // guard already stops it from syncing further, so this only needs to free
  // the now-pointless lease tracking and tell the user why nothing they type
  // from here is being saved.
  const [remoteLockedWhenLastRendered, setRemoteLockedWhenLastRendered] = useState(remoteLocked);
  if (remoteLocked !== remoteLockedWhenLastRendered) {
    setRemoteLockedWhenLastRendered(remoteLocked);
    if (remoteLocked && isEditing) {
      setReleaseSignal((s) => s + 1);
      notifyRemoteLockedAttempt();
    }
  }

  // Double-click entry point: enters edit mode immediately (optimistic —
  // the same "don't make the user wait on a round trip" pattern the rest of
  // this codebase's realtime paths use, e.g. sessionSyncClient's
  // setLocalSelection) while acquiring the edit lease (first-actual-editor-
  // wins — task-annotation-exclusive-edit-leases) in the background. A live
  // remote lease refuses entry outright (fast local check, no round trip
  // needed since it is already known); the persisted lock refuses it too —
  // the capability baseline says a locked object offers only unlock or copy,
  // and an in-place text edit is not that. Matches GroupNode's rename guard
  // (smallfix-locked-annotation-text-still-editable-by-doubleclick).
  //
  // If the background acquisition loses a race (denied), the editor is
  // deliberately *not* force-closed here: `data.remoteLease` converges to
  // reflect the winner within one SSE round trip regardless, at which point
  // the render-phase effect above takes over with its own draft-preserving
  // handling — this only needs to flag that this client never actually held
  // it (so `releaseLease` does not send a pointless release) and tell the
  // user once, immediately, rather than waiting for that convergence.
  const startEditing = (e) => {
    e?.stopPropagation();
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
    if (locked) return;
    setIsEditing(true);
    if (beginEditing) {
      beginEditing([id]).then(({ denied } = {}) => {
        if (denied?.[id]) {
          notifyRemoteLockedAttempt();
        } else {
          leaseHeldRef.current = true;
        }
      });
    } else {
      leaseHeldRef.current = true;
    }
  };

  // Authoritative write on blur/Escape/Enter — trims.
  const commitText = () => {
    setIsEditing(false);
    if (remoteLocked) {
      // Nothing of ours was ever acquired in this branch (startEditing
      // already refused entry when remoteLocked was true from the start),
      // so there is no draft risk here — this is the pre-existing "refused
      // before it began" case, not the mid-edit interruption above.
      setText(data.text || '');
      notifyRemoteLockedAttempt();
      return;
    }
    // Backstop for a lock arriving between the render-phase check above and
    // this write — same reasoning as GroupNode's equivalent backstop on
    // handleLabelBlur.
    if (locked) {
      releaseLease();
      setText(data.text || '');
      return;
    }
    releaseLease();
    const trimmed = text.trim();
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, text: trimmed } } : n))
    );
    notifyChange('text');
  };

  // Live per-keystroke sync. Pushes the raw, untrimmed value on every
  // change — `commitText` still trims on blur/Escape/Enter, the
  // authoritative final write.
  const handleTextChange = (e) => {
    const next = e.target.value;
    setText(next);
    if (remoteLocked) return;
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, text: next } } : n))
    );
    notifyChange('text');
  };

  const handleKeyDown = (e) => {
    if (commitOnEnter && e.key === 'Enter') {
      e.preventDefault();
      commitText();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      releaseLease();
      setText(data.text || '');
      setIsEditing(false);
    }
  };

  return { isEditing, text, inputRef, startEditing, commitText, handleTextChange, handleKeyDown };
}
