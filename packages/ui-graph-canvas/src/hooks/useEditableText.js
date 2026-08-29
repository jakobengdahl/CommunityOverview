import { useContext, useEffect, useRef, useState } from 'react';
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
  const { notifyChange, notifyRemoteLockedAttempt } = useContext(AnnotationContext);
  const remoteLocked = isRemoteLocked(data);
  // The persisted flag, distinct from the remote claim above — see GroupNode's
  // equivalent `locked` (smallfix-locked-annotation-text-still-editable-by-doubleclick).
  const locked = Boolean(data?.locked);

  useEffect(() => {
    setText(data.text || '');
  }, [data.text]);

  useEffect(() => {
    if (isEditing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [isEditing]);

  // A lock can arrive from MCP or a collaborator while the editor is open.
  // Close it the moment that happens rather than leaving it open: this hook
  // also live-syncs on every keystroke (handleTextChange below), so leaving
  // the editor mounted would keep writing through a lock that has already
  // taken effect everywhere else. Adjusted during render rather than in an
  // effect — same reasoning, and the same pattern, as GroupNode's
  // `lockedWhenLastRendered` guard on its rename input.
  const [lockedWhenLastRendered, setLockedWhenLastRendered] = useState(locked);
  if (locked !== lockedWhenLastRendered) {
    setLockedWhenLastRendered(locked);
    if (locked && isEditing) {
      setIsEditing(false);
      setText(data.text || '');
    }
  }

  // Double-click entry point: a live remote claim refuses entry, and so does
  // the persisted lock — the capability baseline says a locked object offers
  // only unlock or copy, and an in-place text edit is not that. Matches
  // GroupNode's rename guard (smallfix-locked-annotation-text-still-editable-by-doubleclick).
  const startEditing = (e) => {
    e?.stopPropagation();
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
    if (locked) return;
    setIsEditing(true);
  };

  // Authoritative write on blur/Escape/Enter — trims.
  const commitText = () => {
    setIsEditing(false);
    if (remoteLocked) {
      setText(data.text || '');
      notifyRemoteLockedAttempt();
      return;
    }
    // Backstop for a lock arriving between the render-phase check above and
    // this write — same reasoning as GroupNode's equivalent backstop on
    // handleLabelBlur.
    if (locked) {
      setText(data.text || '');
      return;
    }
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
      setText(data.text || '');
      setIsEditing(false);
    }
  };

  return { isEditing, text, inputRef, startEditing, commitText, handleTextChange, handleKeyDown };
}
