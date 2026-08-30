import { useEffect, useRef } from 'react';

/**
 * One collapsed group in an annotation's property bar
 * (task-annotation-compact-property-bar).
 *
 * The property editor used to be a single tall column: every section — colour,
 * fill, border, nine-position text alignment, text size, font family, shape
 * subtype, icon name, rotation, opacity, size, layer — stacked with a visible
 * heading above each one. On a shape that is well over a phone's screen
 * height, so the menu covered the object it was editing and the controls at
 * the bottom needed scrolling to reach.
 *
 * This is the collapsed form: the bar shows one small trigger per group, and
 * the group's own controls appear in a panel only once its trigger is
 * activated. The trigger carries the group's CURRENT value wherever the group
 * has one — the fill trigger is a swatch in the current fill, the shape
 * trigger draws the current subtype — so the bar still answers "what is this
 * set to?" at a glance, which is what the visible headings were really for.
 *
 * The panel content is passed through verbatim: this component owns the
 * collapse, not the controls. Each group keeps the exact markup, labels and
 * behaviour it had when it was a section, so nothing about how a colour is
 * chosen changes — only how many taps it takes to see the choices.
 */
export function AnnotationMenuGroup({ groupKey, label, glyph, swatch, open, onToggle, children }) {
  const triggerRef = useRef(null);

  // Closing returns focus to the trigger that opened the panel, so a keyboard
  // user is not dropped back at the top of the bar after every group.
  //
  // Guarded on the focus actually being INSIDE this group. Groups are
  // siblings and switching between them closes A in the same commit that
  // opens B; React flushes sibling effects in tree order, so an unguarded
  // restore had A's close pull focus back onto A's trigger after B had
  // already taken it — opening a group moved focus to the previous one.
  // Restoring only when this group still owns the focus keeps the behaviour
  // for the case it exists for (closing the group you were in) and makes it
  // a no-op for the case it broke.
  const wasOpenRef = useRef(false);
  const groupRef = useRef(null);
  // Whether focus was last seen INSIDE this group. Tracked with `focusin`
  // while open rather than read from `document.activeElement` at close time:
  // by the time the close effect runs the panel is already unmounted, so its
  // focused child is gone and `activeElement` has fallen back to <body> —
  // the check would answer "no" in exactly the case it exists for.
  const heldFocusRef = useRef(false);
  useEffect(() => {
    if (!open) return undefined;
    const el = groupRef.current;
    if (!el) return undefined;
    // Seeded from the current focus: the effect attaches after the click that
    // opened the group, so the trigger's own focusin is already past. Without
    // this, a keyboard user who opens a group and never tabs into the panel
    // leaves `heldFocusRef` false for the whole open lifetime, and the
    // focus-return this component documents never happens.
    heldFocusRef.current = el.contains(document.activeElement);
    const onFocusIn = () => {
      heldFocusRef.current = true;
    };
    // A focus landing anywhere else means this group no longer owns it —
    // which is what happens when the user opens a sibling group, and is
    // precisely the case that must NOT pull focus back here.
    const onDocumentFocusIn = (event) => {
      if (!el.contains(event.target)) heldFocusRef.current = false;
    };
    el.addEventListener('focusin', onFocusIn);
    document.addEventListener('focusin', onDocumentFocusIn);
    return () => {
      el.removeEventListener('focusin', onFocusIn);
      document.removeEventListener('focusin', onDocumentFocusIn);
    };
  }, [open]);

  useEffect(() => {
    if (wasOpenRef.current && !open && heldFocusRef.current) {
      heldFocusRef.current = false;
      triggerRef.current?.focus();
    }
    wasOpenRef.current = open;
  }, [open]);

  return (
    <div className="annotation-menu-group" ref={groupRef}>
      <button
        ref={triggerRef}
        type="button"
        className={`annotation-menu-group-trigger${open ? ' open' : ''}`}
        // `title` as well as `aria-label`: the bar has no room for a visible
        // caption, so the native tooltip is what a mouse user reads to learn
        // what a glyph means. The toolbox can use a styled tooltip of its own
        // because it is a persistent surface; this menu is transient and
        // pointer-positioned, where a second floating layer would land on top
        // of the panel it is describing.
        title={label}
        aria-label={label}
        // A plain disclosure, not a menu: `aria-haspopup="true"` is a synonym
        // for `"menu"` and promised a menu-role popup this never renders (the
        // panel is a `group`). `aria-expanded` alone is the correct pairing.
        aria-expanded={open}
        onClick={() => onToggle(open ? null : groupKey)}
      >
        <span className="annotation-menu-group-glyph" aria-hidden="true">
          {glyph}
        </span>
        {swatch !== undefined && swatch !== null && (
          <span
            className={`annotation-menu-group-swatch${
              swatch === 'transparent' ? ' annotation-menu-group-swatch--transparent' : ''
            }`}
            style={swatch === 'transparent' ? undefined : { backgroundColor: swatch }}
            aria-hidden="true"
          />
        )}
      </button>
      {open && (
        <div className="annotation-menu-group-panel" role="group" aria-label={label}>
          {children}
        </div>
      )}
    </div>
  );
}

export default AnnotationMenuGroup;
