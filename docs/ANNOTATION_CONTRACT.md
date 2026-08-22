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

## V1 non-goals

GIF, SVG, crop, image filters, threaded comments, vote counting, true frame grouping and cross-session annotation libraries are outside v1.
