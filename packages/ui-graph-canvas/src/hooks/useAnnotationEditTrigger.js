import { useCallback, useContext, useEffect, useRef } from 'react';
import { AnnotationContext } from '../components/AnnotationContext';

// A test (or any other caller) providing its own AnnotationContext value
// often supplies only the fields it exercises (`notifyChange`, `labels`,
// ...) — a Provider's `value` replaces AnnotationContext's default wholesale
// rather than merging with it, so `editSheet` can be `undefined` even though
// AnnotationContext's own createContext default always has one. Falling back
// to this stable, capable:false object here (rather than at every read site
// below) is this hook's own "no host wired up" fail-safe, matching every
// other AnnotationContext field's documented default behaviour.
const NOOP_EDIT_SHEET = {
  capable: false,
  container: null,
  requestOpen: () => {},
  requestClose: () => {},
};

/**
 * The contextual "Edit" surface's trigger mechanics
 * (task-annotation-responsive-bottom-toolbox), shared by every annotation
 * kind's own component (NoteNode, LabelNode, ArrowNode,
 * GenericAnnotationNode, FreehandAnnotationNode) so the visible on-selection
 * Edit button behaves identically everywhere rather than five hand-copied
 * implementations. It does NOT own the property-editing UI itself — every
 * kind keeps its own `contextMenu`/`setContextMenu` state and its own menu
 * body JSX exactly as the pre-existing right-click path already does; this
 * hook only decides WHERE that same state/JSX ends up (a floating menu
 * anchored to the button, or the host's mobile edit sheet) and manages the
 * two things a button-triggered menu needs that a right-click-triggered one
 * did not: closing the host sheet when the menu closes, and moving focus in
 * on open / back to the button on close.
 *
 * Desktop and non-integrated hosts (`editSheet.capable` false — see
 * `AnnotationContext`'s `editSheet` field) get a floating menu, positioned
 * under the button rather than at a pointer position, so the exact same
 * portal-to-`document.body`, fixed-position markup right-click already uses
 * serves the keyboard/touch path too — no second menu implementation.
 * Compact/touch integrated hosts (MobileShell wired via GraphCanvas's
 * `annotationEditSheetPortalContainer`/`onRequestAnnotationEditSheet` props)
 * get the same menu content portalled into the shared bottom-sheet surface
 * instead, mirroring PR #525's `AnnotationToolbox` `variant="sheet"` pattern
 * for creation.
 *
 * Focus move-in-on-open/restore-on-close now applies to BOTH entry paths
 * (task-annotation-accessible-shared-controls, generalising what used to be
 * button-only — see `previousFocusRef`'s own doc comment above for the
 * right-click path's restore target): opening a menu, however it opened,
 * moves focus to its first item; closing it restores focus to the Edit
 * button for the button path, or to whatever had focus immediately
 * beforehand for the right-click path (mirroring `ContextMenus.jsx`'s
 * `useMenuOpenFocus`, the graph-node/pane menu system's equivalent). Arrow-
 * key roving navigation and a Tab focus trap within the open menu are a
 * separate, per-caller addition — `useAnnotationMenuKeyNav`
 * (`ContextMenus.jsx`), wired onto the menu container's own `onKeyDown` —
 * kept out of this hook because it operates on `menuRef`'s DOM content
 * directly and has no dependency on the open/close state machine below.
 *
 * @param {object} params
 * @param {object|null} params.contextMenu - the caller's own menu descriptor
 *   state (`null` when closed; `{x, y}` for a floating menu, `{sheet: true}`
 *   once opened via this hook in sheet mode).
 * @param {(next: object|null) => void} params.setContextMenu - the caller's
 *   own state setter.
 * @param {import('react').RefObject} params.menuRef - the ref the caller
 *   already attaches to its portalled menu container (used only to find the
 *   first focusable item on open; never mutated).
 */
export function useAnnotationEditTrigger({ contextMenu, setContextMenu, menuRef }) {
  const { editSheet = NOOP_EDIT_SHEET } = useContext(AnnotationContext);
  const editButtonRef = useRef(null);
  // Whether the CURRENTLY open menu (if any) was opened via this hook's own
  // button, as opposed to the pre-existing right-click path calling the
  // caller's `setContextMenu` directly. Only the button path restores focus
  // to a specific, known element (the button itself) on close and gets the
  // sheet-close call below; the right-click path still gets its own,
  // generic restore — see `previousFocusRef` below.
  const openedViaButtonRef = useRef(false);
  const prevRef = useRef({ open: false, sheet: false });
  // Whatever had focus immediately before a right-click-opened menu appeared
  // — mirrors ContextMenus.jsx's `useMenuOpenFocus` (the graph-node/pane menu
  // system's own version of the same idea), generalising this hook's
  // existing button-path focus-move-in/-restore-out to the right-click path
  // too (task-annotation-accessible-shared-controls, closing the
  // accessibility audit's "Focus restored to the object on close" and
  // "Keyboard way IN to the property editor" gaps: a right-click-opened menu
  // now also moves focus into itself, so a keyboard/screen-reader user who
  // somehow triggers `onContextMenu` — e.g. the Shift+F10 handler
  // GraphCanvas.jsx wires below onto this same button — lands inside the
  // menu with the button path's own restore-on-close, not just for the
  // button entry point).
  const previousFocusRef = useRef(null);

  // Whether a real, non-null `editSheet.container` has been observed during
  // the CURRENT sheet-mode open cycle. Reset in `openEditMenu` below, right
  // before each fresh `requestOpen()`.
  //
  // This exists because `editSheet.container` is legitimately, transiently
  // null for one whole render at the START of every open cycle, not just
  // when the sheet closes: `requestOpen()` flips the host's surface state,
  // which mounts the sheet's (empty) container `<div>` with a callback ref
  // (MobileShell's `onAnnotationEditSheetContainerChange` →
  // `setMobileAnnotationEditContainer` in App.jsx). React invokes that
  // callback ref during the commit phase, and the `setState` inside it
  // schedules a SEPARATE render/commit — so the render that first sets
  // `contextMenu = {sheet: true}` still sees `editSheet.container === null`
  // (the container prop hasn't propagated back down from App.jsx yet), and
  // its passive effects (this one included) flush before that second commit
  // (the one where `container` is finally truthy) ever happens. Without this
  // flag, the guard below can't tell "hasn't arrived yet" apart from "was
  // showing and then genuinely disappeared" (the surface closed elsewhere —
  // switching mobile nav tabs, the BottomSheet's own close control), and
  // fires on the former, closing the sheet before the container it was
  // waiting for even arrives.
  const containerSeenThisCycleRef = useRef(false);

  useEffect(() => {
    if (!contextMenu?.sheet) return undefined;
    if (editSheet.container) {
      containerSeenThisCycleRef.current = true;
      return undefined;
    }
    // Only close for a container that was seen and then went missing again
    // — never while still waiting for it to arrive for the first time.
    if (containerSeenThisCycleRef.current) setContextMenu(null);
    return undefined;
  }, [editSheet.container, contextMenu, setContextMenu]);

  useEffect(() => {
    const isOpen = Boolean(contextMenu);
    const wasOpen = prevRef.current.open;
    if (isOpen === wasOpen) return undefined;
    const wasSheet = prevRef.current.sheet;
    prevRef.current = { open: isOpen, sheet: Boolean(contextMenu?.sheet) };
    if (isOpen) {
      previousFocusRef.current = document.activeElement;
      // One frame late: the menu portal (and, in sheet mode, the host's
      // BottomSheet content) has to actually mount before there is anything
      // to focus. Applies to both entry paths now — see previousFocusRef's
      // doc comment above for why the right-click path is no longer
      // excluded.
      const raf = requestAnimationFrame(() => {
        menuRef.current?.querySelector('button:not([disabled])')?.focus();
      });
      return () => cancelAnimationFrame(raf);
    }
    // Closing.
    const openedViaButton = openedViaButtonRef.current;
    openedViaButtonRef.current = false;
    if (wasSheet) editSheet.requestClose?.();
    if (openedViaButton && editButtonRef.current) {
      editButtonRef.current.focus();
    } else {
      const prev = previousFocusRef.current;
      if (prev && typeof prev.focus === 'function' && document.contains(prev)) prev.focus();
    }
    previousFocusRef.current = null;
    return undefined;
  }, [contextMenu, menuRef, editSheet]);

  const openEditMenu = useCallback(
    (event) => {
      // Mirrors every kind's existing `onContextMenu` handler: prevent the
      // native browser menu / any bubbling click-through, matching the
      // right-click path's own `e.preventDefault(); e.stopPropagation();`.
      event.preventDefault();
      event.stopPropagation();
      openedViaButtonRef.current = true;
      if (editSheet.capable) {
        // A fresh open cycle: the container from any previous cycle (this
        // node's last edit, or another node's) is no longer relevant.
        containerSeenThisCycleRef.current = false;
        editSheet.requestOpen?.();
        setContextMenu({ sheet: true });
      } else {
        const rect = event.currentTarget.getBoundingClientRect();
        setContextMenu({ x: rect.left, y: rect.bottom + 6 });
      }
    },
    [editSheet, setContextMenu]
  );

  return {
    editButtonRef,
    openEditMenu,
    sheetContainer: editSheet.container,
  };
}
