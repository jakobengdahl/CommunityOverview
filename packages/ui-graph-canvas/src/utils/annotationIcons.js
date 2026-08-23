/**
 * The icon set an `icon` annotation draws from (docs/ANNOTATION_CONTRACT.md:
 * "`icon` — a configured icon from the icon set").
 *
 * The canvas package has no access to the host app's react-bootstrap-icons
 * registry (that lives in frontend/web and is wired to the schema's node-type
 * `icon` field), so the annotation icons are their own small, self-contained
 * set of text glyphs — the same approach AnnotationToolbox already uses for
 * its buttons. Bootstrap-style names from that host registry are accepted as
 * aliases for the 11 icons the two sets share, so an annotation authored with
 * `content.icon: "FlagFill"` shows a flag. A name that matches neither a
 * canonical key nor an alias draws the two-character abbreviation of itself
 * that this component drew before the set existed, so no host-registry name
 * became less distinguishable than it was.
 */

// Glyph drawn when an annotation has no icon name at all. Mirrors the host
// app's DEFAULT_ICON reasoning (FloatingToolbar.jsx): a question mark would
// read as a rendering error rather than as an unconfigured icon. A name the
// set does not know is abbreviated instead — see resolveAnnotationIcon.
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

// Alternative names resolving to the same icon: Bootstrap-icon spellings
// (those the host registry actually carries, plus the same convention for a
// few icons it does not) and the everyday synonyms an agent is likely to
// send. Null prototype for the same reason as ANNOTATION_ICONS above.
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
  return (
    name
      .trim()
      .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
      // Second boundary: a run of capitals followed by a capitalised word, so a
      // name whose first word is a single letter ("XCircleFill") splits into
      // x_circle_fill rather than xcircle_fill and still finds its alias.
      .replace(/([A-Z]+)([A-Z][a-z])/g, '$1_$2')
      .replace(/[\s-]+/g, '_')
      .toLowerCase()
  );
}

// Resolve a configured icon name to what should be drawn for it.
//
// The host app's icon registry (FloatingToolbar.jsx) carries 75 Bootstrap-icon
// names and this set covers 11 of them, so most configured names have no glyph
// here. Those fall back to the first two characters of the name — the same
// abbreviation this component drew before the set existed — rather than to a
// neutral dot, so that adding the set never makes a name *less*
// distinguishable than it was: a board of DatabaseFill/GearFill/PeopleFill
// icons keeps three separable marks instead of collapsing into three
// identical dots. (The synonym aliases below merge deliberately - `dot` draws
// the same ● as `circle` where both used to draw different letters - because
// a meaningful icon beats two distinct abbreviations.) `isGlyph` lets the caller style the two cases differently
// (an abbreviation needs the smaller, uppercased treatment a glyph does not).
export function resolveAnnotationIcon(name) {
  const normalized = normalizeIconName(name);
  const resolved = ANNOTATION_ICONS[normalized] ? normalized : ICON_ALIASES[normalized];
  if (resolved && ANNOTATION_ICONS[resolved]) {
    return { text: ANNOTATION_ICONS[resolved], isGlyph: true };
  }
  const trimmed = typeof name === 'string' ? name.trim() : '';
  if (trimmed) return { text: trimmed.slice(0, 2), isGlyph: false };
  return { text: ANNOTATION_ICONS[DEFAULT_ANNOTATION_ICON], isGlyph: true };
}
