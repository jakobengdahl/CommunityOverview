"""Sticky-note-specific helpers over the generic session annotation store.

``session_store``/``session_manager`` treat an annotation as an opaque dict —
they validate only the boundary fields (``type``/``kind``, ``id``, ``position``)
and apply the ``annotation_created``/``annotation_updated``/``annotation_deleted``
ops without knowing what a "note" is. The v1 annotation shape itself (geometry/
position/size projections, note payload fields) is defined by
``packages/ui-graph-canvas/src/utils/annotationModel.js`` and consumed as-is by
the canvas (``packages/ui-graph-canvas/src/utils/annotations.js`` reads a note's
``position``, ``size``, ``text``, ``color``, ``fontSize`` directly). This module
builds and reads that same shape from Python, once, so the MCP sticky-note tools
do not each hand-rolled it.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

NOTE_TYPE = "note"
DEFAULT_NOTE_SIZE = {"w": 160, "h": 96}


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
