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
`frame`, `shape`, `icon`, `vote_dot`, `image`): a rotation control (±15° steps
plus reset), a colour picker for the kinds that paint one (`text`, `frame`,
`shape`, `icon`, `vote_dot` — `image` carries a `color` in the model but
renders none, so it is offered no swatches), for `shape` a subtype picker, for
`icon` a picker grid over the full icon vocabulary, and for `vote_dot` a value
stepper that never counts below zero. `freehand` gets its own right-click
property editor too (color, stroke width, smoothing, opacity) — it is not in
the rotatable set (see [Canvas rendering](#canvas-rendering) on why rotation is
not drawn for it), so it has no rotation control. The dedicated
`note`/`label`/`line`/`freehand` editors and the generic one alike also carry
the shared bring-to-front/send-to-back layer row described under
[Layer order](#layer-order); `group`'s own context menu does not. What the
editors still do not cover: any font control at all for the generic `text`
kind (`note` and `label` have a text-size picker, but no kind has font family,
weight, style or alignment), and cropping/replacing an `image`'s pixel
content.
`label`, `text`, `icon` and `vote_dot` can now also be attached to a node or
another annotation from the GUI, by dragging the annotation within snapping
distance of the target
([Attachment and detach behavior](#attachment-and-detach-behavior)) — there
is still no dedicated "nearby object menu" (the wireframe above) that
pre-wires a new attachable annotation to a target at creation time. Closing
what remains is tracked per type in the [acceptance matrix](#acceptance-matrix);
it is not satisfied by documenting the wireframes above.

### Layer order

An annotation's `z` orders it against the other annotations on the canvas.
The `note`, `label`, `line`, `freehand` and generic-kind context menus each
carry the same layer row (`AnnotationLayerControls`), offering **bring to
front** and **send to back**. `group` has its own context menu and does *not*
carry the row — a group box can be ordered against by other annotations but
cannot be reordered itself.

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
publishes no operation.

Semantic default layers — a per-kind default `z` at creation time, so a frame
starts behind the annotations it frames — are **not** implemented; every
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
browser only claimed graph nodes at all — and are now enforced client-side:
a claim another live client holds blocks local dragging, resizing, and every
per-kind mutation (with a surfaced notice rather than a silent no-op).

**Status update:** the server now also rejects (409 `ClaimConflict`) a
**browser** batch op (`POST /sessions/{id}/ops`, i.e. `SessionManager.apply_ops`
— the path every generic annotation mutation from the GUI's op scheduler goes
through) and the human clipboard-paste/file-upload image endpoint
(`POST /sessions/{id}/annotations/image`) when it would update or delete an
annotation another client currently holds a live claim on. `ClaimMap`
(`backend/core/session_hub.py`) itself is unchanged and stays *advisory* —
`claim()` still always takes over an existing claim (LWW) rather than
refusing — but every write path that goes through `apply_ops` now reads a
snapshot of it first and refuses instead of silently applying, matching the
client-side exclusivity above with a server backstop a client that ignores
`data.remoteSelection` can no longer bypass. `undo_last_action` (`/undo`) is
not covered by this check either.

**Remaining gap:** this is scoped to browser-originated writes only. The
synchronous MCP write path (`upsert_annotation`/`update_annotation`/
`delete_annotation`/`apply_layout`/`add_node_refs`, all keyed to the shared
`mcp-agent` client id — `backend/service/mcp_tools.py`) never goes through
`apply_ops` and is not checked against `ClaimMap` at all: an MCP agent still
silently overrides a live human claim exactly as before, the same way it
already bypasses the client-side exclusivity UI and the `locked` flag today.
Whether MCP-issued ops should be checked against claims too — an agent is
often doing the kind of bulk/automated arrangement work a claim exists to
protect against, but agents also need to act on sessions nobody has open — is
a genuine, still-open product decision, tracked on
`task-annotation-shared-session-realtime`; it is deliberately not guessed at
here. Actor-scoped conditional undo *is* implemented
(`backend/core/session_activity.py`).

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
now exist for every generic kind; image paste/upload now has a GUI path too
(toolbox file picker, clipboard paste, OS file drop); the bottom
toolbox/mobile sheet now also creates `icon` and `vote_dot` (each with a
fixed default), and `icon`'s right-click editor has its own picker grid over
the full vocabulary described below. Recoloring any generic kind and
changing a `vote_dot`'s value after creation are still reachable only
through the MCP tools, which is the gap the acceptance matrix tracks, not
the intended end state.

Each `shape` variant draws its own geometry (`SHAPE_STYLES` in
`GenericAnnotationNode.jsx`).

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
below writes it directly, and `create_sticky_note`/`update_sticky_note` set
one over MCP too (see below). Rotation-aware resize handles are an open gap.
The capability baseline requires it for text/headings, labels/callouts, sticky notes,
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
`rotation` travels on the flow node's `data`. This is the canvas UI's own
enforcement of `locked` — the server never rejects a write to a locked
annotation — but which *tool* performs that write differs by type:

- For the generic types (`text`/`label`/`line`/`frame`/`shape`/`icon`/
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
  `update_group_members`/`delete_group_annotation` are its own dedicated set,
  and none of them models `z`/`locked`/`rotation`).

A translator that dropped any of `z`/`locked`/`rotation` would make the
browser's own next autosave diff the annotation back to its `z: 0` /
`locked: false` / `rotation: 0` default and silently overwrite whatever a
collaborator or agent had just set.

## Acceptance matrix

A downstream task or PR may claim a row **done** only when every cell in
that row is satisfied end to end (not merely coded, but exercised by a test
or, for the device column, a physical-device pass). ✅ = satisfied today,
⚠ = partially satisfied, ❌ = not started, ⬜ = no acceptance test defined
yet (status unknown, treat as not done). See [Downstream closure
rule](#downstream-closure-rule).

| Type | GUI create/edit | MCP create/edit | Persistence/reload/saved views | Realtime/collaboration | Activity/undo | Accessibility/device |
|---|---|---|---|---|---|---|
| `note` | ✅ toolbox create, inline edit, drag/resize, rotate/recolor/resize-text/layer (right-click) | ✅ `create_sticky_note`/`update_sticky_note` take `rotation`, `z` and `locked` (mirroring the generic tools' fields for the same); `list_sticky_notes` reports all three back — the generic `reorder_annotation`/`set_annotation_lock` still refuse note ids by design, but the dedicated tools now cover the same ground | ✅ | ✅ op broadcast + revision | ✅ actor-scoped undo | ⬜ no formal pass yet |
| `text` | ⚠ toolbox create (fixed default), rotate/recolor/layer (right-click), attach by dragging near a node/annotation; no font editor and no way to inspect or clear an attachment other than dragging | ✅ generic tool set | ✅ | ✅ | ✅ | ⬜ |
| `label` | ✅ toolbox create, inline edit, drag/resize, rotate/recolor/resize-text/layer (right-click), attach by dragging near a node/annotation — previously listed "attach" as done, but it was modeled server-side only and never wired into the canvas translation layer until this slice | ✅ generic tool set | ✅ | ✅ | ✅ | ⬜ |
| `line` | ⚠ toolbox create, endpoint attach/drag, recolor/layer (right-click); a `rotation` the MCP tools accept is stored and reported but never drawn | ✅ generic tool set (`arrow` alias) | ✅ | ✅ | ✅ | ⬜ |
| `frame` | ✅ toolbox create (fixed default size), drag/resize, rotate/recolor/layer (right-click) | ✅ generic tool set | ✅ | ✅ | ✅ | ⬜ |
| `group` | ✅ toolbar create-group action | ✅ `create_group_annotation` creates or upserts the box — editing an existing group's label/color/geometry goes through this same upsert-by-id path (resend every field you want kept, unlike the generic types' dedicated patch tool) rather than a separate update tool — `update_group_members` adds/removes member ids without a full resend, and `delete_group_annotation` deletes the box (member graph nodes are never cascade-deleted — a group never owns them as annotations) | ✅ | ✅ | ⚠ creating/deleting the group annotation itself is actor-scoped undoable like any other type, but `group_membership_changed` is outside `session_activity.UNDOABLE_OPS` by design — a membership change is not itself undoable through `undo_last_action` | ⬜ |
| `shape` | ✅ toolbox creates all six variants, each drawn distinctly; right-click editor changes an existing shape's subtype, colour, rotation and layer (front/back) | ✅ generic tool set (`content.shape`) | ✅ | ✅ | ✅ | ⬜ |
| `icon` | ✅ toolbox create (fixed default glyph), move, rotate (right-click) and attach by dragging near a node/annotation; right-click picker grid over the full icon vocabulary changes an existing icon's name — renders every one of the 75 host-registry icon names as its own distinct glyph (see [Canvas rendering](#canvas-rendering)) — plus colour and layer | ✅ generic tool set | ✅ | ✅ | ✅ | ⬜ |
| `vote_dot` | ✅ toolbox create (fixed default value of 1), move, rotate/recolor/layer and a value stepper (right-click), and attach by dragging near a node/annotation | ✅ generic tool set | ✅ | ✅ | ✅ | ⬜ |
| `image` | ✅ clipboard paste, OS file drop, and the toolbox's file-picker item all ingest through `POST /api/sessions/{id}/annotations/image` (same pipeline as MCP); move/resize/rotate (right-click)/layer/lock/copy/delete via the generic annotation context menu once created | ✅ `create_image_annotation` ingests; generic create/update refuse image content, and no session annotation write can persist a *new* non-embedded image URL — note the duplicate, saved-view and budget limits in [enforcement](#image-ingest-enforcement) | ✅ | ✅ | ⚠ actor-scoped undo works, but the op is attributed to a dedicated server client id rather than the pasting browser's own (required so the pasting browser's own SSE subscription sees the embedded result instead of dropping it as a self-authored echo — see `_HUMAN_IMAGE_INGEST_CLIENT_ID` in `rest_api.py`), so only that marker's own undo call reverts it, not the pasting browser's | ⬜ no formal pass yet |
| `freehand` | ⚠ toolbox "Freehand" item arms a one-shot pointer-capture drawing mode (coalesced samples, device pressure when reported, constant-width fallback otherwise, concurrent-input suppressed with a notice); right-click property editor for color/width/smoothing/opacity plus the shared layer row; a `rotation` on the document model is still never drawn (see Canvas rendering) | ✅ generic tool set — `freehand` has been in `GENERIC_ANNOTATION_TYPES` since #422, so create/update/reorder/lock/delete already worked; `duplicate_annotation` was missing the `translate_freehand_points` call `update_annotation`'s patch builder already had (a duplicated stroke kept its original `points` at a moved envelope position), fixed here | ✅ document model round-trips it | ✅ same op broadcast as every other type — MCP creation now gives a way to exercise this live | ✅ `translate_freehand_points` covers move/undo | ❌ no physical stylus/touch pass — the GUI wiring above is verified only under mouse-event emulation, not a real device |
| cross-type | — | — | — | ⚠ create/delete/style/geometry publish immediately and note/label text is now live-synced and debounced at 300 ms, split out from the general autosave debounce; selection claims cover every annotation kind, are enforced client-side, and the server now rejects a browser write against a claim someone else holds — but the MCP write path still bypasses `ClaimMap` entirely, a still-open decision ([gap](#operation-timing-and-leases)) | ✅ actor-scoped conditional undo (`session_activity.py`) | — |

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
