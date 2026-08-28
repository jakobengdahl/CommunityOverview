/**
 * Persists which variant a collapsed "tool slot" button currently represents
 * — one visible button standing in for a family of options (e.g.
 * AnnotationToolbox's shape slot, and the icon slot
 * task-annotation-icon-slot-and-visuals is meant to reuse this for rather
 * than growing a second, hand-copied implementation).
 *
 * localStorage, not component state or the shared annotation session/graph:
 * owner decision 2026-08-26 on task-annotation-shapes-under-one-toolbox-slot.
 * Component state is lost on reload; the session state is shared with every
 * other participant in a collaborative canvas, which is wrong for what is a
 * personal "last shape I drew" tool preference, not document content.
 *
 * `storageKey` must be unique per slot so two slots' remembered choices never
 * collide (e.g. one key for the shape slot, a different one for the icon
 * slot). `validKeys` guards against a stale localStorage value from an older
 * build naming a variant this slot no longer offers.
 */
import { useCallback, useState } from 'react';

function readStoredKey(storageKey, validKeys, defaultKey) {
  try {
    const stored = window.localStorage?.getItem(storageKey);
    if (stored && validKeys.includes(stored)) return stored;
  } catch {
    // localStorage unavailable (private browsing, disabled storage, a quota
    // error) — fall back to the default for this session. Selection still
    // works, it just won't persist across reloads.
  }
  return defaultKey;
}

export function useToolSlotSelection(storageKey, validKeys, defaultKey) {
  const [current, setCurrentState] = useState(() =>
    readStoredKey(storageKey, validKeys, defaultKey)
  );

  const setCurrent = useCallback(
    (next) => {
      setCurrentState(next);
      try {
        window.localStorage?.setItem(storageKey, next);
      } catch {
        // Same fallback as above — the in-memory selection still updates.
      }
    },
    [storageKey]
  );

  return [current, setCurrent];
}
