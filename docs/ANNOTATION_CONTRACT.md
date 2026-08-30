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
[Layer order](#layer-order); `group`'s own context menu does not. What the
editors still do not cover: `note`/`label` still have only a text-size
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
carry the row — a group box can be ordered against by other annotations but
cannot be reordered itself.

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
annotation is created at `z = 0`, so the common case is a pile of ties.
Breaking those ties one step at a time means renumbering the annotations
around the one that moved — and an `annotation_updated` op carries the
*whole* annotation, which for an embedded image is its entire data URI. A
renumber touching a few images would exceed the session op batch's byte cap
and be rejected atomically. Front/back always writes exactly one annotation,
so it cannot reach that cap. One-step forward/back, and the level compaction
it would need, are not implemented.

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

What `group` still lacks is the layer row, not the lock: see
[Layer order](#layer-order) above. Its `z` is carried through the same
translators so a value set over MCP survives the canvas round trip, but
nothing reads it — a group's paint order is array order
(`reorderNodesForParentChild` puts groups first so they sit behind their
members as backdrops), and groups are ReactFlow parents whose members carry
`parentId`. Giving a group a GUI layer control therefore needs a decision
about how group layering relates to that parent/child backdrop model, which
has not been taken.

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
Graph nodes carry no layer of their own, so they sit at 0 alongside a freshly
created annotation, while send-to-back writes one below the backmost
annotation. Whenever that backmost annotation is itself at or below 0 — the
default, since every annotation is created at 0 — the result is negative and
does place the annotation behind the graph's nodes and edges. That is
intended and useful, and it is how a `shape` with a transparent fill
(standing in for the retired `frame`) gets behind the nodes it frames;
it is not, however, a guarantee. Once every annotation has been pushed above
0, send-to-back lands at 0 or higher — level with the graph (where paint
order falls back to document order) or in front of it, but no longer behind
it.

A layer is only ever written when it is an integer strictly past every other
annotation's *and* inside the signed 32-bit range CSS `z-index` accepts, so
the layer that is stored is the layer the browser actually paints. Against a
neighbour already at that bound the click is a no-op: clamping the step back
down to the bound would land level with the neighbour it is meant to pass,
recreating the tie the control exists to break while publishing an operation
that changes nothing on screen.

Semantic default layers — a per-kind default `z` at creation time, so a
transparent-fill shape starts behind the annotations it frames — are **not**
implemented; every
annotation is created at `z = 0` and ordered manually from there.

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

**Remaining gap:** the exclusive edit lease is scoped to browser-originated
writes only. The synchronous MCP write path (`upsert_annotation`/
`update_annotation`/`delete_annotation`/`apply_layout`/`add_node_refs`, all
keyed to the shared `mcp-agent` client id — `backend/service/mcp_tools.py`)
never goes through `apply_ops` and does not acquire or check leases at all in
v1: an MCP agent still silently overrides a live human edit lease exactly as
before, the same way it already bypasses the client-side exclusivity UI and
the `locked` flag today. This is a deliberate v1 boundary, not an oversight —
`dec-mcp-agent-ops-vs-annotation-claimmap` (accepted 2026-08-30) decided
human edit leases are exclusive while leaving MCP writes unchanged for now;
making MCP-issued writes respect a live human lease (never acquiring one of
its own — v1 has no per-agent identity to make that meaningful) is
`task-mcp-annotation-human-edit-guard`'s separate, deliberately-sequenced-after
scope. Actor-scoped conditional undo *is* implemented
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
| Keyboard way IN to the property editor (Shift+F10 / Menu key, or a visible on-focus affordance) | **MISSING**, all six kinds | Test: `AnnotationAccessibilityAudit.test.jsx` — a `contextmenu` event dispatched on the *focused* node wrapper does not open the menu. Code: every kind binds `onContextMenu` on its own inner content div, a DOM descendant of the focused wrapper ReactFlow renders (`NoteNode.jsx` ~L207, `LabelNode.jsx` ~L147, `GenericAnnotationNode.jsx`'s `openContextMenu` L411-420 bound per-kind at L759/809/881/900/920/942, `ArrowNode.jsx` ~L240, `GroupNode.jsx`'s `handleContextMenu` L151-159, `FreehandAnnotationNode.jsx`'s `openContextMenu` L127-135). A keyboard-dispatched `contextmenu` event targets the currently-focused element and only bubbles upward from there, so it never reaches a handler on a nested child — confirmed, not assumed, for `note` and `text` by the test above; the other four kinds share the identical wiring pattern (own root `onContextMenu`, no ancestor-level catch), read directly. Owner: `task-annotation-accessible-shared-controls` — the fix is a shared mechanism (e.g. binding on the ReactFlow wrapper via the existing per-kind `openContextMenu`/equivalent, or a synthetic keydown handler), not a per-kind one-off. |
| Arrow-key navigation between menu items | **MISSING**, all six kinds | Code: `ContextMenus.jsx`'s `useRootMenuKeyNav` (L91-102) implements exactly this — ArrowUp/ArrowDown/Home/End roving focus — but it is wired only into `NodeContextMenu`/`MultiNodeContextMenu`/`EdgeContextMenu`/`PaneContextMenu` (the graph-node/pane menu system), never into any of the six annotation kinds' own `<div className="graph-annotation-context-menu">` portals, which are plain `<button>` lists with no `data-menu-item="root"` markers and no keydown handler of their own beyond a bare Escape-to-close (e.g. `NoteNode.jsx` L60-61). Tab still moves between the buttons (they are real `<button>` elements), just not via the arrow-key roving convention the rest of the app already has. Owner: `task-annotation-accessible-shared-controls` — reuse `useRootMenuKeyNav`/`useMenuOpenFocus` rather than inventing a second implementation. |
| Focus trap while open | **MISSING**, all six kinds | Code: none of the six menus call `.focus()` on open or constrain Tab — confirmed by reading `NoteNode.jsx`, `LabelNode.jsx`, `GenericAnnotationNode.jsx`, `ArrowNode.jsx`, `GroupNode.jsx`, `FreehandAnnotationNode.jsx` in full; only `ContextMenus.jsx`'s `useMenuOpenFocus` (L48-74) does this, again only for the graph-node/pane menu system. Owner: `task-annotation-accessible-shared-controls`. |
| Focus restored to the object on close | **MISSING**, all six kinds | Code: same absence as the row above — no menu close path (`setContextMenu(null)`, on Escape/outside-click/action) calls `.focus()` on the annotation. Contrast: `ToolSlotPicker.jsx` (a different, unrelated fold-out — the toolbox's shape/icon picker) *does* auto-focus on open (L62) and restore focus on close (L83-92), and `ContextMenus.jsx`'s `useMenuOpenFocus` does the same for the graph-node menu system — so the pattern exists twice elsewhere in this codebase and is not used by any of the six annotation menus. Owner: `task-annotation-accessible-shared-controls`. |
| Touch: visible "Edit" entry point (tap object → visible Edit → contextual sheet), vs. still only right-click/long-press | **MISSING**, all six kinds | Code: every kind's property menu opens only from `onContextMenu` (native right-click on desktop; a long-press synthesizes a `contextmenu` event on touch, per the affected browsers' own behaviour — not this app's code). No kind renders a visible "Edit" button, and no `isTouchMode`-conditional UI exists in any of the six node components (grepped; none reference `isTouchMode`/`touch` at all — that prop only reaches `AnnotationToolbox`, the *creation* surface). The mobile "Annotate" sheet (`MobileShell.jsx` L174-178, `surface.isOpen('annotate')`) is reachable only from the phone bottom nav and hosts `AnnotationToolbox` in `variant="sheet"` — a **creation** surface (see area 3); it has no path from tapping an *existing* annotation. Owner: `task-annotation-accessible-shared-controls`, per the decision's accepted flow (tap → visible Edit → contextual bottom sheet with format/duplicate/delete/lock/layer). |

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
| Activating one creates at a sensible location, focused, ready to edit | **⚠ PARTIAL** | Code: `createAnnotationAtViewportCenter`/`createAnnotation` (`GraphCanvas.jsx` L1580-1681) places the new node at the viewport centre (sensible location: ✅) but never sets `selected: true` and never focuses the new DOM node or opens its text editor — confirmed by reading every branch of `createAnnotation`; no branch touches `selected` or calls `.focus()`. So a keyboard user who presses Enter/Space on a toolbox button gets an unselected, unfocused new annotation with no way to immediately edit it without first tabbing/clicking to find it. Owner: `task-annotation-accessible-shared-controls` (or could reasonably be scoped as a small addition to the toolbox's own creation path — naming both rather than picking one, since it touches `createAnnotation`, a shared mechanism `task-annotation-accessible-shared-controls` already owns). |

### 4. Move, resize, rotation, attachment (non-drag alternatives)

| Mechanism | Status | Evidence |
|---|---|---|
| Non-drag position (nudge) | **WORKS**, all six kinds when unlocked/unanchored (whatever `isDraggable` already allows for that kind — e.g. an anchored `arrow` endpoint stays undraggable either way) | Same evidence as area 2's arrow-key row — ReactFlow's built-in 5px/20px-shift nudge fires for any selected node whose `draggable` is true, which every kind computes the same way for both mouse-drag and this keyboard path (`isAnnotationDraggable`, `utils/annotations.js`). |
| Non-drag rotation | **⚠ PARTIAL** | Code: `NoteNode.jsx` L289-315, `LabelNode.jsx` L226-252, `GenericAnnotationNode.jsx`'s rotation row L1182-1208 all render ±15°-step buttons plus a reset — real `<button>`s, keyboard-operable **once the menu is open**. Since opening the menu is itself keyboard-unreachable (area 1), this control is currently only reachable by a user who can right-click/long-press to open the menu and then Tab/click the rotate buttons — not a *keyboard-only* path end to end. Owner: `task-annotation-accessible-shared-controls`, and closing area 1's menu-entry gap closes this one for free. |
| Non-drag resize | **❌ MISSING** | Code: resize is exposed only via ReactFlow's `NodeResizer` drag handles (`NoteNode.jsx`, `GenericAnnotationNode.jsx`'s `resizer`, `GroupNode.jsx`) — no numeric width/height input exists anywhere in any of the six context menus (grepped every menu's JSX; none renders a size `<input>`). Owner: `task-annotation-accessible-shared-controls`. |
| Non-drag attachment — attaching an **existing** annotation to a target ("Attach to" enters target-tap mode) | **❌ MISSING** | Code: grepped `GraphCanvas.jsx`/`utils/annotations.js` for an attach-mode/target-tap mechanism — none exists. The only two attach paths are (a) drag-to-snap (`computeDroppedAttachment`, a drag gesture) and (b) the "Nearby object menu" (`NearbyObjectMenuSection`, `ContextMenus.jsx` L365-387), which only ever creates a **new**, pre-attached label/icon/text at creation time (`attachNearbyAnnotation`, `GraphCanvas.jsx` L1721-1740) — it has no path to attach an annotation that already exists. Both of the two existing paths are themselves reached only from a right-click menu (area 1), so on touch neither is a visible affordance either. Owner: `task-annotation-accessible-shared-controls` — the decision's "Attach to enters target-tap mode" is new mechanism, not a wiring fix. |
| Overlapping objects: a visible way to choose which one you mean | **❌ MISSING** | Code: grepped `GraphCanvas.jsx` for any overlap/cycle/z-order-pick mechanism (`overlap`, `cycle`, `stacked` — zero matches). Clicking a point with multiple annotations stacked on it hits whichever ReactFlow's own top-most-in-DOM-order hit test picks, with no alternative offered. Owner: `task-annotation-accessible-shared-controls`. |

### 5. Touch

| Mechanism | Status | Evidence |
|---|---|---|
| Editing an **existing** annotation via tap alone (tap → visible Edit → sheet) | **MISSING**, all six kinds | Same evidence as area 1's touch row. Today, reaching an existing annotation's property menu on touch depends entirely on a long-press synthesizing a `contextmenu` event (browser/OS behaviour, not app-authored) — exactly the "hidden behind long-press... alone" state the decision requires closing. Long press itself staying *optional* (not the *only* path) is the gap, not long-press existing at all. Owner: `task-annotation-accessible-shared-controls`. |
| Multi-select and its align/distribute actions (PR #524) reachable via tap-based multi-select | **❌ MISSING** — pointer/modifier-key-only today | Code: `GraphCanvas.jsx`'s `<ReactFlow>` sets `multiSelectionKeyCode={['Shift', 'Meta', 'Control']}` (L3452) — ReactFlow's own modifier-held-while-clicking convention, which has no touch equivalent (a touchscreen has no Shift key to hold). Grepped for a dedicated "select multiple" mode toggle (`selectMultipleMode`, `multiSelectMode`, "Select multiple" — zero matches anywhere in `packages/ui-graph-canvas` or `frontend/web`). The align/distribute actions themselves (`alignSelectedNodes`/`distributeSelectedNodes`, `GraphCanvas.jsx` L1543-1564, surfaced via `MultiNodeContextMenu` — PR #524) are real and reachable once a multi-selection exists, but nothing gets a touch user to that multi-selection in the first place. Owner: `task-annotation-accessible-shared-controls`, per the decision's accepted "Select multiple enables tap-based multi-selection with shared bulk actions." |
| Real touch-hardware behaviour (hit-testing at coarse-pointer precision, long-press timing, palm contact) | **UNTESTABLE-HERE** | Needs physical touch/pen hardware, same category `freehand`'s own [Physical device acceptance](#physical-device-acceptance) section already names for that kind — deferred to `task-annotation-manual-accessibility-touch-acceptance`. |

### 6. Screen-reader semantics (role + accessible name)

The decision's own bar is explicit: *"role + accessible name per annotation,
e.g. 'sticky note, Budget Q3'"* — a name that **says what the thing is**, not
merely any string an accname algorithm happens to fall back to.

| Kind | Role | Accessible name today | Status |
|---|---|---|---|
| `note` | `role="button"` (ReactFlow default, area 2) | Falls back to its own text, or the placeholder word "Note" when empty | **MISSING** — Test: `AnnotationAccessibleNameContent.test.jsx` — `{text:'Budget Q3'}` computes to exactly `"Budget Q3"`, not `"sticky note, Budget Q3"`; an empty note computes to `"Note"` only, indistinguishable from every other empty note. |
| `label` | same | Falls back to its own text | **MISSING** — same pattern, no kind word at all (not even "Note"'s placeholder). |
| `text` | same | Falls back to its own text; empty when uncaptioned | **MISSING** |
| `shape` | same | Falls back to its caption when set (any subtype); **empty** when uncaptioned | **MISSING** — a rectangle, circle, triangle, rhombus, hexagon and process-arrow are all indistinguishable from each other and from nothing at all, by name. |
| `icon` | same | Falls back to the rendered **glyph character** (e.g. "★"), not the icon's name ("star") — `title={data.icon}` (`GenericAnnotationNode.jsx`, the `kind === 'icon'` branch) is never reached because the glyph's own text content wins the accname computation first | **MISSING** — confirmed by test, and notable because the code visibly *tries* to offer a name via `title` and the browser's own accname algorithm bypasses it. |
| `vote_dot` | same | **Empty** — no text, no title anywhere in its markup | **MISSING** |
| `image` | same | Falls back to `alt` when set, empty otherwise | **MISSING** — never says "image annotation", just repeats whatever `alt` text (if any) was set. |
| `arrow`/`line` | same | **Empty** — pure SVG, no text or title anywhere | **MISSING** — whatever two objects it connects, the connector itself has no name at all. |
| `freehand` | same | **Empty** — pure SVG path(s), no text or title | **MISSING** |
| `group` | same | Falls back to its header text (folder glyph + label) when named; `"📁 Group"` (the untranslated English default) when not | **MISSING** — never says "group" as a category, and the untranslated fallback also does not respect the host app's i18n for an unnamed group. |

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
picker and typography work. The decision's headline touch flow (tap → visible
Edit → contextual sheet) does **not** exist for any kind yet: every property
editor, on every kind, on every input method, is reachable only through
`onContextMenu` (right-click, or a long-press that happens to synthesize the
same native event) — no visible on-focus/on-tap affordance exists anywhere.
No kind has a role-appropriate accessible name. No touch multi-select mode,
attach-to-target mode, or overlap picker exists. Every one of these shared
gaps is scoped to `task-annotation-accessible-shared-controls`, which this
audit's evidence (test files above, plus this section's file:line citations)
gives a concrete starting point rather than a re-discovery task.

## Acceptance matrix

A downstream task or PR may claim a row **done** only when every cell in
that row is satisfied end to end (not merely coded, but exercised by a test
or, for the device column, a physical-device pass). ✅ = satisfied today,
⚠ = partially satisfied, ❌ = not started, ⬜ = no acceptance test defined
yet (status unknown, treat as not done). See [Downstream closure
rule](#downstream-closure-rule).

| Type | GUI create/edit | MCP create/edit | Persistence/reload/saved views | Realtime/collaboration | Activity/undo | Accessibility/device |
|---|---|---|---|---|---|---|
| `note` | ✅ toolbox create, inline edit, drag/resize, rotate/recolor/resize-text/layer/duplicate (right-click) | ✅ `create_sticky_note`/`update_sticky_note` take `rotation`, `z` and `locked` (mirroring the generic tools' fields for the same); `list_sticky_notes` reports all three back — the generic `reorder_annotation`/`set_annotation_lock` still refuse note ids by design, but the dedicated tools now cover the same ground | ✅ | ✅ op broadcast + revision | ✅ actor-scoped undo | ❌ audited 2026-08-30 (see [audit](#keyboard-touch-and-screen-reader-controls-audit-v1-accessibility-baseline)): keyboard node selection + arrow-key nudge work (ReactFlow default); property menu, rotation control and text-edit entry are right-click/long-press only, no visible touch "Edit" affordance, no designed accessible name |
| `text` | ⚠ toolbox create (fixed default), double-click inline edit (live 300ms-debounced sync, matching note/label — task-annotation-doubleclick-to-edit-text), rotate/recolor/layer/duplicate/nine-position alignment/font size/curated font family (right-click — task-annotation-text-alignment-and-font; see [Typography controls](#typography-controls-text-shape); no colour chosen defaults to `#94a3b8`, `GenericAnnotationNode.jsx`'s `DEFAULT_COLOR` — shared by `icon`/`vote_dot` below and by `shape`'s own unset-fill default), attach by dragging near a node/annotation or, at creation time, via the "nearby object menu" (right-click an eligible node/annotation's own menu, "Add nearby" → Text — pre-wired with the identical `content.attachment` shape); no way to inspect or clear an attachment other than dragging, and the alignment control's vertical axis has no visible effect since `text` still has no explicit box | ✅ generic tool set | ✅ | ✅ | ✅ | ❌ audited 2026-08-30 (see [audit](#keyboard-touch-and-screen-reader-controls-audit-v1-accessibility-baseline)): same shared gaps as `note` — menu/edit only right-click/long-press, no accessible name (empty when uncaptioned) |
| `label` | ✅ toolbox create, inline edit, drag, rotate/recolor/resize-text/layer/duplicate (right-click; no colour chosen defaults to `#64748b`, `LabelNode.jsx`'s `DEFAULT_LABEL_COLOR`), attach by dragging near a node/annotation or, at creation time, via the "nearby object menu" (right-click an eligible node/annotation's own menu, "Add nearby" → Label) — previously listed "attach" as done, but it was modeled server-side only and never wired into the canvas translation layer until this slice | ✅ generic tool set | ⚠ two translator drops. `geometry.w`/`h` is reset to the model's 160×96 default by the next autosave that ships the label and by any saved view (`smallfix-browser-clobbers-unsized-annotation-geometry`); only an agent can set one, since a `label` has no resize handles (this row previously claimed "resize" here — corrected, `smallfix-contract-label-row-claims-resize-it-lacks`), so no user-set size is lost. The overlay also carries only `color` and `fontSize` out of `style`, so any other style key an agent sets — `opacity` among them — is dropped on the same leg (`smallfix-label-overlay-drops-nonvisual-style-keys`). `text`, colour, font size, `attachment`, `rotation`, `z` and `locked` do survive | ✅ | ✅ | ❌ audited 2026-08-30 (see [audit](#keyboard-touch-and-screen-reader-controls-audit-v1-accessibility-baseline)): same shared gaps as `note` — menu/edit only right-click/long-press, accessible name falls back to its own text with no "label" kind word |
| `line` | ⚠ toolbox create, endpoint attach/drag, recolor/layer/duplicate/unlock (right-click; no colour chosen defaults to `#111827`, `ArrowNode.jsx`'s `DEFAULT_ARROW_COLOR`); a `rotation` the MCP tools accept is stored and reported but never drawn | ✅ generic tool set (`arrow` alias) | ⚠ three translator drops. `geometry.w`/`h` is rewritten to the model's 160×96 default by the next autosave that ships the line and by any saved view (`smallfix-browser-clobbers-unsized-annotation-geometry`) — minor here only because nothing draws from a line's box: an agent-created line is stored unsized (`build_annotation` defaults `w`/`h` to `0`) and `ArrowNode` sizes itself from the endpoints. The substantive one: the overlay carries the endpoint coordinates (as `position` plus `dx`/`dy`) and the GUI's own `startAnchor`/`endAnchor`, but never the model's `start`/`end` endpoint descriptors, so an `attachment` an agent set on either endpoint (see [Attachment and detach behavior](#attachment-and-detach-behavior)) is rebuilt as a bare point and lost (`smallfix-line-endpoint-attachment-dropped-by-translator`). Third, the overlay carries only `color` out of `style`, so any other style key an agent sets is dropped on the same leg as the box — the identical branch the `label` row above carries, and covered by the same item (`smallfix-label-overlay-drops-nonvisual-style-keys`, whose id reads label-only). `rotation`, `z`, `locked`, arrowheads, colour and the GUI's own anchors all survive | ✅ | ✅ | ❌ audited 2026-08-30 (see [audit](#keyboard-touch-and-screen-reader-controls-audit-v1-accessibility-baseline)): same shared menu/touch gaps; accessible name is **empty** — pure SVG, no text or title anywhere |
| `group` | ⚠ toolbar create-group action, inline rename, recolor/delete/unlock (right-click); no layer row — a `z` the MCP tools accept round-trips but is never drawn (paint order is the parent/child backdrop order) — and no duplicate action either, deliberately excluded from this task's scope (see [Layer order](#layer-order)) since a group's substance is its member graph nodes, not its own content | ✅ `create_group_annotation` creates or upserts the box — editing an existing group's label/color/geometry goes through this same upsert-by-id path (resend every field you want kept, unlike the generic types' dedicated patch tool) rather than a separate update tool — `update_group_members` adds/removes member ids without a full resend, and `delete_group_annotation` deletes the box (member graph nodes are never cascade-deleted — a group never owns them as annotations) | ✅ | ✅ | ⚠ creating/deleting the group annotation itself is actor-scoped undoable like any other type, but `group_membership_changed` is outside `session_activity.UNDOABLE_OPS` by design — a membership change is not itself undoable through `undo_last_action` | ❌ audited 2026-08-30 (see [audit](#keyboard-touch-and-screen-reader-controls-audit-v1-accessibility-baseline)): same shared menu/touch gaps; accessible name falls back to the header text (folder glyph + label), or the untranslated `"📁 Group"` default when unnamed |
| `shape` | ✅ toolbox creates all six variants, each drawn distinctly; double-click inline caption editing (live 300ms-debounced sync — task-annotation-doubleclick-to-edit-text), inset to the axis-aligned rectangle each variant's clip-path is proven to contain (`SHAPE_TEXT_INSET`) so a caption never spills past the painted outline at the corners; right-click editor changes an existing shape's subtype, independent Fill and Border swatch sections (each a colour or `"transparent"` — task-annotation-merge-frame-into-shape-rectangle, see [Fill and border](#fill-and-border-shape); unset fill defaults to `#94a3b8` same as `text` above, unset border defaults to transparent — this is also where the retired `frame` kind's GUI cell merged into, since a transparent-fill, coloured-border shape is what `frame` used to be, with the border-rendering limitation for the four clip-path variants noted there), rotation, layer (front/back), duplicate, and the caption's alignment/font size/font family (task-annotation-text-alignment-and-font — see [Typography controls](#typography-controls-text-shape)). `triangle`, `rhombus` and `hexagon` are created at a ratio chosen per subtype, and a subtype switch re-proportions the box to the new subtype's ratio — keeping the width the shape already has, so a deliberate resize survives it; switching *to* `rectangle`, `circle` or `process_arrow` leaves the box alone, since those fill whatever they are given. Their resize preserves whatever ratio the box currently has. For `triangle` and `hexagon` that ratio (2 : √3) is what makes the sides equal; a rhombus clip-path has equal sides at *every* ratio, so its 1:1 is there to make it a square on its corner rather than a flat lozenge. Because the resizer takes no target ratio (reactflow's `keepAspectRatio` is a boolean and preserves the measured box), the guarantee holds only for shapes whose box was set by one of those two paths — a shape stored at 160×96 before this stays squashed and locks that. `rectangle`, `circle` and `process_arrow` fill whatever box they are given, so a `circle` in a non-square box is an ellipse | ✅ generic tool set (`content.shape`, `content.text`, `style.fill`/`style.border`) | ✅ | ✅ | ✅ | ❌ audited 2026-08-30 (see [audit](#keyboard-touch-and-screen-reader-controls-audit-v1-accessibility-baseline)): same shared menu/touch gaps; accessible name falls back to the caption when set (any subtype), **empty** when uncaptioned — every subtype indistinguishable by name |
| `icon` | ✅ toolbox create (fixed default glyph), move, rotate (right-click) and attach by dragging near a node/annotation or, at creation time, via the "nearby object menu" (right-click an eligible node/annotation's own menu, "Add nearby" → Icon); right-click picker grid over the full icon vocabulary changes an existing icon's name — renders every one of the 75 host-registry icon names as its own distinct glyph (see [Canvas rendering](#canvas-rendering)) — plus colour (same `#94a3b8` default as `text` above), layer and duplicate | ✅ generic tool set | ✅ | ✅ | ✅ | ❌ audited 2026-08-30 (see [audit](#keyboard-touch-and-screen-reader-controls-audit-v1-accessibility-baseline)): same shared menu/touch gaps; accessible name falls back to the rendered glyph CHARACTER (e.g. "★"), not the icon's name — the `title={data.icon}` attribute is never reached |
| `vote_dot` | ✅ toolbox create, move, rotate/recolor (same `#94a3b8` default as `text` above)/layer/duplicate (right-click) — a plain coloured dot with a fixed black ring and drop shadow (`GenericAnnotationNode.css`'s `.kind-vote_dot`), no other content of its own. task-annotation-vote-dot-simplify removed the value it used to render and its right-click stepper, and retired its attachment behaviour entirely: it is no longer offered on the "nearby object menu", is not a member of `ATTACHABLE_OVERLAY_KINDS`, and does not attach by dragging near a node/annotation the way `label`/`text`/`icon` do | ✅ generic tool set (no type-specific `content` field any more; `style.color` sets its fill the same as `icon`) | ✅ — a stored `value`/`attachment` from before this change round-trips as inert, unread data rather than crashing (`AnnotationBadData.test.jsx`'s vote_dot case) | ✅ | ✅ | ❌ audited 2026-08-30 (see [audit](#keyboard-touch-and-screen-reader-controls-audit-v1-accessibility-baseline)): same shared menu/touch gaps; accessible name is **empty** — no text, no title anywhere in its markup |
| `image` | ✅ clipboard paste, OS file drop, and the toolbox's file-picker item all ingest through `POST /api/sessions/{id}/annotations/image` (same pipeline as MCP); move/resize/rotate (right-click)/layer/duplicate/delete via the generic annotation context menu once created — no `lock` control exists in any annotation context menu (only `Unlock`, on an already-locked annotation; locking a generic annotation is MCP-only, `set_annotation_lock`). This row previously overclaimed `lock` and `copy` both when neither GUI action existed (`smallfix-contract-image-row-claims-absent-lock-and-copy`); `copy`/duplicate has since shipped as a client-side action (`AnnotationDuplicateControl`) that never calls `duplicate_annotation` itself — see [Layer order](#layer-order) — while `lock` remains MCP-only, so only half of that correction still applies | ✅ `create_image_annotation` ingests; generic create/update refuse image content, and no session annotation write can persist a *new* non-embedded image URL — note the duplicate, saved-view and budget limits in [enforcement](#image-ingest-enforcement) | ✅ | ✅ | ⚠ actor-scoped undo works, but the op is attributed to a dedicated server client id rather than the pasting browser's own (required so the pasting browser's own SSE subscription sees the embedded result instead of dropping it as a self-authored echo — see `_HUMAN_IMAGE_INGEST_CLIENT_ID` in `rest_api.py`), so only that marker's own undo call reverts it, not the pasting browser's | ❌ audited 2026-08-30 (see [audit](#keyboard-touch-and-screen-reader-controls-audit-v1-accessibility-baseline)): same shared menu/touch gaps; accessible name falls back to `alt` when set, empty otherwise — never says "image annotation" |
| `freehand` | ⚠ toolbox "Freehand" item arms a one-shot pointer-capture drawing mode (coalesced samples, device pressure when reported, constant-width fallback otherwise, concurrent-input suppressed with a notice); right-click property editor for color/width/smoothing/opacity plus the shared layer and duplicate rows (a stroke drawn without choosing a colour is black — the previous near-white default was invisible on the canvas as rendered); a `rotation` on the document model is still never drawn, and a `w`/`h` resize likewise changes nothing on screen; unlike that rotation, the `w`/`h` is also not preserved across a browser round trip (`smallfix-browser-clobbers-unsized-annotation-geometry`). Both are tracked gaps, not decided non-goals (see Canvas rendering) | ✅ generic tool set — `freehand` has been in `GENERIC_ANNOTATION_TYPES` since #422, so create/update/reorder/lock/delete already worked; `duplicate_annotation` was missing the `translate_freehand_points` call `update_annotation`'s patch builder already had (a duplicated stroke kept its original `points` at a moved envelope position), fixed here | ⚠ the document model round-trips it, but the canvas translator drops `geometry.w`/`h` (`smallfix-browser-clobbers-unsized-annotation-geometry`), so a `w`/`h` an agent set is reset to the model default by the next autosave that ships the stroke, and by any saved view. `points` (with their per-point pressure), `smoothing`, `strokeWidth`, `pointerType`, `pressureSource`, colour, `opacity`, `rotation`, `z` and `locked` all survive | ✅ same op broadcast as every other type — MCP creation now gives a way to exercise this live | ✅ `translate_freehand_points` covers move, and undo restores the sampled points, not just the envelope (`test_undo_of_a_freehand_move_restores_its_sampled_points`) | ❌ no physical stylus/touch pass — the GUI wiring above is verified only under mouse-event emulation, not a real device. Also audited 2026-08-30 for keyboard/screen-reader controls (see [audit](#keyboard-touch-and-screen-reader-controls-audit-v1-accessibility-baseline)): same shared menu/touch gaps as every other kind; accessible name is **empty** — pure SVG path(s), no text or title |
| cross-type | — | — | — | ⚠ create/delete/style/geometry publish immediately and note/label/text/shape text is now live-synced and debounced at 300 ms, split out from the general autosave debounce; every annotation kind now distinguishes a purely cosmetic selection claim (`ClaimMap`, unenforced) from an exclusive edit lease (`LeaseMap`) acquired only when actual editing starts — first-actual-editor-wins, enforced client-side and server-side alike, with the server rejecting a browser write (ops, image ingest and undo alike) against a lease someone else holds (`dec-mcp-agent-ops-vs-annotation-claimmap`, task-annotation-exclusive-edit-leases); the MCP write path still bypasses `LeaseMap` entirely, by deliberate sequencing rather than an open decision — that is `task-mcp-annotation-human-edit-guard`'s own scope ([gap](#operation-timing-and-leases)); the two-real-client conflict matrix ([above](#two-client-conflict-matrix)) is now documented and test-covered; the whole-document-last-write-wins finding it originally recorded for concurrent different-field edits with no lease held is now fixed by field-level patches and per-field `base_version` checking (`dec-annotation-field-patches-and-conflicts`, [Field-level patches and base_version](#field-level-patches-and-base_version)) — a legacy caller that supplies no `base_version` at all keeps the old unprotected behaviour as a documented fallback, not a live gap for a real client; a per-kind reconnect/catch-up/duplicate-suppression/lock-ownership audit across `text`/`shape`/`icon`/`vote_dot`/`image`/`freehand` (`GraphCanvasRemote.test.jsx`, `TestPerKindReconnectCatchUpAndLocks`) found no kind-specific gap | ✅ actor-scoped conditional undo (`session_activity.py`) | ❌ the biggest gaps are shared/cross-type, not per-kind: no keyboard way into any property menu (Shift+F10/Menu-key dispatches to the focused wrapper, not the descendant div every kind's `onContextMenu` is bound on), no visible touch "Edit" entry point (long-press-only), no touch multi-select mode, no attach-to-target mode for an existing annotation, no overlap-object picker, no menu focus-trap/restore/arrow-nav (the pattern exists in `ContextMenus.jsx`/`ToolSlotPicker.jsx` but isn't reused here). Keyboard node selection and arrow-key nudge (ReactFlow defaults) and toolbox creation (this repo's own, real accessible wiring) do already work. See [audit](#keyboard-touch-and-screen-reader-controls-audit-v1-accessibility-baseline); owner for all of the above is `task-annotation-accessible-shared-controls` |

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
