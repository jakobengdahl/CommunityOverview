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

  useEffect(() => {
    setText(data.text || '');
  }, [data.text]);

  useEffect(() => {
    if (isEditing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [isEditing]);

  // Double-click entry point: only a live remote claim refuses entry; a
  // persisted `locked` flag does not (see GenericAnnotationNode.jsx's
  // startEditingText comment — that gap from GroupNode's rename is tracked
  // separately, not fixed here so all three change together).
  const startEditing = (e) => {
    e?.stopPropagation();
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
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
