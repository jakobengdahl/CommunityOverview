"""Annotation-shape helpers over the generic session annotation store.

``session_store``/``session_manager`` treat an annotation as an opaque dict —
they validate only the boundary fields (``type``/``kind``, ``id``, ``position``)
and apply the ``annotation_created``/``annotation_updated``/``annotation_deleted``
ops without knowing what a "note" or a "line" is. The v1 annotation shape
itself (geometry/position/size projections, per-type payload fields) is
defined by ``packages/ui-graph-canvas/src/utils/annotationModel.js`` and
consumed as-is by the canvas. This module builds and reads that same shape
from Python, once, so MCP tools do not each hand-roll it.

Three helper sets live here:

* note-shape helpers (``is_note``, ``build_note_annotation``,
  ``build_note_patch``, ``project_note``) — used by the dedicated
  ``list_sticky_notes``/``create_sticky_note``/``update_sticky_note``/
  ``delete_sticky_note`` MCP tools (``backend/service/mcp_tools.py``).
* generic-type helpers (``build_annotation``, ``build_annotation_patch``,
  ``project_annotation``, and the ``*_type`` functions) — used by the
  generic ``list_annotations``/``create_annotation``/``update_annotation``/
  ``delete_annotation``/``reorder_annotation``/``set_annotation_lock``/
  ``duplicate_annotation`` tools, which cover every v1 type except ``note``
  (kept on its own dedicated tool set above) and ``group`` (node-membership
  boxes, kept on its own tool set below).
* group-shape helpers (``is_group``, ``build_group_annotation``) — used by
  the dedicated ``create_group_annotation``/``update_group_members`` MCP
  tools. Membership itself is edited through the ``group_membership_changed``
  op (``update_group_members``, not this module), never by re-supplying
  ``member_node_ids`` through a generic patch — see
  ``build_group_annotation``'s docstring for why.

``image_annotation_error`` also lives here: the contract rule that an
``image`` annotation's pixel content is always an embedded, server-ingested
data URI is a property of the annotation *shape*, so ``SessionStore`` applies
it in one place rather than per entry point. Two writes are exempt by design
— re-sending the URL already stored under that id, and an undo replaying its
own inverse op — see the function's docstring and ``SessionStore.apply_state_op``.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Optional

NOTE_TYPE = "note"
GROUP_TYPE = "group"
IMAGE_TYPE = "image"
DEFAULT_NOTE_SIZE = {"w": 160, "h": 96}
# A group box has no natural single-member size the way a note does; this is
# just a usable default footprint for a freshly created, still-empty group —
# callers passing member ids up front should size it themselves.
DEFAULT_GROUP_SIZE = {"w": 320, "h": 200}

# The only `content.image.url` form an `image` annotation may be persisted
# with: an embedded base64 data URI of the content type server-side ingest
# *emits* (``image_ingest.OPTIMIZED_CONTENT_TYPE``). Deliberately not the
# wider set of formats ingest *accepts* as input — those are all re-encoded,
# so accepting their prefixes here would widen what a forged data URI may
# claim to be without any path ever producing one. Kept as a literal rather
# than imported so this module (which ``session_store`` imports on every
# annotation op) does not pull Pillow/httpx into the store's import path;
# ``test_session_annotations_image_guard.py`` pins it to the optimizer's
# output so the two cannot drift apart.
EMBEDDED_IMAGE_URL_PREFIXES = ("data:image/webp;base64,",)

# Every v1 type except `note` and `group` — see module docstring for why
# those two are excluded from the generic tool set.
GENERIC_ANNOTATION_TYPES: FrozenSet[str] = frozenset(
    {"text", "label", "line", "frame", "shape", "icon", "vote_dot", "image", "freehand"}
)
ALL_ANNOTATION_TYPES: FrozenSet[str] = GENERIC_ANNOTATION_TYPES | {
    NOTE_TYPE,
    GROUP_TYPE,
}
# Mirrors session_store's `_LEGACY_ANNOTATION_ALIASES`: `arrow` is still an
# accepted input alias for `line` (docs/ANNOTATION_CONTRACT.md).
LEGACY_ANNOTATION_ALIASES: Dict[str, str] = {"arrow": "line"}

# Envelope fields the generic builders/projector manage themselves; a caller
# supplying one of these inside `content` would silently overwrite bookkeeping
# the caller does not otherwise control (e.g. smuggling a `type` change
# through a patch), so it is rejected instead of merged.
_RESERVED_ANNOTATION_KEYS = {
    "id",
    "type",
    "kind",
    "geometry",
    "position",
    "size",
    "style",
    "z",
    "locked",
    "created_at",
    "updated_at",
    "created_by",
    "updated_by",
}

# The `content.shape` variants a `shape` annotation accepts
# (docs/ANNOTATION_CONTRACT.md), mirroring
# `packages/ui-graph-canvas/src/utils/annotationModel.js`'s `ANNOTATION_SHAPES`.
# A string outside this set is not rejected — `backend/DEVELOPMENT.md`
# documents that a name outside the set is "stored verbatim and drawn as a
# rectangle" by the canvas, matching `normalizeShapeName`'s behaviour of
# keeping an unrecognised name rather than discarding it, so this constant is
# used for documentation/tests, not as a rejection list. Only the *type* of
# `content.shape` is validated below (see `_validate_generic_content`).
ANNOTATION_SHAPES: FrozenSet[str] = frozenset(
    {"rectangle", "circle", "triangle", "rhombus", "hexagon", "process_arrow"}
)

# The generic types whose `content.attachment` may bind them to a node
# (docs/ANNOTATION_CONTRACT.md's "Attachment and detach behavior"). `line`
# attaches per-endpoint (`start`/`end`) instead, validated separately.
ATTACHABLE_ANNOTATION_TYPES: FrozenSet[str] = frozenset(
    {"text", "label", "icon", "vote_dot"}
)


def _attachment_error(value: Any, *, field: str) -> Optional[str]:
    """Structural validation for an `attachment = {target_id, target_type,
    anchor, offset}` payload (docs/ANNOTATION_CONTRACT.md's "Attachment and
    detach behavior"). `None` clears/omits the attachment and is always
    valid; anything else must be a well-formed object. Unlike the shape/icon
    checks, a malformed attachment is rejected rather than merely
    type-checked, because a value that isn't a resolvable target reference
    doesn't mean anything (there is no "verbatim but unrecognised" case for
    it the way there is for a shape or icon name).
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        return f"{field} must be an object"
    target_id = value.get("target_id")
    if target_id is None or (isinstance(target_id, str) and not target_id.strip()):
        return f"{field}.target_id is required and must not be empty"
    if not isinstance(target_id, (str, int, float)):
        return f"{field}.target_id must be a string"
    target_type = value.get("target_type")
    if target_type is not None and not isinstance(target_type, str):
        return f"{field}.target_type must be a string"
    anchor = value.get("anchor")
    if anchor is not None and not isinstance(anchor, str):
        return f"{field}.anchor must be a string"
    offset = value.get("offset")
    if offset is not None:
        if (
            not isinstance(offset, dict)
            or not isinstance(offset.get("x"), (int, float))
            or not isinstance(offset.get("y"), (int, float))
        ):
            return f"{field}.offset must be an object with numeric x and y"
    return None


def _line_endpoint_error(value: Any, *, field: str) -> Optional[str]:
    """Structural validation for a `line`'s `start`/`end` endpoint
    (docs/ANNOTATION_CONTRACT.md: "Line endpoints may attach to a node or to
    another annotation, or stay free-floating at a fixed model-space
    point."). `None` is valid (an endpoint carried only via the legacy
    `from`/`to` point fields, with no explicit `start`/`end`).
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        return f"{field} must be an object"
    point = value.get("point")
    if point is not None:
        if (
            not isinstance(point, dict)
            or not isinstance(point.get("x"), (int, float))
            or not isinstance(point.get("y"), (int, float))
        ):
            return f"{field}.point must be an object with numeric x and y"
    return _attachment_error(value.get("attachment"), field=f"{field}.attachment")


def _validate_generic_content(
    ann_type: Optional[str], source: Dict[str, Any]
) -> Optional[str]:
    """Type-specific structural validation for a generic annotation's
    payload fields, given either the `content` dict a builder is about to
    merge or the already-merged annotation dict (both put payload fields at
    the top level — see `_apply_content`). Returns an error message, or
    `None` when *source* is valid for *ann_type*.

    Deliberately narrow: only the fields the v1 contract actually
    type-constrains are checked (`shape`, `icon`, `attachment`, a `line`'s
    `start`/`end`) — everything else in `content` stays the free-form,
    verbatim payload `build_annotation`'s docstring describes. A `shape` or
    `icon` name outside its documented set is *not* an error (see
    `ANNOTATION_SHAPES`'s docstring); only its type is checked, so a caller
    gets a clear `invalid_content` for an obviously wrong payload (a number,
    a list) instead of silently corrupting the stored document with a value
    no renderer expects a string field to hold.
    """
    if not source:
        return None
    if ann_type == "shape" and "shape" in source:
        shape = source["shape"]
        if not isinstance(shape, str) or not shape.strip():
            return "content.shape must be a non-empty string"
    if ann_type == "icon" and "icon" in source:
        icon = source["icon"]
        if not isinstance(icon, str) or not icon.strip():
            return "content.icon must be a non-empty string"
    if ann_type in ATTACHABLE_ANNOTATION_TYPES and "attachment" in source:
        error = _attachment_error(source["attachment"], field="content.attachment")
        if error:
            return error
    if ann_type == "line":
        for key in ("start", "end"):
            if key in source:
                error = _line_endpoint_error(source[key], field=f"content.{key}")
                if error:
                    return error
    return None


def is_note(annotation: Dict[str, Any]) -> bool:
    """Whether *annotation* is a v1 ``note`` annotation (checks type or its
    ``kind`` compatibility alias, matching how the store itself resolves type).
    """
    ann_type = annotation.get("type") or annotation.get("kind")
    return ann_type == NOTE_TYPE


def build_note_annotation(
    *,
    x: float,
    y: float,
    text: str = "",
    color: Optional[str] = None,
    font_size: Optional[float] = None,
    w: Optional[float] = None,
    h: Optional[float] = None,
    rotation: Optional[float] = None,
    z: Optional[float] = None,
    locked: bool = False,
    annotation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a v1 ``note`` annotation dict for the ``annotation_created`` op.

    Mirrors ``createAnnotation({type: 'note', ...})``: geometry, position and
    size all carry the same x/y/w/h so any code reading either projection
    stays consistent. ``annotation_id`` is left out when not given, so the
    store assigns one (``SessionStore.apply_state_op`` mints a
    ``secrets.token_hex(8)`` id for a create with no id) — the caller reads the
    assigned id off the op result instead of inventing one.

    ``rotation``/``z``/``locked`` default the same way ``build_annotation``
    does for the generic types: an omitted ``rotation``/``z`` becomes ``0``,
    an omitted ``locked`` becomes ``False``.
    """
    size = {
        "w": w if w is not None else DEFAULT_NOTE_SIZE["w"],
        "h": h if h is not None else DEFAULT_NOTE_SIZE["h"],
    }
    annotation: Dict[str, Any] = {
        "type": NOTE_TYPE,
        "kind": NOTE_TYPE,
        "position": {"x": x, "y": y},
        "geometry": {
            "x": x,
            "y": y,
            "w": size["w"],
            "h": size["h"],
            "rotation": rotation if rotation is not None else 0,
        },
        "size": size,
        "text": text or "",
        "z": z if z is not None else 0,
        "locked": bool(locked),
    }
    if annotation_id is not None:
        annotation["id"] = annotation_id
    if color is not None:
        annotation["color"] = color
        annotation["style"] = {"color": color}
    if font_size is not None:
        annotation["fontSize"] = font_size
    return annotation


def build_note_patch(
    existing: Dict[str, Any],
    *,
    text: Optional[str] = None,
    color: Optional[str] = None,
    font_size: Optional[float] = None,
    x: Optional[float] = None,
    y: Optional[float] = None,
    w: Optional[float] = None,
    h: Optional[float] = None,
    rotation: Optional[float] = None,
    z: Optional[float] = None,
    locked: Optional[bool] = None,
) -> Dict[str, Any]:
    """Build a partial ``annotation_updated`` patch for an existing note.

    ``SessionStore.apply_state_op`` merges a patch onto the stored annotation
    with a shallow ``dict.update`` — a key that is *present* in the patch wholly
    replaces the stored value, it does not deep-merge. So a position-only move
    still has to carry the note's current w/h inside ``geometry`` (and a
    size-only resize its current x/y), or the untouched half would be dropped
    rather than preserved. Only fields present here as non-``None`` arguments
    are touched; the rest keep the value already in ``existing``.

    ``rotation``/``z``/``locked`` follow ``build_annotation_patch``'s
    convention for the same fields: ``rotation`` is folded into ``geometry``
    alongside any position/size change (so it survives the same shallow
    merge), ``z``/``locked`` are set directly on the patch when given.
    """
    patch: Dict[str, Any] = {
        "id": existing["id"],
        "type": NOTE_TYPE,
        "kind": NOTE_TYPE,
    }
    if text is not None:
        patch["text"] = text
    if color is not None:
        patch["color"] = color
        patch["style"] = {**(existing.get("style") or {}), "color": color}
    if font_size is not None:
        patch["fontSize"] = font_size

    geometry = dict(existing.get("geometry") or {})
    size = dict(existing.get("size") or DEFAULT_NOTE_SIZE)
    position = dict(
        existing.get("position")
        or {"x": geometry.get("x", 0), "y": geometry.get("y", 0)}
    )

    moved = x is not None or y is not None
    resized = w is not None or h is not None
    rotated = rotation is not None
    if moved:
        position["x"] = x if x is not None else position.get("x", 0)
        position["y"] = y if y is not None else position.get("y", 0)
        geometry["x"] = position["x"]
        geometry["y"] = position["y"]
        patch["position"] = position
    if resized:
        size["w"] = w if w is not None else size.get("w", DEFAULT_NOTE_SIZE["w"])
        size["h"] = h if h is not None else size.get("h", DEFAULT_NOTE_SIZE["h"])
        geometry["w"] = size["w"]
        geometry["h"] = size["h"]
        patch["size"] = size
    if rotated:
        geometry["rotation"] = rotation
    if moved or resized or rotated:
        patch["geometry"] = geometry
    if z is not None:
        patch["z"] = z
    if locked is not None:
        patch["locked"] = bool(locked)
    return patch


def project_note(annotation: Dict[str, Any]) -> Dict[str, Any]:
    """Project a stored note annotation into the MCP-facing read shape."""
    geometry = annotation.get("geometry") or {}
    position = annotation.get("position") or {
        "x": geometry.get("x", 0),
        "y": geometry.get("y", 0),
    }
    size = annotation.get("size") or {
        "w": geometry.get("w", DEFAULT_NOTE_SIZE["w"]),
        "h": geometry.get("h", DEFAULT_NOTE_SIZE["h"]),
    }
    return {
        "id": annotation.get("id"),
        "text": annotation.get("text") or "",
        "x": position.get("x", 0),
        "y": position.get("y", 0),
        "w": size.get("w", DEFAULT_NOTE_SIZE["w"]),
        "h": size.get("h", DEFAULT_NOTE_SIZE["h"]),
        "color": annotation.get("color"),
        "font_size": annotation.get("fontSize"),
        "rotation": geometry.get("rotation", 0),
        "z": annotation.get("z", 0),
        "locked": bool(annotation.get("locked", False)),
        "created_at": annotation.get("created_at"),
        "updated_at": annotation.get("updated_at"),
        "created_by": annotation.get("created_by"),
        "updated_by": annotation.get("updated_by"),
    }


# ==================== Group (node-membership box) helpers ====================


def is_group(annotation: Dict[str, Any]) -> bool:
    """Whether *annotation* is a v1 ``group`` annotation (checks type or its
    ``kind`` compatibility alias, matching how the store itself resolves type).
    """
    ann_type = annotation.get("type") or annotation.get("kind")
    return ann_type == GROUP_TYPE


def build_group_annotation(
    *,
    x: float,
    y: float,
    w: Optional[float] = None,
    h: Optional[float] = None,
    label: str = "",
    description: str = "",
    color: Optional[str] = None,
    member_node_ids: Optional[List[str]] = None,
    z: Optional[float] = None,
    locked: bool = False,
    annotation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a v1 ``group`` annotation dict for the ``annotation_created`` op.

    Mirrors ``build_note_annotation``'s shape and upsert behaviour (passing
    an existing ``annotation_id`` replaces that group's fields), with one
    deliberate difference: ``member_node_ids`` is included in the built dict
    only when the caller passes it explicitly, instead of always defaulting
    to ``[]`` the way ``build_note_annotation`` always sets ``text``.
    ``SessionStore`` applies an upsert with a shallow ``dict.update``, so a
    key this function omits is left untouched on the stored annotation
    rather than reset. Membership is meant to be managed through
    ``update_group_members`` (the ``group_membership_changed`` op) once a
    group exists; if re-creating a group by id to change its label or color
    also silently wiped out membership set through that other tool whenever
    the caller did not resend the current list, the two tools would fight
    each other. A brand-new group with no ``member_node_ids`` given is
    simply created empty — the canvas and ``project_annotation`` both treat
    an absent list the same as an empty one.

    ``ValueError`` is raised for a non-list-of-strings ``member_node_ids``,
    matching how the generic builders report a malformed payload as
    ``invalid_content`` at the MCP tool layer.
    """
    if member_node_ids is not None and (
        not isinstance(member_node_ids, list)
        or not all(isinstance(m, str) for m in member_node_ids)
    ):
        raise ValueError("member_node_ids must be a list of strings")
    size = {
        "w": w if w is not None else DEFAULT_GROUP_SIZE["w"],
        "h": h if h is not None else DEFAULT_GROUP_SIZE["h"],
    }
    annotation: Dict[str, Any] = {
        "type": GROUP_TYPE,
        "kind": GROUP_TYPE,
        "position": {"x": x, "y": y},
        "geometry": {"x": x, "y": y, "w": size["w"], "h": size["h"], "rotation": 0},
        "size": size,
        "label": label or "",
        "description": description or "",
        "z": z if z is not None else 0,
        "locked": bool(locked),
    }
    if annotation_id is not None:
        annotation["id"] = annotation_id
    if color is not None:
        annotation["color"] = color
        annotation["style"] = {"color": color}
    if member_node_ids is not None:
        annotation["member_node_ids"] = list(member_node_ids)
    return annotation


# ==================== Generic (non-note, non-group) type helpers ====================


def resolve_annotation_type_alias(raw_type: Any) -> Optional[str]:
    """Resolve the legacy ``arrow`` alias to its canonical type, if applicable.

    Returns ``None`` for anything that is not a string, leaving membership
    checks to the caller.
    """
    if not isinstance(raw_type, str):
        return None
    return LEGACY_ANNOTATION_ALIASES.get(raw_type, raw_type)


def normalize_generic_type(raw_type: Any) -> Optional[str]:
    """Resolve *raw_type* and return it only if it is one of the v1 types the
    generic annotation tool set manages (excludes ``note`` and ``group``,
    which are out of scope here — see module docstring). ``None`` otherwise.
    """
    resolved = resolve_annotation_type_alias(raw_type)
    return resolved if resolved in GENERIC_ANNOTATION_TYPES else None


def annotation_type_of(annotation: Dict[str, Any]) -> Optional[str]:
    """The canonical type of a stored annotation dict (``type`` or its
    ``kind`` fallback, with the legacy ``arrow`` alias resolved).
    """
    raw = annotation.get("type") or annotation.get("kind")
    return resolve_annotation_type_alias(raw)


def is_generic_annotation(annotation: Dict[str, Any]) -> bool:
    """Whether *annotation* is one of the types the generic tool set manages
    (i.e. not a ``note`` and not a ``group``)."""
    return annotation_type_of(annotation) in GENERIC_ANNOTATION_TYPES


def is_embedded_image_url(url: Any) -> bool:
    """Whether *url* is an embedded image data URI ingest is allowed to store."""
    return isinstance(url, str) and url.startswith(EMBEDDED_IMAGE_URL_PREFIXES)


def image_annotation_error(
    annotation: Dict[str, Any], existing: Optional[Dict[str, Any]] = None
) -> Optional[str]:
    """Why *annotation* may not be persisted as an ``image``, or ``None``.

    Enforces docs/ANNOTATION_CONTRACT.md's "Image ingest enforcement" rule
    for the writes ``SessionStore.apply_state_op`` submits to it:
    an ``image`` annotation's pixel content must be the embedded result of
    server-side ingest (``image_ingest.py``), never a remote URL that the
    annotation would then depend on staying reachable — and never a
    ``file:``/``javascript:`` style URL either. Without this, the generic
    ``create_annotation``/``update_annotation`` tools and any client posting a
    raw ``annotation_created`` op could store an arbitrary unvalidated remote
    URL, going around the validation, budgets and SSRF checks
    ``create_image_annotation`` performs.

    *existing* is the annotation already stored under this id, when there is
    one. A write whose ``image.url`` is byte-identical to the stored one is
    allowed even if that URL is not embedded: it introduces no new reference,
    and refusing it would strand annotations persisted before this rule
    existed — the browser echoes the *whole* annotation, image payload
    included, on every move/resize/lock (``sessionSyncClient.js``), so a
    blanket refusal would make such an annotation permanently unmovable. Only
    a *new* non-embedded URL is refused — and a duplicate, which lands on a
    fresh id with no *existing* to match, counts as new.

    The second exemption is not here at all: ``apply_state_op`` skips this
    check entirely for an undo replaying its stored inverse op
    (``trusted_replay``), which restores a copy of the session's own earlier
    state rather than accepting caller input. Without it, deleting an
    annotation persisted before this rule existed would be irreversible,
    since after the delete there is no *existing* left to match against.

    An annotation of another type, or an ``image`` patch that omits the pixel
    payload entirely, is unaffected.
    """
    if annotation_type_of(annotation) != IMAGE_TYPE:
        return None
    if "image" not in annotation:
        return None
    image = annotation.get("image")
    if image is None:
        return None
    if not isinstance(image, dict):
        return "image annotation 'image' payload must be an object"
    url = image.get("url")
    if url is None:
        # No pixel content to validate. A browser echoing an annotation back
        # on a move serialises `image` as `{}` when the payload is missing
        # (`sessionAnnotations.js`), and rejecting that would wedge the whole
        # op batch — including every unrelated op in it — over an annotation
        # that references nothing.
        return None
    if is_embedded_image_url(url):
        return None
    if isinstance(existing, dict):
        stored = existing.get("image")
        if isinstance(stored, dict) and stored.get("url") == url:
            return None
    return (
        "image annotation content must be an embedded image produced by "
        "server-side ingest (a data:image/webp;base64 URI); a remote or "
        "unvalidated URL is not accepted — create or replace the image with "
        "the image ingest path instead"
    )


def iter_saved_view_annotations(metadata: Dict[str, Any]):
    """Yield every annotation-shaped dict embedded in SavedView/VisualizationView
    node metadata: the v1 annotation document's ``annotations`` list and the
    legacy ``annotations`` list the frontend keeps in sync alongside it
    (``frontend/web/src/utils/sessionAnnotations.js``, design 3.1).

    A SavedView node's annotation content is ordinary node metadata, written
    through the generic ``add_nodes``/``update_node`` tools rather than
    through ``SessionStore.apply_state_op`` — so unlike a live session op it
    is not opaque to the caller here, but it uses the identical v1 annotation
    shape and must be checked against the identical rule
    (``saved_view_annotation_error`` below).
    """
    if not isinstance(metadata, dict):
        return
    document = metadata.get("annotation_document")
    if isinstance(document, dict):
        for annotation in document.get("annotations", []) or []:
            if isinstance(annotation, dict):
                yield annotation
    legacy = metadata.get("annotations")
    if isinstance(legacy, list):
        for annotation in legacy:
            if isinstance(annotation, dict):
                yield annotation


def saved_view_annotation_error(metadata: Dict[str, Any]) -> Optional[str]:
    """Why *metadata* may not be persisted on a SavedView/VisualizationView
    node, or ``None`` if every embedded annotation is fine.

    Applies ``image_annotation_error`` — the same rule
    ``SessionStore.apply_state_op`` enforces for live annotation ops — to
    every annotation reachable from saved-view metadata (see
    ``iter_saved_view_annotations``). Unlike a live op, a saved-view write is
    not an incremental patch onto previously-validated state, so there is no
    legitimate *existing* annotation to exempt a re-sent URL against here:
    every image annotation must already be an embedded data URI, with no
    byte-identical-URL exemption. Callers gate this call to nodes of the
    right type themselves — this module has no notion of node types.
    """
    for annotation in iter_saved_view_annotations(metadata):
        error = image_annotation_error(annotation)
        if error:
            return error
    return None


def _sanitize_saved_view_annotation(annotation: Dict[str, Any]) -> Dict[str, Any]:
    if annotation_type_of(annotation) != IMAGE_TYPE:
        return annotation
    image = annotation.get("image")
    if not isinstance(image, dict):
        return annotation
    url = image.get("url")
    if url is None or is_embedded_image_url(url):
        return annotation
    sanitized_image = dict(image)
    sanitized_image.pop("url", None)
    return {**annotation, "image": sanitized_image}


def sanitize_saved_view_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of SavedView/VisualizationView *metadata* with every
    non-embedded image annotation URL stripped.

    Defense in depth alongside ``saved_view_annotation_error``: a view saved
    before that check existed (or whose metadata reached storage by some
    other path) must still not make a viewer fetch a remote host merely by
    opening it — ``GenericAnnotationNode`` renders ``content.image.url``
    straight into an ``<img src>``. Never mutates *metadata* itself (callers
    read this directly off live ``Node.metadata``); returns a shallow copy
    with only the ``annotation_document``/``annotations`` keys replaced when
    something was actually stripped, leaving every other field — and every
    non-image annotation — untouched (and byte-for-byte identical, so callers
    that need to detect "did this change" can compare by equality).
    """
    if not isinstance(metadata, dict):
        return metadata
    sanitized = dict(metadata)
    document = metadata.get("annotation_document")
    if isinstance(document, dict) and isinstance(document.get("annotations"), list):
        new_annotations = [
            _sanitize_saved_view_annotation(a) if isinstance(a, dict) else a
            for a in document["annotations"]
        ]
        if new_annotations != document["annotations"]:
            sanitized["annotation_document"] = {
                **document,
                "annotations": new_annotations,
            }
    legacy = metadata.get("annotations")
    if isinstance(legacy, list):
        new_legacy = [
            _sanitize_saved_view_annotation(a) if isinstance(a, dict) else a
            for a in legacy
        ]
        if new_legacy != legacy:
            sanitized["annotations"] = new_legacy
    return sanitized


def _apply_content(
    target: Dict[str, Any],
    content: Optional[Dict[str, Any]],
    *,
    ann_type: Optional[str] = None,
) -> None:
    if not content:
        return
    reserved = _RESERVED_ANNOTATION_KEYS & content.keys()
    if reserved:
        raise ValueError(
            f"content must not set reserved field(s) {sorted(reserved)}; "
            "those are managed by their own arguments"
        )
    content_error = _validate_generic_content(ann_type, content)
    if content_error:
        raise ValueError(content_error)
    target.update(content)


def build_annotation(
    *,
    type: str,
    x: float,
    y: float,
    w: Optional[float] = None,
    h: Optional[float] = None,
    rotation: Optional[float] = None,
    content: Optional[Dict[str, Any]] = None,
    style: Optional[Dict[str, Any]] = None,
    z: Optional[float] = None,
    locked: bool = False,
    annotation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a v1 annotation dict of *type* for the ``annotation_created`` op.

    Builds the common envelope (``geometry``/``position``/``style``/``z``/
    ``locked``) shared by every v1 type, mirroring ``createAnnotation()``.
    Unlike ``build_note_annotation`` this does not model each type's payload
    shape — that differs too much across line/label/shape/frame/icon/
    vote_dot/image/freehand for one generic builder to hand-build — so *content*
    carries it verbatim and is merged onto the annotation as-is. The
    frontend's ``createAnnotation()`` re-normalizes defensively on load
    either way, so a caller-supplied payload that is merely incomplete (e.g.
    a ``line`` missing ``to``) does not corrupt the document.

    *type* must already be resolved to one of ``GENERIC_ANNOTATION_TYPES``
    (see ``normalize_generic_type``); this function does not itself validate
    it, matching ``build_note_annotation``'s division of labor with its
    callers.
    """
    geometry = {
        "x": x,
        "y": y,
        "w": w if w is not None else 0,
        "h": h if h is not None else 0,
        "rotation": rotation if rotation is not None else 0,
    }
    annotation: Dict[str, Any] = {
        "type": type,
        "kind": type,
        "position": {"x": x, "y": y},
        "geometry": geometry,
        "z": z if z is not None else 0,
        "locked": bool(locked),
    }
    if w is not None or h is not None:
        annotation["size"] = {"w": geometry["w"], "h": geometry["h"]}
    if style is not None:
        annotation["style"] = dict(style)
    _apply_content(annotation, content, ann_type=type)
    if annotation_id is not None:
        annotation["id"] = annotation_id
    return annotation


def translate_line_endpoints(
    existing: Dict[str, Any], dx: float, dy: float
) -> Dict[str, Any]:
    """Translate a line annotation's explicit endpoint coordinates by (dx, dy).

    A line's shape lives in its ``from``/``to`` content fields, outside the
    common ``geometry``/``position`` envelope those fields shadow (the
    envelope's x/y only tracks the anchor, `from` in practice). Moving or
    duplicating a line must translate both ends by the same delta or the
    line stretches/reshapes instead of sliding. Returns the fields to merge
    onto the target patch/copy; empty for annotations without explicit
    endpoint coordinates (every non-``line`` type).
    """
    translated: Dict[str, Any] = {}
    for key in ("from", "to"):
        point = existing.get(key)
        if (
            isinstance(point, dict)
            and isinstance(point.get("x"), (int, float))
            and isinstance(point.get("y"), (int, float))
        ):
            translated[key] = {**point, "x": point["x"] + dx, "y": point["y"] + dy}
    return translated


def translate_freehand_points(
    existing: Dict[str, Any], dx: float, dy: float
) -> Dict[str, Any]:
    """Translate a freehand annotation's sampled points by (dx, dy).

    A freehand stroke's shape lives in its ``points`` content field as
    absolute model-space coordinates, outside the common ``geometry``/
    ``position`` envelope — same reason as ``translate_line_endpoints``:
    moving the annotation must slide every sampled point by the same delta,
    or the stroke reshapes instead of sliding. Returns the fields to merge
    onto the target patch/copy; empty for annotations without a ``points``
    list (every non-``freehand`` type).
    """
    points = existing.get("points")
    if not isinstance(points, list) or not points:
        return {}
    translated = []
    changed = False
    for point in points:
        if (
            isinstance(point, dict)
            and isinstance(point.get("x"), (int, float))
            and isinstance(point.get("y"), (int, float))
        ):
            translated.append({**point, "x": point["x"] + dx, "y": point["y"] + dy})
            changed = True
        else:
            translated.append(point)
    return {"points": translated} if changed else {}


def build_annotation_patch(
    existing: Dict[str, Any],
    *,
    x: Optional[float] = None,
    y: Optional[float] = None,
    w: Optional[float] = None,
    h: Optional[float] = None,
    rotation: Optional[float] = None,
    content: Optional[Dict[str, Any]] = None,
    style: Optional[Dict[str, Any]] = None,
    z: Optional[float] = None,
    locked: Optional[bool] = None,
) -> Dict[str, Any]:
    """Build a partial ``annotation_updated`` patch for an existing annotation.

    Same shallow-merge caveat as ``build_note_patch``: only fields present
    here as non-``None`` arguments are touched; a position-only move still
    carries the existing w/h inside ``geometry`` (and vice versa) so the
    untouched half is not dropped by the store's ``dict.update`` merge.
    """
    ann_type = annotation_type_of(existing) or existing.get("type")
    patch: Dict[str, Any] = {"id": existing["id"], "type": ann_type, "kind": ann_type}

    geometry = dict(existing.get("geometry") or {})
    size = dict(
        existing.get("size") or {"w": geometry.get("w", 0), "h": geometry.get("h", 0)}
    )
    position = dict(
        existing.get("position")
        or {"x": geometry.get("x", 0), "y": geometry.get("y", 0)}
    )

    moved = x is not None or y is not None
    resized = w is not None or h is not None
    rotated = rotation is not None
    if moved:
        original_x = position.get("x", 0)
        original_y = position.get("y", 0)
        position["x"] = x if x is not None else original_x
        position["y"] = y if y is not None else original_y
        geometry["x"] = position["x"]
        geometry["y"] = position["y"]
        patch["position"] = position
        dx = position["x"] - original_x
        dy = position["y"] - original_y
        if dx or dy:
            patch.update(translate_line_endpoints(existing, dx, dy))
            patch.update(translate_freehand_points(existing, dx, dy))
    if resized:
        size["w"] = w if w is not None else size.get("w", 0)
        size["h"] = h if h is not None else size.get("h", 0)
        geometry["w"] = size["w"]
        geometry["h"] = size["h"]
        patch["size"] = size
    if rotated:
        geometry["rotation"] = rotation
    if moved or resized or rotated:
        patch["geometry"] = geometry
    if style is not None:
        patch["style"] = dict(style)
    if z is not None:
        patch["z"] = z
    if locked is not None:
        patch["locked"] = bool(locked)
    _apply_content(patch, content, ann_type=ann_type)
    return patch


def project_annotation(annotation: Dict[str, Any]) -> Dict[str, Any]:
    """Project a stored annotation of any type into the MCP-facing read shape.

    ``content`` holds every field outside the common envelope — the
    type-specific payload (a line's ``from``/``to``, a label's ``text``,
    ...) — verbatim, mirroring how ``build_annotation``'s *content* argument
    writes it.
    """
    geometry = annotation.get("geometry") or {}
    position = annotation.get("position") or {
        "x": geometry.get("x", 0),
        "y": geometry.get("y", 0),
    }
    size = annotation.get("size") or {}
    content = {
        key: value
        for key, value in annotation.items()
        if key not in _RESERVED_ANNOTATION_KEYS
    }
    return {
        "id": annotation.get("id"),
        "type": annotation_type_of(annotation),
        "x": position.get("x", 0),
        "y": position.get("y", 0),
        "w": geometry.get("w", size.get("w", 0)),
        "h": geometry.get("h", size.get("h", 0)),
        "rotation": geometry.get("rotation", 0),
        "style": annotation.get("style") or {},
        "z": annotation.get("z", 0),
        "locked": bool(annotation.get("locked", False)),
        "content": content,
        "created_at": annotation.get("created_at"),
        "updated_at": annotation.get("updated_at"),
        "created_by": annotation.get("created_by"),
        "updated_by": annotation.get("updated_by"),
    }
