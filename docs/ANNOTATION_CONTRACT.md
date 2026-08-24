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
- `frame` — visual-only framing box (see [Attachment and detach
  behavior](#attachment-and-detach-behavior); frames never participate in
  attachment)
- `group` — node-membership box
- `shape` — rectangle, circle, triangle, rhombus, hexagon or process arrow,
  selected via `content.shape`
- `icon` — a configured icon from the icon set
- `vote_dot` — a colored voting dot
- `image` — an embedded, ingested image
- `freehand` — a freehand/stylus stroke

Existing canvas note, label, arrow and group descriptors are migrated into
the v1 model.

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
- `style`: visual style fields such as fill, stroke, color, opacity and font
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
  icon, vote dot, text/heading, arrow) pre-wired to attach to that object.
- **Mobile bottom sheet** — the touch equivalent of the toolbox: a sheet
  that slides up from the bottom, same type grouping, sized for thumb reach.

### Desktop wireframe

```
┌───────────────────────────────────────────────────────────────┐
│                          canvas                                │
│                                                                  │
│                                          ┌──────────────────┐   │
│                                          │ nearby object menu│   │
│                            (node) ───────┤  + label          │   │
│                                          │  + icon           │   │
│                                          │  + vote dot        │   │
│                                          │  + text            │   │
│                                          │  + arrow            │   │
│                                          └──────────────────┘   │
│                                                                  │
├───────────────────────────────────────────────────────────────┤
│ ▲ toolbox (collapsed)                                            │
├───────────────────────────────────────────────────────────────┤
│ ▼ toolbox (expanded)                                             │
│  [note] [text] [label]   [shape ▾] [line] [frame] [group]        │
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
│  shape  line   frame      │
│  group  icon   vote dot   │
│  image  freehand          │
└─────────────────────────┘
```

**Current gap:** `note`, `label`, `line` and `group` have GUI creation today
(dedicated toolbar/toolbox actions). The bottom toolbox also creates `text`,
`frame`, and `shape` in every variant `content.shape` accepts — rectangle,
circle, triangle, rhombus, hexagon and process arrow — each of which now
renders as its own distinct visual. `icon`, `vote_dot`, `image` and
`freehand` still render on canvas but have no GUI creation path — they can
only be created via MCP. A right-click property editor now exists for every
rotatable kind (`note`, `label`, `text`, `frame`, `shape`, `icon`, `vote_dot`,
`image`): a rotation control (±15° steps plus reset), and, for `shape` only,
a subtype picker to change an existing shape's variant after creation. What
it still does not cover: recoloring any generic kind, an icon picker for
`icon`, and cropping/replacing an `image`'s pixel content. `label`, `text`,
`icon` and `vote_dot` can now also be attached to a node or another
annotation from the GUI, by dragging the annotation within snapping distance
of the target ([Attachment and detach behavior](#attachment-and-detach-behavior))
— there is still no dedicated "nearby object menu" (the wireframe above) that
pre-wires a new attachable annotation to a target at creation time. Closing
what remains is tracked per type in the [acceptance matrix](#acceptance-matrix);
it is not satisfied by documenting the wireframes above.

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

**Current gap:** `ClaimMap` (`backend/core/session_hub.py`) implements
selection as an *advisory* soft-lock with a 30 s TTL and last-writer-wins
takeover — a claim held by one client is silently taken over by another's
newer claim, and a stale claim expires rather than blocking. This is not an
exclusive lease: it prevents nothing, it only records who claimed last. The
generic op debounce in `sessionSyncClient.js` batches the outgoing queue as
a whole; it does not distinguish a 300 ms text-specific debounce from
release-time-only geometry publication. Actor-scoped conditional undo *is*
implemented (`backend/core/session_activity.py`). Closing the lease and
timing gaps is tracked in the acceptance matrix as a cross-type row, since
neither is type-specific.

## Attachment and detach behavior

- `label`/`callout`, `icon`, `vote_dot` and `text`/heading annotations may
  attach to a node via `content.attachment = { target_id, target_type,
  anchor, offset }`.
- `line` endpoints (`start`/`end`) may each independently attach to a node
  or to another annotation, or stay free-floating at a fixed model-space
  point.
- Attached objects follow the referenced target's movement.
- If the attachment target is removed, the attached object detaches and
  keeps its last resolved model-space geometry — it does not disappear and
  does not snap to the origin.
- `frame` and `group` are containment/visual constructs, not attachment
  targets or attachers: a frame never attaches to anything and nothing
  attaches to a frame; membership in a `group` is tracked separately via
  `member_node_ids` and the `group_membership_changed` op, not via
  `attachment`.

**GUI attach/detach.** `label`, `text`, `icon` and `vote_dot` can now be
(re)attached and detached from the canvas, not only via a raw
`content.attachment` payload: dropping one of these overlays within
`ATTACH_SNAP_RADIUS` (90px, unscaled) of a node's or another attachable
annotation's centre attaches it there (`frame` and `group` are excluded from
candidacy, per this section's rule that nothing attaches to either — even
when one is the nearest thing to the drop point, a further-away valid target
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
above. What this does not yet add: a "nearby object menu" that offers an
attachable type pre-wired to a target at creation time (see [Human authoring
surfaces](#human-authoring-surfaces)), and a manual way to inspect or clear an
annotation's current attachment target other than dragging it away. `line`
endpoint attach/detach (`startAnchor`/`endAnchor`, drag-to-snap on the
endpoint handle) predates this and is unrelated code, unchanged here.

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

**Enforcement:** `create_image_annotation` ingests the image and is the only
tool that sets pixel content. `create_annotation` refuses `type="image"` and
`update_annotation` refuses a `content` carrying an `image` key, so the
generic envelope can no longer store a supplied `content.image.url`
verbatim. Underneath both, `SessionStore.apply_state_op`'s
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
  persist a self-supplied picture. The per-image, per-session and document
  budgets in `image_ingest.py` are enforced only by
  `SessionManager.upsert_image_annotation`, **not** on the op path, and the
  256KB op-batch cap is per request rather than cumulative — so repeated
  single-op batches can grow a session document far past those budgets. That
  growth path predates this rule (any large `text` payload does the same) and
  is tracked as a follow-up; there is no CSRF or origin check on the ops
  endpoint either, so "a client" here means anything holding the session id.
- A `SavedView` node's `metadata.annotation_document` never passes through
  this check: it is stored as ordinary graph-node metadata and rendered
  straight into the canvas on load, so a saved view carrying a remote image
  URL still makes every viewer's browser fetch that host. Nothing persists
  into the session from it — the resulting op is refused — but the fetch has
  already happened. Also tracked as a follow-up.

So the property this section actually guarantees today is narrower than "no
remote resource anywhere": **no session annotation write persists a new
non-embedded image URL**. No GUI creates image content at all yet — the
`image` GUI cell below is still ❌.

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

The rest of the v1 model except `group` — `text`, `label`, `line` (`arrow`
accepted as a legacy alias), `frame`, `shape`, `icon`, `vote_dot`, `image`,
`freehand` — is exposed the same way through a generic tool set:
`list_annotations` / `create_annotation` / `update_annotation` /
`delete_annotation` / `reorder_annotation` / `set_annotation_lock` /
`duplicate_annotation`, over the same session op protocol and
optimistic-concurrency contract. This includes `freehand`: an earlier
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
recreated a group just to change its label or color. There is no MCP tool
yet to delete a group box itself (only its creation and membership) — see
the acceptance matrix.

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
identically under emulation. No such device acceptance pass exists yet —
`freehand` has an MCP creation entry point (the generic tool set), but no
*GUI* creation entry point (stylus input is not wired), and a physical-device
pass is inherently about real pointer input reaching the canvas, not headless
creation — so device acceptance still cannot be scheduled ahead of that.

## Canvas rendering

`note`, `label` and `line` have dedicated, interactive canvas UX (drag,
resize, inline text editing, anchoring). The rest of the v1 model — `text`,
`frame`, `shape`, `icon`, `vote_dot`, `image`, `freehand` — renders with
selection and drag-to-move for every kind, plus model-space resize (via the
same `NodeResizer` handles as `note`) for the kinds that carry an explicit
box size: `frame`, `shape` and `image`. `text`, `icon` and `vote_dot` render
at a fixed intrinsic size and are not resizable. A locked annotation of any
generic kind hides its resize handles the same way a locked `note` does.

Per-type property editors and GUI creation for these types are required v1
scope (see [Human authoring surfaces](#human-authoring-surfaces)), not a
non-goal. A right-click rotation control and, for `shape`, a subtype picker
now exist for every generic kind; recoloring, an icon picker and image
paste/upload are still reachable only through the MCP tools, which is the gap
the acceptance matrix tracks, not the intended end state.

Each `shape` variant draws its own geometry (`SHAPE_STYLES` in
`GenericAnnotationNode.jsx`).

An `icon` annotation draws the glyph its configured `content.icon` name
resolves to in the canvas package's icon set
(`packages/ui-graph-canvas/src/utils/annotationIcons.js`). That set is small:
it covers **11 of the 75 Bootstrap-icon names in the host app's registry**
(`ICON_REGISTRY` in `frontend/web/src/components/FloatingToolbar.jsx`, the
same vocabulary `schema_config.json`'s `icon` field uses), plus everyday
synonyms. A name that resolves to neither draws the two-character abbreviation
of itself that the canvas drew before the set existed, rather than collapsing
into one neutral marker — so no name from the host registry became less
distinguishable than it was. (The synonym aliases do merge on purpose:
`circle`/`dot`, `check`/`ok`, `cross`/`x`, `warning`/`alert` and
`lightbulb`/`idea` each draw one glyph where they used to draw two different
abbreviations — a meaningful icon is worth more there than two distinct pairs
of letters.) That is a floor, not a good outcome: most names
are still an abbreviation and not their icon, and abbreviations collide (the
registry's 64 uncovered names draw only 36 distinct marks — every
`FileEarmark*` name draws `Fi`, every `List*` name draws `Li`). Covering the
whole registry is an open gap, tracked in the [acceptance
matrix](#acceptance-matrix)'s `icon` row.

`geometry.rotation` is drawn as a transform on the rendered element rather
than on the ReactFlow node wrapper, so hit-testing, dragging and resizing keep
operating on the unrotated bounding box. For the four kinds that are both
rotatable and resizable — `note`, `frame`, `shape` and `image` — that has a
visible cost, not just a benign one: the `NodeResizer` outline and handles are
drawn axis-aligned around the unrotated box, so on a rotated annotation they
sit visibly askew from the object and a handle drag grows the box along the
unrotated axes. `frame` and `shape` are where this is actually reachable
today: both are toolbox-creatable and both accept a rotation through the
generic MCP tools or the GUI rotation control described below. `image` needs
MCP or the GUI control to create the object, but either can set the rotation.
`note` no longer needs a raw op to reach a non-zero rotation — the GUI control
below writes it directly — though no MCP tool can rotate one yet (see below).
Rotation-aware resize handles are an open gap. The capability
baseline requires it for text/headings, labels/callouts, sticky notes,
images, icons/dots and basic shapes including the process arrow; `frame` is
drawn too, because the generic tools accept `rotation` for every type with no
per-type validation (`create_annotation` no longer creates an `image` — #428
moved that to `create_image_annotation`, which takes its own `rotation` — but
`update_annotation` still rotates an existing one), and a frame is a single
box like a shape — storing a rotation, reporting it back from
`list_annotations` and then quietly drawing the frame axis-aligned would be a
silent discard.

`line` and `freehand` are the two that do **not** draw it: their geometry
lives in endpoints and sampled points rather than in a box, so a rotation the
server accepts for them is stored and reported but never rendered. That is a
tracked gap in the [acceptance matrix](#acceptance-matrix), not a decided
non-goal. `group` never reaches this translation layer at all — its helpers
(`annotationsToGroups`/`groupsToAnnotations`) carry no rotation field, so a
group has no rotation to draw or preserve.

**A GUI rotation control now exists.** Right-clicking a `note`, `label`,
`text`, `frame`, `shape`, `icon`, `vote_dot` or `image` opens a property
editor with a rotation row: two step buttons (±15°) and a reset-to-0° button
that also displays the current angle (`GenericAnnotationNode.jsx` for the six
generic kinds; `NoteNode.jsx`/`LabelNode.jsx` for their own). It writes
`data.rotation` on the ReactFlow node the same way the pre-existing
color/text-size controls write their fields, so it round-trips through the same
`overlayToFlowNode`/`flowNodeToOverlay` (`annotations.js`) and
`sessionAnnotations.js` translators [already described](#canvas-rendering)
below — no MCP or backend change was needed. This closes `note`'s
tool-surface gap from the GUI side specifically: `note`'s rotation is now
reachable without any client posting a raw op by hand.

On the MCP tool surface, rotation is otherwise set through
`create_annotation`/`update_annotation`, or `create_image_annotation` when
creating an image. That leaves `note` with no rotation source *on the MCP
tool surface* specifically: the generic tools refuse note ids, and the
dedicated sticky-note tools take no `rotation` argument (`build_note_annotation`
writes `rotation: 0`). The same boundary leaves a note's `z` and `locked`
unwritable through those tools even though `list_sticky_notes` reports `z`
and `locked` back. The raw op endpoint is not bounded that way —
`SessionStore.apply_state_op` accepts an `annotation_created` note carrying
`geometry.rotation`, `z` and `locked`, and `list_annotations` then reports all
three. Adding a `rotation` argument to the sticky-note MCP tool set is still a
tracked gap in the `note` row below, unaffected by the GUI control above.

`z`, `locked` and `rotation` round-trip through every annotation type's canvas
representation (`overlayToFlowNode`/`flowNodeToOverlay` in
`packages/ui-graph-canvas/src/utils/annotations.js`, and the server-model
translators in `frontend/web/src/utils/sessionAnnotations.js`): `z` maps to
the ReactFlow node's `zIndex`, `locked` maps to `draggable: false`, and
`rotation` travels on the flow node's `data`. This is the canvas UI's own
enforcement of `locked` — the server never rejects a write to a locked
annotation (`reorder_annotation` / `set_annotation_lock` /
`update_annotation` all still apply). A translator that dropped any of the
three would make the browser's own next autosave diff the annotation back to
its `z: 0` / `locked: false` / `rotation: 0` default and silently overwrite
whatever a collaborator or agent had just set.

## Acceptance matrix

A downstream task or PR may claim a row **done** only when every cell in
that row is satisfied end to end (not merely coded, but exercised by a test
or, for the device column, a physical-device pass). ✅ = satisfied today,
⚠ = partially satisfied, ❌ = not started, ⬜ = no acceptance test defined
yet (status unknown, treat as not done). See [Downstream closure
rule](#downstream-closure-rule).

| Type | GUI create/edit | MCP create/edit | Persistence/reload/saved views | Realtime/collaboration | Activity/undo | Accessibility/device |
|---|---|---|---|---|---|---|
| `note` | ✅ toolbox create, inline edit, drag/resize, rotate (right-click) | ⚠ sticky note tool set, but it takes no `rotation` argument on create or update — and the generic `reorder_annotation`/`set_annotation_lock` refuse note ids — so a note's `rotation` (and its `z`/`locked`) cannot be set through any MCP tool, though `list_sticky_notes` reports `z` and `locked` back and `list_annotations` reports the rotation; a raw `annotation_created` op or the GUI rotation control can still set it | ✅ | ✅ op broadcast + revision | ✅ actor-scoped undo | ⬜ no formal pass yet |
| `text` | ⚠ toolbox create (fixed default), rotate (right-click), attach by dragging near a node/annotation; no color/font editor and no way to inspect or clear an attachment other than dragging | ✅ generic tool set | ✅ | ✅ | ✅ | ⬜ |
| `label` | ✅ toolbox create, inline edit, drag/resize, rotate (right-click), attach by dragging near a node/annotation — previously listed "attach" as done, but it was modeled server-side only and never wired into the canvas translation layer until this slice | ✅ generic tool set | ✅ | ✅ | ✅ | ⬜ |
| `line` | ⚠ toolbox create, endpoint attach/drag; a `rotation` the MCP tools accept is stored and reported but never drawn | ✅ generic tool set (`arrow` alias) | ✅ | ✅ | ✅ | ⬜ |
| `frame` | ⚠ toolbox create (fixed default size), rotate (right-click); no color editor | ✅ generic tool set | ✅ | ✅ | ✅ | ⬜ |
| `group` | ✅ toolbar create-group action | ⚠ `create_group_annotation` creates/upserts the box, `update_group_members` adds/removes member ids; no MCP tool deletes a group box itself, and editing an existing group's own label/color/geometry is only possible by a full upsert (no partial-update tool the way generic types have) | ✅ | ✅ | ⚠ creating/deleting the group annotation itself is actor-scoped undoable like any other type, but `group_membership_changed` is outside `session_activity.UNDOABLE_OPS` by design — a membership change is not itself undoable through `undo_last_action` | ⬜ |
| `shape` | ✅ toolbox creates all six variants, each drawn distinctly; right-click editor changes an existing shape's subtype and rotation | ✅ generic tool set (`content.shape`) | ✅ | ✅ | ✅ | ⬜ |
| `icon` | ⚠ move, rotate (right-click) and attach by dragging near a node/annotation; still no create UI or icon picker — renders the 11 icon names the canvas set shares with the host registry as glyphs, the other 64 as a two-character abbreviation of the name (which collides: 64 names, 36 distinct marks) | ✅ generic tool set | ✅ | ✅ | ✅ | ⬜ |
| `vote_dot` | ⚠ move, rotate (right-click) and attach by dragging near a node/annotation; still no create UI or color picker | ✅ generic tool set | ✅ | ✅ | ✅ | ⬜ |
| `image` | ⚠ rotate (right-click) once created via MCP; still no paste/upload UI | ✅ `create_image_annotation` ingests; generic create/update refuse image content, and no session annotation write can persist a *new* non-embedded image URL — note the duplicate, saved-view and budget limits in [enforcement](#image-ingest-enforcement) | ✅ | ✅ | ✅ | ⬜ |
| `freehand` | ❌ no create UI (stylus input not wired); a `rotation` on the document model is never drawn | ✅ generic tool set — `freehand` has been in `GENERIC_ANNOTATION_TYPES` since #422, so create/update/reorder/lock/delete already worked; `duplicate_annotation` was missing the `translate_freehand_points` call `update_annotation`'s patch builder already had (a duplicated stroke kept its original `points` at a moved envelope position), fixed here | ✅ document model round-trips it | ✅ same op broadcast as every other type — MCP creation now gives a way to exercise this live | ✅ `translate_freehand_points` covers move/undo | ❌ no physical stylus/touch pass |
| cross-type | — | — | — | ⚠ ops publish immediately, but the 300 ms text debounce and release-time-only geometry are not split out from the general autosave debounce, and edit leases are advisory/LWW with a 30 s TTL rather than exclusive ([gap](#operation-timing-and-leases)) | ✅ actor-scoped conditional undo (`session_activity.py`) | — |

## Downstream closure rule

A Corp planning graph task may be marked done only when every acceptance
matrix row (or cell) it claims to close is ✅, verified by a passing test
(or, for the device column, a recorded physical-device pass) — not merely
by a merged PR that touches the relevant files. A PR that only closes some
cells of a row must update the task's `completed_scope` and
`remaining_scope` to say exactly which cells moved, and the task's parent
must stay `in_progress` until its own full row set is ✅.

## V1 non-goals

GIF, SVG, crop, image filters, threaded comments, vote counting, true frame
grouping and cross-session annotation libraries are outside v1.
