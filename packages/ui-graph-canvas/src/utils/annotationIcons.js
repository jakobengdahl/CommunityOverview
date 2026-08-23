/**
 * The icon set an `icon` annotation draws from (docs/ANNOTATION_CONTRACT.md:
 * "`icon` — a configured icon from the icon set").
 *
 * The canvas package has no access to the host app's react-bootstrap-icons
 * registry (that lives in frontend/web and is wired to the schema's node-type
 * `icon` field), so the annotation icons are their own small, self-contained
 * set of text glyphs — the same approach AnnotationToolbox already uses for
 * its buttons. Bootstrap-style names from that host registry are accepted as
 * aliases for the icons the two sets share, so an annotation authored with
 * `content.icon: "FlagFill"` shows a flag rather than falling back.
 */

// Neutral glyph for an icon name the set does not know. Mirrors the host
// app's DEFAULT_ICON reasoning (FloatingToolbar.jsx): a question mark would
// read as a rendering error rather than as an unconfigured icon.
export const DEFAULT_ANNOTATION_ICON = 'circle';

// Null prototype for the same reason the host app's ICON_REGISTRY uses one
// (FloatingToolbar.jsx): the key comes from an annotation's configured icon
// name, and a plain object literal would resolve "constructor" or "toString"
// to an inherited member instead of falling back to the default glyph.
export const ANNOTATION_ICONS = Object.freeze(
  Object.assign(Object.create(null), {
    circle: '●',
    flag: '⚑',
    star: '★',
    check: '✔',
    cross: '✖',
    warning: '⚠',
    question: '❓',
    info: 'ℹ',
    lightbulb: '\u{1F4A1}',
    pin: '\u{1F4CC}',
    heart: '❤',
    bell: '\u{1F514}',
    target: '\u{1F3AF}',
    lock: '\u{1F512}',
    eye: '\u{1F441}',
  })
);

// Alternative names resolving to the same icon: the host registry's
// Bootstrap-icon names, plus the everyday synonyms an agent is likely to send.
// Null prototype for the same reason as ANNOTATION_ICONS above.
const ICON_ALIASES = Object.assign(Object.create(null), {
  circle_fill: 'circle',
  dot: 'circle',
  flag_fill: 'flag',
  star_fill: 'star',
  check_circle_fill: 'check',
  ok: 'check',
  x: 'cross',
  x_circle_fill: 'cross',
  exclamation_triangle_fill: 'warning',
  alert: 'warning',
  question_circle_fill: 'question',
  info_circle_fill: 'info',
  lightbulb_fill: 'lightbulb',
  idea: 'lightbulb',
  pin_angle_fill: 'pin',
  heart_fill: 'heart',
  bell_fill: 'bell',
  bullseye: 'target',
  lock_fill: 'lock',
  eye_fill: 'eye',
});

function normalizeIconName(name) {
  if (typeof name !== 'string') return '';
  return name
    .trim()
    .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
    .replace(/[\s-]+/g, '_')
    .toLowerCase();
}

// Resolve a configured icon name to the glyph that renders it. Unknown names
// fall back to the neutral default rather than to an abbreviation of the name.
export function annotationIconGlyph(name) {
  const normalized = normalizeIconName(name);
  const resolved = ANNOTATION_ICONS[normalized]
    ? normalized
    : ICON_ALIASES[normalized] || DEFAULT_ANNOTATION_ICON;
  return ANNOTATION_ICONS[resolved];
}
