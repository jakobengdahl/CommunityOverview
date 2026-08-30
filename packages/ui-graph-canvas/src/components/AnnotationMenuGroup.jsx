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
  const wasOpenRef = useRef(false);
  useEffect(() => {
    if (wasOpenRef.current && !open) triggerRef.current?.focus();
    wasOpenRef.current = open;
  }, [open]);

  return (
    <div className="annotation-menu-group">
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
        aria-haspopup="true"
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
