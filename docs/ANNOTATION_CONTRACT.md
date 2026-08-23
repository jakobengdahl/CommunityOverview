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
(dedicated toolbar/toolbox actions). A first slice of the bottom toolbox now
also creates `text`, `frame`, and `shape` limited to its `rectangle` and
`circle` variants — the only two `content.shape` values that render as
distinct visuals today. `icon`, `vote_dot`, `image`, `freehand`, and the
remaining `shape` variants (triangle, rhombus, hexagon, process arrow) still
render on canvas but have no GUI creation path — they can only be created via
MCP. Per-type property editors (recoloring, changing an icon or shape
subtype, cropping an image) do not exist yet for any generic type, including
the ones the toolbox now creates. Closing this is tracked per type in the
[acceptance matrix](#acceptance-matrix); it is not satisfied by documenting
the wireframes above.

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
verbatim. Underneath both, `SessionStore._validate_annotation`
(`image_annotation_error` in `backend/core/session_annotations.py`) rejects
any `annotation_created`/`annotation_updated` op whose `image` payload is
not an embedded `data:image/png|jpeg|webp;base64` URI — so a raw op posted
to `/api/sessions/{id}/ops` is held to the same rule as an MCP call, and a
move/resize/alt-text patch (which carries no changed `image` payload) is
unaffected. What the store cannot tell apart is *which* embedded bytes were
produced by ingest: a client that forges a small data URI of its own still
persists a self-supplied picture (bounded by the generic op-batch cap, and
by the same-origin session it is writing to). No GUI creates one today —
the `image` GUI cell below is still ❌ — and no path fetches, embeds or
re-serves a remote resource, which is the property this section requires.

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

The rest of the v1 model except `group` and `freehand` — `text`, `label`,
`line` (`arrow` accepted as a legacy alias), `frame`, `shape`, `icon`,
`vote_dot`, `image` — is exposed the same way through a generic tool set:
`list_annotations` / `create_annotation` / `update_annotation` /
`delete_annotation` / `reorder_annotation` / `set_annotation_lock` /
`duplicate_annotation`, over the same session op protocol and
optimistic-concurrency contract. `note` stays on its own dedicated tool set;
`group` (node-membership boxes) is not exposed through either — its
`member_node_ids` are edited through the `group_membership_changed` op,
which has no MCP tool. **`freehand` has no MCP tool at all yet** — it can be
read via `list_annotations` (it round-trips through the document model) but
cannot be created, updated or deleted headlessly; see the acceptance matrix.
Neither tool set lets a write silently convert one annotation type into
another: creating or updating across the note/generic boundary, or replacing
an existing generic annotation's id with a different type, is refused rather
than applied. See `backend/DEVELOPMENT.md`'s "Generic annotation tools"
section for the full contract, including the per-type `content` payload
shape.

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
`freehand` also has no creation entry point (GUI or MCP) to test end-to-end
today, so device acceptance cannot be scheduled ahead of that.

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
non-goal — today they are reachable only through the MCP tools, which is
the gap the acceptance matrix tracks, not the intended end state.

`z` and `locked` round-trip through every annotation type's canvas
representation (`overlayToFlowNode`/`flowNodeToOverlay` in
`packages/ui-graph-canvas/src/utils/annotations.js`, and the server-model
translators in `frontend/web/src/utils/sessionAnnotations.js`): `z` maps to
the ReactFlow node's `zIndex`, and `locked` maps to `draggable: false`. This
is the canvas UI's own enforcement of `locked` — the server never rejects a
write to a locked annotation (`reorder_annotation` / `set_annotation_lock` /
`update_annotation` all still apply). A translator that dropped either field
would make the browser's own next autosave diff the annotation back to its
`z: 0` / `locked: false` default and silently overwrite whatever a
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
| `note` | ✅ toolbox create, inline edit, drag/resize | ✅ sticky note tool set | ✅ | ✅ op broadcast + revision | ✅ actor-scoped undo | ⬜ no formal pass yet |
| `text` | ⚠ toolbox create (fixed default), no property editor | ✅ generic tool set | ✅ | ✅ | ✅ | ⬜ |
| `label` | ✅ toolbox create, inline edit, drag/resize, attach | ✅ generic tool set | ✅ | ✅ | ✅ | ⬜ |
| `line` | ✅ toolbox create, endpoint attach/drag | ✅ generic tool set (`arrow` alias) | ✅ | ✅ | ✅ | ⬜ |
| `frame` | ⚠ toolbox create (fixed default size), no color editor | ✅ generic tool set | ✅ | ✅ | ✅ | ⬜ |
| `group` | ✅ toolbar create-group action | ❌ no MCP tool (by design — `group_membership_changed` op only) | ✅ | ✅ | ✅ | ⬜ |
| `shape` | ⚠ toolbox create (rectangle/circle only), no subtype picker for the rest | ✅ generic tool set (`content.shape`) | ✅ | ✅ | ✅ | ⬜ |
| `icon` | ❌ render/move only, no create UI or icon picker | ✅ generic tool set | ✅ | ✅ | ✅ | ⬜ |
| `vote_dot` | ❌ render/move only, no create UI or color picker | ✅ generic tool set | ✅ | ✅ | ✅ | ⬜ |
| `image` | ❌ no paste/upload UI | ✅ `create_image_annotation` ingests; generic create/update refuse image content, and the store rejects any non-embedded image URL on every write path ([enforcement](#image-ingest-enforcement)) | ✅ | ✅ | ✅ | ⬜ |
| `freehand` | ❌ no create UI (stylus input not wired) | ❌ no MCP tool | ✅ document model round-trips it | ⚠ no creation path to exercise it live | ✅ `translate_freehand_points` covers move/undo | ❌ no physical stylus/touch pass |
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
