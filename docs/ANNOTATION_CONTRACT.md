# Canvas annotation contract v1

Status: accepted contract, reconciled against the accepted v1 baseline decision.
It states what v1 must do for every annotation type across GUI, MCP,
persistence, realtime collaboration, undo and accessibility. The
[acceptance matrix](#acceptance-matrix) below is the source of truth for what
is actually built; nothing in the prose above it should be read as
downgrading a row in that matrix to a non-goal.

## Scope

The annotation model is separate from canonical graph nodes and edges. It is
durable session state, can be produced by humans or agents, and is safe to
create before a visual client connects. Session and saved-view persistence
must round-trip the same versioned document.

V1 supports these annotation types:

- `note` — sticky note
- `text` — free text / heading
- `label` — label / callout
- `line` — line / arrow (`arrow` is an accepted legacy alias)
- `group` — node-membership box
- `shape` — rectangle, circle, triangle, rhombus, hexagon or process arrow,
  selected via `content.shape`, with independent fill and border settings
  (each a colour or `"transparent"`) — see
  [Canvas rendering](#canvas-rendering) and
  [Fill and border](#fill-and-border-shape)
- `icon` — a configured icon from the icon set
- `vote_dot` — a plain colored dot (no value, not attachable — see
  task-annotation-vote-dot-simplify)
- `image` — an embedded, ingested image
- `freehand` — a freehand/stylus stroke

Existing canvas note, label, arrow and group descriptors are migrated into
the v1 model.

**`frame` is retired.** A former v1 type — a visual-only framing box with no
fill — `frame` is now folded into `shape` (task-annotation-merge-frame-into-
shape-rectangle): a `shape` with `style.fill: "transparent"` and a coloured
`style.border` covers exactly what `frame` used to draw. `frame` is no
longer a recognised annotation kind at all — not in the toolbox, not in the
generic MCP tool set, not in `ANNOTATION_TYPES`/`GENERIC_ANNOTATION_TYPES` on
either side of the stack. No migration was written for annotations already
stored with kind `frame`: nobody used the annotation feature yet (owner
direction 2026-08-25, the same one that retired the backward-compatibility
requirement — see [Unrecognised annotation
data](#unrecognised-annotation-data)), so a stored `frame` is simply an
unrecognised kind now, dropped while normalising like any other, never
reaching the canvas. It does not crash — that guarantee is exactly what
task-annotation-tolerate-unexpected-data built, and
`AnnotationBadData.test.jsx`/`annotationModel.test.js` pin a stored `frame`
specifically as a regression case, not just "some unknown kind".

**`vote_dot` is simplified.** task-annotation-vote-dot-simplify removed two
things a vote dot used to carry: the `value` it counted (both the number
rendered inside the dot and the right-click stepper that changed it) and its
membership in the attachable kinds (it no longer binds to a node/annotation
via `content.attachment`, is never offered from the "nearby object menu", and
never follows a target). What remains is exactly a plain coloured dot: the
same right-click colour picker every colourable generic kind has (see
[Human authoring surfaces](#human-authoring-surfaces)), now drawn with a
fixed black ring and drop shadow regardless of its fill colour
(`GenericAnnotationNode.css`'s `.kind-vote_dot`) so it stays legible against
any canvas background. As with `frame` above, this is authorized with no
migration for annotations already stored with a `value` and/or `attachment`
(the same 2026-08-25 owner direction, recorded on the task node): both
translator legs (`GENERIC_OVERLAY_FIELDS.vote_dot` in
`packages/ui-graph-canvas/src/utils/annotations.js`,
`genericAnnotationToOverlay`/`genericOverlayToAnnotation` in
`frontend/web/src/utils/sessionAnnotations.js`) and the object-model builder
(`annotationModel.js`'s `withTypePayload`) simply no longer project either
field onto a live node, so a stored `value`/`attachment` is inert data that
is never read as one — not a crash risk, and not a case
`AnnotationBadData.test.jsx` leaves to the general "unknown field" coverage
to imply holds: it is pinned as an explicit regression case of its own.

GUI authoring (creation and editing through the desktop/mobile canvas) and
MCP/headless authoring are **both** required v1 surfaces for every type
above — neither is a fallback for the other, and this contract does not
accept a v1 where a type only ships one of the two. Where a type does not
yet have both today, that is a tracked gap in the acceptance matrix, not an
accepted scope reduction.

## Document shape

```json
{
  "schema_version": 1,
  "annotations": []
}
```

Each annotation has:

- `id`: stable string id
- `type`: one of the v1 types
- `kind`: compatibility alias equal to `type`, except legacy inputs may use `arrow`
- `geometry`: model-space x/y/width/height plus optional rotation
- `position`: compatibility x/y projection
- `style`: visual style fields such as fill, stroke, color, opacity, font
  size/family and text alignment
- `z`: layer order
- `locked`: object lock flag
- `created_by`, `updated_by`, `created_at`, `updated_at`: optional attribution
- type-specific payload fields (`content`), including `attachment` and, for
  `line`, `start`/`end` endpoint descriptors

## Human authoring surfaces

Every v1 type must be creatable and editable from the GUI, not only from
MCP. The required entry points are:

- **Desktop collapsible bottom toolbox** — the primary creation surface.
  Collapsed by default to a slim strip; expands to show every v1 type
  grouped by family (notes/text, shapes, connectors, marks, media).
  The toolbox selects a TOOL rather than creating immediately
  (task-annotation-tool-modes): activating a type arms it, and each
  subsequent click/tap on empty canvas creates one instance of that type at
  the pointed-at position, until a different tool is armed. `select` (plain
  selection/drag/marquee) and `eraser` are tools in the same row, and
  Escape returns to `select`. Drag-to-create from the toolbox is unchanged
  and still places a single object without arming anything. `freehand`
  is sticky too: it used to disarm itself after one stroke, which meant
  lifting the pen and pressing again panned the canvas instead of drawing the
  next line, with nothing on screen saying the tool had gone.
- **Keyboard activation still creates.** Arming is a pointer contract — the
  gesture that completes it is a pointerdown/pointerup on the pane — so a
  keyboard activation (a real Enter/Space keydown, which also preventDefaults
  the click the browser would synthesize from it — not an `event.detail`
  sniff, which misreads ordinary programmatic clicks) creates at the viewport
  centre instead of arming. The mode tools have nothing to create, so for
  those the key still arms. Routing it through arming would leave a
  keyboard user with a live tool and no way to place anything, removing the
  only keyboard route to a standalone annotation.
- **Drag to draw.** For the kinds with a real box (`shape`, `note`) the press
  fixes one corner and the drag sizes the other, with a live preview. A
  subtype with a regular ratio is re-proportioned to it (`regularShapeSize`),
  because `NodeResizer`'s `keepAspectRatio` locks whatever ratio the node
  measures at drag start — writing a swept box verbatim would cement a
  distorted figure. Dragging left or up sets `flipX`/`flipY`, which is the
  only way to aim a directional variant; both are carried under `style` on
  the wire and mirror the drawn figure only, never its caption or its resize
  handles.
- **Eraser** — dragging over an annotation deletes it; dragging over a graph
  node or edge HIDES it. The eraser must never delete graph data: a
  dragged-over node is far too easy to hit for the destructive reading to be
  acceptable. A stylus's inverted tip (`pointerType === 'eraser'`) erases
  regardless of which tool is armed, since flipping the pen over is itself
  the gesture.
- **Nearby object menu** — a contextual menu anchored to an existing
  object (node or annotation) offering the attachable types (label/callout,
  icon, text/heading, arrow) pre-wired to attach to that object.
- **Mobile bottom sheet** — the touch equivalent of the toolbox: a sheet
  that slides up from the bottom, same type grouping, sized for thumb reach.
  Reached from its own **Annotate** slot in the phone bottom navigation
  (`frontend/web/src/components/MobileShell.jsx`), participating in the
  app's shared "at most one mobile bottom surface open at a time" system
  (`useSurfaceManager`) alongside Search/Create/Chat/Menu — not a
  second, uncoordinated floating strip on top of that navigation. It hosts
  the exact same `AnnotationToolbox` component and creation handlers as the
  desktop toolbox (`GraphCanvas`'s `annotationToolboxPortalContainer` prop
  portals it into the sheet's content, the cross-package equivalent of
  `FloatingToolbar`'s own `variant="sheet"` for graph-node creation), laid
  out with `variant="sheet"` — always expanded, no collapse toggle, larger
  grid cells — rather than the narrower always-visible compact strip
  `compact` alone produces on a non-integrated host: a `GraphCanvas`
  consumer that hasn't wired `annotationToolboxPortalContainer` (currently
  `frontend/widget`, which mounts `GraphCanvas` directly with no
  `MobileShell` to portal into) still gets that compact strip, rendered
  inline, on a narrow viewport — never no toolbox at all.
- **Contextual Edit surface** (task-annotation-responsive-bottom-toolbox,
  2026-08-30) — the entry point for editing an annotation that already
  exists, as distinct from the three surfaces above, which all create one.
  Selecting a single annotation shows a small **✎ Edit** button on it (`note`,
  `label`, `arrow`, `freehand`, and every `GenericAnnotationNode` kind —
  `text`/`shape`/`icon`/`vote_dot`/`image`; `group` is out of scope, see
  below); activating it — click, tap, or Tab then Enter/Space — opens the
  same property editor `onContextMenu` (right-click, or a long-press that
  synthesizes it) already opens, which keeps working unchanged alongside it.
  The long-press path accepts `pointerType` **`pen`** as well as `touch`
  (task-annotation-pen-long-press): a stylus reports `pen`, so restricting the
  detector to `touch` left a pen-first device — which has no right-click of
  its own — with no route to any annotation's menu at all. A pen is admitted
  regardless of the host's coarse-pointer signal, since a hybrid device driven
  by pen can legitimately report a fine pointer; mouse still never long-presses.

  For the `GenericAnnotationNode` kinds the editor is a **compact property
  bar** (task-annotation-compact-property-bar): one small trigger per
  property, each carrying that property's current value, opening its controls
  in a panel on demand — one panel at a time. It replaces the single tall
  column of labelled sections, which on a `shape` exceeded a phone's screen
  height, covered the object being edited, and needed scrolling to reach its
  own lower controls. Delete stays directly in the bar; the one-shot commands
  (duplicate, add-nearby, attach/detach) share an overflow group. The controls
  inside each panel are unchanged — only how many taps it takes to see them.
  Desktop and a compact host with no `MobileShell` (e.g. `frontend/widget`)
  get that same editor as a floating menu anchored to the button; a compact,
  integrated host portals it into the shared mobile bottom sheet instead —
  the pre-existing `'detail'` surface `useSurfaceManager` had reserved but
  left unused (`frontend/web/src/hooks/useSurfaceManager.js`) — via
  `GraphCanvas`'s new `annotationEditSheetPortalContainer`/
  `onRequestAnnotationEditSheet`/`onCloseAnnotationEditSheet` props, the
  edit-time counterpart of `annotationToolboxPortalContainer` above. One
  shared hook, `hooks/useAnnotationEditTrigger.js`, drives the button and the
  floating-vs-sheet decision for every kind alike, rather than five
  hand-copied implementations — it does not touch the property-editing UI
  itself (each kind's own menu body, and the mutation handlers behind it,
  are the exact same code the right-click path already called). Opening via
  the button also moves focus into the menu and restores it to the button on
  close, scoped to that one entry path — the pre-existing right-click path is
  unaffected. `group`'s own Edit affordance, arrow-key navigation *within*
  the open menu, a full focus *trap*, and generalising focus-move/-restore to
  the right-click path too all remain open — see the [accessibility
  audit](#keyboard-touch-and-screen-reader-controls-audit-v1-accessibility-baseline)'s
  "Update, 2026-08-30" note for the exact boundary. Also new alongside this
  surface: an **Opacity** row (four levels — 30/50/75/100%) on every one of
  those same kinds' menus, previously offered only by `freehand`.

  **Update, 2026-08-30 — on-screen keyboard avoidance.** The mobile Edit
  sheet reuses the shared `BottomSheet` primitive
  (`frontend/web/src/components/BottomSheet.jsx`), which is
  `position: fixed` and therefore sized against the *layout* viewport by
  mobile browsers, not the smaller *visual* viewport a keyboard leaves
  behind — until this fix, focusing a text/number field inside the sheet
  (a note's rename input, `AnnotationSizeControl`'s width/height inputs,
  etc.) could leave it rendered behind the keyboard rather than above it.
  `BottomSheet` now tracks the gap via the existing
  `useVisualViewportInset` hook and applies it as a `--keyboard-inset`
  CSS custom property that shrinks the sheet's scrim to
  the actually-visible area, plus a focus-triggered `scrollIntoView` so a
  field switched to while the keyboard is already open, or the field that
  triggered the keyboard opening in the first place, is brought into view
  within the sheet's own scrollable content. Degrades to a no-op wherever
  `visualViewport` is unavailable, same as `useVisualViewportInset` itself.
  Covers every `BottomSheet` consumer (Search/Create/Annotate/Edit/Chat),
  not only the Edit surface. `ChatPanel`'s sheet variant previously had its
  own, separate `useVisualViewportInset`-driven margin on the composer for
  the same problem; once `BottomSheet` itself started shrinking its scrim,
  the two stacked and roughly doubled the gap above the keyboard on the
  Chat sheet, so `ChatPanel`'s own mechanism was removed in favor of
  `BottomSheet`'s. Test: `BottomSheet.test.jsx`'s "on-screen keyboard
  avoidance" cases and `ChatPanel.sheetVariant.test.jsx`'s nested-composition
  regression test.

### Desktop wireframe

```
┌───────────────────────────────────────────────────────────────┐
│                          canvas                                │
│                                                                  │
│                                          ┌──────────────────┐   │
│                                          │ nearby object menu│   │
│                            (node) ───────┤  + label          │   │
│                                          │  + icon           │   │
│                                          │  + text            │   │
│                                          │  + arrow            │   │
│                                          └──────────────────┘   │
│                                                                  │
├───────────────────────────────────────────────────────────────┤
│ ▲ toolbox (collapsed)                                            │
├───────────────────────────────────────────────────────────────┤
│ ▼ toolbox (expanded)                                             │
│  [note] [text] [label]   [shape ▾] [line] [group]                │
│  [icon ▾] [vote dot ▾]   [image]  [freehand]                     │
└───────────────────────────────────────────────────────────────┘
```

### Mobile wireframe

```
┌───────────────────────┐
│         canvas         │
│                         │
│                         │
│                         │
├─────────────────────────┤
│   ▔▔▔ (grab handle) ▔▔▔  │  ← bottom sheet, collapsed to a handle
├─────────────────────────┤
│  note   text   label     │
│  shape  line   group      │
│  icon   vote dot          │
│  image  freehand          │
└─────────────────────────┘
```

**Current gap:** `note`, `label`, `line` and `group` have GUI creation today
(dedicated toolbar/toolbox actions). The bottom toolbox also creates `text`
and `shape` in every variant `content.shape` accepts — rectangle,
circle, triangle, rhombus, hexagon and process arrow — each of which now
renders as its own distinct visual. `image` also has GUI creation now
(toolbox file picker, clipboard paste, and OS file drop — all through the
same server-side ingest MCP uses). `freehand` now has GUI creation too: the
toolbox's "Freehand" item arms a one-shot drawing mode (single stroke, then
auto-disarms) that captures actual pointer samples — coalesced samples via
`getCoalescedEvents()` when the browser provides them, device pressure via
`event.pressure` when available, never predicted events — into a vector
stroke (`createFreehandStrokeCapture`,
`packages/ui-graph-canvas/src/utils/freehandStroke.js`, wired into
`GraphCanvas.jsx`). `icon` and `vote_dot` now have a bottom-toolbox/mobile-sheet
creation entry point, each with a fixed default (a generic glyph, and a value
of 1) — GraphCanvas's `createAnnotation` also gives each a data-only
`geometry.w/h` matching its fixed intrinsic rendered size (32×32 / 24×24), so
that geometry survives the session save round trip (see
[Persistence](#persistence) and
`61d5cc7b`/`smallfix-annotation-unsized-generic-geometry-clobber`) instead of
being silently reset to a mismatched box on the next reload. A right-click
property editor now exists for every rotatable kind (`note`, `label`, `text`,
`shape`, `icon`, `vote_dot`, `image`): a rotation control (±15° steps
plus reset), a colour picker for the kinds that paint one (`text`,
`icon`, `vote_dot` — `image` carries a `color` in the model but
renders none, so it is offered no swatches), independent Fill and Border
swatch sections for `shape` (see [Fill and border](#fill-and-border-shape)),
for `shape` also a subtype picker, and for
`icon` a picker grid over the full icon vocabulary. `vote_dot` used to
also get a value stepper here; task-annotation-vote-dot-simplify removed it
along with the `value` field itself — a vote dot's editor is now just the
shared colour swatches, rotation and layer sections, same as any other
colourable kind with no kind-specific control of its own; for `text` and
`shape` (whose caption
is now editable — see below), the right-click editor also carries a
nine-position text-alignment grid, a font-size picker and a curated
font-family picker (task-annotation-text-alignment-and-font — see [Typography
controls](#typography-controls-text-shape)). `freehand` gets its own
right-click property editor too (color, stroke width, smoothing, opacity) —
it is not in the rotatable set (see [Canvas rendering](#canvas-rendering) on
why rotation is not drawn for it), so it has no rotation control. The
dedicated `note`/`label`/`line`/`freehand` editors and the generic one alike
also carry the shared bring-to-front/send-to-back layer row described under
[Layer order](#layer-order), and — new as of task-annotation-responsive-
bottom-toolbox — a shared **Opacity** row (four levels: 30/50/75/100%),
previously `freehand`-only; `group`'s own context menu has neither. Every one
of these menus — right-click, or now the [Edit button](#human-authoring-
surfaces) — is reachable without a mouse. What the editors still do not
cover: `note`/`label` still have only a text-size
picker, not the alignment/font-family control `text`/`shape` now have, and
cropping/replacing an `image`'s pixel content is still unsupported.
`label`, `text` and `icon` can now also be attached to a node or
another annotation from the GUI, by dragging the annotation within snapping
distance of the target
([Attachment and detach behavior](#attachment-and-detach-behavior)).
`vote_dot` used to be a fourth member of this list; task-annotation-vote-dot-
simplify retired its attachment behaviour entirely (it is not in
`ATTACHABLE_OVERLAY_KINDS` any more — `packages/ui-graph-canvas/src/utils/
annotations.js`) — a vote dot never snaps to or follows a target and always
lives on its own once dropped, with no migration for one already stored with
an `attachment` field (see [Unrecognised annotation
data](#unrecognised-annotation-data)'s vote_dot paragraph). The
"nearby object menu" (the wireframe above) now exists too, as a **"Add
nearby"** section on the context menu of any eligible node or annotation
(a graph node, or any annotation except `group`/`arrow` — the same
target candidacy `findSnapTarget` already applies to a post-creation drop via
`computeDroppedAttachment`; a `shape` is included regardless of its
fill/border, including a transparent-fill one — see [Fill and
border](#fill-and-border-shape)): picking `label`, `icon` or `text`
there creates that annotation pre-wired to attach to the object whose menu it
was opened from, offset a small, fixed distance from its centre (well inside
the drag-to-attach snap radius), so it is attached and following its target
from its very first rendered frame — no separate create-then-drag-near step.
It is written through the exact same `content.attachment` shape and
resolve/follow mechanism the drag-to-attach path uses (`GraphCanvas.jsx`'s
`attachNearbyAnnotation`, reusing `createAnnotation`), not a second, parallel
one, so a menu-created attached annotation behaves identically to one created
and dragged near afterward. `arrow`/`line` is deliberately not offered as a
*creatable* kind from this menu: an arrow's own selected endpoints already
give it a creation-adjacent snap-and-drag docking affordance (see [Attachment
and detach behavior](#attachment-and-detach-behavior)), which is what this
entry point closes for the three kinds that had no equivalent at creation
time. A new annotation created this way is never locked, matching every
other creation path. The menu is offered as an *anchor* from every eligible
object's own context menu — a graph node, and every annotation kind except
`group`/`arrow` (`note`, `label`, `shape`, `icon`, `vote_dot`,
`image`, `freehand`) — matching `findSnapTarget`'s full target candidacy
(the same exclusion set `computeDroppedAttachment` and `findSnapTarget`'s own
arrow-to-arrow guard apply), not only the three attachable kinds — a
`vote_dot` may still be the *target* an unrelated label/icon/text attaches
near, it just cannot itself be one of the kinds this menu creates any more.
`arrow` itself is excluded from the anchor side too, not only the creatable-kind
side: an arrow has no stable centre in the attachment-follow effect (an
attached overlay's position is resolved from its target's centre, and arrows
are skipped when that lookup is built), so an annotation attached to one
would never move with it and would silently drop its attachment on the very
next drag-triggered recompute. `ArrowNode`'s own context menu therefore does
not render this section at all. Closing what remains is tracked per type in
the [acceptance matrix](#acceptance-matrix); it is not satisfied by
documenting the wireframes above.

### Layer order

An annotation's `z` orders it against the other annotations on the canvas.
The `note`, `label`, `line`, `freehand` and generic-kind context menus each
carry the same layer row (`AnnotationLayerControls`), offering **bring to
front** and **send to back**. `group` has its own context menu and does *not*
carry that row — it carries a separate one, described in [Group background
layer order](#group-background-layer-order) below, that reorders a group
against other groups only, never against these five kinds.

The same five menus (not `group`) also carry a **duplicate** action, covered
below alongside the locked-menu correction it makes to this section's
history. `group` is excluded there for a sharper reason than the layer row's:
a group's substance is its membership (`member_node_ids`), tracked via
ReactFlow `parentId` on its member nodes, not via the annotation's own
content — a "duplicate" that copied only the empty box would look like data
loss, not a copy, and a duplicate that also cloned the member graph nodes and
their edges is an entirely different, undefined feature, not a variant of
this one. Tracked as `smallfix-group-annotation-has-no-duplicate-control`,
alongside the group's still-open layer-row question.

The value written is always an **integer**, even when annotations already on
the canvas carry fractional `z` values (an agent may set any float over MCP —
`z` is `Optional[float]` server-side). The canvas writes a node's layer
straight into the element's inline style, and CSS `z-index` accepts only
`auto | <integer>`: a browser rejects `z-index: 0.5` outright and the element
silently keeps whatever it had. A fractional layer would publish an operation
and move nothing on screen — worse than refusing, because every other client
would still apply it. Note that a jsdom-based test cannot catch this: jsdom's
CSSOM accepts the fractional value that a real browser discards.

Front/back rather than a one-step forward/back is a deliberate trade. A true
one-step swap needs distinct integer levels to step between, and every
annotation but `shape` is created at `z = 0` (see [Semantic default
layers](#semantic-default-layers) below for the one exception), so the common
case is still a pile of ties — now two piles, one at 0 and one at `shape`'s
own -1, rather than one. Breaking those ties one step at a time means
renumbering the annotations around the one that moved — and an
`annotation_updated` op carries the *whole* annotation, which for an embedded
image is its entire data URI. A renumber touching a few images would exceed
the session op batch's byte cap and be rejected atomically. Front/back always
writes exactly one annotation, so it cannot reach that cap. One-step
forward/back, and the level compaction it would need, are not implemented.

An annotation tied with the current front is not treated as already in front —
the tie is what the click exists to break — so it moves. A click that would
change nothing (already alone at the front, or at the back) is a no-op and
publishes no operation, and so is a click on an annotation another client
currently holds the edit lease on (the attempt is surfaced, as for every
other annotation mutation). A `locked` annotation is not offered the layer row at
all — the row's own `locked` check (`AnnotationLayerControls`) withholds
it independently of any one caller's markup, same as before.

The other half of the capability baseline's "remains selectable but offers
only unlock or copy" is now real: every *overlay* annotation's menu —
`note`, `label`, `line`, `freehand` and the generic kinds, `group` excepted
(below) — offers a **duplicate** action (`AnnotationDuplicateControl`,
`useAnnotationDuplicate`) in *both* its locked and unlocked branches, not
only the row's own layer arithmetic. Duplicating never mutates the locked
source (it reads it and writes a new id), so locking has nothing to protect
by withholding it the way it withholds Layer/Delete/recolour — a locked
object's menu is Unlock plus Duplicate, exactly the pair the baseline names,
closing the gap an earlier revision of this document recorded as
`smallfix-contract-layer-order-overclaims-copy-on-locked-menu` (this
sentence used to say the opposite — that no GUI duplicate action existed at
all — until it shipped). The copy itself is never locked regardless of the
source's `locked` value: `useAnnotationDuplicate` forces `locked: false`
unconditionally on the new node, rather than relying on the invariant that a
locked source's binding was already dropped at lock time
(dec-annotation-lock-semantics point 2) — the same locked-with-a-live-binding
state PR #515/#517 closed for `locked` itself is made structurally
impossible here too, independent of whether that upstream invariant holds.
Everything else on the copy (attachment, anchors, colour, size, rotation,
`z`) is carried over verbatim, offset by a small fixed nudge
(`DUPLICATE_OFFSET`, 24px) so it lands next to its source rather than
exactly on top of it. The canvas `Delete`/`Backspace` handler enforces the
locked/Delete half of the baseline too: a locked overlay is skipped and the
user is told to unlock it first, which closes the one path that could
destroy a locked *overlay* without unlocking it. Both rows are kind-agnostic,
so neither needs a per-component change.

`group` now honours the flag, and follows the baseline exactly: **a locked
group's menu offers Unlock and nothing else.** It honoured it nowhere until
its translators started carrying `locked`: the flag was persisted server-side
(`create_group_annotation` takes it) but dropped in `annotationsToGroups` on
the way to the canvas, so `GroupNode` never saw it and a locked group box
still showed its full colour/hide/delete menu with no way back out of the
lock. A locked group's resize handles and drag are now withheld the same way a
locked overlay's are, its label cannot be renamed by double-clicking the
header, and its colour swatches and Delete Group are out of reach. Nothing
else is left to withhold: those are the whole of the unlocked menu, so the
locked branch carries Unlock alone rather than by exception. The rename guard
goes one step further than the overlay kinds, whose
double-click text editors still refuse only a live remote edit lease and not
the persisted flag — the group's behaviour is the one this baseline describes, and
the overlays' is a tracked gap. The keyboard rule still does not reach a group
— `Delete` skips them entirely, so their children stay correctly parented —
and hands that job to the group's own menu, which now honours the flag.

**There is no longer a Hide Group action, on a locked group or any other.**
It is worth recording why, because the button existed for a long time and its
name will outlive it in people's memory. `handleHideGroup` and
`handleDeleteGroup` called the identical handler, `removeGroupKeepChildren`,
which takes the group off the canvas, un-parents its members and publishes
`notifyChange('delete')`; there was no hidden-group state anywhere in the
codebase to restore from, and none of the round-trip legs carried one. So the
menu offered two buttons that read as different severities and ran the same
destruction, and a locked group — which withheld everything else — was one
click from being dissolved by the safe-sounding one.

The fix was to delete the button rather than to make it real. Deleting a group
already keeps its members, so `Delete Group` is the action `Hide Group`
purported to be, correctly named; removing the duplicate cost no capability.
`handleDeleteGroup` is now the sole path into `removeGroupKeepChildren`.
Whether a group should ever be hideable *separately* from being deleted — with
its identity, label, colour and membership preserved for restoration — is an
open product question, not a bug, and is tracked as one.

A real hole remains, and it is not about the menu: **a locked group's
membership is not locked.** Dragging a graph node into or out of a locked
group re-parents it (`computeGroupPlacement` in `GraphCanvas.jsx` never
consults the flag) and publishes `group_membership_changed`, so the locked
annotation's own `member_node_ids` changes from the GUI. What a group *is* is
largely its membership, so "a locked group is protected" is true of its box
and false of its contents. Tracked separately; stated here so this section's
account of what the flag reaches is not read as more than it claims.

`group` now has a layer control of its own — narrower than
[Layer order](#layer-order) above by design; see [Group background layer
order](#group-background-layer-order) immediately below for the mechanism
(`dec-annotation-group-background-layering`, resolved by
`smallfix-group-annotation-has-no-layer-control`).

### Group background layer order

`dec-annotation-group-background-layering` (founder-accepted 2026-08-30)
draws a hard line: a group background **always** stays behind every graph
node and every other annotation kind. The **only** thing a user or agent may
change is the order of group backgrounds **relative to each other**, when two
or more groups exist. This section is the mechanism that decision resolved
to, replacing the "has not been taken" note this paragraph used to carry.

**Behind everything is structural, not a `z` comparison.** A group's paint
order was already array order, not `z` (`reorderNodesForParentChild` in
`GraphCanvas.jsx` places every group node ahead of every non-group node in the
ReactFlow array, unconditionally — see [Layer order](#layer-order)'s original
description of this, unchanged). `GroupNode.css` backs that with
`.react-flow__node-group { z-index: -1 !important }` against
`.react-flow__node-custom { z-index: 1 !important }`, so even if two nodes
ever landed at the same array position a group could not paint over regular
content. Nothing described below touches either of these — no group-vs-group
write is ever compared against a `shape`'s `z`, a graph node, or any other
kind, and no write can move a group out of the groups-first bucket. That is
deliberate: the follow-up task's own brief was to keep a group behind every
`shape` position a user could reach by hand (including `shape`'s own `-1`
[semantic default](#semantic-default-layers) and anywhere bring-to-front/
send-to-back can move it), in both directions, unconditionally — a numeric
comparison against `shape`'s `z` could not promise that once `shape` moves
outside its default; array-order bucketing can, because it never depends on
what number either side holds.

**Group-to-group order reuses the group's own `z`.** `group` already carried
a `z` field that round-tripped end to end (`create_group_annotation`,
`annotationsToGroups`/`groupsToAnnotations` in
`frontend/web/src/utils/sessionAnnotations.js`) but was never read — this is
what now reads it, and only for this one purpose. `reorderNodesForParentChild`
sorts the groups bucket it already builds by each group's own `data.z`
(ascending — a lower `z` paints earlier, i.e. further back among group
backgrounds), stable on ties via an explicit index tie-break so the common
case — every group still at the shared default `z = 0`, nobody having touched
the new control — keeps exactly the relative order the function already
produced before this sort existed. No new envelope field, and no new
plumbing on either translator leg:
`packages/ui-graph-canvas/src/utils/annotations.js` never touches `group` at
all (it is a different kind's translator — see its own module comment), and
`z`/`locked` already flowed through both `annotationsToGroups`/
`groupsToAnnotations` (`frontend/web/src/utils/sessionAnnotations.js`) before
this task, verified rather than assumed as part of it.

**The control.** `GroupNode`'s context menu now carries a "Group order"
section, two buttons: bring this group forward among groups, or send it
backward among groups (`GROUP_LAYER_FRONT`/`GROUP_LAYER_BACK`,
`utils/groupLayers.js`). `resolveGroupOrderZ` mirrors `resolveLayerZ`'s
arithmetic and no-op rules exactly (integer strictly past every other value;
a tie with the current front is not already in front; both CSS-safe-integer
bounds checked in both directions) but over the groups-only `data.z` space —
it never reads a `zIndex`, never reads a non-group node's `z`, and returns
`null` (a silent no-op, publishing nothing) when there is nothing to order
the group against: the only group on the canvas, or already alone at that
end among groups. Always rendered rather than conditionally hidden below two
groups, the same convention `AnnotationLayerControls` already uses. One write:
only the clicked group's `data.z` changes; every other group's `data`, and
every member's `parentId`/position/`z`, are untouched — `reorderNodesForParentChild`
reorders the *array*, it does not rewrite any node's fields beyond the
one the click targeted.

**Locks and leases, the same pattern as every other layer control.** The row
is entirely absent from a locked group's menu (which offers Unlock alone, as
before); `handleChangeGroupLayer` refuses independently as the hook-level
backstop, the same two-layer discipline `AnnotationLayerControls`/
`useAnnotationLayer` already establishes: a live remote edit lease
(`isRemoteLocked`) refuses and surfaces the attempt via
`notifyRemoteLockedAttempt`; the persisted `locked` flag refuses silently,
since the menu already explains itself with Unlock. No new guard was
invented — this reuses the existing pattern rather than adding a third one.

**Persistence.** Every path that writes a group node already funnels through
`reorderNodesForParentChild` (local create, this control's own click, the
`upsert-group` remote op, the `groupsToRestore` saved-view/session-restore
effect, delete/membership ops) — see [Layer order](#layer-order)'s original
description of the function. The click's own write is exactly the shape
`handleSaveView`'s existing `z: g.data.z ?? 0` re-emission and the
`upsert-group` remote-op handler already carry, so it reaches a saved view, a reload
and every other connected client through paths that already existed and
already round-tripped `z`/`locked` for `group` — nothing new was added to
either leg to make that true.

**Reachable today from the group's own context menu only.** A second,
related work item — a toolbox-hosted contextual Edit sheet reachable from an
ordinary tap, not only right-click — was landing on a separate lane in the
same round this control shipped in and had not yet merged when it did.
Wiring "Group order" into that surface once it lands is a follow-up, not a
redesign: the surface would call the same `resolveGroupOrderZ`/
`reorderNodesForParentChild` path this section describes, the same way the
sheet is expected to reuse `AnnotationLayerControls`' `useAnnotationLayer` for
every other kind rather than reimplementing layer arithmetic a second time.

**Rectangular `shape` annotations are unaffected.** This decision and this
section are about group-vs-group order only. `shape`'s own free layering
(manual bring-to-front/send-to-back, and its `-1` semantic default at
creation) is untouched — see [Layer order](#layer-order) and [Semantic
default layers](#semantic-default-layers).

### Multi-select align and distribute

A right-click multi-selection's context menu (`MultiNodeContextMenu`) offers
two bulk actions beyond the pre-existing multi-select Delete
(task-annotation-render-direct-manipulation's remaining_scope): **Align**
(left / horizontal centers / right / top / vertical middles / bottom) and
**Distribute** (equal-gap horizontal / vertical spacing, offered only at 3+
eligible members — the minimum for a gap to be meaningful). Both are computed
from each member's real on-canvas bounding box (`node.width`/`height`, the
same measured-size fallback `nodeCenter` already used, now factored out as
`nodeSize` in `utils/annotations.js`), not a fixed layout grid — unlike
`arrangeNodes`' cell-based Organize modes, an Align/Distribute result depends
on what is actually selected, so two differently-sized boxes end up flush,
not merely evenly spaced by index.

Unlike Organize (graph nodes only), the eligible set spans graph nodes *and*
overlay annotations — the same "mixed selection" the multi-select Delete
already applies to. Two exclusions are deliberate, both narrower than what
Delete excludes:

- **`arrow` is excluded**, the same way a graph edge is (per the task's own
  scope: "edges themselves have no independent position, so they simply move
  with their connected nodes"). An arrow's on-canvas geometry is a pair of
  connected endpoints (`position` + `dx`/`dy`), not an independently movable
  box, so there is nothing for "align its left edge" to mean that isn't
  already covered by aligning whatever it's anchored to (or, for a free
  arrow, by dragging an endpoint directly). `group` stays excluded too, the
  same as Delete — its own context menu is where its box, as opposed to its
  members, is manipulated.
- **A currently-attached label/text/icon is excluded**, even though it is
  neither locked nor leased. The pre-existing attachment-follow effect
  (`ATTACHABLE_OVERLAY_KINDS`, above) re-glues such an overlay to its
  target's centre on every `nodes` change, unconditionally — moving it here
  would be undone by that effect on the very next render, a fight between
  two mechanisms that would show up as jitter rather than the alignment
  asked for. It is left out of the move set and simply follows if its own
  attachment target happens to move as part of the same selection.

The locked/remote-leased exclusion itself is unchanged from Delete's own
rule (a locked or another-client-leased overlay is skipped, not refused for
the whole selection — "the more forgiving of the two options", matching
delete's own choice recorded above), and the same priority order Delete's
own notice uses when a selection mixes skip reasons: a remote lease's notice
wins over a plain lock's, which wins over the attached-item notice, since a
remote lease is the one the user cannot resolve alone. (Before
task-annotation-exclusive-edit-leases this exclusion fired on a mere remote
*selection*; it now fires only once another client actually starts editing
the annotation — a bystander's selection no longer removes anything from the
eligible set.)

Every position this produces is written through the exact publish paths a
drag or Organize already use — `onNodePositionChange` for a graph node
(same callback `onNodeDragStop`/`applyPositionMoves` call), `onAnnotationChange('geometry')`
once for any annotation moved (same notifier the attach-follow effects and
`onNodeDragStop`'s own attach/detach branch use) — and one undo/redo entry
per action via the same `useCanvasHistory` record Organize uses, so **Ctrl/Cmd+Z**
reverses an Align/Distribute exactly like it reverses a drag or an Organize.
Neither action gained its own keyboard shortcut (Organize's **Ctrl/Cmd+O** is
unaffected); that is a deliberate scope cut, not an oversight — see the PR
that introduced this section.

### Unrecognised annotation data

Annotations have no users yet, so the shapes below may change without migrating
what is already stored: a redesign is free to ignore or discard an existing
annotation rather than carry a compatibility path for it. What replaces that
guarantee is narrower and firmer — **unrecognised annotation data must never
crash the canvas.**

Annotations render inside the same ReactFlow tree as the graph, so an exception
thrown while drawing one unmounts everything. An annotation is a decoration and
the graph is the user's work, so that trade is never worth taking. Three
mechanisms at three depths, deliberately all of them — each catches what the
one below it cannot reach:

- **An annotation that cannot be normalised is skipped, not fatal.**
  `createAnnotation` throws for a kind this version does not know, and
  `normalizeAnnotationDocument` used to let that escape — so one stored
  annotation of a retired kind made the whole document unreadable, which in the
  app means the session fails to open. That is the worst outcome available: the
  user loses the session rather than one decoration. Skipped entries are
  reported to the console, since a silent drop looks like deletion. A payload
  whose annotation slot is not a list at all is still fatal — that is a
  malformed session, not an annotation this version cannot read.
- **The overlay translators refuse what they cannot represent.**
  `overlayToFlowNode` and `flowNodeToOverlay` return `null` for input that is
  not an object or has no string id, and all three call sites — session
  restore, saved-view export, and remote ops from a peer or agent — drop those
  rather than letting a `null` into the node list, where it would crash one
  step later and harder to trace.
- **Every annotation node type is wrapped in `AnnotationErrorBoundary`.** A kind
  that throws anyway renders as a small neutral placeholder the user can select
  and delete. The user-facing notice fires once per session — several copies of
  the same broken shape would otherwise read as something being badly wrong —
  while each failure is logged to the console separately, for whoever has to
  find the defect.

Groups are annotations too, and get the same treatment in both places: a
malformed entry in a restored session's group list is filtered before it can
throw on `g.id` inside an effect — where no boundary reaches — and a primitive
entry is refused rather than silently becoming a group annotation with a
generated id and no members.

An unknown kind therefore never reaches the canvas at all: it is dropped while
normalising, and the restore path filters by the known overlay kinds besides.
The translators tolerate one because they are also used on already-built nodes,
not because a stored unknown kind survives to be rendered.

`custom` — a graph node — is deliberately **not** wrapped. Its data is the
user's real work, carries no licence to change shape, and a render failure
there should be loud rather than hidden behind a placeholder.

This is covered by `packages/ui-graph-canvas/tests/AnnotationBadData.test.jsx`,
which feeds the canvas deliberately malformed annotations. That test is what
makes every later annotation redesign safe to do without a migration.

All of this is client-side. The server never rejects a write to a locked
annotation, whatever the type or tool — see [MCP access](#mcp-access) for the
per-type breakdown of which tool does the writing. `locked` is a shared UI
convention, not a permission, and reads as a stronger guarantee than it is
precisely because the menus and the keyboard now enforce it so uniformly.
Contrast an edit lease, which the server does enforce for browser writes.

Only other *annotations* are consulted: the ordering is computed against
them, and no graph node is ever read to decide the result. That is not the
same as staying within the graph's own band, and should not be read as such.
Graph nodes carry no layer of their own, so they sit at 0 alongside every
annotation kind except a freshly created `shape` (see [Semantic default
layers](#semantic-default-layers) below — `shape` now starts one level behind
that 0 on its own), while send-to-back writes one below the backmost
annotation. Whenever that backmost annotation is itself at or below 0 — the
default for every kind but `shape`, since every kind but `shape` is still
created at 0 — the result is negative and does place the annotation behind
the graph's nodes and edges. That is intended and useful, and it is how a
`shape` with a transparent fill (standing in for the retired `frame`) gets
behind the nodes it frames; it is not, however, a guarantee. Once every
annotation has been pushed above 0, send-to-back lands at 0 or higher — level
with the graph (where paint order falls back to document order) or in front
of it, but no longer behind it.

A layer is only ever written when it is an integer strictly past every other
annotation's *and* inside the signed 32-bit range CSS `z-index` accepts, so
the layer that is stored is the layer the browser actually paints. Against a
neighbour already at that bound the click is a no-op: clamping the step back
down to the bound would land level with the neighbour it is meant to pass,
recreating the tie the control exists to break while publishing an operation
that changes nothing on screen.

### Semantic default layers

A per-kind default `z` at creation time (task-annotation-render-direct-
manipulation's remaining scope) is implemented, narrowly: **only `shape`
moves off the shared 0.** A freshly created `shape` is now given `z = -1` —
one level behind the 0 every graph node and every other annotation kind
(`note`, `text`, `label`, `line`, `icon`, `vote_dot`, `image`, `freehand`,
`group`) is still created at — so it opens already behind content instead of
needing a manual send-to-back to get there. That is exactly the relationship
the paragraph above already describes send-to-back producing for a shape by
hand (the transparent-fill-standing-in-for-`frame` case); this makes it the
shape's starting position rather than a step a user or agent has to take
themselves.

**Why only `shape`.** A shape is the decorative kind most often used as a
background or frame that other annotations get drawn over — the merged-in
`frame` look (a transparent fill with a coloured border) is the clearest
case, but even a filled shape is commonly a backdrop a label or icon sits on
top of. No other kind carries that same "usually a backdrop" reading strongly
enough to default it below the rest without guessing at a use the contract
has no worked example for. `image` was considered — a pasted screenshot or
diagram is sometimes used the same way, as a backdrop for icons/labels drawn
over it — but left at 0 deliberately: the contract's own illustrative case
for this feature names only `shape`, and widening the change to a second kind
on an inference the contract does not state would be exactly the "guessed"
default this section exists to avoid. If a real backdrop-image complaint
shows up in practice, revisit it as its own decision rather than folding it
into this task's scope.

**Backward compatibility.** The default is applied once, at the moment of
creation — never retroactively. An annotation already stored before this
change (every kind, `shape` included) keeps whatever `z` it already has,
typically 0, exactly as it does for every other envelope field. That old
`shape` and a *new* `shape` created after this change end up on different
layers purely because of when each was created — the old one at 0, the new
one at -1 — which is a real, visible difference: the newer shape now renders
*behind* the older one by default, opposite of the usual last-created-is-
on-top expectation. This is judged acceptable rather than confusing because
(a) `shape` is the one kind this section deliberately treats as a
backdrop, so "the newest shape is furthest back" reads as the feature working
as intended, not as a bug, and (b) the manual layer row
(`AnnotationLayerControls`, described above) still works exactly as before on
both — a user who wants the new shape back in front of the old one just
clicks bring-to-front, the same single action this whole feature exists to
make unnecessary for the *common* case, not to forbid for the exceptional
one. No stored annotation's `z` is rewritten by this change, and no migration
was written or is needed. An old `shape` and a newly created non-`shape`
annotation (say, a `label`) never had this concern in the first place: the
label still defaults to 0 exactly as it always has, so it ties with the old
shape exactly as any two same-era annotations already could.

**Both creation paths agree.** The GUI's one-click/drag-to-create toolbox
path (`GraphCanvas.jsx`'s own node-builder `createAnnotation`, which sets
`zIndex` explicitly only on the `shape` branch it builds) and the MCP/REST
creation path (`create_annotation`, `create_image_annotation`, and the image
REST ingest endpoint, all funnelling through `session_annotations.py`'s
`build_annotation`) apply the identical `shape → -1, everything else → 0`
mapping, so a GUI-created and an MCP-created shape start on the same layer.
`packages/ui-graph-canvas/src/utils/annotationModel.js`'s `createAnnotation` —
the shared client-side model both the canvas's document normalizer and the
session save/restore translators route through — carries the same mapping
too, for the same reason: the model documented as the v1 annotation shape's
source of truth should not disagree with what actually gets created.
`duplicate_annotation` (backend) and the GUI's own duplicate action
(`AnnotationDuplicateControl`) are both unaffected by design — each copies
the *source's own stored* `z` verbatim rather than passing through the
default at all, the same as every other envelope field a duplicate carries
over (see the Layer order section above).

**Explicitly out of scope, tracked separately.** `group` is deliberately
*not* given a non-zero default here, even though
dec-annotation-group-background-layering also wants group backgrounds behind
all content: a group's paint order does not read its `z` at all today (it
comes from ReactFlow parent/child array order — see the Layer order section
above), so a default here would be inert, and that decision's own mechanism
is the smallfix-group-annotation-has-no-layer-control task's to design, not
this one's. That follow-up task should treat `shape`'s `-1` as the
one already-claimed layer immediately behind the graph-node baseline: since
`shape` is "the freely layered decorative alternative" (the decision's own
phrase) and can be brought forward or back by hand like anything else,
whatever mechanism the group task builds needs to guarantee a group
background stays behind *every* `shape` position a user could manually
reach, in both directions — not just behind `shape`'s -1 default — since
dec-annotation-group-background-layering requires "behind all content"
unconditionally, not "behind content's starting position." The true one-step
forward/back "level compaction" this task's own history flags as unsafe (the
op-batch byte cap issue described above) is equally out of scope here and
remains unimplemented.

That follow-up task (`smallfix-group-annotation-has-no-layer-control`) has
since shipped, meeting exactly the brief above: see [Group background layer
order](#group-background-layer-order) for the mechanism it built —
array-order bucketing, not a numeric comparison against `shape`'s `z`, is
precisely why it can guarantee "behind everything" unconditionally rather
than only behind `shape`'s starting position.

## Operation layer

Operations are typed and invertible:

- `create`
- `update`
- `transform`
- `reorder`
- `lock`
- `duplicate`
- `delete`
- `clear`

The operation result returns the next document and an inverse operation for
undo/redo. Invalid operations must fail without partially mutating the
document. Agents and GUI code use the same model-space coordinates and
operation semantics.

**Two different meanings of "batch," on purpose.** A raw
`POST /sessions/{id}/ops` call (`SessionManager.apply_ops`) carries a list of
ops and applies them **atomically**: `test_a_poisoned_op_rolls_back_the_whole_batch`
(`backend/core/tests/test_session_annotations_image_guard.py`) pins that a
valid op earlier in that list does not survive a later op's refusal — the
whole list commits or none of it does, because a partial commit there would
leave already-connected browsers and a retried client's next attempt
diverged from each other. None of the create/update MCP tools take a list of
annotations, though — `create_annotation`/`update_annotation`/
`create_group_annotation`/etc. each act on exactly one annotation per call,
so an agent creating several annotations makes several independent tool
calls, each with its own success/failure. For *that* kind of "batch" — a
sequence of independent writes, not one atomic op list — a later call's
failure must never undo an earlier call's success, and it does not: each
tool call commits (or does not) on its own, so the property holds
structurally rather than needing new atomicity semantics that would
conflict with `apply_ops`'s. See
`TestSequentialWritesDoNotRollBackEarlierSuccesses` in
`backend/core/tests/test_annotation_type_matrix.py` for this verified across
the complete v1 type set.

### Operation timing and leases

Required collaboration semantics for every v1 type:

- **Immediate operation save** — discrete ops (create, delete, transform
  commit, reorder, lock, duplicate) are sent and applied as soon as they
  happen, not batched behind a timer.
- **300 ms text debounce** — in-progress text edits (note/label/text content)
  are coalesced and sent at most every 300 ms while typing, rather than one
  op per keystroke or only on blur.
- **Release-time geometry publication** — drag/resize is local-only while
  the pointer is down; the geometry op is published once, at
  pointer-release, not continuously per frame.
- **Exclusive edit leases** — while one actor is actively editing an
  annotation (dragging, resizing, or editing text), other actors must be
  prevented from concurrently mutating the same object, not merely warned
  after the fact.
- **Actor-scoped conditional undo** — undo only offers and reverts the
  acting client's own most recent not-yet-undone operation, never another
  actor's.

**Status (`task-annotation-shared-session-realtime`):** operation timing is
now split out from the generic autosave debounce (`AnnotationContext`'s
`notifyChange(kind)` plus the host's `annotationChangeScheduler.js`):
create/delete/style/geometry publish immediately, and `note`/`label` content
edits sync on every keystroke (not only on blur) with the scheduler
debouncing/coalescing the actual publish to at most once per 300 ms.
Selection claims are extended to every annotation kind — previously the
browser only claimed graph nodes at all — and are a purely cosmetic "who has
this selected" presence marker (`ClaimMap`, `selection_claimed`/
`selection_released`): last-write-wins, never checked before a write. See the
next paragraph for the mechanism that actually is exclusive.

**Status update (`task-annotation-exclusive-edit-leases`, deciding
`dec-mcp-agent-ops-vs-annotation-claimmap`):** exclusivity is now a genuinely
separate, first-actual-editor-wins **edit lease** (`LeaseMap`,
`backend/core/session_hub.py`), acquired via `edit_lease_acquired`/
`edit_lease_released` ops — never by `selection_claimed`. A lease is acquired
only when real editing starts: opening a text field, beginning a geometry
gesture (drag/resize), opening a right-click property editor, or starting a
bulk mutation (multi-select delete, align/distribute) or undo — never on mere
selection, closing the exact gap the previous revision of this section left
open (a claim landing mid-gesture from *selecting* the same object no longer
happens at all, because selection no longer claims anything). Unlike the
selection map, `LeaseMap.acquire()` **refuses** an id already held live by a
different client rather than taking it over: the first client to acquire
keeps the lease until it releases, finishes, lets the 30 s TTL expire, or
disconnects — a second acquisition attempt gets an explicit denial, reported
back to the caller (never a silent takeover). The client-side per-component
guards (every generic annotation component refuses a local mutation while
`isRemoteLocked(data)` is true) now read `data.remoteLease`, populated only
from this map — `data.remoteSelection` remains available for a cosmetic
marker but has no bearing on whether an edit is allowed.

The server also rejects (409 `LeaseConflict`) a **browser** batch op
(`POST /sessions/{id}/ops`, i.e. `SessionManager.apply_ops` — the path every
generic annotation mutation from the GUI's op scheduler goes through) and the
human clipboard-paste/file-upload image endpoint
(`POST /sessions/{id}/annotations/image`) when it would update or delete an
annotation another client currently holds a live edit lease on — the same
enforcement point as before, now checked against leases instead of the old
advisory claim map. `undo_last_action` (`/undo`) is covered too: it replays a
stored inverse op, so the same check runs against that op before anything is
touched. Actor-scoping is not a substitute — undo reverts the caller's *own*
past action, but the annotation it lands on may have come under someone
else's live edit lease since. A refused undo changes nothing and leaves the
record undoable once the lease clears — and because the Activity drawer's
undo button carries no lease awareness of its own, this is the one place a
lease refusal reaches an ordinary user, so the UI renders it as the retryable
"someone else has that selected" message rather than the permanent "can no
longer be undone" one (`classifyUndoError`,
`frontend/web/src/utils/sessionActivity.js`).

A refused/interrupted edit does not lose the user's local draft: a text
editor whose background lease renewal loses a race stays open, showing
whatever the user has typed, with syncing stopped rather than the draft being
discarded (`useEditableText`'s remote-lease-arrives-mid-edit handling) — the
same protection a `startEditing` denial gets by never having entered edit
mode with anything typed yet. A geometry gesture, bulk mutation or property
edit applies **optimistically** the same way every other mutation in this
package does (no round trip before the user sees the result), acquiring/
releasing the lease in the background around it rather than gating the
mutation on the round trip; the server-side `LeaseConflict` check above
remains the authoritative backstop for the rare case where that background
acquisition loses a race.

**Gap closed (`task-mcp-annotation-human-edit-guard`).** The synchronous MCP
write path — every `SessionManager` method that can mutate an existing
annotation and that a `mcp_tools.py` tool calls directly rather than through
`apply_ops` (`upsert_annotation`, `upsert_image_annotation`,
`update_annotation`, `delete_annotation`, `set_group_members`; all keyed to
the shared `mcp-agent` client id) — now checks the same live `LeaseMap`
immediately before its own mutation and raises the same `LeaseConflict` a
browser write does (`SessionManager._reject_if_leased`). This covers every
generic/note/group/image MCP tool that ends up calling one of those five: 13
tools currently wrap them, including `create_sticky_note`/`update_sticky_note`/
`delete_sticky_note`, `create_annotation`/`update_annotation`/
`delete_annotation`/`reorder_annotation`/`set_annotation_lock`/
`duplicate_annotation`, `create_image_annotation` (and, on the human side, the
`POST /sessions/{id}/annotations/image` ingest endpoint the task description
calls out by name — both routes end at the one authoritative check inside
`upsert_image_annotation`), and `create_group_annotation`/
`update_group_members`/`delete_group_annotation` (`group_membership_changed`
is now itself a lease-checkable op — see `_claimed_annotation_target` — which
closes the same gap for the **browser's own** group-membership panel too,
since it sends that op through the identical `apply_ops` batch path).
`apply_layout`/`add_node_refs`/`rename_session_sync`/`delete_session_sync`
stay unguarded because none of their ops can mutate an annotation at all —
out of scope by construction, not merely unaddressed. Each MCP tool surfaces
the refusal as `{"success": false, "error": "lease_conflict", "annotation_id",
"held_by"}`, the MCP-tool-layer mirror of the REST/`LeaseConflict` 409.

**What this guard is, and is not.** It is a *refusal*, checked at the actual
mutation boundary (immediately before the write, after every other
precondition — revision, existence, budget checks — so a conflict leaves
state completely unchanged) and safe against the same "preflight races a
concurrent acquisition" failure mode a check taken earlier and cached would
have: nothing between the lease read and the mutation ever awaits, and while
an `apply_ops` batch is genuinely mid-flight the guarded method refuses with
`LayoutBusy` instead of running against a pre-commit view. It is **not**
authorization or identity verification: `client_id` — on either side of the
check — is caller-supplied and unauthenticated, so `held_by` names whichever
string currently holds the lease, not a verified human. An MCP agent never
acquires, renews, releases or takes over a lease itself in v1 — it only ever
checks against one — matching `dec-mcp-agent-ops-vs-annotation-claimmap`
("agents do not acquire, reserve or take over human edit leases in v1").
After a `lease_conflict`, an agent must re-read current state (e.g.
`list_annotations`) before retrying rather than blindly resubmitting the same
write; the retry succeeds once the lease is released or its 30 s TTL expires.
Full per-agent identities and agent-to-agent leases remain out of scope for
v1; field-level conflict protection for *unleased* concurrent edits is the
separate `dec-annotation-field-patches-and-conflicts` mechanism described
below, not this one. Actor-scoped conditional undo *is* implemented
(`backend/core/session_activity.py`).

### Two-client conflict matrix

PR #443 covers the mid-drag "claim lands mid-gesture" race and the per-kind
mutation guards (every generic annotation component refuses a local mutation
while `isRemoteLocked(data)` is true); PR #451 covers server-side claim
enforcement for one client at a time. Neither documented what actually
happens when **two real clients** touch the **same** annotation at close to
the same moment. This section does, backed by two-real-client tests against
the actual `SessionManager.apply_ops` path — not just a call into the
`LeaseMap` helper in isolation — in `backend/core/tests/
test_session_manager.py`'s `TestConflictMatrixTwoClients`.

**Updated for `task-annotation-exclusive-edit-leases`.** The matrix below
originally read "claim" throughout, describing the LWW selection-claim
enforcement PR #451 shipped. The enforcement mechanism has since changed to
the first-actual-editor-wins edit lease described in [Operation timing and
leases](#operation-timing-and-leases) above — selection no longer gates a
write at all. The **"held" column's outcome is unchanged in substance** (a
second writer is still refused, `409`, with the first writer's edit standing)
— what changed is *how* the first writer comes to hold it (editing must have
actually started, not merely been selected) and *what* the server checks
(`LeaseConflict` against `LeaseMap`, not `ClaimConflict` against `ClaimMap`).
**Update for `dec-annotation-field-patches-and-conflicts` (accepted
2026-08-30).** The "no lease held" column and the whole-annotation-clobber
finding below were exactly the open item that decision closed — a lease
narrows the collision window (two clients can no longer race to edit the
same annotation with neither one protected) but never added field-level
granularity on its own; field-level patches plus per-field version checking
is the complementary layer for whenever a lease is not (yet) held. Both
mechanisms stay in place and do not compete: a live lease still refuses the
whole write outright the instant one is held, unchanged by this update; the
matrix below shows what changed only in the unleased column.

**The mechanism the matrix rested on, before the fix.** Two facts, each
already documented elsewhere in this file, used to combine into a property
that was not obvious from either alone:

1. The browser re-sent the **whole** annotation object on every publish —
   text, geometry, style and all — never a per-field delta.
2. `SessionStore.apply_state_op`'s `annotation_updated` handler applied an
   incoming write as `target.update(incoming)` — a shallow, **top-level**
   merge of whatever keys `incoming` happened to carry.

Combined: an `annotation_updated` op was not "patch this one field" so much
as "replace every top-level key my own local copy currently holds" — and a
client's local copy is only ever as fresh as the last remote op it has
actually received. Two clients editing genuinely *different* fields of the
same annotation therefore did **not** safely merge the way the mechanism
might suggest at a glance: whichever op landed second overwrote every
top-level key its sender's local copy held at commit time, including one the
*other* client changed in the meantime and this client hadn't caught up on
yet. This was whole-annotation last-write-wins, not field-level
last-write-wins — surprising enough relative to "merges rather than
replaces" (this file's own words for the same code, describing a different
scenario — a single client's own successive writes, where it is true) that
it was called out explicitly. `dec-annotation-field-patches-and-conflicts`
(accepted 2026-08-30, realised by
`task-smallfix-whole-annotation-clobber-on-concurrent-different-field-edit`)
closed it; the fix is described next, and its effect on the matrix follows.

**The fix.** Two changes, deliberately kept to a simpler versioned-patch-
with-explicit-conflict model rather than a general-purpose CRDT/operational-
transform editor (out of scope — see "What this does not change" below):

1. **Field-diffed publish.** `sessionSyncClient.js`'s `computeOps` now
   computes the field-level difference between the last-synced baseline and
   the current local value (`diffAnnotationFields`) before emitting an
   `annotation_updated` op — a field this client never touched is simply
   absent from the outgoing patch, so there is nothing left for it to
   clobber, independent of whether the sender's local mirror has caught up
   with a concurrent peer's edit to that field. This alone closes the
   different-field cell of the matrix below, with no version bookkeeping
   involved at all.
2. **Per-field version checking.** Every annotation now carries a
   server-owned `version` (an integer, bumped by one on every applied
   `annotation_created`-as-upsert or `annotation_updated`) and
   `field_versions` (the `version` at which each individual content field
   last actually changed — never touched by a client, always recomputed by
   the server from a real *value* diff, so resending a field's current,
   unchanged value never marks it as "touched"). An op can additionally
   carry `base_version`: the annotation `version` the sender last synced.
   `SessionStore.apply_state_op` checks, only for the fields the incoming
   patch genuinely changes, whether that field's `field_versions` entry is
   newer than `base_version` — an unrelated field changing in between never
   blocks the write; only a field the patch is itself trying to move to a
   different value, that someone else has *also* moved since, is a real
   conflict. See [Field-level patches and
   base_version](#field-level-patches-and-base_version) below for the exact
   wire shape and the all-or-nothing-per-op rule.

**A caller that supplies no `base_version` at all** — an older cached browser
bundle, or an MCP tool call that has not opted in (see the MCP subsection
below for which tools do) — keeps the pre-fix whole-object shallow-merge
behaviour verbatim: `target.update(incoming)`, unconditionally, no conflict
ever raised. This is the documented, deliberate fallback
(`AnnotationFieldConflict`'s docstring in `session_store.py`), not an
oversight left open by this task: a legacy full-object write is not made any
*more* dangerous by this change, it simply does not participate in the new
protection. A real (post-fix) browser never produces an op shaped like that
any more — `computeOps` always emits a field-diffed patch with `base_version`
set — so this fallback is now purely a compatibility path, not the default.

**Update — this was not actually true for a real browser until
smallfix-annotation-version-dropped-by-browser-pipeline (fixed after
`dec-annotation-field-patches-and-conflicts` was accepted).** `computeOps`
itself always did the right thing — `diffAnnotationFields` correctly excludes
`version`/`field_versions` from the outgoing content diff and reads
`before.version` for `base_version` (see `sessionSyncClient.js` and its own
tests) — but every annotation the *rest* of the browser pipeline handed to it
had already lost both fields before `computeOps` ever saw them:
`useSharedSession.js`'s `serverStateToMirror` (session hydration),
`sessionAnnotations.js`'s `annotationsToOverlays`/`overlaysToAnnotations`, the
live-canvas round trip in `packages/ui-graph-canvas/src/utils/annotations.js`
(`overlayToFlowNode`/`flowNodeToOverlay`), and `createAnnotation` in
`annotationModel.js` all built or read their output from a fixed field
whitelist that did not include `version`/`field_versions`. The result: a real
browser's `base_version` was `undefined` on *every* `annotation_updated` op it
ever sent — `JSON.stringify` drops the key entirely — so every real
browser-originated write landed on exactly the legacy fallback this paragraph
says a real browser "never produces... any more". The matrix row below and the
"A real client" language throughout this section describe the *protocol* as
verified against `SessionManager.update_annotation` directly
(`TestFieldVersionedPatches`, `test_session_manager.py`) with a hand-supplied
`base_version` — correct for that path, but not a description of what a real
browser tab was actually sending before this fix. Both translator layers now
carry `version`/`field_versions` through unconditionally (the same envelope
treatment `z`/`locked`/`rotation` already got), so the "A real (post-fix)
browser" claim above is accurate as of this fix, not before it.

**Update — a *single* real client can still race its own prior write, not
just a second collaborator (round 2 review of
smallfix-annotation-version-dropped-by-browser-pipeline, fixed by the
same-client self-conflict follow-up).** `annotationChangeScheduler.js`
publishes most annotation field changes (style, geometry, create, delete)
immediately, with no debounce — only `text` coalesces over ~300 ms — so one
user can fire two rapid edits to the *same* field of the *same* annotation
(two quick color picks, say) before the first op's round trip completes.
`sessionSyncClient.js`'s `syncState` used to assign its internal baseline
straight from the live canvas snapshot on every call; the canvas's own copy
of `version`/`field_versions` only catches up asynchronously, through the
first op's ack round-tripping back onto the canvas node
(`onLocalAnnotationsApplied`) — so the second edit, computed before that ack
landed, read back the identical pre-send version and sent an identical,
already-stale `base_version`. The server correctly refused the resulting
batch with `field_conflict` — there genuinely was a `field_versions` entry
newer than what the op claimed — but the only client the write conflicted
with was itself: a single browser tab, no second collaborator anywhere in
the picture, seeing its own most recent edit rejected and reverted by
`onDropped`'s resync, under a notice worded for a two-client conflict.

The fix (`predictAnnotationVersionsForSend`/`foldAckedAnnotationOp` in
`sessionSyncClient.js`) has the client predict, the moment an op is sent
rather than only once its ack arrives, the `version`/`field_versions` the
server is expected to bump a touched annotation to — mirroring
`session_store.py`'s own bump exactly, so a same-field re-edit computed
before the first ack lands already carries a fresh `base_version` instead of
a stale one. The prediction is reconciled against the server's authoritative
ack afterwards without ever regressing a *later*, still-unacked local
prediction on the same annotation (the ack is only authoritative as of the
earlier op it is acking) — closing the general case, not only the
back-to-back two-edit one: three or more rapid same-field edits, and a
same-field re-edit with a different field's edit interleaved in between, are
all covered by the same mechanism and test-covered
(`frontend/web/tests/sessionSyncClient.test.js`, "same-client self-conflict
race"). This is purely a client-side prediction; nothing about the server's
`AnnotationFieldConflict` check, the wire shape, or a *genuine* two-client
race (the matrix below) changes.

**Update — a genuinely different collaborator's own EARLIER write could still
regress this client's baseline, via the SSE broadcast channel rather than the
POST-ack channel (round 3 review of
smallfix-annotation-version-dropped-by-browser-pipeline, fixed by the
remote-broadcast-reordering follow-up).** The fix above closes the race
between *this client's own* predicted and acked versions of the *same*
op-in-flight. It left a related, genuinely two-client gap open: this client's
own POST-ack channel and its SSE `'op'` broadcast stream are two independent
HTTP connections with no cross-ordering guarantee (`sessionSyncClient.js`'s
own `_appliedSeq` comment documents this as an accepted architectural
property — a concurrent op's broadcast can still be in flight when this
client's own later op's ack has already landed). So a genuinely different
collaborator B's broadcast for a chronologically **earlier** op could arrive
*after* this client A's own already-acked **later** op on the same
annotation. Before this fix, `_handleEvent`'s `'op'` case folded every remote
broadcast into the baseline unconditionally (`applyOpToMirror`, no guard at
all) — so B's late, stale broadcast would silently regress both
`version`/`field_versions` and content back to B's pre-A-edit snapshot. A's
*next* edit would then diff against that reverted baseline and send a stale
`base_version`, which the server correctly refuses as `field_conflict` — the
exact same class of spurious conflict the round-2 fix closed, this time from
a genuine two-client interleaving rather than a same-client race, and
untouched by that fix since it only ever guarded the ack path.

The fix (`foldRemoteAnnotationOp` in `sessionSyncClient.js`, used only by
`_handleEvent`'s `'op'` case for an op attributed to a different client)
rejects a stale/reordered remote broadcast atomically — both its
`version`/`field_versions` and its content — whenever its `version` is lower
than what the baseline already holds for that annotation, rather than
`foldAckedAnnotationOp`'s own approach of merging `version`/`field_versions`
to the higher value while still taking content from the incoming op
unconditionally. That merge-not-reject approach is correct for the ack path
(the ack is always this client's own trustworthy write, just possibly behind
a further not-yet-confirmed local prediction) but would be wrong for a
remote broadcast: since the server hands back the *whole* annotation record
on every `annotation_created`/`annotation_updated` op (`session_store.py`
mutates the one shared server-side object in place, never emits a diff), a
lower-versioned broadcast is a strictly older, already-superseded snapshot in
full — accepting even one of its fields would produce a version number and a
content field from two different points in time that no real server
snapshot ever actually was. Dropping the whole stale op is also safe: the
server applies ops for one annotation strictly in sequence against that same
shared object, so whichever of two ops landed first server-side is already
folded into whichever landed second — nothing a rejected stale broadcast
could have contributed is ever permanently lost, because this client's own
next ack (always a full, cumulative snapshot too) recovers it if needed.
Test-covered end-to-end through the real `SessionSyncClient`/`_handleEvent`
path with a genuine simulated SSE `'op'` event
(`frontend/web/tests/sessionSyncClient.test.js`, "remote broadcast reordering
vs. an already-acked own write"), plus pure-function coverage for
`foldRemoteAnnotationOp` itself. A version tie or a genuinely newer broadcast
still applies normally — this guard only ever changes behaviour for an
actual regression.

**Update — the related canvas-layer gap this fix originally left open is now
closed (round 5, smallfix-applyremoteop-canvas-no-version-guard).** An
earlier revision of this section recorded a "known related gap" here:
`App.jsx`'s `applyRemoteOp` (the handler `onRemoteOps` invokes) applied an
annotation broadcast's content straight onto the *live canvas* React store
unconditionally, with no version check of its own, so a stale broadcast this
guard correctly kept out of `sessionSyncClient.js`'s internal baseline could
still flash onto the canvas itself via that separate path. That revision
described the canvas as "self-healing once this client's own ack (or a
later, genuinely newer broadcast) lands" — that framing oversold the actual
risk and was corrected once traced through in full: nothing *automatically*
corrects a wrongly-applied stale annotation. A remote apply alone triggers no
save (`GraphCanvas.jsx`'s remote-annotation-ops effect only clears the queue,
via `onRemoteAnnotationsApplied`), so the stale content simply sits on the
canvas until the *next*, entirely unrelated autosave trigger (a node drag, a
different annotation edit, Save View). That autosave builds its outgoing
snapshot from whatever the canvas currently shows — the stale, reverted
content, never corrected — and diffs it against the sync baseline, which
(thanks to this same guard) is correctly still at the true higher version.
`predictAnnotationVersionsForSend` computes a valid-looking `base_version`
for the resulting patch, the server has no way to distinguish this from a
genuine edit and accepts it as a new version, and a collaborator's confirmed
change is silently overwritten and re-broadcast to everyone as real — the
same data-loss class this whole four-round chain exists to close, reachable
through ordinary two-collaborator editing, not only a narrow race window a
self-heal could plausibly outrun.

The fix reuses this guard's own comparison rather than adding a second,
independently-maintained version check at the canvas layer: `_handleEvent`'s
`'op'` case now computes `isAnnotationOpStale` (the same predicate
`foldRemoteAnnotationOp` above acts on, factored out as its own pure
function) against the pre-fold baseline and, when the incoming
`annotation_created`/`annotation_updated` broadcast is stale, never invokes
`onRemoteOps` for it at all. `App.jsx`'s `applyRemoteOp`/
`applyAnnotationUpsertToCanvas` are consequently never called with a stale
annotation broadcast in the first place — the canvas and the sync baseline
are now gated by the exact same decision, computed once, so the two cannot
independently drift into disagreeing with each other the way two separately
maintained checks could. A brand-new annotation (no baseline entry yet)
still applies unconditionally, and a genuinely newer broadcast still reaches
the canvas normally — this only ever changes behaviour for an actual
regression. Test-covered at the sync-client layer (`frontend/web/tests/
sessionSyncClient.test.js`, the "remote broadcast reordering" describe block
now also asserts on `onRemoteOps`) and end-to-end through the real
`SessionSyncClient`/`sessionAnnotations.js`/`GraphCanvas` chain
(`frontend/web/tests/annotationRemoteCanvasVersionGuard.test.jsx`).

**The matrix.** Rows are what two clients (A writes first; B is the second
writer, arriving after A) attempt on the same annotation — same mutation
category on both sides (e.g. both edit text) or a different one (e.g. A edits
text, B edits geometry); columns are whether a live edit lease is held by A
when B's op arrives (A came to hold it by actually starting to edit — opening
a text field, a geometry gesture, a property editor, a bulk mutation — not by
merely selecting the annotation). Verified for `shape` as the representative
generic kind (`TestConflictMatrixTwoClients`); the lease check itself
(`_claimed_annotation_target`, `session_manager.py`) is keyed only on
annotation `id`, never on `type`, so the leased columns hold identically for
every v1 kind — the per-kind audit below confirms this for the other five
generic kinds plus `note`.

| A's edit | B's edit (no lease held by A) | Outcome, no lease | B's edit (A holds the lease) | Outcome, A holds the lease |
|---|---|---|---|---|
| text | text (same field) | A **real client**: 409 `field_conflict`, explicit — never a silent overwrite (`TestFieldVersionedPatches.test_same_field_conflict_is_explicit_not_a_silent_overwrite`); B re-derives from the conflict's own `server_annotation`/`server_version` and its retry then succeeds (`test_stale_write_is_rejected_then_succeeds_after_rederiving`). A **legacy caller with no `base_version`**: unchanged, B's text wins, whole-document LWW (`test_same_field_text_edits_without_a_lease_second_writer_wins`) | text | 409 `LeaseConflict`; A's edit stands (`TestLeaseEnforcement.test_non_holder_update_is_rejected`) |
| text | geometry (different field) | A **real client**: both survive — B's field-diffed patch never even mentions "text" at all, so there is nothing to check or clobber, independent of `base_version` (`test_geometry_edit_from_a_stale_client_no_longer_clobbers_a_concurrent_text_edit`). A **legacy full-object write**: still clobbers exactly as before this task (`test_legacy_whole_object_write_without_base_version_still_clobbers`) | geometry | 409 `LeaseConflict` — refused regardless of which field B touches, so A's text edit is never at risk from a leased annotation (`test_lease_blocks_a_different_field_edit_too_not_only_same_field`) |
| (any) | style | Same rule as the different-field row above — merges for a field-diffed write, still whole-document LWW for a legacy no-`base_version` write | style | 409 `LeaseConflict` (`test_non_holder_style_edit_is_rejected_while_leased`) |
| (any) | lock toggle | Same rule — `locked` is an ordinary field, checked/versioned like any other on an `annotation_updated` op, not a separate op type | lock toggle | 409 `LeaseConflict` (`test_non_holder_lock_toggle_is_rejected_while_leased`) — a non-holder cannot lock out from under the lease holder either |
| (any) | delete | Unaffected by this task — `annotation_deleted` has no field-level granularity to protect; still just removes the annotation, no field survives to clobber | delete | 409 `LeaseConflict`, annotation intact (`test_non_holder_delete_of_a_shape_is_rejected_while_leased`, mirroring the existing `note` case) |

Reading the matrix: the **leased** column is unchanged by this task and
remains the safe one for every row regardless of `base_version` — holding the
lease protects every field of the annotation, not merely the one the holder
itself is editing, because the check is per-annotation-id, not per-field. The
**unleased** column now depends on whether B's write opted into the new
protocol: a real (field-diffed, `base_version`-carrying) write merges
independent fields silently and surfaces a genuine same-field race as an
explicit `field_conflict` instead of ever silently overwriting it; the
documented legacy fallback (no `base_version` at all) keeps the old
whole-document-LWW behaviour verbatim, unprotected exactly as before. A live
edit lease still closes the window outright for the annotation it was
actually acquired on, unaffected by either case above — it does not
retroactively protect a write that never went through a lease-acquiring
entry point, and field-level patches do not change what the *leased* column
enforces.

**What this does not change.** The edit-lease mechanism itself (`LeaseMap`,
[Operation timing and leases](#operation-timing-and-leases)) is untouched by
`dec-annotation-field-patches-and-conflicts` — the two layers are
complementary, not overlapping: a lease still refuses a write outright the
instant one is held, and field-level patches only ever apply in the unleased
window a lease does not (yet) cover. This is deliberately **not** a
general-purpose CRDT or operational-transform editor — no automatic
character-level text merge, no arbitrary multi-field 3-way merge inside one
op. A single `annotation_updated` op is still all-or-nothing: if any field it
touches genuinely conflicts, the whole op is refused, even when other fields
in the same patch do not conflict, and the caller re-derives a smaller/fresher
patch rather than the server attempting a partial apply. That scope decision
is recorded in `dec-annotation-field-patches-and-conflicts` itself.

### Field-level patches and base_version

**Stored/broadcast shape.** Every annotation carries two server-owned
bookkeeping fields, alongside its ordinary content — never caller-settable
(a payload that supplies either is silently overwritten, not merged in):

- `version` (int, starts at `1` on create) — bumped by one on every applied
  `annotation_updated` and on an `annotation_created` upsert-in-place
  (a same-id create retry); read-only, returned in every read/write
  (`list_annotations`/`list_sticky_notes`, `create_annotation`,
  `update_annotation`'s result) as `annotation.version`.
- `field_versions` (`{field_name: version}`) — the `version` at which each
  individual content field last actually *changed value* (never merely
  present in an incoming payload with the same value it already held).
  Internal bookkeeping only, not projected to MCP callers or the browser's
  content payload — a caller only ever needs `version`, to hand straight
  back as `base_version` on its next write.

**The `annotation_updated` op.** `annotation` carries only the fields the
sender actually changed, plus the `id`/`type`/`kind` identifying/validating
triple every update must carry regardless. `base_version`, alongside
`annotation`, is optional:

```json
{
  "op": "annotation_updated",
  "annotation": { "id": "shape-1", "type": "shape", "kind": "shape",
                   "geometry": { "x": 40, "y": 40, "w": 160, "h": 96 } },
  "base_version": 3
}
```

- **Given:** for each field the patch is genuinely changing (a value diff
  against current stored state, computed server-side — a field whose
  incoming value already matches what is stored is never "touched", however
  it got there), the server compares that field's `field_versions` entry to
  `base_version`. A field with no entry defaults to the annotation's
  creation version (`1`). If every touched field's version is `<=
  base_version`, the op applies and bumps `version` + the touched fields'
  `field_versions` to the new value. If **any** touched field's version is
  `> base_version`, the whole op is refused with `AnnotationFieldConflict` —
  never a partial apply (see "What this does not change" above).
- **Omitted:** the pre-fix, unconditional whole-object shallow merge
  (`target.update(incoming)`) — the documented legacy fallback.

**The conflict response.** `AnnotationFieldConflict` surfaces as HTTP 409
wherever a browser or MCP write can raise it:

- `POST /sessions/{id}/ops` (`rest_api.py`): `detail` is a structured object
  — `{"error": "field_conflict", "annotation_id", "conflicting_fields":
  {field: server_version}, "server_version", "message"}` — distinct from
  `LeaseConflict`'s plain-string `detail` on the same status code, so
  `sessionSyncClient.js`'s terminal-rejection handling (already shared for
  every 409, per [Operation timing and leases](#operation-timing-and-leases))
  can tell the two apart for an accurate notice (`App.jsx`'s `onDropped`:
  "someone else changed this at the same time", not the lease-specific
  "someone else is editing this").
- The `update_annotation` MCP tool (`backend/service/mcp_tools.py`) accepts
  an optional `base_version` argument and, on conflict, returns
  `{"success": false, "error": "field_conflict", "conflicting_fields",
  "server_version", "annotation": <current server value>}` — the same
  information, MCP-shaped.

**Re-derive, never blindly retry** ("do not silently retry a stale value
against a newer version" — `dec-annotation-field-patches-and-conflicts`'s own
words): a client that gets `field_conflict` reads the conflict response's own
current annotation/`server_version` and computes a fresh patch from that,
rather than resending the same rejected content once whatever blocked it has
changed again. `sessionSyncClient.js` inherits this for free from the
pre-existing 409/`LeaseConflict` terminal-rejection path
(`task-annotation-exclusive-edit-leases`): a dropped op is removed from the
outbound queue and never automatically resent — the *next* local edit
computes an entirely new field diff against the (now resynced) baseline
rather than replaying the stale one.

**MCP path parity — which tools opt in.** `SessionManager.update_annotation`
(the shared chokepoint every MCP annotation-patch tool calls into, exactly
like the browser's `POST /ops`) accepts `base_version` uniformly, so the
version/conflict semantics are identical for both entry points, not a
separate mechanism. The generic `update_annotation` MCP tool exposes
`base_version` to its caller, and so does `update_sticky_note` (added by
smallfix-annotation-version-dropped-by-browser-pipeline): a note's
`build_note_patch` still resends the whole `geometry` sub-object on any
position/size/rotation change (see that function's shallow-merge note above),
so without `base_version` a position-only move silently clobbered a
concurrent geometry-subfield edit — e.g. a resize — with no conflict raised,
exactly the class of bug this section otherwise closes. `reorder_annotation`
and `set_annotation_lock` still omit `base_version` and so stay on the
documented legacy-fallback (unconditional merge) path; each of those two
patches exactly one field it alone manages (`z`, `locked`), so there is no
second field in the same patch for a concurrent edit to race against, unlike
`update_sticky_note`'s combined geometry write. Widening those two to accept
`base_version` too is straightforward follow-up, not a correctness gap this
task leaves open.

**Reconnect/catch-up and undo.** Neither needed a protocol change:
`SessionStore.apply_state_op` already returns (and always did) the **whole**
post-update annotation as the applied op's `annotation` — the ring buffer
(`ops_since`, used for `catch_up`) and the full-state `snapshot` path both
therefore already carry `version`/`field_versions` through to a reconnecting
client with no code change on that side; only what a client *publishes* had
to change. `undo_last_action` replays a stored full-object inverse op under
`trusted_replay=True`, which skips `base_version` checking entirely — the
replayed content is this session's own recorded prior state, not fresh
caller input, so there is nothing to conflict-check against. `version` still
only ever moves forward on an undo (never restored to its pre-edit value),
so a client's `base_version` bookkeeping stays monotonic across an undo the
same way `updated_at` already did before this task.

## Attachment and detach behavior

- `label`/`callout`, `icon` and `text`/heading annotations may
  attach to a node via `content.attachment = { target_id, target_type,
  anchor, offset }`. (`vote_dot` used to be a fourth member of this list;
  task-annotation-vote-dot-simplify retired it — a vote dot is now a plain
  coloured dot that always lives on its own.)
- `line` endpoints (`start`/`end`) may each independently attach to a node
  or to another annotation, or stay free-floating at a fixed model-space
  point.
- Attached objects follow the referenced target's movement.
- If the attachment target is removed, the attached object detaches and
  keeps its last resolved model-space geometry — it does not disappear and
  does not snap to the origin.
- `group` is a containment/visual construct, not an attachment target or an
  attacher: nothing attaches to a group; membership in a `group` is tracked
  separately via `member_node_ids` and the `group_membership_changed` op, not
  via `attachment`. (The retired `frame` kind used to carry this same rule —
  see [Fill and border](#fill-and-border-shape) for why a `shape`, whatever
  its fill/border, does not.)

**GUI attach/detach.** `label`, `text` and `icon` can now be
(re)attached and detached from the canvas, not only via a raw
`content.attachment` payload: dropping one of these overlays within
`ATTACH_SNAP_RADIUS` (90px, unscaled) of a node's or another attachable
annotation's centre attaches it there (`group` is excluded from candidacy,
per this section's rule that nothing attaches to it — even
when it is the nearest thing to the drop point, a further-away valid target
is preferred, or the overlay stays unattached), storing the drop point's
offset from that centre so
the overlay keeps exactly where it was released rather than jumping onto the
target (the "free fine adjustment" the capability baseline calls for);
dropping it outside every snap zone detaches it and keeps the position it was
released at. Once attached, the overlay stays draggable — a further drag
recomputes the attachment (still-attached with a new offset, or detached) the
same way. While attached it follows its target's movement every render
(`packages/ui-graph-canvas/src/components/GraphCanvas.jsx`'s attachment-follow
effect, mirroring the pre-existing arrow-anchor effect); if the target
disappears from the view (filtered, collapsed, not yet loaded, or deleted) the
overlay is left at its last resolved position rather than being recomputed or
reset, matching "detaches and keeps its last resolved model-space geometry"
above. The "nearby object menu" (see [Human authoring
surfaces](#human-authoring-surfaces)) now offers an attachable type pre-wired
to a target at creation time, writing this exact same `content.attachment`
shape rather than a second mechanism — so a menu-created attachment resolves
and follows through the same effect this section describes. What this still
does not add: a manual way to inspect or clear an annotation's current
attachment target other than dragging it away. `line` endpoint attach/detach
(`startAnchor`/`endAnchor`, drag-to-snap on the endpoint handle) predates this
and is unrelated code, unchanged here.

**Locking and bindings** (`dec-annotation-lock-semantics`). `locked` freezes
**all** geometry change on an annotation, not only user-initiated edits — this
is stricter than "blocks edits but not the resolution of a binding the user
already created," which was considered and rejected as harder to state and
easier to drift from. Concretely, both of `GraphCanvas.jsx`'s geometry-follow
effects skip a locked annotation outright: the attachment-follow effect above
leaves a locked, attached `text`/`label`/`icon` exactly where it
was the moment it was locked, even while its target keeps moving, and the
anchored-arrow-resolve effect (the `line`-endpoint counterpart, driving
`startAnchor`/`endAnchor`) does the same for a locked, anchored arrow.

Locking an attached or anchored annotation also **drops its binding**: the
lock write (`set_annotation_lock`, `backend/service/mcp_tools.py` — locking a
generic annotation is MCP-only, there is no GUI "Lock" action, only "Unlock")
resolves the binding one final time — its geometry is already kept resolved
continuously by the browser's own follow effect while unlocked, so there is
nothing left to compute — and clears the attachment/anchor reference in the
same write. Without this, a locked-but-still-bound annotation would silently
claim an attachment it no longer honours: freezing geometry alone (the
paragraph above) is not enough on its own, because the moment such an
annotation is later *unlocked*, the follow effect would resume and snap it
onto its target's now-different position — a jump the user did not ask for.
Dropping the binding at lock time closes that: **unlocking does not restore
it**, and re-attaching is a deliberate, separate user action afterward.

The same rule applies at `create_annotation` (`backend/service/mcp_tools.py`),
not only at `set_annotation_lock`: that tool's `locked` parameter lets a
caller set `locked=True` in the same call as an attached/anchored `content`,
for a fresh create or for an upsert-replace (`annotation_id` matching an
existing annotation — the write goes through `session_manager.upsert_annotation`
there, not through `update_annotation`/`set_annotation_lock`, so it does not
otherwise pass through this rule at all). That write is a *partial* merge,
not a full replace: the store (`backend/core/session_store.py`) applies it as
a shallow `existing.update(annotation)` onto the previously stored record, so
any field this call omits survives from before — including a binding field an
upsert-replace call did not think to resend just to flip `locked`. Both paths
share the same `_lock_detach_content` helper `set_annotation_lock` uses; for a
fresh create there is no prior state, so it is applied to the freshly built
annotation, but for an upsert-replace it is applied to a merged view (the
existing stored annotation overlaid by this call's own fields) so an omitted
binding field is looked up from the stored annotation rather than reading as
absent. Either way, `create_annotation` can never persist `locked=True`
alongside a binding.

Two related pieces of the same decision live elsewhere in this document
rather than here, because each is a detail of the section it sits in: a
locked group's own menu withholds every destructive action, [Layer
order](#layer-order) above explains why the lock protects content rather than
(a non-existent) visibility; and the multi-select delete path filters
locked/leased/ungrouped-node selections exactly like the keyboard
`Delete`/`Backspace` handler that same section describes, so the two paths
cannot drift apart the way they once did.

**Validation.** `backend/core/session_annotations.py`'s generic annotation
builder/patcher (used by `create_annotation`/`update_annotation`) rejects a
structurally malformed `attachment` or `line` `start`/`end` before it ever
reaches the stored document — a missing/empty `target_id`, a non-object
`attachment`, or a non-numeric `offset`/`point` all fail with
`invalid_content` rather than being stored and silently failing to resolve
later. This is a *structural* check, not a target-existence check: an
`attachment.target_id` that does not (yet, or ever) name a real node is
still accepted, matching "attached objects follow the referenced target's
movement" being a rendering-time concern, not a write-time one. `shape` and
`icon` get the same treatment for their own field: `content.shape`/
`content.icon` must be a non-empty string, but — unlike `attachment` — an
unrecognised *value* is not an error (see the [canvas
rendering](#canvas-rendering) section on why an unknown shape/icon name is
stored verbatim rather than rejected). `packages/ui-graph-canvas`'s
client-side `annotationModel.js` deliberately does **not** mirror this
rejection: it must stay able to load and render a session's already-stored
document without throwing, including one written before this validation
existed, so it normalizes defensively instead of refusing.

## Image ingest enforcement

Required for every path that can set an `image` annotation's pixel content:

- The persisted `content.image.url` must always be the result of
  server-side ingest (`backend/core/image_ingest.py`): decoded, format- and
  size-validated from the real bytes, downscaled, and re-encoded as an
  embedded `data:` URI.
- Persisting a generic, unvalidated external image URL as
  `content.image.url` is **not acceptable** in v1, regardless of entry
  point (paste, upload, MCP data, MCP URL import) — an annotation must not
  depend on a remote resource staying reachable to keep rendering.

**Enforcement:** the MCP `create_image_annotation` tool and the REST `POST
/api/sessions/{id}/annotations/image` endpoint (the human clipboard-paste /
file-upload path) are the only two entry points that set pixel content, and
both call the identical `image_ingest.py` validate/optimize/embed pipeline
plus `SessionManager.upsert_image_annotation` — there is one ingest
implementation, not two. `create_annotation` refuses `type="image"` and
`update_annotation` refuses a `content` carrying an `image` key, so the
generic envelope can no longer store a supplied `content.image.url`
verbatim. Underneath all of these, `SessionStore.apply_state_op`'s
`annotation_created`/`annotation_updated` branches (`image_annotation_error`
in `backend/core/session_annotations.py`) reject any op whose `image` payload
sets a URL that is not an embedded `data:image/webp;base64` URI — the content
type ingest emits — so a raw op posted to `/api/sessions/{id}/ops` is held to
the same rule as an MCP call.

Two writes are deliberately exempt, because refusing them would break state
the session already holds rather than keep anything out:

- A payload whose `url` is byte-identical to the one already stored under
  that id. The browser re-sends the *whole* annotation on every move, resize
  and lock (`sessionSyncClient.js`), so without this an annotation persisted
  before this rule existed would be permanently unmovable.
- An undo replaying its stored inverse op (`trusted_replay`), which restores
  a copy of this session's own earlier state — otherwise deleting such an
  annotation would be irreversible.

Duplication is deliberately *not* exempt: a copy lands on a new id, with no
stored URL to match, so duplicating an annotation whose URL was persisted
before this rule existed is refused with the ingest error (reorder, lock,
move and delete on that same annotation still work). Duplicating a properly
ingested image is unaffected.

**What this does not do.** Two limits are worth stating exactly, because it
is tempting to read more into the rule than it delivers:

- The store can tell an embedded data URI from a remote link, but not *which*
  embedded bytes came from ingest. A client can still forge a data URI and
  persist a self-supplied picture — bounded by the per-image budget (below),
  not unlimited. There is no CSRF or origin check on the ops endpoint either,
  so "a client" here means anything holding the session id.

  The per-image, per-session and document budgets in `image_ingest.py` used
  to be enforced only by `SessionManager.upsert_image_annotation` (the
  dedicated MCP ingest path), leaving two gaps, now closed: an op carrying a
  validated-shape embedded image on the `apply_ops` path (a browser
  move/resize/relayer/lock, or a `duplicate_annotation` copy through
  `upsert_annotation`) is now classified and budgeted the same way instead of
  hitting the small flat op-batch cap that always rejected a realistically
  sized picture; and `apply_ops` now also checks the *cumulative* session
  image/document totals after each batch, not just that one batch's size, so
  many small, individually-legal batches can no longer grow a session
  document past budget over time (the growth path is not image-specific — a
  large `text` payload reaches it the same way — so the cumulative check
  applies to any state-changing batch, not only image-carrying ones). See
  `docs/MULTI_USER_SESSIONS_DESIGN.md` §3.9 for the mechanism.
- A `SavedView`/`VisualizationView` node's `metadata.annotation_document` and
  legacy `metadata.annotations` are ordinary graph-node metadata, written
  through the generic `add_nodes`/`update_node` mutations rather than through
  `SessionStore.apply_state_op` — so they never went through
  `image_annotation_error` on their own. `saved_view_annotation_error`
  (`backend/core/session_annotations.py`) closes the write side: `add_nodes`
  and `update_node` (`backend/service/mutations.py`) apply it to any node of
  either type before persisting, with no byte-identical-URL exemption (a
  saved-view write is not an incremental patch onto previously-validated
  state the way a live op is). `sanitize_saved_view_metadata` closes the read
  side as defense in depth: `get_saved_view` (`backend/service/views.py`) and
  the generic `serialize_node` (`backend/service/serializers.py`, so every
  read path reaches it — including the canvas's "double-click a SavedView
  node to open it" flow, which reads `metadata.annotations` off an
  already-serialized node rather than calling `get_saved_view` again) strip
  any non-embedded image URL before it can reach an `<img src>`, so a view
  that reached storage before this rule existed still cannot make a viewer
  fetch a remote host merely by opening it. `adopt_federated_node`
  (`backend/service/mutations.py`) is a narrower residual gap: it calls
  `storage.add_nodes` directly rather than the wrapper above, so a federated
  `SavedView` node adopted from a source graph on an older, unpatched build
  is not write-validated — though the read-side sanitizer still applies to it
  on every subsequent load, so it cannot render an unembedded URL either way.
  Tracked as a follow-up (`small-fix`-tagged Task node in the Corp planning
  graph).

So the property this section actually guarantees today is narrower than "no
remote resource anywhere": **no session annotation write persists a new
non-embedded image URL**. The GUI now creates image content too (clipboard
paste, file drop, and the annotation toolbox's file picker — see the `image`
GUI cell below), through the same validated endpoint described above.

## Persistence

Session snapshots and saved views store the complete annotation document.
Reload must accept v1 documents and legacy arrays of notes, labels, arrows
and groups. Persisted annotations must not write graph nodes or graph
edges.

## MCP access

`note` annotations (sticky notes) are exposed headlessly through
`list_sticky_notes` / `create_sticky_note` / `update_sticky_note` /
`delete_sticky_note` (`backend/service/mcp_tools.py`), so an agent can read
and edit them in a session before or independently of a connected browser,
using the same model-space coordinates and stable ids the canvas uses. Writes
go through the session op protocol (`annotation_created` / `annotation_updated`
/ `annotation_deleted`) and share its optimistic-concurrency contract
(`expected_revision` / `revision_conflict`) — see
`backend/DEVELOPMENT.md`'s "Sticky note tools" section for the full contract.
`update_sticky_note` additionally accepts the same optional `base_version` as
the generic `update_annotation` tool, rejecting a stale same-field write with
`field_conflict` instead of silently merging it — see [Field-level patches and
base_version](#field-level-patches-and-base_version) and "MCP path parity"
above.

The rest of the v1 model except `group` — `text`, `label`, `line` (`arrow`
accepted as a legacy alias), `shape`, `icon`, `vote_dot`, `image`,
`freehand` — is exposed the same way through a generic tool set:
`list_annotations` / `create_annotation` / `update_annotation` /
`delete_annotation` / `reorder_annotation` / `set_annotation_lock` /
`duplicate_annotation`, over the same session op protocol and
optimistic-concurrency contract. `update_annotation` additionally accepts an
optional `base_version` (read from a prior `list_annotations`/write result's
`annotation.version`) for the finer-grained, per-field conflict check
described in [Field-level patches and
base_version](#field-level-patches-and-base_version) — omitted, this tool
and every other write in this section keep the coarser
`expected_revision`/session-wide contract this paragraph already describes,
unchanged. This includes `freehand`: an earlier
revision of this document claimed it had no MCP tool at all, but
`freehand` has been a member of `GENERIC_ANNOTATION_TYPES`
(`backend/core/session_annotations.py`) since the type was added (#422),
which was already enough for every generic tool above to create, read,
move (translating its `points`, same as a `line`'s endpoints), restyle,
reorder, lock, duplicate and delete one — that claim was simply wrong, not
a gap that has since closed. `duplicate_annotation` did have a real, narrower
gap this task closed: it translated a `line`'s endpoints by the given
offset but not a `freehand` stroke's `points`, so a duplicated freehand
annotation kept its original geometry at a moved envelope position; it now
calls `translate_freehand_points` the same way `update_annotation`'s patch
builder already did.

`duplicate_annotation` is still explicitly headless: its own docstring says
it "only acts on the generic types `create_annotation` manages (not
`note`/`group`)". The GUI's **Duplicate** action ([Layer order](#layer-order))
does not change that boundary, because it does not go through this tool at
all — like every other GUI mutation here (Unlock included), it clones the
browser's own ReactFlow node and lets the existing local-mutation-plus-
session-sync path publish the resulting `annotation_created` op, the same
mechanism a toolbox "create" already uses. That path has no server-side
type restriction, which is exactly why `note` gets a working GUI Duplicate
despite having no MCP `duplicate_annotation` equivalent at all (`note` is
excluded from that tool, not merely from a subset of its behaviour) — the
GUI and MCP surfaces for duplication are two independent mechanisms that
happen to produce the same op shape, not one built on the other.

`note` stays on its own dedicated tool set; `group` (node-membership boxes)
has its own dedicated tool set too: `create_group_annotation` creates or
upserts the box (label/description/color/geometry/an optional starting
`member_node_ids`), and `update_group_members` adds and/or removes member
node ids by wrapping the `group_membership_changed` op — resolving the
current list against the requested change itself so a caller does not have
to read-modify-write the full membership. Like every other MCP annotation
write this is last-write-wins under a genuine race between two concurrent
calls, not a locked read-modify-write (`expected_revision` is the real
conflict-detection mechanism, same as the rest of the surface). Membership is
deliberately kept off `create_group_annotation`'s upsert-by-id path unless a
caller explicitly passes `member_node_ids`: the op merges by shallow
`dict.update`, so an upsert that always reset membership to `[]` when
omitted would fight `update_group_members`'s own writes any time a caller
recreated a group just to change its label or color. `delete_group_annotation`
removes the group box by id, the same revision-checked delete contract as
`delete_annotation`/`delete_sticky_note`; it removes only the box, not the
graph nodes named in `member_node_ids` — a group never owns those nodes as
annotations, so there is nothing else to cascade-delete, and this matches
the GUI's own "Delete Group" action (`GroupNode.jsx`'s
`removeGroupKeepChildren`), which un-parents and keeps every member node.

None of the three tool sets lets a write silently convert one annotation
type into another: creating or updating across the note/generic/group
boundary, or
replacing an existing generic annotation's id with a different type, is
refused rather than applied. See `backend/DEVELOPMENT.md`'s "Generic
annotation tools" and "Group annotation tools" sections for the full
contract, including the per-type `content` payload shape.

`image` is created through its own dedicated tool instead —
`create_image_annotation`, the only tool that sets image pixel content
(`create_annotation` refuses `type="image"`, and `update_annotation` refuses
a `content` carrying an `image` key; every other operation on an existing
image annotation stays on the generic tool set). Rather than taking a
`content` payload directly,
it takes the image itself (`image_data` or `image_url`), ingests it
server-side (`backend/core/image_ingest.py`: format/size validation,
downscaling, re-encoding as WebP) and stores the result as an embedded
`data:` URI in `content.image.url` — never the original remote link — so the
annotation still renders once the source disappears. It enforces its own
image-specific byte budgets in place of the small generic op-batch cap the
other writes share. See [Image ingest enforcement](#image-ingest-enforcement)
for the rule this exists to satisfy, and `backend/DEVELOPMENT.md`'s "Image
annotation tool" section for the full contract.

## Physical device acceptance

`freehand` targets stylus and touch input as first-class pointer sources,
not just mouse. V1 acceptance for `freehand` requires testing on at least
one physical touch/stylus device (not only a mouse-driven emulator), because
pointer pressure, palm rejection and coalesced-event sampling do not behave
identically under emulation. No such device acceptance pass exists yet.

`freehand` now has both a GUI creation entry point (the toolbox's "Freehand"
drawing mode, described above and in
[Canvas rendering](#canvas-rendering)) and its pre-existing MCP one (the
generic tool set), so a physical-device pass — which is inherently about real
pointer input reaching the canvas, not headless creation — can now be
scheduled; it just has not happened yet. What the GUI wiring does today,
verified only under jsdom/mouse-event emulation, not a physical device:
samples actual pointer events (coalesced samples via `getCoalescedEvents()`
when the browser reports them; `getPredictedEvents()` is never called,
matching `persist_predicted_points: false`), captures device pressure via
`event.pressure` onto persisted points when reported, and falls back to a
constant stroke width (rather than a velocity-derived one) for mouse/touch/
pressure-less-pen input. It also structurally suppresses concurrent input
while a stroke is active — `createFreehandStrokeCapture` tracks only the
first ("primary") pointer of a stroke, and a second pointer going down
mid-stroke is a no-op accompanied by a surfaced notice — but "structurally
suppresses" is not the same claim as "verified against a real palm-rejection
scenario on a physical touchscreen with an active pen," which is exactly what
the still-missing device pass would confirm or correct. Panning, marquee
selection and node dragging are disabled for the duration of the armed
drawing mode so a stroke does not fight the canvas's other gestures.

## Canvas rendering

`note`, `label` and `line` each have dedicated, interactive canvas UX, but not
an identical set — only `note` is resizable in the `NodeResizer` sense: it
supports drag, resize and inline text editing. `label` supports drag, inline
text editing and attachment to a node/annotation, but renders no
`NodeResizer` and has no box to resize (see the `label` row of the
[acceptance matrix](#acceptance-matrix)). `line` supports endpoint drag and
per-endpoint anchor/attach, but is not resizable either — its geometry is its
two endpoints, not a box — and has no inline text editing. The rest of the v1
model — `text`, `shape`, `icon`, `vote_dot`, `image`, `freehand` —
renders with
selection and drag-to-move for every kind, plus model-space resize (via the
same `NodeResizer` handles as `note`) for the kinds that carry an explicit
box size: `shape` and `image`. `text`, `icon` and `vote_dot` render
at a fixed intrinsic size and are not resizable. `freehand` is not resizable
either, and for a further reason: its shape is not in a box at all but in its
sampled `points`, so there is nothing for a resize to scale. The canvas
offers it no handles. The MCP tools do accept a `w`/`h` patch: the server
stores it and `list_annotations` echoes it back, and nothing ever draws from
it. Nor does it survive contact with a browser. `freehand` is one of the
three types whose canvas translator carries no size across —
`freehandAnnotationToOverlay` in `sessionAnnotations.js`, alongside the
`label` and `line` branches beside it, and unlike
`genericAnnotationToOverlay`, which does carry it for all five generic kinds
— so hydrating a stroke resets its box to the model's 160×96 default. The
next autosave that ships that annotation writes the default back over
whatever an agent set. When that happens depends on the client: a browser
already connected when the patch lands folds the server's real geometry into
its baseline while its overlay drops it, so the next autosave for any reason
at all carries the reset even though nobody touched the stroke; a browser
that loaded afterwards needs an actual edit to it first. Saving a view loses
it unconditionally — the document is rebuilt from the canvas overlays, which
never carried the box (`handleConfirmSaveView` →
`legacyMetadataToAnnotationDocument`), so every affected annotation in the
view is stored at the default. `rotation` is not affected for any of the
three: the same translators do carry that across in both directions. This is
the "unsized-geometry clobber" class of bug already closed for `icon`/
`vote_dot`/`text` (`smallfix-annotation-unsized-generic-geometry-clobber`),
still open for these three and tracked as
`smallfix-browser-clobbers-unsized-annotation-geometry`. Like the never-drawn `rotation`
below, it is a tracked gap rather than a decided non-goal: scaling the
sampled points on a `w`/`h` patch remains open, and would need that
translator fixed first, or an agent's resize would be undone by the next
client to touch the stroke. A locked annotation of any
generic kind hides its resize handles the same way a locked `note` does.
`text` and `shape` now have their own inline text editing too
(task-annotation-doubleclick-to-edit-text) — see below — so inline text
editing is no longer exclusive to `note`/`label`. `line` was never part of
that: its own dedicated UX above is endpoint attach/drag and anchoring, not
inline text.

Per-type property editors and GUI creation for these types are required v1
scope (see [Human authoring surfaces](#human-authoring-surfaces)), not a
non-goal. A right-click rotation control and, for `shape`, a subtype picker
now exist for every generic kind; image paste/upload now has a GUI path too
(toolbox file picker, clipboard paste, OS file drop); the bottom
toolbox/mobile sheet now also creates `icon` and `vote_dot` (each with a
fixed default), and `icon`'s right-click editor has its own picker grid over
the full vocabulary described below. Recoloring any generic kind is still
reachable only through the MCP tools, which is the gap the acceptance matrix
tracks, not the intended end state.

Each `shape` variant draws its own geometry (`SHAPE_STYLES` in
`GenericAnnotationNode.jsx`).

**Double-click inline text editing (`text`, `shape`).** Double-clicking a
`text` annotation — or any `shape` variant, including `process_arrow` — opens
inline editing, following NoteNode/LabelNode's established pattern exactly:
double-click to enter, blur or Escape to commit, live per-keystroke sync at
the shared 300ms text debounce (see [Operation timing and
leases](#operation-timing-and-leases)). `shape` gains a new optional `text`
content field for this — a caption on the shape, stored and read the same
free-form way as every other non-structurally-validated content field (see
[Attachment and detach behavior](#attachment-and-detach-behavior)'s
Validation note) — while `text`'s own `content.text` was already there. A
`shape`'s clip-path clips its own outline, so a caption centred in the
bounding box would spill past the visible figure at the corners for every
non-rectangular variant; `GenericAnnotationNode.jsx`'s `SHAPE_TEXT_INSET`
insets the text layer to the axis-aligned rectangle each variant's clip-path
is proven to contain (a derived, not eyeballed, region — see that constant's
comment) rather than growing the shape to fit. Growing was rejected
deliberately: it would fight `triangle`/`hexagon`/`rhombus`'s fixed aspect
ratios (`REGULAR_SHAPE_ASPECT`; there is no single side to grow that keeps
the figure regular) and would move the annotation's stored geometry as a side
effect of typing rather than of a deliberate resize gesture — inset-only
means `shape`'s width/height semantics, and everything resize/aspect-lock
does with them, are untouched by this. `icon`,
`vote_dot` and `image` are excluded too — none carries a free-text field in
the v1 content model (`vote_dot` carries no content field of its own at all
— task-annotation-vote-dot-simplify made it a plain coloured dot).

### Fill and border (`shape`)

`shape` carries two independent visual settings, `style.fill` and
`style.border`, each either a CSS colour string or the literal string
`"transparent"` (task-annotation-merge-frame-into-shape-rectangle) — the same
`style`-not-`content` convention `fontSize`/`font`/`textAlign` already use —
see [MCP access](#mcp-access) and `create_annotation`'s docstring
(`backend/service/mcp_tools.py`). An omitted `fill` defaults to a solid grey
(the same look every `shape` had before this field existed); an omitted
`border` defaults to `"transparent"` (no visible border) — so an existing
shape with neither field stored keeps rendering exactly as it did before.

This is what subsumes the retired `frame` kind: a `shape` (any variant, not
only `rectangle`) with `fill: "transparent"` and a coloured `border` is what
`frame` used to be — a box with no fill and a visible outline. There is no
migration from stored `frame` data to this — see [Unrecognised annotation
data](#unrecognised-annotation-data) — the equivalence is only that a user
or agent can now reach the same visual by configuring a `shape`.

**Rendering limitation, stated plainly.** `rectangle` and `circle` draw a
correct border (an axis-aligned CSS border, and a border on a
`border-radius: 50%` box, both render exactly as expected). The four
clip-path variants (`triangle`, `rhombus`, `hexagon`, `process_arrow`) do
not: a CSS `border` is drawn as a ring around the axis-aligned box and *then*
clipped by `clip-path` along with everything else, so only the portion of
that ring that survives the clip is visible — which is not the same as a
border tracing the polygon's own slanted edges (`GenericAnnotationNode.jsx`'s
`shape` render branch has the full derivation). Tracing the true outline
would need an SVG stroke or a second, larger clip-path per variant, which is
out of scope for this task; a border is still offered uniformly across every
variant rather than withheld from four of six, since a plain, imperfect
border is strictly more useful than none. Tracked as a possible follow-up,
not a decided non-goal.

**Attachment/snap-target candidacy is unaffected by fill/border.** A `shape`
remains a valid target for [Attachment and detach
behavior](#attachment-and-detach-behavior)'s drag-to-attach and the "nearby
object menu" regardless of its fill/border — including a transparent-fill
one that looks exactly like the old `frame`. This is a deliberate decision,
not an oversight: eligibility in `computeDroppedAttachment`
(`packages/ui-graph-canvas/src/utils/annotations.js`) and
`attachNearbyAnnotation` (`GraphCanvas.jsx`) is keyed on `node.type`
everywhere else (`group`, `arrow`) — introducing a content-dependent
exclusion just for one configuration of `shape` would be a new, inconsistent
kind of rule, and a surprising one: two visually-similar transparent-fill
shapes would attach differently depending on an internal field a user has no
reason to remember they set. Keeping `shape` uniformly attachable is the
smaller, more predictable change now that `frame` is no longer a distinct
type.

### Typography controls (`text`, `shape`)

`text` and `shape` (task-annotation-text-alignment-and-font) are the only two
kinds with editable free text (see above), so they are the only two kinds
this task adds typography to: a nine-position text-alignment grid
(`top-left` through `bottom-right`, matching `content.attachment.anchor`'s
box-position vocabulary in spirit though it is a separate field), a font-size
picker, and a curated font-family picker. All three live under `style`
(`style.textAlign`, `style.fontSize`, `style.font`), not `content` — the same
place `text`'s pre-existing `fontSize` already lived — and each is optional,
falling back independently to whatever the annotation already rendered as
before this task: `text` defaults to `top-left` (its plain block-flow layout
with no alignment rule at all), `shape`'s caption defaults to `middle-center`
(the centred layout `GenericAnnotationNode.css` used to hardcode), and both
default their font size to what was previously hardcoded in CSS (16px for
`text`, 14px for a `shape` caption) and their font family to the app's own
ambient font (no override). An existing annotation with none of these fields
stored therefore renders exactly as it did before this task.

**Font scope.** The font-family picker is a short curated list of CSS
*generic* font families (`GENERIC_FONT_FAMILIES` in
`packages/ui-graph-canvas/src/utils/annotations.js`: `serif`, `monospace`,
`cursive`, plus the unstyled default) rather than free-form font-name entry
or an uploaded font file. A generic family name is resolved by the viewer's
own browser/OS, so it renders predictably on every client with nothing to
ship or embed — the trade-off is a fixed, small set of looks rather than a
specific named typeface.

**`text` has no box, so its vertical alignment is inert today.** `text` is
not one of the kinds that carries an explicit box size
(`RESIZABLE_KINDS`/`SIZED_GENERIC_KINDS` in `GenericAnnotationNode.jsx`/
`annotations.js` — unchanged by this task) — it always renders exactly as
large as its own content, the same as before. The alignment control's
horizontal axis is still visible for `text` whenever the caption spans
multiple lines (typed line breaks): shorter lines sit left/center/right of
the widest one. The vertical axis (`top`/`middle`/`bottom`) has no visible
effect for `text` — with the box always equal to the content, there is no
extra room to place it in — until `text` itself becomes resizable, which
this task deliberately leaves out of scope (the same call `61d5cc7b` already
made for `icon`/`vote_dot`: making an unsized kind resizable is a separate,
bigger UX change). `shape`'s caption has a real box (the shape's own
`w`/`h`), so all nine positions are visibly distinct there today.

**MCP.** `create_annotation`/`update_annotation` read these three fields from
their existing `style` argument — see their docstrings
(`backend/service/mcp_tools.py`) — so no new MCP tool or parameter was added;
an agent setting `style.fontSize` on a `text`/`shape` annotation already goes
through the generic, un-typed `style` passthrough
(`backend/core/session_annotations.py`) that `text`'s `fontSize` already used
before this task.

An `icon` annotation draws the glyph its configured `content.icon` name
resolves to in the canvas package's icon set
(`packages/ui-graph-canvas/src/utils/annotationIcons.js`). That set now has a
canonical or aliased entry for **all 75 Bootstrap-icon names in the host
app's registry** (`ICON_REGISTRY` in
`frontend/web/src/components/FloatingToolbar.jsx`, the same vocabulary
`schema_config.json`'s `icon` field uses), each with its own distinct glyph,
plus everyday synonyms. The canvas package still has no access to the host
app's react-bootstrap-icons registry — and, separately, every generic
annotation renders its icon as plain text content rather than a mounted SVG
component, so reusing the host registry's React components was never a fit
regardless — so this remains its own self-contained text-glyph set rather
than a reference to the host's. A name that resolves to neither a canonical
key nor an alias (i.e. one the host registry does not carry, including any it
adds after this was written) draws the two-character abbreviation of itself
that the canvas drew before the set existed, rather than collapsing into one
neutral marker — so a future gap still never makes a name less distinguishable
than it was. (The synonym aliases do merge on purpose: `circle`/`dot`,
`check`/`ok`, `cross`/`x`, `warning`/`alert` and `lightbulb`/`idea` each draw
one glyph where they used to draw two different abbreviations — a meaningful
icon is worth more there than two distinct pairs of letters.) Full-registry
coverage is verified by `packages/ui-graph-canvas/tests/annotationIcons.test.js`,
which asserts every one of the 75 names resolves to a glyph and that all 75
glyphs are pairwise distinct.

`geometry.rotation` is drawn as a transform on the rendered element rather
than on the ReactFlow node wrapper, so hit-testing, dragging and resizing keep
operating on the unrotated bounding box. For the three kinds that are both
rotatable and resizable — `note`, `shape` and `image` — that has a
visible cost, not just a benign one: the `NodeResizer` outline and handles are
drawn axis-aligned around the unrotated box, so on a rotated annotation they
sit visibly askew from the object and a handle drag grows the box along the
unrotated axes. `shape` is where this is most reachable today: it is
toolbox-creatable and accepts a rotation through the generic MCP tools or the
GUI rotation control described below. `image` needs MCP or the GUI control to
create the object, but either can set the rotation. `note` no longer needs a
raw op to reach a non-zero rotation — the GUI control below writes it
directly, and `create_sticky_note`/`update_sticky_note` set one over MCP too
(see below). Rotation-aware resize handles are an open gap. The capability
baseline requires it for text/headings, labels/callouts, sticky notes,
images, icons/dots and basic shapes including the process arrow.

`line` and `freehand` are the two that do **not** draw it: their geometry
lives in endpoints and sampled points rather than in a box, so a rotation the
server accepts for them is stored and reported but never rendered. That is a
tracked gap in the [acceptance matrix](#acceptance-matrix), not a decided
non-goal. `group` never reaches this translation layer at all — its helpers
(`annotationsToGroups`/`groupsToAnnotations`) carry no rotation field, so a
group has no rotation to draw or preserve. They do carry `z` and `locked`.

**A GUI rotation control now exists.** Right-clicking a `note`, `label`,
`text`, `shape`, `icon`, `vote_dot` or `image` opens a property
editor with a rotation row: two step buttons (±15°) and a reset-to-0° button
that also displays the current angle (`GenericAnnotationNode.jsx` for the
five generic kinds; `NoteNode.jsx`/`LabelNode.jsx` for their own). It writes
`data.rotation` on the ReactFlow node the same way the pre-existing
color/text-size controls write their fields, so it round-trips through the same
`overlayToFlowNode`/`flowNodeToOverlay` (`annotations.js`) and
`sessionAnnotations.js` translators [already described](#canvas-rendering)
below — no MCP or backend change was needed. This closes `note`'s
tool-surface gap from the GUI side specifically: `note`'s rotation is now
reachable without any client posting a raw op by hand.

On the MCP tool surface, rotation is set through `create_annotation`/
`update_annotation`, `create_image_annotation` when creating an image, or
`create_sticky_note`/`update_sticky_note` when creating or editing a note —
those two take `rotation`, `z` and `locked` arguments (mirroring the generic
tools' conventions for the same fields), and `list_sticky_notes` reports all
three back alongside the rest of a note's envelope. The raw op endpoint
still accepts the same fields directly (`SessionStore.apply_state_op`), but
that is no longer the only path for a note: this closed the gap tracked in
the `note` row below.

`z`, `locked` and `rotation` round-trip through every annotation type's canvas
representation (`overlayToFlowNode`/`flowNodeToOverlay` in
`packages/ui-graph-canvas/src/utils/annotations.js`, and the server-model
translators in `frontend/web/src/utils/sessionAnnotations.js`): `z` maps to
the ReactFlow node's `zIndex`, `locked` maps to `draggable: false`, and
`rotation` travels on the flow node's `data`.

`group` is apart from this, and not only in degree: it never reaches
`overlayToFlowNode`/`flowNodeToOverlay`. Two of the three call sites filter on
`OVERLAY_TYPES`, which excludes `group`; the third — the `upsert-overlay`
branch of the remote-op effect — does not, and is kept clear one layer up
instead, by `App.jsx` routing an incoming `kind: 'group'` annotation to an
`upsert-group` op. That routing is load-bearing: `overlayToFlowNode` does not
itself refuse a group, it falls through to the line branch and would hand back
a `group` node carrying arrow `data`. Its envelope is carried instead by
`annotationsToGroups`/`groupsToAnnotations` in the browser's translators, by
the two group builders and `handleSaveView` in `GraphCanvas.jsx` on the canvas
side, and by `_annotation_document_to_legacy_metadata` in
`backend/service/views.py` on the server. It has no `rotation`, and its `z`
lands on the flow node's `data` rather than on `zIndex`, because a group's
paint order comes from the node array rather than from `zIndex` (see
[Layer order](#layer-order)).

Its `locked` refuses broadly what every other kind's does, with two
differences worth knowing. One is described under [Layer order](#layer-order)
above: the rename guard is stricter. The other is here — an unlocked group
resolves `draggable` to `undefined` rather than `true`, so it still defers to
the canvas-wide `nodesDraggable` switch the way it did before it was
lock-aware. ReactFlow tests `typeof node.draggable === 'undefined'`, so an
explicit `undefined` and an absent key behave alike, and a literal `true`
would override the switch.

This is the canvas UI's own enforcement of `locked` — the server never rejects
a write to a locked annotation — but which *tool* performs that write differs
by type:

- For the generic types (`text`/`label`/`line`/`shape`/`icon`/
  `vote_dot`/`image`), `reorder_annotation`, `set_annotation_lock` and
  `update_annotation` all still apply regardless of the annotation's current
  `locked` value.
- For `note`, those three refuse the id outright — they resolve every id
  through the generic annotation set (`_find_generic_annotation`), which
  excludes `note` by design (see [MCP access](#mcp-access)) — so none of the
  three "still apply" to a note the way the sentence above once implied.
  `update_sticky_note` is the equivalent write (`rotation`/`z`/`locked`
  alongside content/position/size), and `create_sticky_note` sets `locked`
  at creation time; neither tool checks the note's current `locked` value
  either, so the same "client enforces, server does not" property holds for
  notes — just through their own dedicated tool, not the generic three.
- For `group`, none of the three apply — group annotations are not exposed
  through the generic tool set at all (`create_group_annotation`/
  `update_group_members`/`delete_group_annotation` are its own dedicated set).
  `create_group_annotation` is the equivalent write: it takes `z` and `locked`
  and `build_group_annotation` persists both, so a group can be created or
  upserted locked and at a given layer, and like the note tools it does not
  check the group's current `locked` value. Only `rotation` is genuinely
  unmodelled for a group — `build_group_annotation` hardcodes it to 0.

A translator that dropped any of `z`/`locked`/`rotation` would make the
browser's own next autosave diff the annotation back to its `z: 0` /
`locked: false` / `rotation: 0` default and silently overwrite whatever a
collaborator or agent had just set.

## Keyboard, touch and screen-reader controls audit (v1 accessibility baseline)

Audit for `dec-annotation-v1-accessibility-and-touch` (accepted 2026-08-30),
run against `main` as of this PR by `task-annotation-accessibility-controls-audit`.
It maps what the six mechanism areas the decision names actually do today,
per annotation kind, so `task-annotation-accessible-shared-controls` (the
follow-up implementation task) starts from a verified baseline instead of a
guess. **This audit implements nothing** — every gap below is left for that
task, or named to the specific existing component/task that already owns it.

**Explicit non-claims.** This is a product-minimum audit, not a legal
accessibility/WCAG-conformance review — no conformance claim (A/AA/AAA, or
any WCAG success criterion) is made anywhere below, per the decision's own
`out_of_scope`. It also does not propose, and found no trace of, a separate
"list every annotation" panel — that alternative was considered and rejected
(see [Human authoring surfaces](#human-authoring-surfaces)); every mechanism
below is contextual, reached from the canvas object itself, the toolbox, or
the existing right-click/tap menus.

**How this was verified.** Three kinds of evidence appear below, and every row
says which kind it is:
- **Code** — read directly from the component source, cited by file and (where
  stable enough to cite) line.
- **Test** — a real, running assertion. Two new test files exercise the actual
  `packages/ui-graph-canvas` code, not a description of it:
  - `tests/AnnotationAccessibilityAudit.test.jsx` drives the real `reactflow`
    package (v11.11.4) unmocked — every other file in this suite mocks it out
    entirely (see that file's own doc comment), which means until this audit
    nothing in the suite had ever exercised ReactFlow's own default
    keyboard/focus/aria wiring against this app's actual `nodeTypes` and
    `<ReactFlow>` props. It proves, with real DOM events: the `role`/
    `tabIndex`/`aria-label` every annotation kind's node wrapper actually
    carries; that a keyboard-dispatched `contextmenu` event (what Shift+F10 /
    the Menu key fires) on the *focused* node wrapper does **not** open the
    annotation's own property menu, while the identical event dispatched on
    the inner content div (what a real right-click or long-press targets)
    does; that ArrowRight nudges a selected, focused node by ReactFlow's
    built-in 5px step with no app code involved; and that Enter selects an
    unselected node but never opens its text editor.
  - `tests/AnnotationAccessibleNameContent.test.jsx` computes the real
    accessible name (`dom-accessibility-api`'s `computeAccessibleName`, the
    same accname implementation Testing Library's `getByRole(..., {name})`
    uses) for each of the six kinds' actual rendered markup, wrapped in a
    synthetic `role="button"` container standing in for ReactFlow's real node
    wrapper. (It stands in rather than reusing the real wrapper because
    jsdom does no layout: `updateNodeDimensions` needs `window.DOMMatrixReadOnly`,
    which jsdom does not implement, so a real `<ReactFlow>` node can never
    reach `initialized: true` there and stays `visibility: hidden` — the
    same limitation `GroupMembershipMissingParentCrash.test.jsx`'s doc
    comment names. An accname computation correctly reports empty for hidden
    content regardless of what it contains, which would measure that jsdom
    gap, not this feature.)
  - Both files pass today (`npm run test:canvas` — 1025 tests including
    these), and the full `npm run test:unit` (all four workspaces, 1973
    tests) and `npm run lint` are green with the additions in this PR.
  - What Test rows prove: real DOM/accname behaviour under jsdom. What they do
    **not** prove: how a real screen reader (NVDA/JAWS/VoiceOver/TalkBack)
    actually announces any of this, or real touch-hardware behaviour (palm
    contact, long-press timing, coarse-pointer hit-testing) — those stay
    **UNTESTABLE-HERE**, deferred to
    `task-annotation-manual-accessibility-touch-acceptance`.
- **UNTESTABLE-HERE** — needs a real screen reader, real touch/pen hardware,
  or a live multi-client session; not something a headless test or a code
  read can settle. Deferred to `task-annotation-manual-accessibility-touch-acceptance`.

Six mechanism areas follow, matching the decision's own breakdown. Every kind
below means the same six components: `NoteNode.jsx`, `LabelNode.jsx`,
`ArrowNode.jsx`, `GroupNode.jsx`, `GenericAnnotationNode.jsx` (covers `text`,
`shape`, `icon`, `vote_dot`, `image`) and `FreehandAnnotationNode.jsx`.

### 1. Context menu / property editing

| Mechanism | Status | Evidence |
|---|---|---|
| Keyboard way IN to the property editor (Shift+F10 / Menu key, or a visible on-focus affordance) | **WORKS**, all six kinds — closed 2026-08-30 in two steps: `task-annotation-responsive-bottom-toolbox`'s edit-surface half (five of six, below), then `task-annotation-accessible-shared-controls` (the Shift+F10/Menu-key path itself, plus `group`'s own Edit button) | As of the first close: "a visible on-focus affordance" was no longer missing: `NoteNode.jsx`, `LabelNode.jsx`, `ArrowNode.jsx`, `FreehandAnnotationNode.jsx` and `GenericAnnotationNode.jsx` (covering `text`/`shape`/`icon`/`vote_dot`/`image`) each render a real `<button aria-label="Edit">` while selected, wired through the shared `hooks/useAnnotationEditTrigger.js`, that opens the same menu on click **or** Tab+Enter/Space — a genuine, always-visible, non-gesture path in, real Tab order and native activation, no synthetic-event trick needed. `GroupNode.jsx` was out of that task's scope and still had none. Test: `tests/AnnotationEditTrigger.test.jsx`. **Closed 2026-08-30** by `task-annotation-accessible-shared-controls`: `GraphCanvas.jsx`'s document-level keydown handler now also matches `e.key === 'ContextMenu'` or Shift+F10 while focus is on a `.react-flow__node` wrapper and clicks that wrapper's own `.annotation-edit-trigger` button (reusing the Edit button above rather than the dead `contextmenu`-dispatch approach), and `GroupNode.jsx` now gets that same Edit button too — see the "Update, 2026-08-30 (task-annotation-accessible-shared-controls — this task)" section below for the full mechanism and tests. |
| Arrow-key navigation between menu items | **WORKS**, all six kinds — closed 2026-08-30 by `task-annotation-accessible-shared-controls` | As of the audit: `ContextMenus.jsx`'s `useRootMenuKeyNav` (L91-102) implemented exactly this — ArrowUp/ArrowDown/Home/End roving focus — but was wired only into `NodeContextMenu`/`MultiNodeContextMenu`/`EdgeContextMenu`/`PaneContextMenu` (the graph-node/pane menu system), never into any of the six annotation kinds' own `<div className="graph-annotation-context-menu">` portals. **Closed 2026-08-30**: a new shared hook, `useAnnotationMenuKeyNav` (`ContextMenus.jsx`, reusing the same roving-index algorithm), is now wired onto all six kinds' own menu containers via `onKeyDown` — true whether the menu was opened by right-click or by the Edit button, since both render the identical menu markup. Test: `AnnotationAccessibleSharedControls.test.jsx`'s "menu keyboard navigation and focus trap" cases. |
| Focus trap while open | **WORKS**, all six kinds — closed 2026-08-30 by `task-annotation-accessible-shared-controls` | As of the audit: a menu opened via the Edit button moved focus to its first item on open, but nothing constrained Tab from leaving the menu while it stayed open — only `ContextMenus.jsx`'s `useMenuOpenFocus` (L48-74) did, again only for the graph-node/pane menu system. **Closed 2026-08-30**: the same `useAnnotationMenuKeyNav` hook that closed the row above also makes Tab on the last item / Shift+Tab on the first wrap rather than escaping the menu — a real, if minimal, trap. Test: `AnnotationAccessibleSharedControls.test.jsx`'s "Tab on the last item wraps to the first…" case. |
| Focus restored to the object on close | **WORKS**, all six kinds, both entry paths — closed 2026-08-30 in two steps: `task-annotation-responsive-bottom-toolbox`'s edit-surface half (Edit-button path, five of six), then `task-annotation-accessible-shared-controls` (generalised to the right-click path too, and to `group`) | As of the first close: a menu opened via the Edit button moved focus to its first item on open and returned focus to that same Edit button on close, but a menu opened the pre-existing way, by right-click, was completely unchanged (no focus move on open, no restore on close) — `hooks/useAnnotationEditTrigger.js`, test-covered in `tests/AnnotationEditTrigger.test.jsx`. **Closed 2026-08-30**: `useAnnotationEditTrigger.js` no longer gates that behaviour on `openedViaButtonRef` — every menu, however it opened, now moves focus to its first item on open; on close it restores to the Edit button for the button path, or to whatever had focus immediately before for the right-click path (mirroring `ContextMenus.jsx`'s `useMenuOpenFocus`). `group` gets the same Edit button (and so the same restore behaviour) for the first time too. |
| Touch: visible "Edit" entry point (tap object → visible Edit → contextual sheet), vs. still only right-click/long-press | **WORKS**, all six kinds — closed 2026-08-30 in two steps: `task-annotation-responsive-bottom-toolbox`'s edit-surface half (five of six), then `task-annotation-accessible-shared-controls` (`group`) | The same Edit button named above is the visible entry point the decision's flow describes: tapping it on a compact, integrated host (MobileShell wired via GraphCanvas's new `annotationEditSheetPortalContainer`/`onRequestAnnotationEditSheet` props) opens the identical property editor inside the shared mobile bottom sheet, on the pre-existing `'detail'` surface `useSurfaceManager` already reserved (`frontend/web/src/hooks/useSurfaceManager.js` — present, tested, unused until now). A non-integrated host (no `MobileShell`, e.g. `frontend/widget`) or a desktop pointer gets the same button opening a floating menu anchored to it instead — never nothing. Long-press/right-click keeps working unchanged alongside it; this is an addition, not a replacement. Test: `tests/AnnotationEditTrigger.test.jsx`, `frontend/web/tests/MobileShell.test.jsx`'s "Edit sheet (detail surface)" cases. `group` was out of that task's scope; **closed 2026-08-30** by `task-annotation-accessible-shared-controls` wiring `GroupNode.jsx` through the same `useAnnotationEditTrigger`. |

### 2. Canvas focus

| Mechanism | Status | Evidence |
|---|---|---|
| Keyboard-reachable selection (Tab / roving tabindex), focused one visibly marked | **WORKS**, all six kinds — via ReactFlow's own default, not app code | Test: `AnnotationAccessibilityAudit.test.jsx`'s role/tabIndex assertions (all seven rendered kinds get `role="button"` and `tabindex="0"`). Code: `GraphCanvas.jsx`'s `<ReactFlow>` (L3413-3457) never sets `nodesFocusable={false}` or `disableKeyboardA11y`, so ReactFlow 11.11.4's own defaults apply — every node gets `tabIndex=0`/`role="button"`/native keyboard handling (`@reactflow/core` `NodeWrapper`, `node_modules/@reactflow/core/dist/esm/index.js` ~L3029). "Visibly marked" is CSS-driven (the `.selected` class each kind's own stylesheet already styles for mouse-click selection) and applies identically whether selection arrived via click or via ReactFlow's own Enter/Space keyboard path — confirmed by the Enter-selects test. This is real, in-app behaviour (it is what actually runs in the browser today), but it is a library default this repo's own code never touched, not a designed keyboard-selection feature — worth naming as such so a future change to `nodesFocusable` doesn't silently regress it unnoticed. |
| Arrow-key move of a selected node (non-drag alternative for position) | **WORKS**, all six kinds — same ReactFlow default | Test: `AnnotationAccessibilityAudit.test.jsx` — ArrowRight on a selected, focused `label` node moves it exactly 5px (20px with Shift, ReactFlow's own default step, `arrowKeyDiffs`/`useUpdateNodePositions` in `@reactflow/core`). Confirmed for a real controlled `nodes`/`onNodesChange` prop wiring matching `GraphCanvas.jsx`'s own. This closes part of the decision's "non-drag alternative for position" requirement for free-floating annotations already — the gap is size/rotation/attachment (see area 4), not position. |
| Focus/selection state announced to assistive tech | **UNTESTABLE-HERE** | Code: ReactFlow's own `A11yDescriptions`/`AriaLiveMessage` (`@reactflow/core`, `ARIA_NODE_DESC_KEY`) does announce arrow-key moves via an `aria-live` region when `disableKeyboardA11y` is not set (it is not, here) — but whether a real screen reader actually announces *selection itself* (as opposed to a move) depends on how it interprets `role="button"`/`aria-describedby` with no `aria-label` (see area 6), which needs a live AT pass. Deferred to `task-annotation-manual-accessibility-touch-acceptance`. |

### 3. Creation

| Mechanism | Status | Evidence |
|---|---|---|
| Every toolbox button keyboard-activatable (desktop toolbar and, since PR #525, the mobile sheet) | **WORKS** | Code: `AnnotationToolbox.jsx`'s `renderToolboxItem` (L437-508) and the shape/icon slot's main button (L520-648, L650-738) are all real `<button type="button">` elements with `aria-label` — native Tab/Enter/Space support, no pointer-only wiring. The mobile sheet (`variant="sheet"`, L206-210) renders the exact same component/buttons, just always-expanded with no collapse toggle — same keyboard path. |
| Shape/icon tool-slot pickers (their own fold-out UI) keyboard-operable | **WORKS** | Code: the corner button opening each picker is a second, independently focusable real `<button>` (`aria-haspopup`, `aria-expanded` — `AnnotationToolbox.jsx` L612-622, L709-719); `ToolSlotPicker.jsx` auto-focuses its first option on open (L62), traps Escape to close (L116-125), and restores focus to the corner button on close (L83-92) unless focus already moved elsewhere. This is the "owner-directed accessible affordance (2026-08-26)" the component's own doc comment names, and it is real, not aspirational — read directly, matching the pre-existing `ContextMenus.jsx` focus-restore pattern (area 1's contrast). |
| Activating one creates at a sensible location, focused, ready to edit | **WORKS** (toolbox click/keyboard creation) — closed 2026-08-30 by `task-annotation-accessible-shared-controls` | As of the audit: `createAnnotationAtViewportCenter`/`createAnnotation` (`GraphCanvas.jsx`) placed the new node at the viewport centre (sensible location: ✅) but never set `selected: true` and never focused the new DOM node — confirmed by reading every branch of `createAnnotation`; no branch touched `selected` or called `.focus()`. **Closed 2026-08-30**: `createAnnotation` now marks the new node `selected: true` (deselecting whatever was selected before) and focuses its DOM wrapper one frame later, so a keyboard user who activates a toolbox button lands on a selected, focused annotation with its Edit button one Tab away. Scoped to click/keyboard toolbox creation; `freehand`'s own pointer-drawn creation is unchanged (there is no keyboard equivalent to make "ready" for a gesture-drawn stroke). The focus call itself is now also conditional: it is skipped while the mobile `'annotate'` `BottomSheet` (`aria-modal="true"`) is still open, so it never moves DOM focus onto a canvas node hidden behind that modal sheet — a bug found and closed in the same task's own follow-up review, see `GraphCanvas.jsx`'s `createAnnotation` and `GraphCanvasAccessibleSharedControls.test.jsx`'s "creation focus must never escape an open modal mobile sheet" cases. Test: `AnnotationAccessibleSharedControls.test.jsx`'s "keyboard creation lands selected and focused" case. |

### 4. Move, resize, rotation, attachment (non-drag alternatives)

| Mechanism | Status | Evidence |
|---|---|---|
| Non-drag position (nudge) | **WORKS**, all six kinds when unlocked/unanchored (whatever `isDraggable` already allows for that kind — e.g. an anchored `arrow` endpoint stays undraggable either way) | Same evidence as area 2's arrow-key row — ReactFlow's built-in 5px/20px-shift nudge fires for any selected node whose `draggable` is true, which every kind computes the same way for both mouse-drag and this keyboard path (`isAnnotationDraggable`, `utils/annotations.js`). |
| Non-drag rotation | **WORKS** — closed 2026-08-30 by `task-annotation-accessible-shared-controls` | As of the audit: `NoteNode.jsx` L289-315, `LabelNode.jsx` L226-252, `GenericAnnotationNode.jsx`'s rotation row L1182-1208 all rendered ±15°-step buttons plus a reset — real `<button>`s, keyboard-operable **once the menu is open** — but opening the menu was itself keyboard-unreachable (area 1), so this control was only reachable by a user who could right-click/long-press. **Closed 2026-08-30**: closing area 1's Shift+F10/Menu-key gap closes this one for free, exactly as predicted — the same rotate buttons are now reachable keyboard-only end to end via the Edit button/Shift+F10 path. |
| Non-drag resize | **WORKS** — closed 2026-08-30 by `task-annotation-accessible-shared-controls` | As of the audit: resize was exposed only via ReactFlow's `NodeResizer` drag handles (`NoteNode.jsx`, `GenericAnnotationNode.jsx`'s `resizer`, `GroupNode.jsx`) — no numeric width/height input existed anywhere in any of the six context menus. **Closed 2026-08-30**: a new shared component, `AnnotationSizeControl.jsx` — two real `<input type="number">` fields (`aria-label`ed Width/Height) plus an Apply button — reads the node's current `style.width`/`height` on mount and commits via the same `setNodes`/`notifyChange('geometry')` path a drag-resize uses, offered by every kind that already has a NodeResizer box: `NoteNode`, `GenericAnnotationNode`'s `shape`/`image`, and `GroupNode`. On a rotated Note/shape/image it compensates `position` the same way drag-resize's own `resolveRotatedResizeGeometry` does (a follow-up bug fix, same task — the control originally left a rotated box's position uncompensated, visibly jumping it on apply; see `resolveRotatedResizeGeometry`'s own comment in `utils/annotations.js` for why a rotated box's centre has to shift). Test: `AnnotationAccessibleSharedControls.test.jsx`'s "non-drag width/height" cases, including the rotated-position regression coverage. |
| Non-drag attachment — attaching an **existing** annotation to a target ("Attach to" enters target-tap mode) | **WORKS** — closed 2026-08-30 by `task-annotation-accessible-shared-controls` | As of the audit: no attach-mode/target-tap mechanism existed anywhere in `GraphCanvas.jsx`/`utils/annotations.js`. The only two attach paths were (a) drag-to-snap (`computeDroppedAttachment`, a drag gesture) and (b) the "Nearby object menu" (`NearbyObjectMenuSection`), which only ever created a **new**, pre-attached label/icon/text at creation time — no path to attach an annotation that already exists, and both existing paths were themselves reached only from a right-click menu. **Closed 2026-08-30**: an annotation's own menu (for the three `ATTACHABLE_OVERLAY_KINDS` — `label`, `text`, `icon`) now gets an "Attach to…" button that puts `GraphCanvas.jsx` into a one-annotation attach-pending state, surfaced by a real `status`-role banner with a Cancel button; the next click on an eligible node/annotation resolves the attachment via a new `computeAttachmentToTarget` helper (refactored out of `computeDroppedAttachment` so the two share one implementation). A new "Detach" button (same three kinds) clears it without dragging. Escape, or clicking empty canvas, cancels the mode. Test: `AnnotationAccessibleSharedControls.test.jsx`'s "non-drag 'Attach to…' wiring" cases. |
| Overlapping objects: a visible way to choose which one you mean | **WORKS** — closed 2026-08-30 by `task-annotation-accessible-shared-controls` | As of the audit: no overlap/cycle/z-order-pick mechanism existed — clicking a point with multiple annotations stacked on it hit whichever ReactFlow's own top-most-in-DOM-order hit test picked, with no alternative offered. **Closed 2026-08-30**: `GraphCanvas.jsx`'s new `onNodeClick` handler computes every node whose stored box contains the click's flow position (`nodesAtPoint`, `group` excluded as a near-always-overlapping backdrop) without calling `preventDefault`/`stopPropagation` — ReactFlow's own default selection still happens exactly as before. When more than one candidate exists, a small picker (labelled by each candidate's own computed accessible name) offers the rest; picking one selects and focuses exactly that node. A rotated annotation's true outline is not checked — the hit test is the stored axis-aligned box, a stated approximation, not a silent one. Test: `GraphCanvasAccessibleSharedControls.test.jsx`'s "the overlap-object picker" cases. |

### 5. Touch

| Mechanism | Status | Evidence |
|---|---|---|
| Editing an **existing** annotation via tap alone (tap → visible Edit → sheet) | **WORKS**, all six kinds — closed 2026-08-30 in two steps: `task-annotation-responsive-bottom-toolbox`'s edit-surface half (five of six), then `task-annotation-accessible-shared-controls` (`group`) | Same evidence as area 1's touch row. Long-press/right-click still works too (never removed), but is no longer the only path — the decision's "hidden behind long-press... alone" bar is met. `group` was out of the first task's scope; **closed 2026-08-30** by `task-annotation-accessible-shared-controls` wiring `GroupNode.jsx` through the same trigger. |
| Multi-select and its align/distribute actions (PR #524) reachable via tap-based multi-select | **WORKS** — closed 2026-08-30 by `task-annotation-accessible-shared-controls` | As of the audit: `GraphCanvas.jsx`'s `<ReactFlow>` set `multiSelectionKeyCode={['Shift', 'Meta', 'Control']}` — ReactFlow's own modifier-held-while-clicking convention, with no touch equivalent, and no dedicated "select multiple" mode toggle existed anywhere. The align/distribute actions themselves (`alignSelectedNodes`/`distributeSelectedNodes`, surfaced via `MultiNodeContextMenu` — PR #524) were real and reachable once a multi-selection existed, but nothing got a touch user to that multi-selection. **Closed 2026-08-30**: a new toggle (`aria-pressed`, labelled "Select multiple") in the compact/touch control cluster (`isCompact` only). While active, `onNodeClick` restores whichever other nodes were selected immediately before the tap ReactFlow's own click-to-select just cleared — net effect, each tap ADDS to the selection, the touch equivalent of holding Shift/Ctrl. Toggling an already-selected node back OFF individually is not yet supported (tapping empty canvas still clears the whole selection) — a stated scope boundary, not a hidden one. Test: `GraphCanvasAccessibleSharedControls.test.jsx`'s "explicit touch multi-select mode" cases. |
| Real touch-hardware behaviour (hit-testing at coarse-pointer precision, long-press timing, palm contact) | **UNTESTABLE-HERE** | Needs physical touch/pen hardware, same category `freehand`'s own [Physical device acceptance](#physical-device-acceptance) section already names for that kind — deferred to `task-annotation-manual-accessibility-touch-acceptance`. |

### 6. Screen-reader semantics (role + accessible name)

The decision's own bar is explicit: *"role + accessible name per annotation,
e.g. 'sticky note, Budget Q3'"* — a name that **says what the thing is**, not
merely any string an accname algorithm happens to fall back to.

All ten rows below are **WORKS — closed 2026-08-30 by `task-annotation-accessible-shared-controls`**. The "Accessible name today" column is left as the pre-fix, audit-time description (still true of the raw ReactFlow/DOM fallback these kinds no longer rely on); the fix is one shared function, `computeAnnotationAriaLabel` (`utils/annotations.js`), wired at `GraphCanvas.jsx`'s `nodesWithAriaLabels` (a `useMemo` over `nodes`, writing `node.ariaLabel` immediately before `<ReactFlow>`) so every kind's name recomputes on the very next render rather than going stale after a later edit. Every kind word is an i18n-props `labels` default (`ariaKindNote`, `ariaKindIcon`, …), not hardcoded English. Test: `AnnotationAccessibleSharedControls.test.jsx`'s `computeAnnotationAriaLabel` suite; `AnnotationAccessibleNameContent.test.jsx` (pre-fix baseline, now historical).

| Kind | Role | Accessible name today | Status |
|---|---|---|---|
| `note` | `role="button"` (ReactFlow default, area 2) | **Closed:** `"Sticky note, {text}"`, or `"Sticky note"` when empty | Previously fell back to its own text, or the placeholder word "Note" when empty — `AnnotationAccessibleNameContent.test.jsx`'s `{text:'Budget Q3'}` computed to exactly `"Budget Q3"`, not `"sticky note, Budget Q3"`. |
| `label` | same | **Closed:** `"Label, {text}"` | Previously fell back to its own text with no kind word at all. |
| `text` | same | **Closed:** `"Text, {text}"` | Previously fell back to its own text; empty when uncaptioned. |
| `shape` | same | **Closed:** `"{subtype} shape, {caption}"`, e.g. `"rectangle shape, Phase 1"` — every subtype distinguishable by name | Previously fell back to its caption when set, **empty** when uncaptioned — a rectangle, circle, triangle, rhombus, hexagon and process-arrow were indistinguishable from each other and from nothing at all. |
| `icon` | same | **Closed:** `"{name} icon"`, e.g. `"star icon"` | Previously fell back to the rendered **glyph character** (e.g. "★"), not the icon's name — `title={data.icon}` was never reached because the glyph's own text content won the accname computation first. |
| `vote_dot` | same | **Closed:** a fixed `"Vote dot"` | Previously **empty** — no text, no title anywhere in its markup. |
| `image` | same | **Closed:** `"Image, {alt}"`, or `"Image"` alone | Previously fell back to `alt` when set, empty otherwise — never said "image annotation". |
| `arrow`/`line` | same | **Closed:** a fixed `"Arrow"` | Previously **empty** — pure SVG, no text or title anywhere. |
| `freehand` | same | **Closed:** a fixed `"Freehand stroke"` | Previously **empty** — pure SVG path(s), no text or title. |
| `group` | same | **Closed:** `"Group, {label}"`, or `"Group"` alone — never the untranslated `"📁 Group"` fallback | Previously fell back to its header text (folder glyph + label) when named, `"📁 Group"` (untranslated) when not. |

**Root cause, one line for all ten rows:** `overlayToFlowNode` and
`createAnnotation` (`packages/ui-graph-canvas/src/utils/annotations.js`
L355-457 and `GraphCanvas.jsx` L1580-1681) never set a ReactFlow node's
`ariaLabel` field for any kind, and ReactFlow only ever writes `aria-label`
from that field (`node.ariaLabel`, `@reactflow/core`'s `NodeRenderer`) — grepped
the whole package for `ariaLabel` (outside this audit's own tests and the
unrelated `ToolSlotPicker`/`ContextMenus`/`AnnotationToolbox` prop of the same
name): zero matches. This is one shared mechanism, not ten per-kind fixes —
`task-annotation-accessible-shared-controls` can likely close every row above
with one function that derives a name per kind (`"sticky note, {text}"`,
`"label, {text}"`, `"{shape} shape, {caption}"`, `"{icon} icon"`, `"vote
dot"`, `"image, {alt}"`, `"arrow"`, `"freehand stroke"`, `"group, {label}"`,
with a sensible untranslated-but-present fallback for an empty caption/label)
and writes it onto `data.ariaLabel`/the node's `ariaLabel` field at both
`overlayToFlowNode` (hydration) and `createAnnotation` (creation).

*(This paragraph is the pre-fix, audit-time diagnosis, kept for the reasoning
trail — see the table above and `computeAnnotationAriaLabel` in
`utils/annotations.js` for what actually shipped 2026-08-30, which took the
`node.ariaLabel`-on-write approach sketched above and generalised it into one
`useMemo` over every node rather than writing it at each individual mutation
site.)*

**UNTESTABLE-HERE for all ten rows:** how a real screen reader actually
announces `role="button"` plus whatever name ships (current or fixed) —
verb choice ("button" vs. no role announcement at all for some AT/browser
combinations), whether `aria-describedby`'s ReactFlow-authored move-hint
text is read before or after the name, and locale behaviour. Deferred to
`task-annotation-manual-accessibility-touch-acceptance`.

### Summary

Of roughly 40 mechanism × kind cells audited: real, verified **WORKS** exist
for keyboard node selection, arrow-key position nudge, and toolbox creation
(all three real ReactFlow/app defaults, not assumptions) — genuine progress a
stale writeup would have missed, per PR #524/#525 and the earlier shape/icon
picker and typography work.

**Update, 2026-08-30 (same day, later in the sorting order):**
`task-annotation-responsive-bottom-toolbox`'s edit-surface half closed the
audit's headline finding — the decision's touch flow (tap → visible Edit →
contextual sheet) now exists, for five of the six kinds (`note`, `label`,
`arrow`, `text`/`shape`/`icon`/`vote_dot`/`image` via
`GenericAnnotationNode`, `freehand`; `group` was out of that task's scope). A
real `<button aria-label="Edit">`, shown on selection, is a second, always-
visible path into the exact same property editor `onContextMenu` already
opened — reachable by tap, by mouse click, and by keyboard (Tab, then Enter
or Space) — and on a compact, integrated host it opens inside the shared
mobile bottom sheet (the pre-existing, previously-unused `'detail'` surface)
rather than a floating menu. Long-press/right-click keep working exactly as
before; this is additive. Opening via the new button also moves focus into
the menu and restores it to the button on close — the "focus restored to the
object" row now partly closed too, scoped to that one entry path. See areas 1
and 5 above for the row-level detail, and `hooks/useAnnotationEditTrigger.js`
for the mechanism every one of those five kinds shares.

**Still open**, unaffected by the above: arrow-key navigation *within* an
open menu, a real focus *trap*, generalising focus-move/-restore to the
right-click path too, `group`'s own Edit affordance, non-drag resize,
attach-to-target mode for an existing annotation, an overlap-object picker,
touch multi-select, and a role-appropriate accessible name for any kind.
Every one of these remains scoped to `task-annotation-accessible-shared-
controls`, which this audit's evidence (test files above, plus this
section's file:line citations — now naming the code as it stood before the
update above; re-verify a citation before treating it as current) gives a
concrete starting point rather than a re-discovery task.

**Update, 2026-08-30 (task-annotation-accessible-shared-controls — this
task).** Closes the "Still open" list above, in full, across all six kinds
plus `group`, verified by three new test files
(`AnnotationAccessibleSharedControls.test.jsx`,
`GraphCanvasAccessibleSharedControls.test.jsx`,
`AnnotationEditTriggerReachability.test.jsx` — the last one real-`reactflow`,
unmocked, following `AnnotationAccessibilityAudit.test.jsx`'s own pattern for
exactly the reason that file states: jsdom's `visibility:hidden` artifact
would otherwise mask a mock-only pass):

- **Accessible name (area 6, all ten rows).** One shared function,
  `computeAnnotationAriaLabel` (`utils/annotations.js`), derives a per-kind
  name — `"Sticky note, {text}"`, `"Label, {text}"`, `"Text, {text}"`,
  `"{shape} shape, {caption}"`, `"{icon} icon"`, `"Vote dot"`, `"Image,
  {alt}"`, `"Arrow"`, `"Freehand stroke"`, `"Group, {label}"` — matching the
  decision's own "sticky note, Budget Q3" bar. It is wired at exactly one
  point, `GraphCanvas.jsx`'s `nodesWithAriaLabels` (a `useMemo` mapped over
  `nodes` immediately before the `<ReactFlow>` element, writing `node.
  ariaLabel` for the array ReactFlow actually renders), rather than at
  `overlayToFlowNode`/`createAnnotation`/every remote-op/group-upsert branch
  individually — a node's `ariaLabel` is therefore always current (a rename,
  recolour or retype recomputes it on the very next render) rather than
  something that could go stale the way a value written once at creation
  time would after the annotation is later edited. Every kind word is a
  `labels`-prop default (`ariaKindNote`, `ariaKindIcon`, …), following this
  package's existing i18n-props rule — not a hardcoded English string —
  threaded through `AnnotationContext`, `GraphCanvas.jsx`'s own `cml`
  defaults, `App.jsx` and both `en.json`/`sv.json`.
- **Keyboard way in (area 1) — the literal Shift+F10/Menu-key gap.**
  `GraphCanvas.jsx`'s existing document-level keydown handler now also
  matches `e.key === 'ContextMenu'` or `Shift+F10`: when either fires while
  focus is on a `.react-flow__node` wrapper, it finds that wrapper's own
  `.annotation-edit-trigger` button and clicks it — reusing the exact visible
  Edit button task-annotation-responsive-bottom-toolbox built, rather than
  trying to make the dead `contextmenu`-event-on-the-wrapper dispatch work.
  `group` gets that same Edit button for the first time (it was the one kind
  task-annotation-responsive-bottom-toolbox left out), wired through
  `useAnnotationEditTrigger` exactly like the other five, so the Shift+F10
  path — and the pointer/touch Edit-button path — now reach it too.
- **Arrow-key menu navigation and a focus trap (area 1).** A new shared hook,
  `useAnnotationMenuKeyNav` (`ContextMenus.jsx`, alongside — and reusing the
  same roving-index algorithm as — the pre-existing `useRootMenuKeyNav` the
  graph-node/pane menu system already had), wired onto all six kinds' own
  menu containers via `onKeyDown`: ArrowUp/ArrowDown/Home/End rove focus
  across the menu's real `<button>`s, and Tab on the last item / Shift+Tab on
  the first wraps rather than escaping the menu.
- **Focus move-in/restore-out generalised to the right-click path (area 1).**
  `useAnnotationEditTrigger.js` no longer gates its focus-move-on-open/
  restore-on-close behaviour on `openedViaButtonRef` — every menu, however it
  opened, now moves focus to its first item on open; on close it restores to
  the Edit button for the button path, or to whatever had focus immediately
  before for the right-click path (mirroring `ContextMenus.jsx`'s
  `useMenuOpenFocus`).
- **Ready to edit on create (area 3).** `createAnnotation`
  (`GraphCanvas.jsx`) now marks the new node `selected: true` (deselecting
  whatever was selected before) and focuses its DOM wrapper one frame later —
  a keyboard user who activates a toolbox button lands on a selected,
  focused annotation with its Edit button one Tab away, rather than having to
  hunt for what was just created. Scoped to click/keyboard toolbox creation;
  `freehand`'s own pointer-drawn creation is unchanged (drawing a stroke is
  inherently a pointer gesture — there is no keyboard equivalent to make
  "ready" here beyond what area-2's existing keyboard selection already
  gives the finished stroke).
- **Non-drag resize (area 4).** A new shared component,
  `AnnotationSizeControl.jsx` — two real `<input type="number">` fields (`aria-label`ed Width/Height) plus an Apply button, reading the node's current
  `style.width`/`height` on mount and committing via the same `setNodes`/
  `notifyChange('geometry')` path a drag-resize uses — offered by every kind
  that already has a NodeResizer box: `NoteNode`, `GenericAnnotationNode`'s
  `shape`/`image` (its `RESIZABLE_KINDS`), and `GroupNode`. `label`/`text`/
  `icon`/`vote_dot`/`arrow`/`freehand` render it nowhere, matching exactly
  what their own drag handles already do (or do not) offer — see [Canvas
  rendering](#canvas-rendering) for which kinds have no box to resize at all.
- **Non-drag "Attach to…" target-tap mode (area 4).** A new cross-cutting
  mechanism: an annotation's own menu (for the three `ATTACHABLE_OVERLAY_
  KINDS` — `label`, `text`, `icon`) gets an "Attach to…" button that puts
  `GraphCanvas.jsx` into a one-annotation attach-pending state
  (`attachModeId`), surfaced by a real `status`-role banner with a Cancel
  button (never only "long-press" or a bare instruction); the next click on
  an eligible node/annotation (any kind but `group` or the annotation itself
  — `isEligibleAttachTarget`, matching `computeDroppedAttachment`'s own
  candidacy rule) resolves the attachment via a new `computeAttachmentToTarget`
  helper (refactored out of `computeDroppedAttachment` so the two share one
  implementation), keeping the annotation's current on-screen offset from the
  target's centre — the same "free fine adjustment" a drop-to-snap gives. A
  new "Detach" button (same three kinds, shown once `data.attachment` is set)
  clears it without needing to drag the annotation away. Escape, or clicking
  empty canvas, cancels the mode.
- **Overlap-object picker (area 4).** `GraphCanvas.jsx`'s new `onNodeClick`
  handler (previously unset — node click-to-select was entirely ReactFlow's
  own default) computes every node whose *stored* box contains the click's
  flow position (`nodesAtPoint`, a new `utils/annotations.js` export;
  `group` excluded as a near-always-overlapping backdrop) without ever
  calling `preventDefault`/`stopPropagation` — ReactFlow's own default
  selection of the clicked node still happens exactly as before. When more
  than one candidate exists, a small picker (`role`-plain button list, each
  labelled by its own computed accessible name) offers the rest; picking one
  selects exactly that node and focuses it. A rotated annotation's true
  outline is not checked — the hit test is the stored axis-aligned box, a
  conservative approximation stated plainly rather than silently shipped.
- **Explicit touch multi-select mode (area 5).** A new toggle
  (`aria-pressed`, labelled "Select multiple") in the compact/touch control
  cluster (`isCompact` only — this is specifically the touch-first
  equivalent of holding Shift/Ctrl, which a touchscreen has no key for).
  While active, `onNodeClick` restores whichever other nodes were selected
  immediately before the tap ReactFlow's own click-to-select just cleared —
  net effect, each tap ADDS to the selection. Toggling an already-selected
  node back OFF individually is not yet supported (tapping empty canvas
  still clears the whole selection, same as always) — stated as a real, not
  hidden, scope boundary rather than claimed as done. The pre-existing align/
  distribute bulk actions (PR #524) are reachable from that multi-selection
  exactly as they already were from a Shift/Ctrl-built one.

**Still genuinely open, not fabricated as done:** everything this section's
own **UNTESTABLE-HERE** rows named all along — a real screen reader's actual
announcement of any of the above, and real touch/pen hardware behaviour for
the new touch-first controls (multi-select toggle, Attach-to/overlap-picker
taps, the size-control number inputs on a real on-screen keyboard) — both
still deferred to `task-annotation-manual-accessibility-touch-acceptance`; no
physical-device or live-AT pass happened in this task, and none is claimed
here. The mixed-selection/type exception matrix (locked, remote-leased, vote-
dot-unattachable, group's own background rule) is exercised by this task's
own tests per mechanism (each new control's own lock/lease guard, matching
the pattern every existing menu action already used) rather than gathered
into one separate table — the per-kind/per-mechanism rows above and the
acceptance matrix below are that mapping.

## Acceptance matrix

A downstream task or PR may claim a row **done** only when every cell in
that row is satisfied end to end (not merely coded, but exercised by a test
or, for the device column, a physical-device pass). ✅ = satisfied today,
⚠ = partially satisfied, ❌ = not started, ⬜ = no acceptance test defined
yet (status unknown, treat as not done). See [Downstream closure
rule](#downstream-closure-rule).

| Type | GUI create/edit | MCP create/edit | Persistence/reload/saved views | Realtime/collaboration | Activity/undo | Accessibility/device |
|---|---|---|---|---|---|---|
| `note` | ✅ toolbox create, inline edit, drag/resize, rotate/recolor/resize-text/layer/duplicate/opacity (right-click or, since task-annotation-responsive-bottom-toolbox, the Edit button — see [Human authoring surfaces](#human-authoring-surfaces)) | ⚠ `create_sticky_note`/`update_sticky_note` take `rotation`, `z` and `locked` (mirroring the generic tools' fields for the same); `list_sticky_notes` reports all three back — the generic `reorder_annotation`/`set_annotation_lock` still refuse note ids by design, but the dedicated tools now cover the same ground. Neither dedicated tool takes `style`/`opacity` at all, unlike the generic create/update tools' `style` dict — a note's new opacity control is GUI-only; an agent cannot set it | ✅ (the GUI-set value survives the round trip; an agent simply has no tool parameter to set it in the first place — see the previous column) | ✅ op broadcast + revision | ✅ actor-scoped undo | ⚠ audited 2026-08-30, updated same day by `task-annotation-responsive-bottom-toolbox` (see [audit](#keyboard-touch-and-screen-reader-controls-audit-v1-accessibility-baseline)): keyboard node selection + arrow-key nudge work (ReactFlow default); a visible Edit button now opens the same property menu by click/tap/keyboard — text-edit entry (double-click) is still mouse/touch-only. **Update 2026-08-30 (task-annotation-accessible-shared-controls):** a designed accessible name ("Sticky note, {text}"), Shift+F10 keyboard reachability, menu arrow-nav/focus-trap, non-drag resize and selected-and-focused-on-create are now real and test-covered (see the audit's own later dated addendum); a real screen-reader/physical-device pass is still deferred to `task-annotation-manual-accessibility-touch-acceptance` |
| `text` | ⚠ toolbox create (fixed default), double-click inline edit (live 300ms-debounced sync, matching note/label — task-annotation-doubleclick-to-edit-text), rotate/recolor/layer/duplicate/nine-position alignment/font size/curated font family (right-click — task-annotation-text-alignment-and-font; see [Typography controls](#typography-controls-text-shape); no colour chosen defaults to `#94a3b8`, `GenericAnnotationNode.jsx`'s `DEFAULT_COLOR` — shared by `icon`/`vote_dot` below and by `shape`'s own unset-fill default), attach by dragging near a node/annotation or, at creation time, via the "nearby object menu" (right-click an eligible node/annotation's own menu, "Add nearby" → Text — pre-wired with the identical `content.attachment` shape); no way to inspect or clear an attachment other than dragging, and the alignment control's vertical axis has no visible effect since `text` still has no explicit box | ✅ generic tool set | ✅ | ✅ | ✅ | ⚠ audited 2026-08-30, updated same day (see [audit](#keyboard-touch-and-screen-reader-controls-audit-v1-accessibility-baseline)): same as `note` — a visible Edit button now opens the menu. **Update 2026-08-30 (task-annotation-accessible-shared-controls):** now has a designed accessible name ("Text, {text}", or just "Text" when empty), Shift+F10 reachability, menu arrow-nav/focus-trap, an "Attach to…"/Detach pair (`text` is one of `ATTACHABLE_OVERLAY_KINDS`) and selected-and-focused-on-create; screen-reader/physical-device verification still deferred |
| `label` | ✅ toolbox create, inline edit, drag, rotate/recolor/resize-text/layer/duplicate (right-click; no colour chosen defaults to `#64748b`, `LabelNode.jsx`'s `DEFAULT_LABEL_COLOR`), attach by dragging near a node/annotation or, at creation time, via the "nearby object menu" (right-click an eligible node/annotation's own menu, "Add nearby" → Label) — previously listed "attach" as done, but it was modeled server-side only and never wired into the canvas translation layer until this slice | ✅ generic tool set | ⚠ two translator drops. `geometry.w`/`h` is reset to the model's 160×96 default by the next autosave that ships the label and by any saved view (`smallfix-browser-clobbers-unsized-annotation-geometry`); only an agent can set one, since a `label` has no resize handles (this row previously claimed "resize" here — corrected, `smallfix-contract-label-row-claims-resize-it-lacks`), so no user-set size is lost. The overlay used to carry only `color` and `fontSize` out of `style`, dropping any other style key an agent set; `opacity` is no longer one of them — task-annotation-responsive-bottom-toolbox's opacity control needed the same leg wired both ways and closed it for that one key while adding the control (`smallfix-label-overlay-drops-nonvisual-style-keys` — partially closed; a hypothetical future style key beyond `color`/`fontSize`/`opacity` would still be dropped, since the translator still lists fields explicitly rather than passing `style` through). `text`, colour, font size, opacity, `attachment`, `rotation`, `z` and `locked` do survive | ✅ | ✅ | ⚠ audited 2026-08-30, updated same day (see [audit](#keyboard-touch-and-screen-reader-controls-audit-v1-accessibility-baseline)): same as `note` — a visible Edit button now opens the menu. **Update 2026-08-30 (task-annotation-accessible-shared-controls):** now has a designed accessible name ("Label, {text}"), Shift+F10 reachability, menu arrow-nav/focus-trap, an "Attach to…"/Detach pair and selected-and-focused-on-create; screen-reader/physical-device verification still deferred |
| `line` | ⚠ toolbox create, endpoint attach/drag, recolor/layer/duplicate/unlock (right-click; no colour chosen defaults to `#111827`, `ArrowNode.jsx`'s `DEFAULT_ARROW_COLOR`); a `rotation` the MCP tools accept is stored and reported but never drawn | ✅ generic tool set (`arrow` alias) | ⚠ three translator drops. `geometry.w`/`h` is rewritten to the model's 160×96 default by the next autosave that ships the line and by any saved view (`smallfix-browser-clobbers-unsized-annotation-geometry`) — minor here only because nothing draws from a line's box: an agent-created line is stored unsized (`build_annotation` defaults `w`/`h` to `0`) and `ArrowNode` sizes itself from the endpoints. The substantive one: the overlay carries the endpoint coordinates (as `position` plus `dx`/`dy`) and the GUI's own `startAnchor`/`endAnchor`, but never the model's `start`/`end` endpoint descriptors, so an `attachment` an agent set on either endpoint (see [Attachment and detach behavior](#attachment-and-detach-behavior)) is rebuilt as a bare point and lost (`smallfix-line-endpoint-attachment-dropped-by-translator`). Third, the overlay used to carry only `color` out of `style` — the identical branch the `label` row above carries, and covered by the same item (`smallfix-label-overlay-drops-nonvisual-style-keys`, whose id reads label-only); `opacity` was added alongside `label`'s own fix and is no longer dropped, same caveat about a hypothetical future style key. `rotation`, `z`, `locked`, arrowheads, colour, opacity and the GUI's own anchors all survive | ✅ | ✅ | ⚠ audited 2026-08-30, updated same day (see [audit](#keyboard-touch-and-screen-reader-controls-audit-v1-accessibility-baseline)): a visible Edit button now opens the menu. **Update 2026-08-30 (task-annotation-accessible-shared-controls):** now has a designed accessible name ("Arrow", a fixed word — a line has no caption to include), Shift+F10 reachability and menu arrow-nav/focus-trap; not one of `ATTACHABLE_OVERLAY_KINDS` (it attaches per-endpoint, its own separate mechanism, unchanged) so it gets no "Attach to…"/Detach pair; screen-reader/physical-device verification still deferred |
| `group` | ⚠ toolbar create-group action, inline rename, recolor/delete/unlock (right-click); a group-vs-group-only "Group order" layer row (bring forward / send backward among groups — see [Group background layer order](#group-background-layer-order); `z` is still never drawn as CSS, this row writes it purely as the groups-bucket sort key) — and no duplicate action, deliberately excluded from this task's scope (see [Layer order](#layer-order)) since a group's substance is its member graph nodes, not its own content | ✅ `create_group_annotation` creates or upserts the box — editing an existing group's label/color/geometry goes through this same upsert-by-id path (resend every field you want kept, unlike the generic types' dedicated patch tool) rather than a separate update tool — `update_group_members` adds/removes member ids without a full resend, and `delete_group_annotation` deletes the box (member graph nodes are never cascade-deleted — a group never owns them as annotations) | ✅ | ✅ | ⚠ creating/deleting the group annotation itself is actor-scoped undoable like any other type, but `group_membership_changed` is outside `session_activity.UNDOABLE_OPS` by design — a membership change is not itself undoable through `undo_last_action` | ⚠ audited 2026-08-30 (see [audit](#keyboard-touch-and-screen-reader-controls-audit-v1-accessibility-baseline)): was the one kind task-annotation-responsive-bottom-toolbox left out entirely (no Edit button, no accessible name). **Update 2026-08-30 (task-annotation-accessible-shared-controls):** now has an Edit button (click/tap/keyboard, incl. Shift+F10), the same menu arrow-nav/focus-trap and focus-move/-restore as the other five kinds, a non-drag size control, and a designed accessible name ("Group, {label}", never the untranslated "📁 Group" fallback); screen-reader/physical-device verification still deferred |
| `shape` | ✅ toolbox creates all six variants, each drawn distinctly; double-click inline caption editing (live 300ms-debounced sync — task-annotation-doubleclick-to-edit-text), inset to the axis-aligned rectangle each variant's clip-path is proven to contain (`SHAPE_TEXT_INSET`) so a caption never spills past the painted outline at the corners; right-click editor changes an existing shape's subtype, independent Fill and Border swatch sections (each a colour or `"transparent"` — task-annotation-merge-frame-into-shape-rectangle, see [Fill and border](#fill-and-border-shape); unset fill defaults to `#94a3b8` same as `text` above, unset border defaults to transparent — this is also where the retired `frame` kind's GUI cell merged into, since a transparent-fill, coloured-border shape is what `frame` used to be, with the border-rendering limitation for the four clip-path variants noted there), rotation, layer (front/back), duplicate, and the caption's alignment/font size/font family (task-annotation-text-alignment-and-font — see [Typography controls](#typography-controls-text-shape)). `triangle`, `rhombus` and `hexagon` are created at a ratio chosen per subtype, and a subtype switch re-proportions the box to the new subtype's ratio — keeping the width the shape already has, so a deliberate resize survives it; switching *to* `rectangle`, `circle` or `process_arrow` leaves the box alone, since those fill whatever they are given. Their resize preserves whatever ratio the box currently has. For `triangle` and `hexagon` that ratio (2 : √3) is what makes the sides equal; a rhombus clip-path has equal sides at *every* ratio, so its 1:1 is there to make it a square on its corner rather than a flat lozenge. Because the resizer takes no target ratio (reactflow's `keepAspectRatio` is a boolean and preserves the measured box), the guarantee holds only for shapes whose box was set by one of those two paths — a shape stored at 160×96 before this stays squashed and locks that. `rectangle`, `circle` and `process_arrow` fill whatever box they are given, so a `circle` in a non-square box is an ellipse | ✅ generic tool set (`content.shape`, `content.text`, `style.fill`/`style.border`) | ✅ | ✅ | ✅ | ⚠ audited 2026-08-30 (see [audit](#keyboard-touch-and-screen-reader-controls-audit-v1-accessibility-baseline)): a visible Edit button now opens the menu. **Update 2026-08-30 (task-annotation-accessible-shared-controls):** now has a designed accessible name ("{subtype} shape, {caption}" — e.g. "hexagon shape, Phase 2" — every subtype now distinguishable by name, not just by caption), Shift+F10 reachability, menu arrow-nav/focus-trap and a non-drag size control (one of `RESIZABLE_KINDS`); screen-reader/physical-device verification still deferred |
| `icon` | ✅ toolbox create (fixed default glyph), move, rotate (right-click) and attach by dragging near a node/annotation or, at creation time, via the "nearby object menu" (right-click an eligible node/annotation's own menu, "Add nearby" → Icon); right-click picker grid over the full icon vocabulary changes an existing icon's name — renders every one of the 75 host-registry icon names as its own distinct glyph (see [Canvas rendering](#canvas-rendering)) — plus colour (same `#94a3b8` default as `text` above), layer and duplicate | ✅ generic tool set | ✅ | ✅ | ✅ | ⚠ audited 2026-08-30 (see [audit](#keyboard-touch-and-screen-reader-controls-audit-v1-accessibility-baseline)): a visible Edit button now opens the menu. **Update 2026-08-30 (task-annotation-accessible-shared-controls):** now has a designed accessible name ("{name} icon", e.g. "star icon" — the configured NAME, computed centrally rather than left to the accname algorithm's own glyph-content fallback), Shift+F10 reachability, menu arrow-nav/focus-trap and an "Attach to…"/Detach pair (one of `ATTACHABLE_OVERLAY_KINDS`); screen-reader/physical-device verification still deferred |
| `vote_dot` | ✅ toolbox create, move, rotate/recolor (same `#94a3b8` default as `text` above)/layer/duplicate (right-click) — a plain coloured dot with a fixed black ring and drop shadow (`GenericAnnotationNode.css`'s `.kind-vote_dot`), no other content of its own. task-annotation-vote-dot-simplify removed the value it used to render and its right-click stepper, and retired its attachment behaviour entirely: it is no longer offered on the "nearby object menu", is not a member of `ATTACHABLE_OVERLAY_KINDS`, and does not attach by dragging near a node/annotation the way `label`/`text`/`icon` do | ✅ generic tool set (no type-specific `content` field any more; `style.color` sets its fill the same as `icon`) | ✅ — a stored `value`/`attachment` from before this change round-trips as inert, unread data rather than crashing (`AnnotationBadData.test.jsx`'s vote_dot case) | ✅ | ✅ | ⚠ audited 2026-08-30 (see [audit](#keyboard-touch-and-screen-reader-controls-audit-v1-accessibility-baseline)): a visible Edit button now opens the menu. **Update 2026-08-30 (task-annotation-accessible-shared-controls):** now has a designed, fixed accessible name ("Vote dot") and Shift+F10/menu arrow-nav/focus-trap reachability; deliberately gets no "Attach to…" — `vote_dot` was removed from `ATTACHABLE_OVERLAY_KINDS` by task-annotation-vote-dot-simplify and stays that way here; screen-reader/physical-device verification still deferred |
| `image` | ✅ clipboard paste, OS file drop, and the toolbox's file-picker item all ingest through `POST /api/sessions/{id}/annotations/image` (same pipeline as MCP); move/resize/rotate (right-click)/layer/duplicate/delete via the generic annotation context menu once created — no `lock` control exists in any annotation context menu (only `Unlock`, on an already-locked annotation; locking a generic annotation is MCP-only, `set_annotation_lock`). This row previously overclaimed `lock` and `copy` both when neither GUI action existed (`smallfix-contract-image-row-claims-absent-lock-and-copy`); `copy`/duplicate has since shipped as a client-side action (`AnnotationDuplicateControl`) that never calls `duplicate_annotation` itself — see [Layer order](#layer-order) — while `lock` remains MCP-only, so only half of that correction still applies | ✅ `create_image_annotation` ingests; generic create/update refuse image content, and no session annotation write can persist a *new* non-embedded image URL — note the duplicate, saved-view and budget limits in [enforcement](#image-ingest-enforcement) | ✅ | ✅ | ⚠ actor-scoped undo works, but the op is attributed to a dedicated server client id rather than the pasting browser's own (required so the pasting browser's own SSE subscription sees the embedded result instead of dropping it as a self-authored echo — see `_HUMAN_IMAGE_INGEST_CLIENT_ID` in `rest_api.py`), so only that marker's own undo call reverts it, not the pasting browser's | ⚠ audited 2026-08-30 (see [audit](#keyboard-touch-and-screen-reader-controls-audit-v1-accessibility-baseline)): a visible Edit button now opens the menu. **Update 2026-08-30 (task-annotation-accessible-shared-controls):** now has a designed accessible name ("Image, {alt}", or just "Image" — always says what it is, not only an echo of whatever `alt` happens to be), Shift+F10 reachability, menu arrow-nav/focus-trap and a non-drag size control (one of `RESIZABLE_KINDS`); screen-reader/physical-device verification still deferred |
| `freehand` | ⚠ toolbox "Freehand" item arms a one-shot pointer-capture drawing mode (coalesced samples, device pressure when reported, constant-width fallback otherwise, concurrent-input suppressed with a notice); right-click property editor for color/width/smoothing/opacity plus the shared layer and duplicate rows (a stroke drawn without choosing a colour is black — the previous near-white default was invisible on the canvas as rendered); a `rotation` on the document model is still never drawn, and a `w`/`h` resize likewise changes nothing on screen; unlike that rotation, the `w`/`h` is also not preserved across a browser round trip (`smallfix-browser-clobbers-unsized-annotation-geometry`). Both are tracked gaps, not decided non-goals (see Canvas rendering) | ✅ generic tool set — `freehand` has been in `GENERIC_ANNOTATION_TYPES` since #422, so create/update/reorder/lock/delete already worked; `duplicate_annotation` was missing the `translate_freehand_points` call `update_annotation`'s patch builder already had (a duplicated stroke kept its original `points` at a moved envelope position), fixed here | ⚠ the document model round-trips it, but the canvas translator drops `geometry.w`/`h` (`smallfix-browser-clobbers-unsized-annotation-geometry`), so a `w`/`h` an agent set is reset to the model default by the next autosave that ships the stroke, and by any saved view. `points` (with their per-point pressure), `smoothing`, `strokeWidth`, `pointerType`, `pressureSource`, colour, `opacity`, `rotation`, `z` and `locked` all survive | ✅ same op broadcast as every other type — MCP creation now gives a way to exercise this live | ✅ `translate_freehand_points` covers move, and undo restores the sampled points, not just the envelope (`test_undo_of_a_freehand_move_restores_its_sampled_points`) | ❌ no physical stylus/touch pass — the GUI wiring above is verified only under mouse-event emulation, not a real device. Also audited 2026-08-30 for keyboard/screen-reader controls (see [audit](#keyboard-touch-and-screen-reader-controls-audit-v1-accessibility-baseline)): a visible Edit button (via its own right-click menu — freehand's own opacity control stayed a separate implementation, see the audit's "Update, 2026-08-30" note) now opens the menu. **Update 2026-08-30 (task-annotation-accessible-shared-controls):** now has a designed, fixed accessible name ("Freehand stroke"), Shift+F10 reachability and menu arrow-nav/focus-trap; no size control (a stroke's geometry is its sampled points, not a box — see [Canvas rendering](#canvas-rendering)) and no "Attach to…" (not one of `ATTACHABLE_OVERLAY_KINDS`); screen-reader/physical-device verification still deferred (unchanged from the "no physical stylus/touch pass" line above, which this does not close) |
| cross-type | — | — | — | ⚠ create/delete/style/geometry publish immediately and note/label/text/shape text is now live-synced and debounced at 300 ms, split out from the general autosave debounce; every annotation kind now distinguishes a purely cosmetic selection claim (`ClaimMap`, unenforced) from an exclusive edit lease (`LeaseMap`) acquired only when actual editing starts — first-actual-editor-wins, enforced client-side and server-side alike, with the server rejecting a browser write (ops, image ingest and undo alike) against a lease someone else holds (`dec-mcp-agent-ops-vs-annotation-claimmap`, task-annotation-exclusive-edit-leases); the MCP write path's own bypass is now closed too (`task-mcp-annotation-human-edit-guard`): every synchronous MCP write method that can mutate an existing annotation checks the same `LeaseMap` at its own mutation boundary and never acquires one itself ([gap closed](#operation-timing-and-leases)); the two-real-client conflict matrix ([above](#two-client-conflict-matrix)) is now documented and test-covered; the whole-document-last-write-wins finding it originally recorded for concurrent different-field edits with no lease held is now fixed by field-level patches and per-field `base_version` checking (`dec-annotation-field-patches-and-conflicts`, [Field-level patches and base_version](#field-level-patches-and-base_version)) — a legacy caller that supplies no `base_version` at all keeps the old unprotected behaviour as a documented fallback, not a live gap for a real client; a per-kind reconnect/catch-up/duplicate-suppression/lock-ownership audit across `text`/`shape`/`icon`/`vote_dot`/`image`/`freehand` (`GraphCanvasRemote.test.jsx`, `TestPerKindReconnectCatchUpAndLocks`) found no kind-specific gap | ✅ actor-scoped conditional undo (`session_activity.py`) | ⚠ **Update 2026-08-30 (task-annotation-accessible-shared-controls):** every shared/cross-type gap this row used to name is now closed and test-covered — Shift+F10/Menu-key now finds and clicks the visible Edit button (`GraphCanvas.jsx`'s document-level keydown handler); a touch multi-select mode (a real toggle, tap-to-add); a non-drag "Attach to…" target-tap mode plus Detach, for `label`/`text`/`icon`; an overlap-object picker (`onNodeClick`, `nodesAtPoint`); a menu focus-trap/arrow-nav shared by all six kinds' own menus (`useAnnotationMenuKeyNav`); focus-move/-restore generalised to the right-click path too (`useAnnotationEditTrigger.js`); non-drag resize for the kinds that carry a box; and a per-kind designed accessible name (`computeAnnotationAriaLabel`, wired once for every kind via `GraphCanvas.jsx`'s `nodesWithAriaLabels`). Keyboard node selection and arrow-key nudge (ReactFlow defaults), toolbox creation, and a visible, keyboard/tap-reachable Edit entry point on all six kinds now (including `group`), do already work. Every lock/lease/type exception each new control respects is exercised by its own test alongside the mechanism (locked → unlock/duplicate only; `isRemoteLocked` → refuse + `notifyRemoteLockedAttempt`; `vote_dot` excluded from attach; `group` excluded from the overlap picker and from non-drag attach targeting). **Still genuinely open, not fabricated as done:** a real screen reader's actual announcement of any of the above, and real touch/pen hardware behaviour for the new touch-first controls — both deferred to `task-annotation-manual-accessibility-touch-acceptance`, which is why this row is ⚠, not ✅, per the [Downstream closure rule](#downstream-closure-rule)'s own "not merely coded" bar. See [audit](#keyboard-touch-and-screen-reader-controls-audit-v1-accessibility-baseline) |

## Downstream closure rule

A Corp planning graph task may be marked done only when every acceptance
matrix row (or cell) it claims to close is ✅, verified by a passing test
(or, for the device column, a recorded physical-device pass) — not merely
by a merged PR that touches the relevant files. A PR that only closes some
cells of a row must update the task's `completed_scope` and
`remaining_scope` to say exactly which cells moved, and the task's parent
must stay `in_progress` until its own full row set is ✅.

## V1 non-goals

GIF, SVG, crop, image filters, threaded comments, vote counting, and
cross-session annotation libraries are outside v1. (The retired `frame`
kind never had real containment/grouping behaviour of its own either — it
was always a plain box, the same as any `shape` — see [Fill and
border](#fill-and-border-shape) and the "true frame grouping" non-goal this
line used to name.)
