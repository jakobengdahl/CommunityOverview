"""Annotation-shape helpers over the generic session annotation store.

``session_store``/``session_manager`` treat an annotation as an opaque dict —
they validate only the boundary fields (``type``/``kind``, ``id``, ``position``)
and apply the ``annotation_created``/``annotation_updated``/``annotation_deleted``
ops without knowing what a "note" or a "line" is. The v1 annotation shape
itself (geometry/position/size projections, per-type payload fields) is
defined by ``packages/ui-graph-canvas/src/utils/annotationModel.js`` and
consumed as-is by the canvas. This module builds and reads that same shape
from Python, once, so MCP tools do not each hand-roll it.

Two helper sets live here:

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
  boxes, out of scope — editing ``member_node_ids`` goes through the
  ``group_membership_changed`` op, not exposed over MCP).
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, Optional

NOTE_TYPE = "note"
GROUP_TYPE = "group"
DEFAULT_NOTE_SIZE = {"w": 160, "h": 96}

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
    annotation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a v1 ``note`` annotation dict for the ``annotation_created`` op.

    Mirrors ``createAnnotation({type: 'note', ...})``: geometry, position and
    size all carry the same x/y/w/h so any code reading either projection
    stays consistent. ``annotation_id`` is left out when not given, so the
    store assigns one (``SessionStore.apply_state_op`` mints a
    ``secrets.token_hex(8)`` id for a create with no id) — the caller reads the
    assigned id off the op result instead of inventing one.
    """
    size = {
        "w": w if w is not None else DEFAULT_NOTE_SIZE["w"],
        "h": h if h is not None else DEFAULT_NOTE_SIZE["h"],
    }
    annotation: Dict[str, Any] = {
        "type": NOTE_TYPE,
        "kind": NOTE_TYPE,
        "position": {"x": x, "y": y},
        "geometry": {"x": x, "y": y, "w": size["w"], "h": size["h"], "rotation": 0},
        "size": size,
        "text": text or "",
        "z": 0,
        "locked": False,
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
) -> Dict[str, Any]:
    """Build a partial ``annotation_updated`` patch for an existing note.

    ``SessionStore.apply_state_op`` merges a patch onto the stored annotation
    with a shallow ``dict.update`` — a key that is *present* in the patch wholly
    replaces the stored value, it does not deep-merge. So a position-only move
    still has to carry the note's current w/h inside ``geometry`` (and a
    size-only resize its current x/y), or the untouched half would be dropped
    rather than preserved. Only fields present here as non-``None`` arguments
    are touched; the rest keep the value already in ``existing``.
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
    if moved or resized:
        patch["geometry"] = geometry
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
        "z": annotation.get("z", 0),
        "locked": bool(annotation.get("locked", False)),
        "created_at": annotation.get("created_at"),
        "updated_at": annotation.get("updated_at"),
        "created_by": annotation.get("created_by"),
        "updated_by": annotation.get("updated_by"),
    }


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


def _apply_content(target: Dict[str, Any], content: Optional[Dict[str, Any]]) -> None:
    if not content:
        return
    reserved = _RESERVED_ANNOTATION_KEYS & content.keys()
    if reserved:
        raise ValueError(
            f"content must not set reserved field(s) {sorted(reserved)}; "
            "those are managed by their own arguments"
        )
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
    _apply_content(annotation, content)
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
    _apply_content(patch, content)
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
