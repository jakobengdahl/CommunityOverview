# Configurable node shapes

The models, strict validation, resolution contract, schema transport and empty
session maps are implemented. Rendering, editing, shared appearance operations
and library persistence remain future work. Unconfigured nodes continue to use
the existing CustomNode renderer.

[Light and dark gallery](node-shapes/gallery.html) ·
[Clickable editor](node-shapes/editor.html). Open either file directly; no server,
build, fonts, external images or dependencies are needed. Mock actions are local
illustrations and do not persist data.

## Starting point

The following existing code is the integration baseline:

- [`CustomNode.jsx`](../../packages/ui-graph-canvas/src/components/CustomNode.jsx)
  renders type, label and summary with top target and bottom source handles.
  Its CSS uses a 2px type-colour border, 8px corners, 150–250px width,
  `0 2px 8px` shadow, uppercase 0.75rem type, 0.9rem semibold title,
  and a two-line 0.75rem summary. Skill, staged, marked, pulse, dimmed,
  selected and remote-selection states must survive the new renderer.
- [`CustomNode.css`](../../packages/ui-graph-canvas/src/components/CustomNode.css)
  uses dark surfaces `#1a1a1a`, text `#ddd` / `#999`, and a light media-query
  variant with white surfaces and `#213547` / `#666` text. There is no shared
  shape-token system today; extract equivalent semantic tokens before reuse.
- [`MetamodelExplorerDialog.jsx`](../../frontend/web/src/components/MetamodelExplorerDialog.jsx)
  and its CSS provide the above-canvas overlay (z-index 2100), 920px / 95vw
  dialog, header, tabs, initial focus and Escape handling. It is read-only;
  reuse its layout language, not its schema-editing responsibilities.
- [`config_loader.py`](../../backend/config/config_loader.py) defines
  `NodeTypeConfig`, `SchemaConfig`, and `SchemaFileConfig`; the loader validates
  configuration and injects system types. New fields need explicit models.
  [`rest_api.py`](../../backend/service/rest_api.py) exposes `/schema`;
  [`api.js`](../../frontend/web/src/services/api.js) fetches it and
  [`graphStore.js`](../../frontend/web/src/store/graphStore.js) stores schema
  and presentation via `setConfig`. Extend this complete path, including system types.
- [`sessionStore.js`](../../frontend/web/src/services/sessionStore.js) keeps
  only local recents; old local snapshots are purged. Content belongs in
  [`session_store.py`](../../backend/core/session_store.py), whose state keys
  and operation types are explicit. Do not add shape persistence to localStorage.
- [`sessionSyncClient.js`](../../frontend/web/src/services/sessionSyncClient.js)
  diffs snapshots, batches POST operations, mirrors remote operations to avoid
  echoes, and uses sequenced SSE catch-up/resync. Shapes require additions to
  both mirror/diff and server validation, not just canvas props.
- [`graphLayout.js`](../../packages/ui-graph-canvas/src/utils/graphLayout.js)
  intersects centre rays with rectangles and chooses cardinal Bezier directions.
  [`SimpleFloatingEdge.jsx`](../../packages/ui-graph-canvas/src/components/SimpleFloatingEdge.jsx)
  consumes these results. [`GraphCanvas.jsx`](../../packages/ui-graph-canvas/src/components/GraphCanvas.jsx)
  also uses DOM `elementFromPoint` for erasing; transparent wrapper corners
  cannot remain clickable when the painted shape is triangular.

## Curated base shapes

Keep the catalogue small. These are entity cards, independent of annotation
shapes, with no arbitrary SVG, rotation, custom paths or uploaded geometry.
Colours remain node-type colours. All shapes retain a coloured top band,
card surface, typography and shadow. The triangle band is the silhouette's
intersection with its top 28px, including a small cap for an upward triangle.

| Base | Nominal body at scale 1 | Text placement | Three areas |
| --- | --- | --- | --- |
| `rectangle` | 200 × 120px, 8px corners | nine positions | band, title, summary |
| `rectangle-flat` | 240 × 96px, 8px corners | nine positions | band, title, summary |
| `triangle-up` | 240 × 180px | centre, bottom | above/outside, middle, bottom |
| `triangle-down` | 240 × 180px | centre, bottom | above/outside, middle, bottom |

Rectangle positions are top-left, top-centre, top-right, middle-left,
middle-centre, middle-right, bottom-left, bottom-centre, bottom-right.
They place the title/summary stack within the padded body; band text stays in
the band and follows the chosen horizontal alignment. Triangle placement chooses
a centre or lower safe text stack inside the polygon. In a downward triangle,
“bottom” uses the lowest region wide enough for text, never the pointed tip.
Area identities never change when placement changes.

Scale is a finite number from 0.1 through 10.0 inclusive, relative to the
base's scale-1 dimensions and text metrics. It scales the whole card, including
font, padding and band. Zoom remains independent. At tiny sizes use a minimum
24px screen-space interaction target and accessible full text; offer zoom to
read. Do not silently clamp the saved scale. Existing unconfigured cards retain
their content-driven dimensions, rather than being forced to the nominal size.

Each area maps to a node attribute or none. Default rectangle bindings are type,
name (rendered today as label), summary; triangles use the same three bindings
above, middle and bottom. A risk example maps name above, risk_level in the
middle and none at the bottom. The type band remains visible even when its text
is none. Missing or unreadable attributes render empty, never a made-up value.
Accept the built-in type/name/summary bindings plus scalar fields declared by the
node type. Render plain escaped text; no HTML, expression evaluation, paths into
private metadata or object dumps. Truncate visually to area bounds, retaining
full accessible text. Preview missing values explicitly in the editor.

## Proposed JSON contract

Named definitions are a separate root config data type, `node_shapes`.
`schema.node_types.<type>.shape` contains a name only; omission means the built-in
card. Names are stable keys (1–64 lowercase ASCII letters, digits or hyphens,
starting with a letter). A display name can be changed without changing the key.
Configuration definitions are read-only in the editor.

This JSON Schema describes one definition (Draft 2020-12). The two rectangle
bases share the nine positions; triangles deliberately allow only two.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["version", "base", "scale", "placement", "areas"],
  "properties": {
    "version": {"const": 1},
    "base": {"enum": ["rectangle", "rectangle-flat", "triangle-up", "triangle-down"]},
    "scale": {"type": "number", "minimum": 0.1, "maximum": 10},
    "placement": {"type": "string"},
    "areas": {
      "type": "object",
      "additionalProperties": false,
      "required": ["first", "second", "third"],
      "properties": {
        "first": {"$ref": "#/$defs/binding"},
        "second": {"$ref": "#/$defs/binding"},
        "third": {"$ref": "#/$defs/binding"}
      }
    }
  },
  "$defs": {
    "binding": {
      "oneOf": [
        {"type": "null"},
        {"type": "string", "minLength": 1, "maxLength": 64}
      ]
    }
  },
  "allOf": [{
    "if": {"properties": {"base": {"enum": ["triangle-up", "triangle-down"]}}},
    "then": {"properties": {"placement": {"enum": ["centre", "bottom"]}}},
    "else": {"properties": {"placement": {"enum": ["top-left", "top-centre", "top-right", "middle-left", "middle-centre", "middle-right", "bottom-left", "bottom-centre", "bottom-right"]}}}
  }]
}
```

Full config fragment, with an example for every base (other node type fields
are omitted here):

```json
{
  "node_shapes": {
    "standard": {"version": 1, "base": "rectangle", "scale": 1, "placement": "top-left", "areas": {"first": "type", "second": "name", "third": "summary"}},
    "compact": {"version": 1, "base": "rectangle-flat", "scale": 0.8, "placement": "middle-centre", "areas": {"first": "type", "second": "name", "third": "summary"}},
    "risk": {"version": 1, "base": "triangle-up", "scale": 1.2, "placement": "centre", "areas": {"first": "name", "second": "risk_level", "third": null}},
    "inverted": {"version": 1, "base": "triangle-down", "scale": 1, "placement": "bottom", "areas": {"first": "type", "second": "name", "third": "summary"}}
  },
  "schema": {"node_types": {"Risk": {"shape": "risk"}}}
}
```

The outer config validates `node_shapes` as a map of the definition above,
checks key syntax, and validates each type's nullable `shape` string against
that map. Attribute eligibility is checked against the consuming type; a library
shape may be reused by another type with an empty missing field and editor warning.
Unknown bases, properties, versions, invalid scale or placement are rejected on
writes. Loading malformed external data isolates that definition, reports a
structured diagnostic, and renders the built-in card without losing the graph.

Session selections (models defined; mutation operations are not yet accepted by the API):

```json
{
  "shape_overrides": {"node-example": {"ref": "saved:risk-view"}},
  "type_shapes": {"Risk": {"inline": {"version": 1, "base": "triangle-up", "scale": 1, "placement": "centre", "areas": {"first": "name", "second": "risk_level", "third": null}}}}
}
```

Each selection is exactly one of `{ "ref": "config:<name>" }`,
`{ "ref": "saved:<name>" }`, `{ "inline": <definition> }` or
`{ "builtin": true }`; reject extra keys. Namespaces prevent collisions.
Removing a selection means inherit; built-in explicitly stops inheritance.
Keep graph attributes and graph type definitions unchanged by session edits.

## Resolution and deletion

Resolve a complete selection, with no field-by-field merging:
node override → session type default → config type default → built-in card.
Inline edits clone the resolved definition. Applying to all of a type sets the
session type default and removes the selected node's override in one operation;
other explicit node overrides remain, and the confirmation explains this.
Newly added nodes of that type inherit automatically.

A missing, deleted, invalid or unsupported reference resolves directly to the
built-in card, with a diagnostic, rather than exposing a lower-priority shape.
Deletion therefore reverts every reference user to the default, including closed
sessions when reopened. Preserve a tombstone so a reused name never resurrects
an old selection; recommend forbidding key reuse. Reset to inherited look is a
separate action that removes the selection.

## Storage and editing rights

Propose a server-side shape library beside session persistence, outside graph
data and outside checked-in config. Use an atomic file-backed implementation
behind a persistence interface, following `SessionPersistenceBackend`; do not
write runtime edits into `schema_config.json`. Store key, display name,
definition, owner identity, revision, timestamps and deletion tombstone.
Saved references follow the current revision; inline copies remain independent.

Session participants with existing session-write permission may change session
looks; locked/read-only access cannot. Library creation requires authenticated
identity; everyone may edit/delete every saved entry for now. Keep this permission
check in one replaceable function. Never trust client owner fields.
Until a trustworthy identity/permission boundary is available, disable library
writes rather than treating a client ID as an owner. Config changes remain an
operator file workflow. The ownership decision is recorded below.

On edit/delete, show the count of referencing sessions, including inactive
persisted ones, without exposing their names to unauthorized users. Recheck
revision and usage at confirmation; a stale revision returns conflict and
requires review. Deletion is a tombstone transaction, so no cross-session bulk
rewrite is necessary. Maintain a reverse reference index, rebuilt from persisted
sessions after recovery. Count references in node and type selections.

## Editor flow

Open “Node appearance” for the selected entity, above the canvas. Show the node
name, type and current source of its look. Keep a local draft and preview; Cancel
or Escape discards it and restores opener focus. Canvas changes occur only on Apply.

1. **Shape:** choose one of four bases or pick a saved/config shape. Set scale
   with a number input and slider; invalid input disables Apply and explains why.
2. **Text:** three labelled attribute selectors including None; placement choices
   depend on base. Changing base preserves bindings and resets unsupported
   placement to its default with an announcement. Preview light/dark and long text.
3. **Apply & library:** choose selected node or session default for this type.
   Show existing overrides retained. Save as named shape creates a library entry;
   picking it explicitly creates a reference. Edit saved updates that entry only
   after the usage warning. Save copy creates a fresh key. Delete asks for
   confirmation, names the default-card fallback and leaves a tombstone.

Library saves and session Apply are distinct transactions. If saving succeeds
but Apply fails, keep the saved entry and draft, report the failure, and allow
retry. Never report success for an optimistic preview. The static editor shows
these steps and warnings; it is a flow mockup rather than a working library.

## Shared sessions

Add validated, sequenced operations `node_shape_set` (node ID + selection/null)
and `type_shape_set` (type + selection/null, optionally clearing the initiating
node override atomically). Add both maps to persisted state, snapshots, mirror,
diff, remote reducer, catch-up, undo and export/import. Removed nodes lose their
overrides. Apply checks the current session permission and supported version.
Each target has a revision; stale writes conflict instead of overwriting a
collaborator's newer edit. Accept retries through an operation ID, once only.

A library edit/delete emits a library revision invalidation to all sessions using
it. Clients fetch the current permitted definition and update geometry; reconnect
fetches the library revision before resolving session state. Closed sessions
resolve current library state when loaded. Preserve unsent drafts across resync
but require rebase/review before Apply. Session switching must discard events
for the previous session. Retain existing size/rate caps; send one type-default
operation rather than thousands of per-node edits. Never copy remote state back
out as fresh operations. Old clients must be read-only for appearance-enabled
sessions unless they can preserve the new fields and operations.

## Geometry and hit areas

One geometry descriptor must drive drawing, hit tests, edge anchors and layout
bounds. Rectangle rays intersect straight segments and rounded corners; triangles
intersect the three polygon edges. Use the nearest positive centre-ray
intersection; coincident centres use a stable top/bottom fallback without NaN.
Choose Bezier direction from the outward edge normal's dominant cardinal axis.
Retain stable source/target handle identities and refresh React Flow internals
when dimensions change, including references updated by library invalidation.

Triangle vertices are (w/2,0), (w,h), (0,h) upward and (0,0), (w,0), (w/2,h)
downward. Rectangle-flat uses the same rounded-box algorithm as rectangle.
Edges attach to body boundaries, never outside labels or shadows. Reserve
outside-label space above triangles for layout, fit-view and group bounds;
keep it separate from the anchor body. Preserve node centre when resizing and
recalculate connected edges, without silently running a full graph layout.

Pointer selection/drag/context menu and erasing use the painted body plus its
visible text labels. Transparent corners pass through. A noninteractive wrapper
and explicit SVG/label hit surfaces must work with `elementFromPoint`; merely
clipping a child while the React Flow wrapper catches events is insufficient.
Marquee selection uses polygon/selection-rectangle overlap (including outside
label bounds). Broad-phase bounding boxes remain useful for spatial indexing.
Minimum screen-space hit targets must resolve overlap by nearest painted body,
then z-order. Controls/badges stay outside clipped bodies and remain operable.

## Theme, accessibility and scale

Extract semantic card surface, primary/secondary text, border, band foreground,
focus and shadow tokens from current styles. Honour the existing media-query
theme behaviour. Choose white or dark band text for at least 4.5:1 contrast;
if neither works with a supplied colour, use an opaque accessible text backing.
Use a 3:1 visible boundary/focus treatment; colour never carries state alone.
Keep marked, staged, skill and remote indicators identifiable in both themes,
and preserve reduced-motion and stronger-contrast preferences.

Use a labelled modal with focus trap, inert canvas, Escape/Cancel and focus
return. Tabs support arrow/Home/End keys and expose selected state. Every
selector has a label; preview changes announce a short status without reading
all text repeatedly. Node accessible names include type and name plus the three
mapped values in order, even if visually clipped or None hides a visual area;
None contributes no value. Detail access still exposes the original full node
content. Selection, appearance editing and related-node actions work by keyboard.
SVG decoration is hidden from screen readers; avoid duplicate SVG/HTML readings.
Production UI strings belong in both English and Swedish i18n files; these static
English design artifacts are not application strings.

Memoize definitions by key/revision and resolved look by node/type revision.
Cache normalized geometry per base/scale and batch dimension measurements.
Reuse inline paths, avoid per-node filters and effects; use existing lightweight
shadows and viewport culling. Do not fetch attributes on every render; derive
bindings from already-loaded node data. Bound label lines and cap text processing
before layout. Type-default changes invalidate that type, library edits only
its users; edge updates are limited to affected endpoints.

Before release, measure a fixed synthetic 1,000- and 5,000-node graph, mixed
shapes at 0.1/1/10 scale, in both themes: initial render, pan frame time, memory,
shape apply and edge update. Proposed budget: under 10% regression against the
same legacy-card scene and no additional per-node network requests. Record
hardware/browser and p50/p95; use the confirmed relative 10% budget. These benchmarks are future implementation work, not results claimed here.

## Migration and small implementation phases

Absent fields keep the exact existing `CustomNode` path, dimensions, bindings,
positions and styling. Default empty maps on session load. No graph-data rewrite
is needed. Config shape references are opt-in. Back up sessions/library before
rollout; preserve unknown appearance data during rollback and disable writes
from incompatible clients. A removed configured definition uses the same
built-in fallback as a deleted saved shape.

Each phase is a proposed PR boundary, not a separate project status tracker:

1. Define models, validation and resolution contract; add schema transport and
   empty session maps. Target tests: valid/invalid definitions and precedence,
   with an old-session fixture proving unchanged round-trip behaviour.
2. Extract appearance tokens and geometry for rectangle/flat, then triangles.
   Target geometry tests: boundary anchors, coincident centres, polygon hits and
   scaled bounds. Verify unchanged unconfigured cards with existing render tests.
3. Add renderer and accessible text, preserving state badges and keyboard actions.
   Target component coverage for each distinct rendering guarantee; manually
   inspect clipping, dark/light, zoom extremes, controls and screen reader output.
4. Add session operations, permission/revision checks, mirror and resync. Extend
   existing session tests for two-client conflict, retry, reconnect and switching;
   verify a type change remains one operation.
5. Add the editor draft/Apply flow and i18n. Target Cancel isolation, validation,
   scope application and keyboard/focus behaviour in existing component suites.
6. Add library persistence, permission checks, reference
   index and invalidations. Test unauthorized mutation, stale edit, deletion in
   active/closed sessions, restart and tombstone non-resurrection. Run the focused
   performance scenes and accessibility review before enabling library writes.

Use normally one test per guarantee, a second only to falsify a distinct failure;
extend sibling suites and keep repo test-volume limits. No broad test suite or
live-service test is required for these documentation-only artifacts.

## Open questions for the owner

The following decisions were confirmed on 2026-11-12:

- **Library ownership:** everyone can edit every saved shape for now. Keep the
  permission check in one replaceable function so restrictions can be added later.
- **Edit propagation:** live references with usage warnings, revision conflict
  checks and Save copy. Deletion returns reference users to the built-in card.
- **Catalogue:** the four bases documented above only.
- **Attribute types:** scalar fields only; missing values render empty with a
  visible editor warning.
- **Scale meaning:** uniform scaling with zoom-to-read and minimum interaction targets.
- **Performance budget:** relative 10% budget on the 1,000- and 5,000-node scenes;
  record frame times.
