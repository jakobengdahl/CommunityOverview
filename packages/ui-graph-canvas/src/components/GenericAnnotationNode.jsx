import { memo, useContext, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { NodeResizer, useReactFlow } from 'reactflow';
import { AnnotationContext } from './AnnotationContext';
import { ANNOTATION_ICONS, resolveAnnotationIcon } from '../utils/annotationIcons';
import {
  rotationStyle,
  ROTATABLE_OVERLAY_KINDS,
  isRemoteLocked,
  remoteEditBadge,
  resolveRotatedResizeGeometry,
  DEFAULT_GENERIC_TEXT_FONT_SIZE,
  DEFAULT_SHAPE_CAPTION_FONT_SIZE,
  GENERIC_TEXT_FONT_SIZES,
  GENERIC_FONT_FAMILIES,
  TEXT_ALIGN_VALUES,
  TEXT_ALIGN_DEFAULT_BY_KIND,
  TEXT_ALIGN_STYLES,
  isAnnotationDraggable,
  ATTACHABLE_OVERLAY_KINDS,
} from '../utils/annotations';
import AnnotationLayerControls, { useAnnotationLayer } from './AnnotationLayerControls';
import AnnotationDuplicateControl, { useAnnotationDuplicate } from './AnnotationDuplicateControl';
import AnnotationOpacityControl, { useAnnotationOpacity } from './AnnotationOpacityControl';
import AnnotationSizeControl from './AnnotationSizeControl';
import { AnnotationMenuGroup } from './AnnotationMenuGroup';
import { NearbyObjectMenuSection, useAnnotationMenuKeyNav } from './ContextMenus';
import { useEditableText } from '../hooks/useEditableText';
import { useAnnotationEditLease } from '../hooks/useAnnotationEditLease';
import { useAnnotationEditTrigger } from '../hooks/useAnnotationEditTrigger';
import './GenericAnnotationNode.css';

const DEFAULT_COLOR = '#94a3b8';

// A right-click property editor exists for every rotatable generic kind:
// each has at least its rotation and its layer to edit, plus — per kind —
// `shape`'s subtype, `icon`'s configured name and, for the kinds that paint
// one, a colour. `vote_dot` is a plain coloured dot (task-annotation-vote-
// dot-simplify): it no longer has a value to edit, only its colour. (`shape`,
// `icon` and `vote_dot` are all already members of ROTATABLE_OVERLAY_KINDS,
// so this is exactly that set.)
const EDITABLE_KINDS = ROTATABLE_OVERLAY_KINDS;

// The generic kinds whose `color` field is actually painted by a branch
// below — text's text colour, icon's border, vote_dot's fill. `image`
// carries a `color` in the model (GENERIC_OVERLAY_FIELDS in
// utils/annotations.js) but nothing renders it, so offering a recolour there
// would be a control with no visible effect. `shape` used to be here too
// (its `color` painted its fill) — task-annotation-merge-frame-into-shape-
// rectangle replaced that single swatch with the independent `fill`/`border`
// sections below (see FILL_BORDER_SWATCHES), so `shape` no longer uses this
// generic single-colour editor at all.
const COLORABLE_KINDS = new Set(['text', 'icon', 'vote_dot']);

// Palette for the generic kinds' colour picker. Saturated rather than the
// pastels NoteNode/LabelNode use, because these paint borders, glyphs and
// small filled dots rather than a large sticky-note ground — a pastel
// vote_dot on a light canvas is nearly invisible. DEFAULT_COLOR leads so the
// picker can always return an annotation to the colour it was created with.
const GENERIC_COLORS = [
  DEFAULT_COLOR,
  '#ef4444',
  '#f97316',
  '#eab308',
  '#22c55e',
  '#3b82f6',
  '#a855f7',
  '#0f172a',
];

// The swatch options for `shape`'s fill/border editors: every GENERIC_COLORS
// choice, plus `'transparent'` — the setting that subsumes what the retired
// `frame` kind was (a box with no fill). Independent for fill and border, so
// a shape can be a solid fill with no border (a plain shape, as before), a
// transparent fill with a coloured border (what `frame` used to draw), both,
// or neither.
const FILL_BORDER_SWATCHES = ['transparent', ...GENERIC_COLORS];

// `shape`'s own defaults when `fill`/`border` are unset — chosen to match
// exactly how a `shape` rendered before this task added the two fields, so an
// existing annotation with neither stored is unaffected: a solid grey fill
// and no border, the same look a plain `color`-only shape had.
const DEFAULT_SHAPE_FILL = DEFAULT_COLOR;
const DEFAULT_SHAPE_BORDER = 'transparent';

// The generic kinds that get inline double-click-to-edit text, following the
// exact pattern NoteNode/LabelNode already established (double-click to
// enter, blur/Escape to commit, live sync at the shared 300ms text debounce —
// see AnnotationContext's notifyChange('text') doc comment). `text`'s whole
// purpose is holding text; `shape` covers every content.shape variant
// (rectangle through process_arrow) so a shape can carry a caption — including
// a transparent-fill one that visually stands in for the retired `frame`
// kind, which never had a caption of its own either. `icon`, `vote_dot` and
// `image` have no free-text field to edit this way either — none carries a
// caption in the v1 content model, and `vote_dot` is a plain coloured dot
// with no content field of its own at all (task-annotation-vote-dot-simplify).
const EDITABLE_TEXT_KINDS = new Set(['text', 'shape']);

// Maps each half of a `textAlign` value ('top-left' -> ['top','left']) to the
// `labels` key carrying its translated word, so the 3x3 alignment picker's
// aria-label/title can compose "Top left" etc. from the same six short words
// rather than needing nine separately-translated strings.
const ALIGN_LABEL_KEYS = Object.freeze({
  top: 'alignTop',
  middle: 'alignMiddle',
  bottom: 'alignBottom',
  left: 'alignLeft',
  center: 'alignCenter',
  right: 'alignRight',
});

// Maps each GENERIC_FONT_FAMILIES entry to the `labels` key carrying its
// translated display name. The *stored* value stays the untranslated CSS
// generic keyword (so it round-trips identically for every locale/host and
// the font-picker button can still preview it inline via
// `style={{ fontFamily: family }}`), but per this package's i18n rule (see
// this repo's root CLAUDE.md: "All user-visible text in
// packages/ui-graph-canvas must be accepted as props with English
// defaults"), the *visible button text* must not be the bare keyword itself.
const FONT_FAMILY_LABEL_KEYS = Object.freeze({
  serif: 'fontFamilySerif',
  monospace: 'fontFamilyMonospace',
  cursive: 'fontFamilyCursive',
});

// The axis-aligned rectangle each shape variant's clip-path is *guaranteed*
// to fully contain, as inset percentages (top/right/bottom/left) against the
// node's own box. A text layer positioned inside this rectangle can never
// spill past the visible painted outline, however the clip-path itself is
// drawn — solving the "text overflows the corners" problem this task exists
// to close without touching the shape's stored geometry.
//
// Insetting the text was chosen over growing the shape to fit: growing would
// fight the fixed aspect ratios REGULAR_SHAPE_ASPECT enforces for triangle/
// hexagon/rhombus (there is no single side to grow that keeps the figure
// regular), and would move the annotation's stored width/height as a side
// effect of typing rather than of a deliberate resize gesture. Insetting only
// changes where the text layer draws; `shape`'s geometry semantics — and
// everything resize/aspect-lock does with them — stay exactly as they are.
//
// Derivation per variant (reading SHAPE_STYLES' clip-paths, not eyeballed):
// - `rectangle` has no clip-path at all: the full box is safe.
// - `process_arrow`'s body (the 0%-70% block before its point) and
//   `hexagon`'s vertical band (25%-75%, full height, since both its slanted
//   edges only move *outward* from x=25%/75% as they run from y=0%/100%
//   toward y=50%) are each already an axis-aligned rectangle wholly inside
//   the polygon.
// - `circle` inscribes a centred square (or, for a non-square box, a
//   same-fraction axis-aligned rectangle) of side length 1/sqrt(2) of the
//   box on each axis — the largest one a circle/ellipse can contain — so the
//   margin on every side is (1 - 1/sqrt(2))/2.
// - `triangle` (apex top-centre, full-width base) and `rhombus` (a point at
//   each edge midpoint) both reduce to the textbook maximal-area
//   axis-aligned inscribed rectangle for their shape: half the box's width
//   and half its height, centred — sitting on the base for the triangle
//   (inset-top 50%, inset-bottom 0%, since the triangle only widens going
//   down), centred on all four sides for the rhombus.
const CIRCLE_TEXT_INSET_PCT = ((1 - 1 / Math.sqrt(2)) / 2) * 100;
const SHAPE_TEXT_INSET = Object.freeze(
  Object.assign(Object.create(null), {
    rectangle: { top: 0, right: 0, bottom: 0, left: 0 },
    circle: {
      top: CIRCLE_TEXT_INSET_PCT,
      right: CIRCLE_TEXT_INSET_PCT,
      bottom: CIRCLE_TEXT_INSET_PCT,
      left: CIRCLE_TEXT_INSET_PCT,
    },
    triangle: { top: 50, right: 25, bottom: 0, left: 25 },
    rhombus: { top: 25, right: 25, bottom: 25, left: 25 },
    hexagon: { top: 0, right: 25, bottom: 0, left: 25 },
    process_arrow: { top: 0, right: 30, bottom: 0, left: 0 },
  })
);

// Falls back to the rectangle inset (the whole box) for an unrecognised
// shape name, matching SHAPE_STYLES' own fallback — including a name that
// collides with an inherited Object member, since SHAPE_TEXT_INSET has no
// prototype to collide with.
function shapeTextInsetStyle(shape) {
  const inset = SHAPE_TEXT_INSET[shape] || SHAPE_TEXT_INSET.rectangle;
  return {
    position: 'absolute',
    top: `${inset.top}%`,
    right: `${inset.right}%`,
    bottom: `${inset.bottom}%`,
    left: `${inset.left}%`,
  };
}

const ROTATE_STEP = 15;
function normalizeAngle(deg) {
  return ((deg % 360) + 360) % 360;
}

// shape/image are the generic kinds that carry an explicit box size
// (SIZED_GENERIC_KINDS in utils/annotations.js) and are the only ones
// resizable in this slice; text/icon/vote_dot render at a fixed intrinsic
// size, so resizing them has no model-space geometry to change.
const RESIZABLE_KINDS = new Set(['shape', 'image']);
const MIN_SIZE = 40;

// Every `content.shape` variant the contract accepts, as the CSS that draws
// it. Kept here rather than in the stylesheet so each variant's geometry is
// one testable value: the rectangle/circle-only rendering this replaces
// painted triangle, rhombus, hexagon and process arrow as plain rectangles,
// which no class-name assertion could have caught. Null prototype because the
// key is an annotation's configured shape name (same reason as
// annotationIcons.js and the host app's ICON_REGISTRY).
const SHAPE_STYLES = Object.freeze(
  Object.assign(Object.create(null), {
    rectangle: {},
    circle: { borderRadius: '50%' },
    triangle: { clipPath: 'polygon(50% 0%, 100% 100%, 0% 100%)' },
    rhombus: { clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' },
    hexagon: { clipPath: 'polygon(25% 0%, 75% 0%, 100% 50%, 75% 100%, 25% 100%, 0% 50%)' },
    // A full-height block meeting a point of the same height — the process-flow
    // chevron used to chain steps left to right. It is deliberately not an
    // arrow glyph (a thin shaft with a wider head), which is what this drew
    // before and which reads as "direction" rather than "a step".
    process_arrow: {
      clipPath: 'polygon(0% 0%, 70% 0%, 100% 50%, 70% 100%, 0% 100%)',
    },
  })
);

// Width-to-height ratios that make a subtype's clip-path draw a *regular*
// figure. The clip-paths are all percentages, so they resolve against the node
// box, and at the generic 160x96 default a hexagon comes out with long
// horizontals and short slants while a triangle is wide and flat.
//
// For the triangle and the hexagon the ratio is what makes the SIDES equal,
// and both work out to 2 : sqrt(3) wide-to-tall. The rhombus is a different
// case worth stating plainly, because it is easy to get wrong: its four sides
// are sqrt(w^2 + h^2)/2 whatever the box, so a rhombus is equal-sided at EVERY
// ratio. 1:1 is here to make it a square standing on its corner — equal
// diagonals and right angles — which is what "a rhombus" is normally drawn as
// and what a 160x96 box was failing to give.
//
// Subtypes absent from this map fill whatever box they are given: rectangle by
// definition, and process_arrow because a process step's length is how you
// show a long step. `circle` is deliberately absent too — it draws with
// border-radius rather than a clip-path and becomes an ellipse in a non-square
// box, the same class of surprise but not part of what was reported. See
// task-annotation-shape-equal-sided-geometry.
const REGULAR_SHAPE_ASPECT = Object.freeze(
  Object.assign(Object.create(null), {
    triangle: 2 / Math.sqrt(3),
    hexagon: 2 / Math.sqrt(3),
    rhombus: 1,
  })
);

export function regularShapeAspect(shape) {
  return REGULAR_SHAPE_ASPECT[shape] ?? null;
}

// The width a shape is created at, and the width a subtype switch resizes it
// back to. Kept next to the ratio rather than in GraphCanvas so one test can
// cover both halves — the created size living in a different file from the
// ratio that justifies it is exactly why the creation branch went untested.
export const SHAPE_BASE_WIDTH = 160;
const SHAPE_FALLBACK_SIZE = Object.freeze({ width: 160, height: 96 });

/**
 * The box a `shape` of this subtype should occupy. A subtype with a regular
 * ratio gets the height that ratio implies; every other subtype fills the
 * generic box.
 */
export function regularShapeSize(shape, currentWidth) {
  const aspect = regularShapeAspect(shape);
  if (!aspect) return null;
  // Keep the width the shape already has, so switching subtype re-proportions
  // the figure without discarding a resize the user did on purpose. Only
  // creation, which has no width yet, falls back to the base.
  const width = Number.isFinite(currentWidth) && currentWidth > 0 ? currentWidth : SHAPE_BASE_WIDTH;
  return { width, height: Math.round(width / aspect) };
}

/**
 * The box a newly created `shape` should occupy: its regular ratio if it has
 * one, otherwise the generic box every unsized annotation gets.
 */
export function newShapeSize(shape) {
  return regularShapeSize(shape) || { ...SHAPE_FALLBACK_SIZE };
}

// The shape-subtype picker's option order — every variant SHAPE_STYLES draws.
const SHAPE_NAMES = ['rectangle', 'circle', 'triangle', 'rhombus', 'hexagon', 'process_arrow'];

// The icon picker's option order: every canonical name annotationIcons.js
// draws a distinct glyph for (the full set the module doc comment there
// describes — a canonical or aliased entry for every one of the host
// registry's 75 icon names, plus the friendly synonyms that predate it).
// Reusing this set rather than inventing a second vocabulary is the point:
// whatever a user picks here, resolveAnnotationIcon already knows how to
// draw on the annotation itself.
const ICON_NAMES = Object.keys(ANNOTATION_ICONS);

// A clip-path clips the element's outline away too, so the dashed selection
// outline every other generic kind uses is invisible on a triangle, rhombus,
// hexagon or process arrow — and a locked one shows no resize handles either,
// leaving a selected shape with no feedback at all. The halo therefore goes on
// an unclipped wrapper: an element's own filter is rendered *before* its
// clip-path, so a drop shadow on the clipped element itself would be clipped
// away with it.
const SELECTED_SHAPE_HALO = Object.freeze({
  filter: 'drop-shadow(0 0 3px rgba(255, 255, 255, 0.9)) drop-shadow(0 0 1px rgba(0, 0, 0, 0.6))',
});

/**
 * GenericAnnotationNode - a simple visual representation for the v1
 * annotation types that have no dedicated per-type editor yet (text, shape,
 * icon, vote_dot, image; see docs/ANNOTATION_CONTRACT.md). These were
 * previously normalized by annotationModel.js but dropped by the overlay
 * translation layer, so an MCP-created annotation of one of these types never
 * rendered. Selection and move (drag) are handled generically by GraphCanvas
 * for every annotation type; this component adds the visual selection
 * outline, for the sized kinds, model-space resize via ReactFlow's
 * NodeResizer, and — for the kinds EDITABLE_KINDS names — a right-click
 * property editor (colour, shape subtype, icon name, rotation
 * and layer order), and — for EDITABLE_TEXT_KINDS (`text`, `shape`) —
 * double-click-to-edit inline text, following NoteNode/LabelNode's own
 * pattern exactly, plus nine-position text alignment, font size and a
 * curated font-family picker (task-annotation-text-alignment-and-font).
 */
// See NoteNode's equivalent default: an annotation whose payload is missing
// should draw empty rather than throw. Every read below already uses `?.` or a
// fallback; this closes the two that dereferenced `data` directly.
function GenericAnnotationNode({ id, type, data = {}, selected }) {
  const kind = type;
  // `shape` no longer has a single `color` — its resizer accent instead
  // prefers whichever of fill/border is a real colour (fill first, since it
  // usually covers more area), falling back to DEFAULT_COLOR when both are
  // transparent or unset, so the resize handles are never left uncoloured.
  const shapeFill = data?.fill ?? DEFAULT_SHAPE_FILL;
  const shapeBorder = data?.border ?? DEFAULT_SHAPE_BORDER;
  const color =
    kind === 'shape'
      ? shapeFill !== 'transparent'
        ? shapeFill
        : shapeBorder !== 'transparent'
          ? shapeBorder
          : DEFAULT_COLOR
      : data?.color || DEFAULT_COLOR;
  const locked = Boolean(data?.locked);
  const {
    notifyChange,
    notifyRemoteLockedAttempt,
    labels,
    attachNearby,
    enterAttachMode,
    beginEditing,
    endEditing,
  } = useContext(AnnotationContext);
  // See NoteNode's equivalent comment: another client's live edit lease
  // (task-annotation-exclusive-edit-leases) refuses every mutation below.
  const remoteLocked = isRemoteLocked(data);
  const { setNodes } = useReactFlow();
  const selectedClass = selected ? ' selected' : '';
  // Rotation is applied to the rendered element, not to the ReactFlow node
  // wrapper, so drag hit-testing keeps using the unrotated bounding box.
  const rotation = rotationStyle(kind, data?.rotation);

  // Typography for EDITABLE_TEXT_KINDS (`text`, `shape`) —
  // task-annotation-text-alignment-and-font. Computed unconditionally (not
  // just inside those two branches) so the shared `textEditor` below, which
  // renders for either kind, can use the same values without re-deriving
  // them per branch. Harmless for every other kind: nothing reads these.
  const textAlign = data?.textAlign || TEXT_ALIGN_DEFAULT_BY_KIND[kind] || 'top-left';
  const textAlignStyle = TEXT_ALIGN_STYLES[textAlign] || TEXT_ALIGN_STYLES['top-left'];
  // `??`, not `||`: a stored fontSize of 0 is a real, explicit value (however
  // degenerate) and must be honored, not silently replaced by the default —
  // only an *omitted* fontSize (null/undefined) should fall back.
  const textFontSize =
    data?.fontSize ??
    (kind === 'shape' ? DEFAULT_SHAPE_CAPTION_FONT_SIZE : DEFAULT_GENERIC_TEXT_FONT_SIZE);
  // Unset/falsy means "no override" — the annotation keeps inheriting the
  // app's ambient font exactly as it does today (GENERIC_FONT_FAMILIES has no
  // "default" entry of its own; see its doc comment).
  const textFontFamily = data?.font || undefined;

  const [contextMenu, setContextMenu] = useState(null);
  const contextMenuRef = useRef(null);
  useAnnotationEditLease(id, Boolean(contextMenu));
  const { editButtonRef, openEditMenu, sheetContainer } = useAnnotationEditTrigger({
    contextMenu,
    setContextMenu,
    menuRef: contextMenuRef,
  });
  // Snapshot of {x, y, width, height} at the start of the current resize
  // gesture, read by handleResizeEnd to map the gesture's net delta back
  // through this annotation's rotation (resolveRotatedResizeGeometry).
  const resizeStartRef = useRef(null);

  // Inline text editing for EDITABLE_TEXT_KINDS (`text`, `shape`) — via the
  // shared useEditableText hook, so the same double-click/blur/Escape/
  // live-sync behaviour holds for every editable kind (NoteNode, LabelNode,
  // this one) rather than three hand-copied editing models.
  const {
    isEditing: isEditingText,
    text: textDraft,
    inputRef: textInputRef,
    startEditing: startEditingTextIfEditable,
    commitText,
    handleTextChange,
    handleKeyDown: handleTextKeyDown,
  } = useEditableText(id, data);

  useEffect(() => {
    if (!contextMenu) return;
    const handleDismiss = (e) => {
      if (contextMenuRef.current && contextMenuRef.current.contains(e.target)) return;
      setContextMenu(null);
    };
    const handleKeyDown = (e) => {
      if (e.key === 'Escape') setContextMenu(null);
    };
    const timer = setTimeout(() => {
      document.addEventListener('mousedown', handleDismiss, true);
      document.addEventListener('contextmenu', handleDismiss, true);
      document.addEventListener('keydown', handleKeyDown, true);
    }, 0);
    return () => {
      clearTimeout(timer);
      document.removeEventListener('mousedown', handleDismiss, true);
      document.removeEventListener('contextmenu', handleDismiss, true);
      document.removeEventListener('keydown', handleKeyDown, true);
    };
  }, [contextMenu]);

  const openContextMenu = (e) => {
    if (!EDITABLE_KINDS.has(kind)) return;
    e.preventDefault();
    e.stopPropagation();
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
    setContextMenu({ x: e.clientX, y: e.clientY });
  };

  // Double-click entry point for EDITABLE_TEXT_KINDS, matching NoteNode/
  // LabelNode's onDoubleClick exactly: the persisted `locked` flag refuses
  // entry the same way the live remote claim does (task-locked-annotation-
  // doubleclick-guard) — both live in the shared useEditableText hook, so
  // this wrapper only adds the kind gate.
  const startEditingText = (e) => {
    if (!EDITABLE_TEXT_KINDS.has(kind)) return;
    startEditingTextIfEditable(e);
  };

  const changeShape = (shape) => {
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
    // Switching subtype has to re-proportion the box. A rectangle is 160x96;
    // making it a triangle without touching the box gives exactly the squashed
    // figure this change exists to prevent — and `keepAspectRatio` would then
    // lock that wrong ratio in place, since it preserves what it measures.
    //
    // Only the height moves: the width the user has given the shape is theirs,
    // so a deliberately widened triangle stays that wide when it becomes a
    // hexagon. Switching to a subtype with no ratio leaves the box alone
    // entirely — a rectangle fills whatever box it is given, so there is
    // nothing to correct and resetting it would throw away a resize.
    setNodes((nds) =>
      nds.map((n) => {
        if (n.id !== id) return n;
        const size = regularShapeSize(shape, n.style?.width);
        const next = { ...n, data: { ...n.data, shape } };
        return size ? { ...next, style: { ...n.style, ...size } } : next;
      })
    );
    setContextMenu(null);
    // 'geometry' rather than 'style' because the box can move — the documented
    // vocabulary in AnnotationContext.js, not a timing difference: the
    // scheduler branches only on 'text' and publishes both of these
    // immediately, and the kind never reaches the server.
    notifyChange('geometry');
  };

  const changeIcon = (name) => {
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, icon: name } } : n))
    );
    setContextMenu(null);
    notifyChange('style');
  };

  const changeColor = (next) => {
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, color: next } } : n))
    );
    notifyChange('style');
  };

  // `shape`'s independent fill/border settings
  // (task-annotation-merge-frame-into-shape-rectangle) — each is either a
  // colour or the literal string `'transparent'`, following exactly the same
  // wiring `changeColor` above uses for every other colourable kind.
  const changeFill = (next) => {
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, fill: next } } : n))
    );
    notifyChange('style');
  };

  const changeBorder = (next) => {
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, border: next } } : n))
    );
    notifyChange('style');
  };

  // Typography changes for EDITABLE_TEXT_KINDS (`text`, `shape`) — same
  // 'style' notification NoteNode/LabelNode's own changeFontSize uses (a
  // presentation change, not geometry or text content).
  const changeTextAlign = (next) => {
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, textAlign: next } } : n))
    );
    notifyChange('style');
  };

  const changeFontSize = (next) => {
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, fontSize: next } } : n))
    );
    notifyChange('style');
  };

  // `null` clears the override (falls back to the ambient app font) — see
  // GENERIC_FONT_FAMILIES's doc comment on why the curated list itself has no
  // "default" member for this to pick instead.
  const changeFont = (next) => {
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, font: next } } : n))
    );
    notifyChange('style');
  };

  const changeLayer = useAnnotationLayer(id, data);
  const duplicate = useAnnotationDuplicate(id, data);
  const changeOpacity = useAnnotationOpacity(id, data);

  const changeRotation = (deg) => {
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
    const next = normalizeAngle(deg);
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, rotation: next } } : n))
    );
    notifyChange('geometry');
  };

  const remove = () => {
    if (remoteLocked) {
      setContextMenu(null);
      notifyRemoteLockedAttempt();
      return;
    }
    setNodes((nds) => nds.filter((n) => n.id !== id));
    setContextMenu(null);
    notifyChange('delete');
  };

  // Non-drag detach (task-annotation-accessible-shared-controls) — see
  // LabelNode's identical helper for why this stays local rather than a
  // shared AnnotationContext function.
  const detach = () => {
    if (remoteLocked) {
      notifyRemoteLockedAttempt();
      return;
    }
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, attachment: undefined } } : n))
    );
    setContextMenu(null);
    notifyChange('style');
  };

  // Locking withholds everything except the two actions the capability
  // baseline names for a locked object: unlock, and duplicate —
  // colour/rotation/shape/font-size/delete stay out of reach while `locked`
  // is set, matching resize/drag already refusing it.
  const unlock = () => {
    if (remoteLocked) {
      setContextMenu(null);
      notifyRemoteLockedAttempt();
      return;
    }
    setNodes((nds) =>
      nds.map((n) => {
        if (n.id !== id) return n;
        const nextData = { ...n.data, locked: false };
        return { ...n, data: nextData, draggable: isAnnotationDraggable({ ...n, data: nextData }) };
      })
    );
    setContextMenu(null);
    notifyChange('style');
  };

  // Geometry gesture (task-annotation-exclusive-edit-leases): acquired
  // fire-and-forget at resize start (NodeResizer already begins the visual
  // drag by the time this fires) and released at resize end.
  const handleResizeStart = (event, params) => {
    resizeStartRef.current = params;
    beginEditing?.([id]);
  };

  // NodeResizer computes `params` as if this annotation's box were
  // axis-aligned (see resolveRotatedResizeGeometry's comment for why that is
  // wrong once the box is visually rotated); remap the gesture's net delta
  // through the rotation before it lands on the node. Guarded so a caller
  // that invokes this with no params (as an existing test does) still just
  // notifies, unchanged from before this fix.
  const handleResizeEnd = (event, params) => {
    if (params && resizeStartRef.current) {
      const geometry = resolveRotatedResizeGeometry({
        start: resizeStartRef.current,
        end: params,
        rotation: data?.rotation ?? 0,
      });
      setNodes((nds) =>
        nds.map((n) =>
          n.id === id
            ? {
                ...n,
                position: { x: geometry.x, y: geometry.y },
                style: { ...n.style, width: geometry.width, height: geometry.height },
              }
            : n
        )
      );
    }
    resizeStartRef.current = null;
    notifyChange('geometry');
    endEditing?.([id]);
  };

  // Locked annotations already refuse to drag (draggable: !locked in
  // overlayToFlowNode); hide the resize handles too so "locked" reads as one
  // consistent geometry lock rather than only blocking one of two ways to
  // move/resize the object. A remote edit lease (another client actively
  // editing) hides them the same way.
  // Locking the ratio is what keeps a regular figure regular through a resize.
  // Without it the box is free and the percentage clip-path distorts again the
  // moment the user drags a handle, which is how the squashed shapes were
  // reported in the first place.
  //
  // NodeResizer's `keepAspectRatio` is a boolean (reactflow 11.11.4,
  // @reactflow/node-resizer types.d.ts) — it preserves the ratio the node
  // MEASURES at drag start, and takes no target ratio. That is enough here
  // only because every other path now puts the box at the right ratio before
  // a drag can start: creation and the subtype switch both size it from
  // regularShapeSize. Passing a number here would be silently truthy and read
  // as "lock whatever it currently is", which is what it already does.
  const lockedAspect =
    kind === 'shape' ? regularShapeAspect(data?.shape || 'rectangle') !== null : false;
  const resizer = RESIZABLE_KINDS.has(kind) && (
    <NodeResizer
      minWidth={MIN_SIZE}
      minHeight={MIN_SIZE}
      keepAspectRatio={lockedAspect}
      isVisible={Boolean(selected) && !locked && !remoteLocked}
      lineStyle={{ stroke: color, strokeWidth: 2 }}
      handleStyle={{ width: 10, height: 10, background: color, border: '2px solid white' }}
      onResizeStart={handleResizeStart}
      onResizeEnd={handleResizeEnd}
    />
  );

  const badge = remoteEditBadge(data);
  const remoteBadge = badge && (
    <div
      className="graph-node-remote-badge"
      style={{ backgroundColor: badge.color }}
      title={badge.displayName}
    >
      {badge.displayName}
    </div>
  );

  // Opacity (task-annotation-responsive-bottom-toolbox) applies to every
  // generic kind alike, unlike colour (COLORABLE_KINDS) or the shape/icon
  // pickers — see `opacityStyle` below for where it lands per kind.
  const opacity = Number.isFinite(data?.opacity) ? data.opacity : 1;
  const opacityStyle = { opacity };

  // The contextual "Edit" surface's visible entry point
  // (task-annotation-responsive-bottom-toolbox) — see NoteNode's equivalent
  // comment. Gated on EDITABLE_KINDS like `openContextMenu` itself, so a kind
  // with no property editor at all (there is none today, but this mirrors
  // that guard rather than assuming every future kind has one) never shows a
  // button that would open nothing.
  const editTrigger = selected && EDITABLE_KINDS.has(kind) && (
    <button
      ref={editButtonRef}
      type="button"
      className="annotation-edit-trigger nodrag nopan"
      aria-label={labels.editAnnotation}
      aria-haspopup="true"
      aria-expanded={Boolean(contextMenu)}
      onClick={(e) => {
        if (remoteLocked) {
          notifyRemoteLockedAttempt();
          return;
        }
        openEditMenu(e);
      }}
    >
      ✏️
    </button>
  );

  const currentRotation = data?.rotation ?? 0;
  const menu = contextMenu && (
    <ContextMenuPortal
      menuRef={contextMenuRef}
      position={contextMenu}
      sheet={Boolean(contextMenu.sheet)}
      sheetContainer={sheetContainer}
      id={id}
      data={data}
      kind={kind}
      shape={data.shape || 'rectangle'}
      icon={data.icon}
      color={color}
      fill={shapeFill}
      border={shapeBorder}
      rotation={currentRotation}
      locked={locked}
      labels={labels}
      textAlign={textAlign}
      fontSize={textFontSize}
      font={data?.font}
      opacity={opacity}
      onChangeShape={changeShape}
      onChangeIcon={changeIcon}
      onChangeColor={changeColor}
      onChangeFill={changeFill}
      onChangeBorder={changeBorder}
      onChangeLayer={changeLayer}
      onChangeRotation={changeRotation}
      onChangeTextAlign={changeTextAlign}
      onChangeFontSize={changeFontSize}
      onChangeFont={changeFont}
      onChangeOpacity={changeOpacity}
      onDelete={remove}
      onUnlock={unlock}
      onDuplicate={duplicate}
      // Every generic kind here — `shape` included, whatever its fill/border —
      // is a valid nearby-attach target, the same decision
      // computeDroppedAttachment's own candidacy filter makes (see its doc
      // comment in utils/annotations.js) now that the former `frame` kind is
      // folded into `shape` rather than excluded as its own special case.
      onAttachNearby={(nearbyKind) => attachNearby(id, nearbyKind)}
      onEnterAttachMode={
        enterAttachMode
          ? () => {
              enterAttachMode(id);
              setContextMenu(null);
            }
          : undefined
      }
      onDetach={detach}
    />
  );

  // Shared by `text` and `shape` below — the only difference between the two
  // was `rows`, which `shape`'s inset wrapper overrides via CSS `height: 100%`
  // anyway (see GenericAnnotationNode.css), so one element serves both rather
  // than two copies that can quietly drift apart. Typography (fontSize/font/
  // textAlign, task-annotation-text-alignment-and-font) is inline here rather
  // than left to GenericAnnotationNode.css, so the textarea matches the
  // committed text's own rendering below while the user is actively editing
  // it instead of visibly jumping the instant it commits.
  const textEditor = (
    <textarea
      ref={textInputRef}
      className="graph-generic-annotation-text-input nodrag"
      rows={2}
      style={{
        fontSize: textFontSize,
        fontFamily: textFontFamily,
        textAlign: textAlignStyle.textAlign,
      }}
      value={textDraft}
      onChange={handleTextChange}
      onBlur={commitText}
      onKeyDown={handleTextKeyDown}
      onClick={(e) => e.stopPropagation()}
      onDoubleClick={(e) => e.stopPropagation()}
    />
  );

  if (kind === 'text') {
    return (
      <>
        <div
          className={`graph-generic-annotation-node kind-text${selectedClass}`}
          style={{
            color,
            fontSize: textFontSize,
            fontFamily: textFontFamily,
            display: 'flex',
            flexDirection: 'column',
            // Vertical alignment (justifyContent) has no visible effect here
            // today — `text` has no box, so it always exactly fills its own
            // content (see TEXT_ALIGN_STYLES's doc comment) — but is applied
            // anyway so this keeps rendering correctly if `text` ever gains
            // one, without this branch needing to change.
            justifyContent: textAlignStyle.justifyContent,
            alignItems: textAlignStyle.alignItems,
            ...rotation,
            ...opacityStyle,
          }}
          onDoubleClick={startEditingText}
          onContextMenu={openContextMenu}
        >
          {isEditingText ? (
            textEditor
          ) : (
            <span style={{ textAlign: textAlignStyle.textAlign }}>{data.text || ''}</span>
          )}
        </div>
        {editTrigger}
        {menu}
        {remoteBadge}
      </>
    );
  }

  if (kind === 'shape') {
    const shape = data.shape || 'rectangle';
    // Fill and border are independent and each is either a colour or
    // `'transparent'` (task-annotation-merge-frame-into-shape-rectangle).
    // `borderWidth`/`borderStyle` are always set, even at `'transparent'`, so
    // toggling a border on/off never shifts the box's rendered size — the
    // same reason a transparent border, not `border: none`, is what a
    // former `frame` (fill: 'transparent', border: <colour>) renders with.
    //
    // Known limitation, stated plainly rather than silently shipped: a CSS
    // `border` is drawn as an axis-aligned ring around the box and *then*
    // clipped by `clip-path` along with everything else — for `rectangle`
    // and `circle` (border-radius, not clip-path) that draws a correct
    // outline, but for the four clip-path variants (triangle/rhombus/
    // hexagon/process_arrow) only the parts of that ring that survive the
    // clip are visible, which is not the same as a border tracing the
    // polygon's own slanted edges. Tracing the true outline would need an
    // SVG stroke or a second, larger clip-path per variant — out of scope for
    // this merge; a border is still offered uniformly across variants rather
    // than withheld from four of six, since a plain, imperfect border is
    // strictly more useful than none.
    const fill = data.fill ?? DEFAULT_SHAPE_FILL;
    const border = data.border ?? DEFAULT_SHAPE_BORDER;
    return (
      <>
        <div
          className="graph-generic-annotation-shape-halo"
          data-testid="shape-halo"
          style={{
            width: '100%',
            height: '100%',
            position: 'relative',
            ...rotation,
            ...(selected ? SELECTED_SHAPE_HALO : null),
          }}
          onDoubleClick={startEditingText}
          onContextMenu={openContextMenu}
        >
          {/* Inside the halo (rather than a sibling of it) so the resize
              handles rotate along with it. */}
          {resizer}
          <div
            // No `selected` class: the shared dashed outline it carries is
            // clipped away on the four clipped variants and would be
            // inconsistent on the other two, so a selected shape is marked by
            // the halo above instead — for every variant alike.
            className={`graph-generic-annotation-node kind-shape shape-${shape}`}
            style={{
              backgroundColor: fill === 'transparent' ? 'transparent' : fill,
              borderColor: border === 'transparent' ? 'transparent' : border,
              borderWidth: 2,
              borderStyle: 'solid',
              width: '100%',
              height: '100%',
              ...(SHAPE_STYLES[shape] || SHAPE_STYLES.rectangle),
              ...opacityStyle,
            }}
          />
          {/* Sits on the halo (unclipped) rather than inside the shape div
              above (clipped), at the inset SHAPE_TEXT_INSET computes for
              this variant — see that constant's comment for why an inset
              text layer, not a grown shape, is the fix for the clip-path
              overflow this task reported. Only mounted while there is
              something to show/type, so an empty, untouched shape keeps no
              extra hit-testable element sitting over its resize handles. */}
          {(isEditingText || data.text) && (
            <div
              className="graph-generic-annotation-shape-text"
              style={{
                ...shapeTextInsetStyle(shape),
                justifyContent: textAlignStyle.justifyContent,
                alignItems: textAlignStyle.alignItems,
                ...opacityStyle,
              }}
            >
              {isEditingText ? (
                textEditor
              ) : (
                <span
                  className="graph-generic-annotation-shape-text-content"
                  style={{
                    fontSize: textFontSize,
                    fontFamily: textFontFamily,
                    textAlign: textAlignStyle.textAlign,
                  }}
                >
                  {data.text}
                </span>
              )}
            </div>
          )}
        </div>
        {editTrigger}
        {menu}
        {remoteBadge}
      </>
    );
  }

  if (kind === 'icon') {
    // An abbreviated name needs the smaller, uppercased treatment the glyphs
    // do not: two letters at glyph size overflow the badge.
    const icon = resolveAnnotationIcon(data.icon);
    const iconClass = icon.isGlyph ? '' : ' kind-icon-abbreviated';
    return (
      <>
        <div
          className={`graph-generic-annotation-node kind-icon${iconClass}${selectedClass}`}
          style={{ borderColor: color, ...rotation, ...opacityStyle }}
          title={data.icon}
          onContextMenu={openContextMenu}
        >
          {icon.text}
        </div>
        {editTrigger}
        {menu}
        {remoteBadge}
      </>
    );
  }

  if (kind === 'vote_dot') {
    // A plain coloured dot (task-annotation-vote-dot-simplify): no rendered
    // value, whatever a stored annotation's `value` field happens to hold —
    // there is no content field here for it to read.
    return (
      <>
        <div
          className={`graph-generic-annotation-node kind-vote_dot${selectedClass}`}
          style={{ backgroundColor: color, ...rotation, ...opacityStyle }}
          onContextMenu={openContextMenu}
        />
        {editTrigger}
        {menu}
        {remoteBadge}
      </>
    );
  }

  if (kind === 'image') {
    const url = data.image?.url;
    if (!url) {
      return (
        <>
          <div
            className="graph-generic-annotation-rotate-wrap"
            style={{ width: '100%', height: '100%', position: 'relative', ...rotation }}
          >
            {resizer}
            <div
              className={`graph-generic-annotation-node kind-image kind-image-empty${selectedClass}`}
              style={opacityStyle}
              onContextMenu={openContextMenu}
            >
              {data.alt || ''}
            </div>
          </div>
          {editTrigger}
          {menu}
          {remoteBadge}
        </>
      );
    }
    return (
      <>
        <div
          className="graph-generic-annotation-rotate-wrap"
          style={{ width: '100%', height: '100%', position: 'relative', ...rotation }}
        >
          {resizer}
          <img
            className={`graph-generic-annotation-node kind-image${selectedClass}`}
            src={url}
            alt={data.alt || ''}
            style={{ width: '100%', height: '100%', ...opacityStyle }}
            onContextMenu={openContextMenu}
          />
        </div>
        {editTrigger}
        {menu}
        {remoteBadge}
      </>
    );
  }

  return null;
}

// The right-click property editor's portal content, split out only so the
// five kind branches above can each attach it without repeating its JSX.
// Rotation and layer controls show for every EDITABLE_KINDS member, the
// layer row then the "Nearby object menu" section then duplicate then
// Delete, matching the note/label/line/freehand menus. The colour swatches
// show for COLORABLE_KINDS; `shape` instead gets its own independent Fill and
// Border sections (task-annotation-merge-frame-into-shape-rectangle) — each
// a FILL_BORDER_SWATCHES grid including `'transparent'`, the setting that
// subsumes what the retired `frame` kind was. The nine-position alignment
// grid, font-size picker and curated font-family picker
// (task-annotation-text-alignment-and-font) show for EDITABLE_TEXT_KINDS
// (`text`, `shape`), the shape-subtype grid only for `kind === 'shape'`, the
// icon-name grid only for `kind === 'icon'`. `vote_dot` is a plain coloured
// dot (task-annotation-vote-dot-simplify) — it gets only the shared colour
// swatches, rotation and layer sections, no kind-specific section of its
// own. A locked annotation gets none of them — the
// capability baseline is "a locked object remains selectable but offers only
// unlock or copy" — so this shows only the unlock and duplicate actions
// instead.
function ContextMenuPortal({
  menuRef,
  position,
  sheet = false,
  sheetContainer = null,
  id,
  data,
  kind,
  shape,
  icon,
  color,
  fill,
  border,
  rotation,
  locked,
  labels,
  textAlign,
  fontSize,
  font,
  opacity,
  onChangeShape,
  onChangeIcon,
  onChangeColor,
  onChangeFill,
  onChangeBorder,
  onChangeLayer,
  onChangeRotation,
  onChangeTextAlign,
  onChangeFontSize,
  onChangeFont,
  onChangeOpacity,
  onDelete,
  onUnlock,
  onDuplicate,
  onAttachNearby,
  onEnterAttachMode,
  onDetach,
}) {
  // The contextual "Edit" surface's mobile-sheet path
  // (task-annotation-responsive-bottom-toolbox): the container may not have
  // mounted yet even though `sheet` is true (the host's next render supplies
  // it — see useAnnotationEditTrigger's own doc comment), so there is
  // nothing to portal into until then.
  const handleMenuKeyDown = useAnnotationMenuKeyNav(menuRef);
  // Which property group's panel is open, or null for none. One at a time:
  // two open panels would overlap each other on a pointer-positioned menu,
  // and the bar exists to keep the surface small.
  const [openGroup, setOpenGroup] = useState(null);
  const portalTarget = sheet ? sheetContainer : document.body;
  if (!portalTarget) return null;
  const menuClassName = `graph-annotation-context-menu${sheet ? ' sheet' : ''}`;
  const menuStyle = sheet ? undefined : { left: position.x, top: position.y };
  if (locked) {
    return createPortal(
      <div ref={menuRef} className={menuClassName} style={menuStyle} onKeyDown={handleMenuKeyDown}>
        <button type="button" className="context-menu-unlock" onClick={onUnlock}>
          🔓 {labels.unlock}
        </button>
        <AnnotationDuplicateControl labels={labels} onDuplicate={onDuplicate} />
      </div>,
      portalTarget
    );
  }
  // The compact property bar (task-annotation-compact-property-bar). Every
  // section that used to stack vertically under its own visible heading is now
  // one trigger in a horizontal row, opening its controls in a panel on
  // demand. See AnnotationMenuGroup for why. The sections' own markup is
  // unchanged inside each panel — only the chrome around them moved.
  const alignPreview = TEXT_ALIGN_STYLES[textAlign] || TEXT_ALIGN_STYLES['top-left'];
  const toggleGroup = (next) => setOpenGroup(next);
  // A group trigger's accessible name is the ONLY name it has — the bar has
  // no room for a visible caption — so it must never be empty. The host
  // always supplies these (GraphCanvas's annotation labels, translated in
  // App.jsx); the English fallback covers a caller that passes a partial
  // label set, which would otherwise leave an unnamed button in an
  // icon-only row. Same convention AnnotationToolbox uses for its own labels.
  const gl = (key, fallback) => labels[key] || fallback;
  return createPortal(
    <div
      ref={menuRef}
      className={`${menuClassName} graph-annotation-context-menu--bar`}
      style={menuStyle}
      onKeyDown={handleMenuKeyDown}
    >
      {COLORABLE_KINDS.has(kind) && (
        <AnnotationMenuGroup
          groupKey="color"
          label={gl('color', 'Color')}
          glyph="●"
          swatch={color}
          open={openGroup === 'color'}
          onToggle={toggleGroup}
        >
          <div className="context-menu-colors">
            {GENERIC_COLORS.map((c) => (
              <button
                key={c}
                type="button"
                className={`color-button${color === c ? ' active' : ''}`}
                style={{ backgroundColor: c }}
                aria-label={c}
                onClick={() => onChangeColor(c)}
              />
            ))}
          </div>
        </AnnotationMenuGroup>
      )}
      {kind === 'shape' && (
        <>
          <AnnotationMenuGroup
            groupKey="fill"
            label={gl('fill', 'Fill')}
            glyph="◼"
            swatch={fill ?? 'transparent'}
            open={openGroup === 'fill'}
            onToggle={toggleGroup}
          >
            <div className="context-menu-colors">
              {FILL_BORDER_SWATCHES.map((c) => (
                <button
                  key={`fill-${c}`}
                  type="button"
                  className={`color-button${
                    c === 'transparent' ? ' color-button-transparent' : ''
                  }${fill === c ? ' active' : ''}`}
                  style={c === 'transparent' ? undefined : { backgroundColor: c }}
                  aria-label={`${labels.fill} ${c === 'transparent' ? labels.transparent : c}`}
                  onClick={() => onChangeFill(c)}
                />
              ))}
            </div>
          </AnnotationMenuGroup>
          <AnnotationMenuGroup
            groupKey="border"
            label={gl('border', 'Border')}
            glyph="◻"
            swatch={border ?? 'transparent'}
            open={openGroup === 'border'}
            onToggle={toggleGroup}
          >
            <div className="context-menu-colors">
              {FILL_BORDER_SWATCHES.map((c) => (
                <button
                  key={`border-${c}`}
                  type="button"
                  className={`color-button${
                    c === 'transparent' ? ' color-button-transparent' : ''
                  }${border === c ? ' active' : ''}`}
                  style={c === 'transparent' ? undefined : { backgroundColor: c }}
                  aria-label={`${labels.border} ${c === 'transparent' ? labels.transparent : c}`}
                  onClick={() => onChangeBorder(c)}
                />
              ))}
            </div>
          </AnnotationMenuGroup>
        </>
      )}
      {EDITABLE_TEXT_KINDS.has(kind) && (
        <>
          <AnnotationMenuGroup
            groupKey="textAlign"
            label={gl('textAlign', 'Text alignment')}
            // The trigger previews the CURRENT alignment using the same dot
            // the nine options draw, so the bar shows where the text sits
            // without opening anything.
            glyph={
              <span
                className="align-picker-dot"
                style={{
                  justifyContent: alignPreview.justifyContent,
                  alignItems: alignPreview.alignItems,
                }}
              >
                <span className="align-picker-dot-mark" />
              </span>
            }
            open={openGroup === 'textAlign'}
            onToggle={toggleGroup}
          >
            <div className="context-menu-align">
              {TEXT_ALIGN_VALUES.map((pos) => {
                const [vertical, horizontal] = pos.split('-');
                const ariaLabel = `${labels[ALIGN_LABEL_KEYS[vertical]]} ${labels[ALIGN_LABEL_KEYS[horizontal]]}`;
                return (
                  <button
                    key={pos}
                    type="button"
                    className={`align-picker-button${textAlign === pos ? ' active' : ''}`}
                    aria-label={ariaLabel}
                    title={ariaLabel}
                    onClick={() => onChangeTextAlign(pos)}
                  >
                    <span
                      className="align-picker-dot"
                      style={{
                        justifyContent: TEXT_ALIGN_STYLES[pos].justifyContent,
                        alignItems: TEXT_ALIGN_STYLES[pos].alignItems,
                      }}
                    >
                      <span className="align-picker-dot-mark" />
                    </span>
                  </button>
                );
              })}
            </div>
          </AnnotationMenuGroup>
          <AnnotationMenuGroup
            groupKey="textSize"
            label={gl('textSize', 'Text size')}
            glyph="A"
            open={openGroup === 'textSize'}
            onToggle={toggleGroup}
          >
            <div className="context-menu-sizes">
              {GENERIC_TEXT_FONT_SIZES.map((s) => (
                <button
                  key={s}
                  type="button"
                  className={`size-button${fontSize === s ? ' active' : ''}`}
                  style={{ fontSize: Math.min(s, 18) }}
                  onClick={() => onChangeFontSize(s)}
                >
                  A
                </button>
              ))}
            </div>
          </AnnotationMenuGroup>
          <AnnotationMenuGroup
            groupKey="font"
            label={gl('fontFamily', 'Font')}
            glyph="Aa"
            open={openGroup === 'font'}
            onToggle={toggleGroup}
          >
            <div className="context-menu-fonts">
              <button
                type="button"
                className={`font-button${!font ? ' active' : ''}`}
                onClick={() => onChangeFont(null)}
              >
                {labels.fontDefault}
              </button>
              {GENERIC_FONT_FAMILIES.map((family) => (
                <button
                  key={family}
                  type="button"
                  className={`font-button${font === family ? ' active' : ''}`}
                  style={{ fontFamily: family }}
                  onClick={() => onChangeFont(family)}
                >
                  {labels[FONT_FAMILY_LABEL_KEYS[family]] || family}
                </button>
              ))}
            </div>
          </AnnotationMenuGroup>
        </>
      )}
      {kind === 'shape' && (
        <AnnotationMenuGroup
          groupKey="shape"
          label={gl('shape', 'Shape')}
          glyph={
            <span
              className={`shape-picker-swatch shape-${shape}`}
              style={SHAPE_STYLES[shape] || SHAPE_STYLES.rectangle}
            />
          }
          open={openGroup === 'shape'}
          onToggle={toggleGroup}
        >
          <div className="context-menu-shapes">
            {SHAPE_NAMES.map((name) => (
              <button
                key={name}
                type="button"
                className={`shape-picker-button${shape === name ? ' active' : ''}`}
                aria-label={name}
                title={name}
                onClick={() => onChangeShape(name)}
              >
                <span
                  className={`shape-picker-swatch shape-${name}`}
                  style={SHAPE_STYLES[name] || SHAPE_STYLES.rectangle}
                />
              </button>
            ))}
          </div>
        </AnnotationMenuGroup>
      )}
      {kind === 'icon' && (
        <AnnotationMenuGroup
          groupKey="icon"
          label={gl('icon', 'Icon')}
          glyph={resolveAnnotationIcon(icon).text}
          open={openGroup === 'icon'}
          onToggle={toggleGroup}
        >
          <div className="context-menu-icons">
            {ICON_NAMES.map((name) => (
              <button
                key={name}
                type="button"
                className={`icon-picker-button${icon === name ? ' active' : ''}`}
                aria-label={name}
                title={name}
                onClick={() => onChangeIcon(name)}
              >
                {resolveAnnotationIcon(name).text}
              </button>
            ))}
          </div>
        </AnnotationMenuGroup>
      )}
      <AnnotationMenuGroup
        groupKey="rotation"
        label={gl('rotation', 'Rotation')}
        glyph="⟳"
        open={openGroup === 'rotation'}
        onToggle={toggleGroup}
      >
        <div className="context-menu-rotate">
          <button
            type="button"
            className="rotate-button"
            aria-label={labels.rotateLeft}
            onClick={() => onChangeRotation(rotation - ROTATE_STEP)}
          >
            ⟲
          </button>
          <button
            type="button"
            className="rotate-button rotate-reset"
            aria-label={labels.rotateReset}
            onClick={() => onChangeRotation(0)}
          >
            {Math.round(rotation)}°
          </button>
          <button
            type="button"
            className="rotate-button"
            aria-label={labels.rotateRight}
            onClick={() => onChangeRotation(rotation + ROTATE_STEP)}
          >
            ⟳
          </button>
        </div>
      </AnnotationMenuGroup>
      <AnnotationMenuGroup
        groupKey="opacity"
        label={gl('opacity', 'Opacity')}
        glyph="◐"
        open={openGroup === 'opacity'}
        onToggle={toggleGroup}
      >
        <AnnotationOpacityControl
          labels={labels}
          opacity={opacity}
          onChangeOpacity={onChangeOpacity}
        />
      </AnnotationMenuGroup>
      {/* Non-drag alternative to the NodeResizer handles `shape`/`image`
          render above — task-annotation-accessible-shared-controls.
          `text`/`icon`/`vote_dot` have no explicit box (RESIZABLE_KINDS
          excludes them; see this component's own doc comment), so nothing
          renders for those kinds — matching what the drag handles already
          do (or rather, do not) offer them. */}
      {RESIZABLE_KINDS.has(kind) && (
        <AnnotationMenuGroup
          groupKey="size"
          label={gl('size', 'Size')}
          glyph="⤢"
          open={openGroup === 'size'}
          onToggle={toggleGroup}
        >
          <AnnotationSizeControl id={id} data={data} labels={labels} />
        </AnnotationMenuGroup>
      )}
      <AnnotationMenuGroup
        groupKey="layer"
        label={gl('layer', 'Layer')}
        glyph="≡"
        open={openGroup === 'layer'}
        onToggle={toggleGroup}
      >
        <AnnotationLayerControls labels={labels} locked={locked} onChangeLayer={onChangeLayer} />
      </AnnotationMenuGroup>
      {/* Duplicate, the two attachment actions and "Add nearby" are one-shot
          commands rather than settings, so they share a single overflow group
          instead of each spending a slot in the bar. Delete stays out of it,
          directly reachable, because it is the action a user most often opens
          this menu for. */}
      <AnnotationMenuGroup
        groupKey="actions"
        label={gl('moreActions', 'More actions')}
        glyph="⋯"
        open={openGroup === 'actions'}
        onToggle={toggleGroup}
      >
        <NearbyObjectMenuSection labels={labels} onAttach={onAttachNearby} />
        {/* Non-drag "Attach to…" target-tap mode
            (task-annotation-accessible-shared-controls) — offered only for the
            kinds ATTACHABLE_OVERLAY_KINDS actually names (`text`, `icon` here;
            `label` gets the identical pair in its own component). Unlike the
            "Add nearby" section above (creates a NEW pre-attached annotation),
            this attaches THIS existing one. */}
        {ATTACHABLE_OVERLAY_KINDS.has(kind) && onEnterAttachMode && (
          <button type="button" className="context-menu-attach" onClick={onEnterAttachMode}>
            🧷 {labels.attachTo}
          </button>
        )}
        {ATTACHABLE_OVERLAY_KINDS.has(kind) && data?.attachment && (
          <button type="button" className="context-menu-attach" onClick={onDetach}>
            {labels.detach}
          </button>
        )}
        <AnnotationDuplicateControl labels={labels} onDuplicate={onDuplicate} />
      </AnnotationMenuGroup>
      <button
        type="button"
        className="context-menu-delete"
        aria-label={gl('delete', 'Delete')}
        title={gl('delete', 'Delete')}
        onClick={onDelete}
      >
        🗑️
      </button>
    </div>,
    portalTarget
  );
}

export default memo(GenericAnnotationNode);
