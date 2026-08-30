/**
 * The icon set an `icon` annotation draws from (docs/ANNOTATION_CONTRACT.md:
 * "`icon` — a configured icon from the icon set").
 *
 * The canvas package has no access to the host app's react-bootstrap-icons
 * registry (that lives in frontend/web and is wired to the schema's node-type
 * `icon` field) — and even if it did, that registry hands out React
 * components, while every generic annotation (GenericAnnotationNode.jsx)
 * renders its icon as plain text content, so mounting an SVG component isn't
 * a fit here regardless. Instead this file owns its own self-contained set
 * of text glyphs — the same approach AnnotationToolbox already uses for its
 * buttons — with one canonical entry per Bootstrap-icon name in the host
 * registry (`ICON_REGISTRY` in frontend/web/src/components/FloatingToolbar.jsx,
 * the full 75-name vocabulary `schema_config.json`'s `icon` field draws
 * from), keyed by that name's normalized (snake_case) form, plus a handful
 * of everyday synonyms as aliases. A name that matches neither a canonical
 * key nor an alias draws the two-character abbreviation of itself that this
 * component drew before the set existed, so a name the vocabulary later
 * grows to include never becomes less distinguishable than it was.
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
//
// The first block (circle..eye) predates full host-registry coverage and
// keeps its short, friendly canonical names — ICON_ALIASES below maps the
// matching Bootstrap-icon spelling onto each. The second block adds the
// remaining 64 host-registry names that had no glyph before (see
// docs/ANNOTATION_CONTRACT.md's icon acceptance-matrix cell); those are
// keyed directly by their normalized Bootstrap name since there is no
// shorter synonym already established for them, so no alias entry is
// needed to reach them.
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

    // Remaining 64 ICON_REGISTRY names (FloatingToolbar.jsx), normalized.
    person_fill: '\u{1F464}',
    rocket_takeoff_fill: '\u{1F680}',
    lightning_fill: '⚡',
    file_earmark_text_fill: '\u{1F4C4}',
    shield_fill_check: '\u{1F6E1}',
    tags_fill: '\u{1F3F7}',
    trophy_fill: '\u{1F3C6}',
    calendar_event_fill: '\u{1F4C5}',
    database_fill: '\u{1F5C4}',
    cpu_fill: '\u{1F5A5}',
    bookmark_fill: '\u{1F516}',
    folder_fill: '\u{1F4C1}',
    clipboard_data_fill: '\u{1F4CB}',
    people_fill: '\u{1F465}',
    sliders: '\u{1F39A}',
    list_ol: '\u{1F522}',
    diagram3_fill: '\u{1F9EC}',
    card_checklist: '\u{1F4DD}',
    input_cursor_text: '✏',
    list_check: '☑',
    gear_fill: '⚙',
    collection_fill: '\u{1F5C3}',
    funnel_fill: '\u{1F53B}',
    gear_wide_connected: '\u{1F527}',
    globe_europe_africa_fill: '\u{1F30D}',
    geo_alt_fill: '\u{1F4CD}',
    map_fill: '\u{1F5FA}',
    building_fill: '\u{1F3E2}',
    buildings_fill: '\u{1F3D9}',
    bank: '\u{1F3E6}',
    bar_chart_fill: '\u{1F4CA}',
    pie_chart_fill: '◐',
    graph_up_arrow: '\u{1F4C8}',
    file_earmark_spreadsheet_fill: '\u{1F9EE}',
    file_earmark_bar_graph_fill: '\u{1F4C9}',
    file_earmark_code_fill: '⌨',
    journal_bookmark_fill: '\u{1F4D4}',
    book_fill: '\u{1F4D8}',
    bookshelf: '\u{1F4DA}',
    clipboard_check_fill: '\u{1F9FE}',
    clipboard2_data_fill: '\u{1F4C7}',
    shield_lock_fill: '\u{1F510}',
    shield_fill_exclamation: '\u{1F6A8}',
    key_fill: '\u{1F511}',
    robot: '\u{1F916}',
    motherboard_fill: '\u{1F50C}',
    router_fill: '\u{1F4F6}',
    chat_fill: '\u{1F4AC}',
    envelope_fill: '✉',
    megaphone_fill: '\u{1F4E3}',
    mortarboard_fill: '\u{1F393}',
    award_fill: '\u{1F3C5}',
    diagram2_fill: '\u{1F578}',
    kanban_fill: '\u{1F5C2}',
    boxes: '\u{1F4E6}',
    layers_fill: '\u{1F9F1}',
    grid_fill: '\u{1F532}',
    compass_fill: '\u{1F9ED}',
    puzzle_fill: '\u{1F9E9}',
    binoculars_fill: '\u{1F52D}',
    translate: '\u{1F310}',
    person_lines_fill: '\u{1F4DB}',
    list_nested: '☰',
    tag_fill: '\u{1F3AB}',
  })
);

// Alternative names resolving to the same icon: Bootstrap-icon spellings for
// the 11 icons above that predate full host-registry coverage (the other 64
// names are keyed directly in ANNOTATION_ICONS and need no alias — see the
// comment there) and the everyday synonyms an agent is likely to send. Null
// prototype for the same reason as ANNOTATION_ICONS above.
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
// names and this set now has a canonical or aliased entry for every one of
// them, each with its own distinct glyph. A name that matches neither a
// canonical key nor an alias (e.g. one the host registry adds later) falls
// back to the first two characters of the name — the same abbreviation this
// component drew before the set existed — rather than to a neutral dot, so
// that a future gap never makes a name *less* distinguishable than it was.
// (The synonym aliases above merge deliberately - `dot` draws the same ● as
// `circle` where both used to draw different letters - because a meaningful
// icon beats two distinct abbreviations.) `isGlyph` lets the caller style the
// two cases differently (an abbreviation needs the smaller, uppercased
// treatment a glyph does not).
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
