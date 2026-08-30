/**
 * Annotation helpers shared by GraphCanvas.
 *
 * Overlays (note/label/arrow) are free-floating annotation nodes. Groups are a
 * separate, pre-existing annotation kind with their own containment/parenting
 * logic, so they are tracked apart from these overlays.
 */

// text/shape/icon/vote_dot/image/freehand are the rest of the v1
// annotation model (docs/ANNOTATION_CONTRACT.md) that isn't note/label/
// arrow/group. text/shape/icon/vote_dot/image render through
// GenericAnnotationNode — a simple, non-interactive visual representation
// rather than dedicated per-type UX like NoteNode. freehand renders through
// its own FreehandAnnotationNode (an SVG path, like ArrowNode) but still
// shares this generic envelope-field handling.
//
// `frame` used to be a member of this set: a plain box with no fill, drawn
// by the exact same generic machinery as `shape`. task-annotation-merge-
// frame-into-shape-rectangle folded it into `shape` as one merged kind with
// independent fill/border settings (each transparent-or-coloured) — a
// `shape` with `fill: 'transparent'` now covers what `frame` used to draw.
// `frame` is no longer a recognised annotation kind at all; a stored `frame`
// annotation from before this change is dropped while normalising
// (annotationModel.js's `createAnnotation` throws for an unknown type, and
// `normalizeAnnotationDocument` skips rather than propagates that — see its
// doc comment) and never reaches this file.
export const GENERIC_OVERLAY_TYPES = new Set([
  'text',
  'shape',
  'icon',
  'vote_dot',
  'image',
  'freehand',
]);
export const OVERLAY_TYPES = new Set(['note', 'label', 'arrow', ...GENERIC_OVERLAY_TYPES]);
export const ANNOTATION_TYPES = new Set([
  'group',
  'note',
  'label',
  'arrow',
  ...GENERIC_OVERLAY_TYPES,
]);

// Default box size (px) for a generic overlay that carries explicit
// dimensions (frame/shape/image) but wasn't given a size.
const DEFAULT_GENERIC_SIZE = { w: 160, h: 96 };

// Natural on-canvas size (px) of the two fixed-intrinsic-size kinds that a
// one-click toolbox creation can usefully default to — matches the CSS box
// GenericAnnotationNode.css draws for `.kind-icon`/`.kind-vote_dot` (these
// two constants are the single JS-side source of that size; the CSS value is
// the visual source of truth and duplicates it, since the two can't share a
// value directly). `text` has no natural fixed size (it grows with content),
// so it gets no equivalent constant here.
export const ICON_INTRINSIC_SIZE = { w: 32, h: 32 };
export const VOTE_DOT_INTRINSIC_SIZE = { w: 24, h: 24 };

// The v1 types that may bind to a node/annotation via `content.attachment`
// (docs/ANNOTATION_CONTRACT.md's "Attachment and detach behavior"). `line`
// attaches per-endpoint (`start`/`end`) instead, via its own mechanism
// (startAnchor/endAnchor below), not this field.
//
// `vote_dot` used to be a member of this set. task-annotation-vote-dot-
// simplify removed it: a vote dot is now a plain coloured dot with no
// attachment behaviour of its own — it never snaps to or follows a target,
// and lives entirely on its own once dropped. No migration was written for a
// vote_dot already stored with an `attachment` field (nobody used the
// annotation feature yet — the same 2026-08-25 owner direction
// task-annotation-tolerate-unexpected-data's docstring cites); it is simply
// never read as one, since every reader of this set (GraphCanvas.jsx's
// drop-to-attach and attachment-follow effects, `computeDroppedAttachment`'s
// callers) is keyed on membership here, not on whether the field happens to
// be present.
export const ATTACHABLE_OVERLAY_KINDS = new Set(['text', 'label', 'icon']);

// Per-kind payload fields carried on a generic overlay's `data`, beyond the
// shared id/type/position/style. Drives both overlayToFlowNode and its
// inverse so the two stay exact mirrors of each other.
//
// `textAlign`/`font` (task-annotation-text-alignment-and-font) are new on
// both `text` and `shape`; `fontSize` is new on `shape` (`text` already had
// it, carried since before this task but never actually rendered — see
// GenericAnnotationNode.jsx). All three are optional and, left unset, resolve
// to the default GenericAnnotationNode.jsx computes per kind, so an existing
// annotation with none of them keeps rendering exactly as it did before this
// task — the same "omitted field = default" contract `color`/`rotation`
// already follow.
//
// `shape` carries `fill`/`border` instead of a single `color`
// (task-annotation-merge-frame-into-shape-rectangle): each is independently
// either a colour or the literal string `'transparent'`, so a shape can be a
// solid fill with no border (the old plain-shape look), a transparent fill
// with a coloured border (what `frame` used to draw), both, or neither.
// `opacity` (0-1, task-annotation-responsive-bottom-toolbox's edit-surface
// half) is appended to every one of these five kinds' field lists — it was
// previously a `freehand`-only control (see that kind's own list below); an
// omitted value keeps rendering fully opaque, the same "absent field = no
// change from before this task" contract every other newly-added optional
// field here already follows.
const GENERIC_OVERLAY_FIELDS = {
  text: ['text', 'color', 'fontSize', 'textAlign', 'font', 'attachment', 'opacity'],
  // `text` here is a `shape`'s optional caption
  // (task-annotation-doubleclick-to-edit-text), not a separate annotation
  // kind — a shape with no caption keeps `text: ''`, matching every other
  // kind's empty-string default rather than an absent field.
  shape: ['shape', 'fill', 'border', 'text', 'fontSize', 'textAlign', 'font', 'opacity'],
  icon: ['icon', 'color', 'attachment', 'opacity'],
  // A vote dot is a plain coloured dot (task-annotation-vote-dot-simplify):
  // no `value` (the number it used to render and the stepper that changed
  // it are both gone) and no `attachment` (it is not in
  // ATTACHABLE_OVERLAY_KINDS above any more, so it never snaps to or follows
  // a target). A stored vote_dot carrying either field from before this
  // change simply never has it projected onto the live node — see this
  // object's own doc comment above for why that is enough, with no
  // migration, to make old data render correctly.
  vote_dot: ['color', 'opacity'],
  image: ['image', 'alt', 'color', 'opacity'],
  // `points` are node-relative (relative to the node's own `position`, the
  // stroke's anchor/first sampled point) — the same convention arrow's
  // dx/dy uses, so a plain ReactFlow drag (which only updates `position`)
  // moves the whole stroke without this layer having to touch `points`.
  // `opacity` (0-1) mirrors `color`'s convention: a top-level overlay/flow-node
  // field that the host layer (frontend/web's sessionAnnotations.js) projects
  // to/from the server annotation's `style.opacity`.
  freehand: [
    'points',
    'color',
    'strokeWidth',
    'smoothing',
    'opacity',
    'pointerType',
    'pressureSource',
  ],
};

// Generic overlay kinds that carry an explicit box size (shape/image);
// icon/vote_dot/text render at a fixed intrinsic size instead.
const SIZED_GENERIC_KINDS = new Set(['shape', 'image']);

// The kinds that draw geometry.rotation. The capability baseline names
// text/headings, labels/callouts, sticky notes, images, icons/dots and basic
// shapes (process arrow included, as a shape variant, and — since the merge —
// a shape with a transparent fill, which covers what `frame` used to be).
//
// `line` and `freehand` are the real exclusions: their geometry lives in
// endpoints and sampled points rather than in a box, so rotation there is a
// tracked gap in the acceptance matrix, not a decision this file makes.
// `group` never reaches this translation layer at all.
export const ROTATABLE_OVERLAY_KINDS = new Set([
  'note',
  'label',
  'text',
  'shape',
  'icon',
  'vote_dot',
  'image',
]);

// Inline style that draws an annotation's geometry.rotation, or an empty
// style when this kind is not rotatable or has no rotation. Applied to the
// rendered element rather than to the ReactFlow node wrapper, so drag and
// resize keep working against the unrotated bounding box.
export function rotationStyle(kind, rotation) {
  if (!ROTATABLE_OVERLAY_KINDS.has(kind)) return {};
  if (!Number.isFinite(rotation) || rotation === 0) return {};
  return { transform: `rotate(${rotation}deg)`, transformOrigin: 'center center' };
}

// Recompute a resized annotation's geometry so a rotated note/frame/shape/
// image grows along its own local axes instead of the canvas's global ones.
// NodeResizer's own math (in @reactflow/node-resizer) always operates as if
// the box were axis-aligned: it tracks the raw pointer's global flow-space
// movement and applies it straight to width/height (and, for a top/left
// handle, to position, keeping the *global* opposite corner fixed). That is
// exactly wrong for a rotated shape, whose handles are drawn (via CSS,
// rotating with the shape) at its *rotated* corners: dragging one should
// keep the shape's own opposite corner fixed in its *local* rotated frame,
// which moves in global terms as the box grows — so the shape's centre
// shifts, not just its size.
//
// `start`/`end` are `{x, y, width, height}` snapshots (flow coordinates)
// bracketing one resize gesture, taken from NodeResizer's onResizeStart/
// onResizeEnd callbacks. Which side moved (`start.x/y` vs `end.x/y`) tells us
// which corner or edge was dragged — the anchor is the opposite one. When a
// dimension did not change (a single-axis edge drag), its invert sign is
// irrelevant: the old and new local offsets on that axis are then identical
// (same width/height) and cancel out regardless of which sign is used.
//
// At rotation 0 this reduces exactly to NodeResizer's own math (the rotation
// is the identity), so it is safe to call unconditionally rather than only
// when an annotation actually carries a rotation.
export function resolveRotatedResizeGeometry({ start, end, rotation }) {
  const rad = ((rotation ?? 0) * Math.PI) / 180;
  const cos = Math.cos(rad);
  const sin = Math.sin(rad);
  const rotateVec = (v) => ({ x: v.x * cos - v.y * sin, y: v.x * sin + v.y * cos });

  const invertX = end.x !== start.x;
  const invertY = end.y !== start.y;
  const anchorLocal = (w, h) => ({ x: invertX ? w / 2 : -w / 2, y: invertY ? h / 2 : -h / 2 });

  const oldCenter = { x: start.x + start.width / 2, y: start.y + start.height / 2 };
  const anchorGlobal = {
    x: oldCenter.x + rotateVec(anchorLocal(start.width, start.height)).x,
    y: oldCenter.y + rotateVec(anchorLocal(start.width, start.height)).y,
  };
  const newAnchorOffset = rotateVec(anchorLocal(end.width, end.height));
  const newCenter = {
    x: anchorGlobal.x - newAnchorOffset.x,
    y: anchorGlobal.y - newAnchorOffset.y,
  };

  return {
    x: newCenter.x - end.width / 2,
    y: newCenter.y - end.height / 2,
    width: end.width,
    height: end.height,
  };
}

// Default text sizes (px) for note body and label text; overridable per node.
export const DEFAULT_NOTE_FONT_SIZE = 14;
export const DEFAULT_LABEL_FONT_SIZE = 16;

// Default font sizes (px) for the generic `text` kind's own text and for a
// `shape`'s caption — chosen to match the size each rendered at (hardcoded in
// GenericAnnotationNode.css) before this task made it a real, settable field,
// so an existing annotation with no stored `fontSize` renders identically to
// before.
export const DEFAULT_GENERIC_TEXT_FONT_SIZE = 16;
export const DEFAULT_SHAPE_CAPTION_FONT_SIZE = 14;

// The size options the generic `text`/`shape` font-size picker offers —
// deliberately the same shape as NOTE_FONT_SIZES/LabelNode's own list (a
// short, fixed set rather than free numeric entry), widened a little at the
// top end since `text` also serves as a free-standing heading, not only a
// caption.
export const GENERIC_TEXT_FONT_SIZES = [12, 14, 16, 18, 24, 32];

// Curated font-family choices for the generic `text`/`shape` kinds
// (task-annotation-text-alignment-and-font's FONT SCOPE decision): a short,
// fixed list of CSS *generic* font families rather than free-form font-name
// entry. Every one of these is a CSS Fonts Level 3 generic family name,
// resolved by the viewer's own browser/OS to whatever it already has
// installed for that generic — so rendering is predictable and consistent
// across every client with no font files to ship, at the cost of not
// offering a specific named typeface. `undefined`/absent means "no override"
// — the annotation keeps inheriting the app's own ambient font, exactly as
// every `text`/`shape` annotation already renders today, so this list has no
// "default" entry of its own; the control that clears the override is a
// separate action, not a member of this list.
export const GENERIC_FONT_FAMILIES = ['serif', 'monospace', 'cursive'];

// The nine box positions `textAlign` accepts, row-major (top row, middle row,
// bottom row) — the order a 3x3 alignment-grid picker lays its buttons out in.
export const TEXT_ALIGN_VALUES = Object.freeze([
  'top-left',
  'top-center',
  'top-right',
  'middle-left',
  'middle-center',
  'middle-right',
  'bottom-left',
  'bottom-center',
  'bottom-right',
]);

// The default `textAlign` per kind when none is stored — chosen to exactly
// match how each kind already renders with no alignment field at all, so an
// existing annotation is unaffected by this task adding the field:
// `text` has no box (it hugs its own content — GenericAnnotationNode.jsx's
// RESIZABLE_KINDS excludes it) and, with no CSS alignment rule at all today,
// lays out top-left by plain block-flow default; `shape`'s caption is drawn
// centred in its inset box today (GenericAnnotationNode.css's now-removed
// hardcoded `align-items/justify-content: center`).
export const TEXT_ALIGN_DEFAULT_BY_KIND = Object.freeze({
  text: 'top-left',
  shape: 'middle-center',
});

// Maps a `textAlign` value to the flexbox properties (for a column-direction
// flex container: `justifyContent` is the vertical position, `alignItems` the
// horizontal one) plus the `textAlign` CSS needed so multi-line/wrapped
// content aligns the same way *within* its own box, not only the box itself
// within its container. Exported so GenericAnnotationNode.jsx and its tests
// share one source rather than re-deriving the mapping.
//
// Honest limitation: `shape` always has an explicit box (SIZED_GENERIC_KINDS),
// so all nine positions are visibly distinct there. `text` has none — its box
// always exactly matches its own content (no NodeResizer, no stored width) —
// so the vertical component (`justifyContent`) has no visible effect for
// `text` today: with nothing but the content itself inside the box, "top"
// and "bottom" are literally the same pixel position. The horizontal
// component (`alignItems`/`textAlign`) IS visible for `text` whenever the
// content spans multiple lines (typed line breaks), since those lines can be
// narrower than the widest one. Making `text` itself resizable — the only way
// to make its vertical alignment mean something — is a separate, bigger UX
// change (the same call 61d5cc7b already made for icon/vote_dot), not part
// of this task.
const FLEX_START = 'flex-start';
const FLEX_END = 'flex-end';
const CENTER = 'center';
export const TEXT_ALIGN_STYLES = Object.freeze({
  'top-left': { justifyContent: FLEX_START, alignItems: FLEX_START, textAlign: 'left' },
  'top-center': { justifyContent: FLEX_START, alignItems: CENTER, textAlign: 'center' },
  'top-right': { justifyContent: FLEX_START, alignItems: FLEX_END, textAlign: 'right' },
  'middle-left': { justifyContent: CENTER, alignItems: FLEX_START, textAlign: 'left' },
  'middle-center': { justifyContent: CENTER, alignItems: CENTER, textAlign: 'center' },
  'middle-right': { justifyContent: CENTER, alignItems: FLEX_END, textAlign: 'right' },
  'bottom-left': { justifyContent: FLEX_END, alignItems: FLEX_START, textAlign: 'left' },
  'bottom-center': { justifyContent: FLEX_END, alignItems: CENTER, textAlign: 'center' },
  'bottom-right': { justifyContent: FLEX_END, alignItems: FLEX_END, textAlign: 'right' },
});

// Flow distance (px, unscaled) within which an arrow endpoint snaps onto a
// node/annotation centre. Kept generous so connecting is easy (design intent).
export const SNAP_RADIUS = 40;

export function isManualNode(node) {
  return node.type === 'group' || node.id.startsWith('group-') || OVERLAY_TYPES.has(node.type);
}

// An arrow with either endpoint bound to a node/annotation should not be
// dragged as a whole — its anchored ends must stay on their targets. Only its
// endpoint handles move it. Free arrows drag normally.
export function isArrowAnchored(data) {
  return Boolean(data?.startAnchor || data?.endAnchor);
}

// Whether another live client currently holds an *edit lease* on this
// annotation (task-annotation-exclusive-edit-leases, deciding
// dec-mcp-agent-ops-vs-annotation-claimmap: edit leases are genuinely
// exclusive — first-actual-editor-wins, refused rather than taken over — as
// opposed to the advisory, last-write-wins selection claim `data.
// remoteSelection` carries; this now reads a *different* field on purpose).
// GraphCanvas's remote-lease effect stamps `data.remoteLease` from the sync
// client's live lease map (`sessionSyncClient.getRemoteLeases()`), populated
// only when the other client actually started editing (opened a text field,
// began a geometry gesture, opened a property editor, started a bulk
// mutation/undo) — never on mere selection. This is the single read-side
// check every annotation component uses to refuse a local edit while
// someone else holds the lease. Distinct from `data.locked` (a persisted,
// geometry-only edit lock a user sets deliberately) — a remote lease clears
// itself (release, completion or 30s TTL expiry) the moment the other
// client lets go, and is unrelated to `data.remoteSelection`, which stays a
// purely cosmetic "who has this selected" marker with no bearing on
// whether an edit is allowed.
export function isRemoteLocked(data) {
  return Boolean(data?.remoteLease);
}

// The marker to render as the collaborator badge/outline: the id of whoever
// is actively *editing* this annotation when there is one (more specific and
// more urgent to show than a mere selection), else whoever merely has it
// selected. Both are `{ clientId, color, displayName }` or null/undefined —
// see `data.remoteLease` and `data.remoteSelection` above. Centralises what
// every annotation component's own badge JSX would otherwise re-derive
// identically six times.
export function remoteEditBadge(data) {
  return data?.remoteLease || data?.remoteSelection || null;
}

// Whether an annotation should currently accept a plain ReactFlow drag,
// combining its persisted lock, a live remote edit lease, and — for arrows
// only — whether either endpoint is anchored to a target (anchored arrows
// move only via their endpoint handles, never as a whole). Drives
// GraphCanvas's remote-lease effect, which takes the `false` verbatim but
// maps a `true` for a group to `undefined`, so the group keeps deferring to
// the canvas-wide `nodesDraggable` switch instead of overriding it.
// `overlayToFlowNode` computes the locked/anchor-only half of this itself at
// hydration time, when no remote lease can yet exist.
export function isAnnotationDraggable(node) {
  const data = node?.data;
  if (Boolean(data?.locked) || isRemoteLocked(data)) return false;
  if (node?.type === 'arrow') return !isArrowAnchored(data);
  return true;
}

// Build a ReactFlow node for a note/label/arrow overlay from the host's
// canvas-shape annotation ({id, kind, position, ...payload}).
export function overlayToFlowNode(overlay) {
  // Null and id-less input are the two shapes this cannot make a node out of.
  // Returning null rather than throwing lets the caller drop one bad
  // annotation instead of failing the whole hydration — annotations may be
  // redesigned without migrating what is stored, so unrecognised input is an
  // expected condition here, not an exceptional one.
  if (!overlay || typeof overlay !== 'object') return null;
  if (typeof overlay.id !== 'string' || !overlay.id) return null;
  const base = { id: overlay.id, type: overlay.kind, position: overlay.position || { x: 0, y: 0 } };
  // `z` (layer order) and `locked` (the canvas UI's own edit-lock convention,
  // set via the generic MCP annotation tools) are envelope fields on every v1
  // annotation type. They must round-trip through the ReactFlow node — a flow
  // node that silently dropped them would, on the next autosave, diff back out
  // as an `annotation_updated` that resets a collaborator's/agent's `z`/`locked`
  // to their defaults, overwriting the very change realtime sync just delivered.
  const locked = Boolean(overlay.locked);
  const zIndex = overlay.z ?? 0;
  // Rotation is an envelope field for the same reason as z/locked: a flow node
  // that dropped it would diff back out on the next autosave as a rotation
  // reset, silently overwriting whatever an agent or collaborator had set.
  const rotation = overlay.rotation ?? 0;
  // `version`/`field_versions` are server-owned same-field-conflict bookkeeping
  // (dec-annotation-field-patches-and-conflicts), carried the same envelope way
  // as z/locked/rotation rather than defaulted — an annotation that has never
  // round-tripped through the server (a brand-new local creation) legitimately
  // has neither yet, and inventing 0/{} here would make a later diff think the
  // server had already assigned one. Read-only from this file's point of view:
  // nothing here ever changes them, only carries whatever was last read.
  const version = overlay.version;
  const fieldVersions = overlay.field_versions;
  if (overlay.kind === 'note') {
    return {
      ...base,
      data: {
        text: overlay.text || '',
        color: overlay.color,
        fontSize: overlay.fontSize,
        opacity: overlay.opacity,
        locked,
        rotation,
        version,
        field_versions: fieldVersions,
      },
      style: overlay.size
        ? { width: overlay.size.w, height: overlay.size.h }
        : { width: 200, height: 140 },
      draggable: !locked,
      zIndex,
    };
  }
  if (overlay.kind === 'label') {
    return {
      ...base,
      data: {
        text: overlay.text || '',
        color: overlay.color,
        fontSize: overlay.fontSize,
        attachment: overlay.attachment,
        opacity: overlay.opacity,
        locked,
        rotation,
        version,
        field_versions: fieldVersions,
      },
      draggable: !locked,
      zIndex,
    };
  }
  if (GENERIC_OVERLAY_TYPES.has(overlay.kind)) {
    const data = { locked, rotation, version, field_versions: fieldVersions };
    for (const field of GENERIC_OVERLAY_FIELDS[overlay.kind]) data[field] = overlay[field];
    const node = { ...base, data, draggable: !locked, zIndex };
    if (SIZED_GENERIC_KINDS.has(overlay.kind)) {
      node.style = overlay.size
        ? { width: overlay.size.w, height: overlay.size.h }
        : { width: DEFAULT_GENERIC_SIZE.w, height: DEFAULT_GENERIC_SIZE.h };
    } else if (overlay.size) {
      // icon/vote_dot/text draw at a fixed intrinsic size (no `style` box, no
      // NodeResizer — RESIZABLE_KINDS in GenericAnnotationNode.jsx), but
      // sessionAnnotations.js now carries their geometry.w/h through its own
      // translators unconditionally, the same envelope-field treatment as
      // z/locked/rotation (smallfix-annotation-unsized-generic-geometry-clobber).
      // Without this, a live ReactFlow node had nowhere to hold that value, so
      // the browser's hydrate -> autosave round trip still dropped it even
      // after that fix. Kept in `data` (not `style`) so it never affects the
      // CSS box or resize handles — silently-preserved geometry only, not a
      // new resizable control (61d5cc7b's decision note: making these kinds
      // resizable is a separate, bigger UX change, out of scope here).
      data.size = overlay.size;
    }
    return node;
  }
  // arrow / line: endpoints carry independent head symbols and optional anchors.
  const data = {
    dx: overlay.dx ?? 160,
    dy: overlay.dy ?? 0,
    color: overlay.color,
    opacity: overlay.opacity,
    startArrow: overlay.startArrow ?? false,
    endArrow: overlay.endArrow ?? true,
    locked,
    rotation,
    version,
    field_versions: fieldVersions,
  };
  if (overlay.startAnchor) data.startAnchor = overlay.startAnchor;
  if (overlay.endAnchor) data.endAnchor = overlay.endAnchor;
  // `start`/`end` carry a line endpoint's *attachment* (docs/ANNOTATION_
  // CONTRACT.md — bind to a node or another annotation), a distinct concept
  // from the startAnchor/endAnchor GUI snap above. This is data passthrough
  // only, so a live GraphCanvas snapshot matches sessionAnnotations.js's
  // round trip (smallfix-line-endpoint-attachment-dropped-by-translator) —
  // it does not add rendering/dragging behaviour for how an attached
  // endpoint should follow its target; that stays a separate, bigger task.
  // Carried only when the attachment is actually present, same as
  // sessionAnnotations.js, so a plain arrow overlay does not grow a
  // spurious `start`/`end` field.
  if (overlay.start?.attachment) data.start = overlay.start;
  if (overlay.end?.attachment) data.end = overlay.end;
  return { ...base, data, draggable: !locked && !isArrowAnchored(data), zIndex };
}

// Serialize a ReactFlow overlay node back to the host's canvas-shape annotation.
export function flowNodeToOverlay(node) {
  if (!node || typeof node !== 'object' || typeof node.id !== 'string' || !node.id) return null;
  const base = { id: node.id, kind: node.type, position: node.position };
  // Mirrors overlayToFlowNode's envelope fields; see its comment for why these
  // must survive the round trip. `node.zIndex`/`node.data.locked` are undefined
  // on a freshly created node (never synced yet), hence the defaults below.
  const z = node.zIndex ?? 0;
  const locked = Boolean(node.data?.locked);
  const rotation = node.data?.rotation ?? 0;
  // Mirrors overlayToFlowNode's version/field_versions handling above: read
  // whatever the live node carries, default to nothing (never invent a
  // version an annotation hasn't actually been assigned server-side yet).
  const version = node.data?.version;
  const fieldVersions = node.data?.field_versions;
  if (node.type === 'note') {
    return {
      ...base,
      text: node.data?.text || '',
      color: node.data?.color,
      fontSize: node.data?.fontSize,
      opacity: node.data?.opacity,
      size: node.style ? { w: node.style.width, h: node.style.height } : undefined,
      z,
      locked,
      rotation,
      version,
      field_versions: fieldVersions,
    };
  }
  if (node.type === 'label') {
    return {
      ...base,
      text: node.data?.text || '',
      color: node.data?.color,
      fontSize: node.data?.fontSize,
      attachment: node.data?.attachment,
      opacity: node.data?.opacity,
      z,
      locked,
      rotation,
      version,
      field_versions: fieldVersions,
    };
  }
  if (GENERIC_OVERLAY_TYPES.has(node.type)) {
    const out = { ...base, z, locked, rotation, version, field_versions: fieldVersions };
    for (const field of GENERIC_OVERLAY_FIELDS[node.type]) out[field] = node.data?.[field];
    if (SIZED_GENERIC_KINDS.has(node.type) && node.style) {
      out.size = { w: node.style.width, h: node.style.height };
    } else if (node.data?.size) {
      // Mirrors overlayToFlowNode's `data.size` slot for icon/vote_dot/text —
      // see its comment. Nothing in this file ever writes to it besides
      // hydration, since these kinds have no resize UI.
      out.size = node.data.size;
    }
    return out;
  }
  const out = {
    ...base,
    dx: node.data?.dx ?? 160,
    dy: node.data?.dy ?? 0,
    color: node.data?.color,
    opacity: node.data?.opacity,
    startArrow: node.data?.startArrow ?? false,
    endArrow: node.data?.endArrow ?? true,
    z,
    locked,
    rotation,
    version,
    field_versions: fieldVersions,
  };
  if (node.data?.startAnchor) out.startAnchor = node.data.startAnchor;
  if (node.data?.endAnchor) out.endAnchor = node.data.endAnchor;
  // Mirrors overlayToFlowNode's start/end passthrough above; see its comment.
  if (node.data?.start?.attachment) out.start = node.data.start;
  if (node.data?.end?.attachment) out.end = node.data.end;
  return out;
}

// On-canvas size (flow px) of a node: ReactFlow's own measured `width`/
// `height` (set once the node has actually rendered) when available, falling
// back to an explicit `style.width`/`style.height` for a node not yet
// measured (or one — like an unmounted overlay snapshot — that never will
// be). Shared by nodeCenter below and by the multi-select align/distribute
// bounding-box math (task-annotation-render-direct-manipulation), so the two
// agree on what a node's box is.
export function nodeSize(node) {
  return {
    w: node.width || node.style?.width || 0,
    h: node.height || node.style?.height || 0,
  };
}

// Centre point (flow coords) of a node, using its measured size when available.
// Returns null when the node has no usable position.
export function nodeCenter(node) {
  const pos = node.positionAbsolute || node.position;
  if (!pos) return null;
  const { w, h } = nodeSize(node);
  return { x: pos.x + w / 2, y: pos.y + h / 2 };
}

// Find the nearest snappable node/annotation centre to `point` within `radius`.
// Arrows never snap to other arrows, nor to themselves. Returns the target's
// id or null when nothing is close enough.
export function findSnapTarget(point, nodes, { excludeId, radius = SNAP_RADIUS } = {}) {
  let best = null;
  let bestDist = radius;
  for (const n of nodes) {
    if (n.id === excludeId || n.type === 'arrow') continue;
    const c = nodeCenter(n);
    if (!c) continue;
    const d = Math.hypot(c.x - point.x, c.y - point.y);
    if (d <= bestDist) {
      bestDist = d;
      best = n.id;
    }
  }
  return best;
}

// Whether an arrow endpoint is *currently* held to a present target. An anchor
// id is preserved even while its target is absent from the view (filtered,
// collapsed, or not yet loaded) so it re-glues when the target returns — but
// while absent the arrow is not held and stays freely draggable. `existingIds`
// is the set of node ids currently rendered.
export function isArrowHeld(data, existingIds) {
  const { startAnchor, endAnchor } = data || {};
  return Boolean(
    (startAnchor && existingIds.has(startAnchor)) || (endAnchor && existingIds.has(endAnchor))
  );
}

// Recompute an arrow's geometry so its anchored endpoints sit on the current
// centres of their target nodes. `centers` maps nodeId -> {x, y}. Returns a new
// {position, dx, dy} when it differs from the arrow's current geometry, else
// null (nothing to update). Endpoints without a live anchor keep their place.
export function resolveAnchoredArrow(arrow, centers) {
  const { startAnchor, endAnchor } = arrow.data || {};
  if (!startAnchor && !endAnchor) return null;
  const pos = arrow.position;
  const dx = arrow.data?.dx ?? 160;
  const dy = arrow.data?.dy ?? 0;
  const start = (startAnchor && centers.get(startAnchor)) || { x: pos.x, y: pos.y };
  const end = (endAnchor && centers.get(endAnchor)) || { x: pos.x + dx, y: pos.y + dy };
  const newDx = end.x - start.x;
  const newDy = end.y - start.y;
  if (start.x === pos.x && start.y === pos.y && newDx === dx && newDy === dy) return null;
  return { position: { x: start.x, y: start.y }, dx: newDx, dy: newDy };
}

// Distance (px, unscaled) within which a dropped attachable overlay
// (label/text/icon) snaps onto — and stays attached to — a node or
// another annotation's centre. Looser than SNAP_RADIUS (an arrow endpoint,
// a precise point) because "attach this label to that node" is a coarser
// gesture aimed at "near this object", not a pixel-precise line endpoint.
export const ATTACH_SNAP_RADIUS = 90;

// Fixed offset (px, model space) from a target's centre that the "Nearby
// object menu" creation entry point (docs/ANNOTATION_CONTRACT.md "Human
// authoring surfaces") places a newly created, pre-wired label/icon/
// text at. Diagonal (not simply "above" or "below") so the new
// annotation doesn't sit exactly on top of the target's own centre, and its
// magnitude (~51px) is deliberately well inside ATTACH_SNAP_RADIUS so the
// annotation is attached from its very first rendered frame rather than
// merely close enough to have qualified — the same margin
// AnnotationDuplicateControl's DUPLICATE_OFFSET keeps for its own, unrelated
// "don't land exactly on top of the source" nudge.
export const NEARBY_ATTACH_OFFSET = { x: 36, y: -36 };

// Compute the attachment a dropped attachable overlay (label/text/icon)
// should carry after being released at `position`: attaches to the
// nearest node/annotation centre within ATTACH_SNAP_RADIUS, storing the drop
// point's offset from that centre so the overlay keeps exactly where it was
// dropped (the contract's "free fine adjustment") instead of jumping onto the
// centre. Returns null when nothing is close enough — the caller detaches
// (contract: "snap to the node edge ... and detach outside the snap zone").
// `group` is excluded from candidacy: the contract's Attachment section is
// explicit that a group is a "containment/visual construct, not an
// attachment target" — nothing attaches to a group, the same way nothing
// attaches to another line/arrow (findSnapTarget's own exclusion).
//
// `frame` used to be excluded here too, on the same reasoning. Now that it is
// folded into `shape` (task-annotation-merge-frame-into-shape-rectangle),
// this is a deliberate decision, not an oversight: a `shape` — whatever its
// fill/border, including a transparent-fill one that looks exactly like the
// old `frame` — stays a valid attach target, the same as any other shape.
// Eligibility here is keyed on `node.type` everywhere else in this file
// (arrow, group); making one configuration of `shape`'s own style fields
// silently opt it out of attachment would be a new, content-dependent kind of
// exclusion this codebase does not otherwise have, and a surprising one: two
// visually-similar transparent-fill shapes would attach differently depending
// on an internal field the user has no reason to remember they set. Keeping
// `shape` uniformly attachable is the smaller, more predictable change, and
// the more consistent one now that `frame` is no longer a distinct type.
// Builds the `content.attachment` shape for a point attaching to `target`,
// shared by computeDroppedAttachment (nearest-target-within-radius, for a
// drag release) and computeAttachmentToTarget (an explicit, caller-chosen
// target, for the non-drag "Attach to…" mode below) so the two attachment
// entry points can never drift into two different field shapes.
function buildAttachment(position, target) {
  const center = target && nodeCenter(target);
  if (!center) return null;
  return {
    target_id: target.id,
    target_type: ANNOTATION_TYPES.has(target.type) ? 'annotation' : 'node',
    offset: { x: position.x - center.x, y: position.y - center.y },
  };
}

export function computeDroppedAttachment(position, nodes, excludeId) {
  const candidates = nodes.filter((n) => n.type !== 'group');
  const targetId = findSnapTarget(position, candidates, { excludeId, radius: ATTACH_SNAP_RADIUS });
  if (!targetId) return null;
  const target = candidates.find((n) => n.id === targetId);
  return buildAttachment(position, target);
}

// The non-drag "Attach to…" mode's own attachment computation
// (task-annotation-accessible-shared-controls, closing the audit's
// "attaching an EXISTING annotation to a target" gap): the caller has
// already resolved an explicit target (via a target-tap/click, not a
// proximity search), so this only has to build the attachment shape for it —
// reusing buildAttachment rather than a second implementation. Keeps the
// annotation's current on-screen offset from the target's centre (the
// contract's "free fine adjustment"), the same behaviour a drop that snapped
// onto this exact target would have produced, rather than jumping the
// annotation onto the target's centre. `node` is the flow node being
// attached (its own `.position`, not the target's); `group` targets are
// rejected by the caller before this is reached (a group is a containment
// construct, not an attachment target — see computeDroppedAttachment's own
// comment), so this does not re-check it.
export function computeAttachmentToTarget(node, target) {
  const position = node?.position;
  if (!position) return null;
  return buildAttachment(position, target);
}

// Whether `node` is a valid target for the "Attach to…" mode: any node or
// annotation except a group (a containment/visual construct, not an
// attachment target — matches computeDroppedAttachment's own candidate
// filter) or the annotation being attached itself.
export function isEligibleAttachTarget(node, selfId) {
  return Boolean(node) && node.id !== selfId && node.type !== 'group';
}

// Per-kind screen-reader accessible name (dec-annotation-v1-accessibility-
// and-touch's bar: "role + accessible name per annotation, e.g. 'sticky
// note, Budget Q3'" — a name that SAYS WHAT THE THING IS, not merely
// whatever an accname algorithm happens to fall back to). One shared
// function rather than ten per-kind fixes, per the accessibility audit's own
// root-cause note (docs/ANNOTATION_CONTRACT.md's "Keyboard, touch and
// screen-reader controls audit" section): every caller that builds or
// hydrates a ReactFlow node writes this onto `node.ariaLabel`, the only field
// ReactFlow itself ever reads for a node's `aria-label` (`@reactflow/core`'s
// `NodeRenderer`).
//
// `labels` carries the kind words with English defaults, following this
// package's own props-with-defaults i18n rule (see AnnotationContext.js) —
// never a bare English literal, so a host with Swedish `labels` gets a
// Swedish accessible name too. Shape subtype names (`rectangle`, `process_
// arrow`, …) and icon names are left untranslated, matching the existing
// precedent of GenericAnnotationNode.jsx's own shape/icon picker buttons
// (`aria-label={name}`) — this file adds no new translation surface for
// vocabulary that was already untranslated elsewhere in the same menu.
export function computeAnnotationAriaLabel(kind, data, labels = {}) {
  const d = data || {};
  const withDetail = (kindWord, detail) => (detail ? `${kindWord}, ${detail}` : kindWord);
  const text = (v) => (typeof v === 'string' && v.trim() ? v.trim() : '');
  switch (kind) {
    case 'note':
      return withDetail(labels.ariaKindNote || 'Sticky note', text(d.text));
    case 'label':
      return withDetail(labels.ariaKindLabel || 'Label', text(d.text));
    case 'text':
      return withDetail(labels.ariaKindText || 'Text', text(d.text));
    case 'shape': {
      const shapeName = (d.shape || 'rectangle').replace(/_/g, ' ');
      const kindWord = `${shapeName} ${labels.ariaKindShape || 'shape'}`;
      return withDetail(kindWord, text(d.text));
    }
    case 'icon': {
      const kindWord = labels.ariaKindIcon || 'icon';
      return d.icon ? `${d.icon} ${kindWord}` : kindWord;
    }
    case 'vote_dot':
      return labels.ariaKindVoteDot || 'Vote dot';
    case 'image':
      return withDetail(labels.ariaKindImage || 'Image', text(d.alt));
    case 'arrow':
      return labels.ariaKindArrow || 'Arrow';
    case 'freehand':
      return labels.ariaKindFreehand || 'Freehand stroke';
    case 'group':
      return withDetail(labels.ariaKindGroup || 'Group', text(d.label));
    default:
      return '';
  }
}

// Recompute an attached overlay's position from its target's current centre
// plus the offset captured when it (re)attached. Returns the new {x, y}
// position when it differs from the node's current one, else null — the same
// idempotent, loop-safe contract resolveAnchoredArrow keeps despite depending
// on `nodes`. Returns null when the target is absent (filtered, collapsed, not
// yet loaded, or deleted): the overlay keeps its last resolved position rather
// than being recomputed or reset (contract: "detaches and keeps its last
// resolved model-space geometry").
// Fallback hit-box half-size (flow px) for a kind with no explicit
// style.width/height (icon/vote_dot's fixed intrinsic size, and arrow/text/
// freehand's boxless geometry) — see nodesAtPoint's own comment for why a
// zero-size box would otherwise never register as "clicked".
const POINTLESS_KIND_HIT_RADIUS = 16;

// Every node whose box contains `point` (flow coordinates) — the overlap-
// object picker's own hit test (task-annotation-accessible-shared-controls,
// closing the audit's "Overlapping objects: a visible way to choose which
// one you mean | MISSING" row). Deliberately independent of ReactFlow's own
// pointer-event hit testing (which already resolved one specific node before
// a click handler ever sees it, by DOM z-order): this recomputes candidacy
// geometrically so a caller can tell "exactly one node here" (nothing to
// disambiguate) apart from "several nodes here" (offer a picker) using the
// same `nodes` array GraphCanvas already holds.
//
// Uses each node's *stored* box (nodeSize/position), not its rotated visual
// outline — a rotated annotation's axis-aligned bounding box is a
// conservative approximation of what it actually paints, not a pixel-exact
// hit test. `group` is excluded: a group is a containment/visual backdrop
// almost always larger than whatever sits on it, so including it would
// nearly always "overlap" and defeat the picker's purpose of disambiguating
// genuinely stacked objects.
export function nodesAtPoint(nodes, point) {
  if (!point) return [];
  return nodes.filter((n) => {
    if (n.type === 'group') return false;
    const pos = n.positionAbsolute || n.position;
    if (!pos) return false;
    const { w, h } = nodeSize(n);
    if (!w || !h) {
      const r = POINTLESS_KIND_HIT_RADIUS;
      return (
        point.x >= pos.x - r && point.x <= pos.x + r && point.y >= pos.y - r && point.y <= pos.y + r
      );
    }
    return point.x >= pos.x && point.x <= pos.x + w && point.y >= pos.y && point.y <= pos.y + h;
  });
}

export function resolveAttachedPosition(node, centers) {
  const targetId = node.data?.attachment?.target_id;
  if (!targetId) return null;
  const center = centers.get(targetId);
  if (!center) return null;
  const offset = node.data.attachment.offset || { x: 0, y: 0 };
  const next = { x: center.x + offset.x, y: center.y + offset.y };
  const pos = node.position || {};
  if (pos.x === next.x && pos.y === next.y) return null;
  return next;
}
