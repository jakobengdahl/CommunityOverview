# Canvas annotation contract v1

Status: accepted implementation contract. This document operationalizes the accepted annotation baseline for the open-core canvas without adding new product scope.

## Scope

The annotation model is separate from canonical graph nodes and edges. It is durable session state, can be produced by humans or agents, and is safe to create before a visual client connects. Session and saved-view persistence must round-trip the same versioned document.

V1 supports these annotation types:

- `note`
- `text`
- `label`
- `line`
- `frame`
- `group`
- `shape`
- `icon`
- `vote_dot`
- `image`

Existing canvas note, label, arrow and group descriptors are migrated into the v1 model. Legacy `arrow` remains an accepted input alias and normalizes to `line`.

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
- type-specific payload fields

Attachments are represented with `attachment` for object-to-node binding and `start`/`end` endpoint descriptors for lines. Attached objects follow the referenced object while it exists and detach at their last model-space geometry when the target is removed.

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

The operation result returns the next document and an inverse operation for undo/redo. Invalid operations must fail without partially mutating the document. Agents and GUI code use the same model-space coordinates and operation semantics.

## Persistence

Session snapshots and saved views store the complete annotation document. Reload must accept v1 documents and legacy arrays of notes, labels, arrows and groups. Persisted annotations must not write graph nodes or graph edges.

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

The rest of the v1 model — `text`, `label`, `line` (`arrow` accepted as a
legacy alias), `frame`, `shape`, `icon`, `vote_dot`, `image` — is exposed the
same way through a generic tool set: `list_annotations` /
`create_annotation` / `update_annotation` / `delete_annotation` /
`reorder_annotation` / `set_annotation_lock` / `duplicate_annotation`, over
the same session op protocol and optimistic-concurrency contract. `note`
stays on its own dedicated tool set; `group` (node-membership boxes) is not
exposed through either — its `member_node_ids` are edited through the
`group_membership_changed` op, which has no MCP tool. Neither tool set lets
a write silently convert one annotation type into another: creating or
updating across the note/generic boundary, or replacing an existing generic
annotation's id with a different type, is refused rather than applied. See
`backend/DEVELOPMENT.md`'s "Generic annotation tools" section for the full
contract, including the per-type `content` payload shape.

## Canvas rendering

`note`, `label` and `line` have dedicated, interactive canvas UX (drag,
resize, inline text editing, anchoring). The rest of the v1 model — `text`,
`frame`, `shape`, `icon`, `vote_dot`, `image` — renders with selection and
drag-to-move for every kind, plus model-space resize (via the same
`NodeResizer` handles as `note`) for the three kinds that carry an explicit
box size: `frame`, `shape` and `image`. `text`, `icon` and `vote_dot` render
at a fixed intrinsic size and are not resizable. A locked annotation of any
generic kind hides its resize handles the same way a locked `note` does.
Per-type property editors (recolouring, changing an icon, cropping an image)
remain out of scope for v1 and are done through the MCP tools above instead.
There is also no canvas-UI creation path for these six types yet — they are
only created through the MCP tools; once created there, the canvas renders
and manipulates them like any other annotation.

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

## V1 non-goals

GIF, SVG, crop, image filters, threaded comments, vote counting, true frame grouping and cross-session annotation libraries are outside v1.
