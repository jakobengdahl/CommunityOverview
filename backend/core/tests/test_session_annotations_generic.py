"""Tests for the generic (non-note, non-group) annotation builder/patch/
projection helpers in ``backend/core/session_annotations.py``.

Pure functions over plain dicts (no session/store dependency), mirroring
``test_session_annotations.py``'s coverage of the note-specific siblings.
"""

import pytest

from backend.core.session_annotations import (
    ALL_ANNOTATION_TYPES,
    GENERIC_ANNOTATION_TYPES,
    annotation_type_of,
    build_annotation,
    build_annotation_patch,
    is_generic_annotation,
    normalize_generic_type,
    project_annotation,
    resolve_annotation_type_alias,
)


class TestTypeResolution:
    def test_generic_types_exclude_note_and_group(self):
        assert "note" not in GENERIC_ANNOTATION_TYPES
        assert "group" not in GENERIC_ANNOTATION_TYPES
        assert {
            "text",
            "label",
            "line",
            "frame",
            "shape",
            "icon",
            "vote_dot",
            "image",
            "freehand",
        } == set(GENERIC_ANNOTATION_TYPES)

    def test_all_types_includes_note_and_group(self):
        assert GENERIC_ANNOTATION_TYPES <= ALL_ANNOTATION_TYPES
        assert "note" in ALL_ANNOTATION_TYPES
        assert "group" in ALL_ANNOTATION_TYPES

    def test_resolve_alias(self):
        assert resolve_annotation_type_alias("arrow") == "line"
        assert resolve_annotation_type_alias("label") == "label"
        assert resolve_annotation_type_alias(None) is None
        assert resolve_annotation_type_alias(123) is None

    def test_normalize_generic_type_accepts_alias(self):
        assert normalize_generic_type("arrow") == "line"
        assert normalize_generic_type("line") == "line"
        assert normalize_generic_type("shape") == "shape"

    def test_normalize_generic_type_rejects_note_and_group(self):
        assert normalize_generic_type("note") is None
        assert normalize_generic_type("group") is None

    def test_normalize_generic_type_rejects_unknown(self):
        assert normalize_generic_type("banana") is None

    def test_annotation_type_of_falls_back_to_kind_and_resolves_alias(self):
        assert annotation_type_of({"type": "line"}) == "line"
        assert annotation_type_of({"kind": "arrow"}) == "line"
        assert annotation_type_of({}) is None

    def test_is_generic_annotation(self):
        assert is_generic_annotation({"type": "line"}) is True
        assert is_generic_annotation({"type": "note"}) is False
        assert is_generic_annotation({"type": "group"}) is False


class TestBuildAnnotation:
    def test_builds_common_envelope(self):
        annotation = build_annotation(type="label", x=10, y=20, content={"text": "hi"})
        assert annotation["type"] == "label"
        assert annotation["kind"] == "label"
        assert annotation["position"] == {"x": 10, "y": 20}
        assert annotation["geometry"] == {
            "x": 10,
            "y": 20,
            "w": 0,
            "h": 0,
            "rotation": 0,
        }
        assert annotation["z"] == 0
        assert annotation["locked"] is False
        assert annotation["text"] == "hi"
        assert "id" not in annotation

    def test_w_h_populate_geometry_and_size(self):
        annotation = build_annotation(type="frame", x=0, y=0, w=200, h=100)
        assert annotation["geometry"]["w"] == 200
        assert annotation["geometry"]["h"] == 100
        assert annotation["size"] == {"w": 200, "h": 100}

    def test_no_w_h_omits_size_key(self):
        annotation = build_annotation(type="line", x=0, y=0)
        assert "size" not in annotation
        assert annotation["geometry"]["w"] == 0

    def test_rotation(self):
        annotation = build_annotation(type="shape", x=0, y=0, rotation=45)
        assert annotation["geometry"]["rotation"] == 45

    def test_explicit_id(self):
        annotation = build_annotation(type="icon", x=0, y=0, annotation_id="icon-1")
        assert annotation["id"] == "icon-1"

    def test_style_and_z_and_locked(self):
        annotation = build_annotation(
            type="shape", x=0, y=0, style={"fill": "#fff"}, z=5, locked=True
        )
        assert annotation["style"] == {"fill": "#fff"}
        assert annotation["z"] == 5
        assert annotation["locked"] is True

    def test_content_is_merged_verbatim(self):
        annotation = build_annotation(
            type="line",
            x=0,
            y=0,
            content={"to": {"x": 100, "y": 0}, "endArrow": True},
        )
        assert annotation["to"] == {"x": 100, "y": 0}
        assert annotation["endArrow"] is True

    def test_content_cannot_override_reserved_fields(self):
        with pytest.raises(ValueError):
            build_annotation(type="label", x=0, y=0, content={"type": "note"})

    def test_content_cannot_smuggle_id(self):
        with pytest.raises(ValueError):
            build_annotation(type="label", x=0, y=0, content={"id": "hijack"})


class TestBuildAnnotationPatch:
    def _existing(self, **overrides):
        base = {
            "id": "line-1",
            "type": "line",
            "kind": "line",
            "position": {"x": 5, "y": 6},
            "geometry": {"x": 5, "y": 6, "w": 100, "h": 0, "rotation": 0},
            "size": {"w": 100, "h": 0},
            "to": {"x": 105, "y": 6},
            "endArrow": True,
            "style": {"stroke": "#000"},
            "z": 0,
            "locked": False,
        }
        base.update(overrides)
        return base

    def test_no_fields_given_yields_minimal_patch(self):
        patch = build_annotation_patch(self._existing())
        assert patch == {"id": "line-1", "type": "line", "kind": "line"}

    def test_position_patch_preserves_size_in_geometry(self):
        patch = build_annotation_patch(self._existing(), x=50, y=60)
        assert patch["position"] == {"x": 50, "y": 60}
        assert patch["geometry"] == {"x": 50, "y": 60, "w": 100, "h": 0, "rotation": 0}
        assert "size" not in patch

    def test_size_patch_preserves_position_in_geometry(self):
        patch = build_annotation_patch(self._existing(), w=300, h=150)
        assert patch["size"] == {"w": 300, "h": 150}
        assert patch["geometry"] == {"x": 5, "y": 6, "w": 300, "h": 150, "rotation": 0}
        assert "position" not in patch

    def test_rotation_only_patch(self):
        patch = build_annotation_patch(self._existing(), rotation=90)
        assert patch["geometry"]["rotation"] == 90
        assert patch["geometry"]["x"] == 5 and patch["geometry"]["y"] == 6

    def test_content_patch_is_merged_verbatim(self):
        patch = build_annotation_patch(
            self._existing(), content={"to": {"x": 999, "y": 6}}
        )
        assert patch["to"] == {"x": 999, "y": 6}
        assert "endArrow" not in patch  # untouched fields are not repeated

    def test_style_patch_replaces_whole_style_dict(self):
        patch = build_annotation_patch(self._existing(), style={"stroke": "#fff"})
        assert patch["style"] == {"stroke": "#fff"}

    def test_z_and_locked_patch(self):
        patch = build_annotation_patch(self._existing(), z=7, locked=True)
        assert patch["z"] == 7
        assert patch["locked"] is True

    def test_z_zero_and_locked_false_are_still_applied(self):
        """``0``/``False`` are valid new values, not "unset" sentinels."""
        patch = build_annotation_patch(
            self._existing(z=9, locked=True), z=0, locked=False
        )
        assert patch["z"] == 0
        assert patch["locked"] is False

    def test_content_cannot_override_reserved_fields(self):
        with pytest.raises(ValueError):
            build_annotation_patch(self._existing(), content={"geometry": {}})

    def test_preserves_type_via_kind_fallback(self):
        existing = self._existing()
        del existing["type"]
        existing["kind"] = "arrow"  # legacy stored alias
        patch = build_annotation_patch(existing, x=1)
        assert patch["type"] == "line"
        assert patch["kind"] == "line"

    def test_position_move_translates_line_endpoint(self):
        """Moving a line must translate its `to` endpoint by the same delta,
        preserving line shape instead of leaving `to` behind (stretching it)."""
        existing = self._existing()  # position (5, 6), to (105, 6)
        patch = build_annotation_patch(existing, x=55, y=16)
        assert patch["to"] == {"x": 155, "y": 16}

    def test_position_move_translates_explicit_from_endpoint(self):
        existing = self._existing(**{"from": {"x": 5, "y": 6}})
        patch = build_annotation_patch(existing, x=55, y=16)
        assert patch["from"] == {"x": 55, "y": 16}
        assert patch["to"] == {"x": 155, "y": 16}

    def test_resize_only_does_not_translate_endpoints(self):
        """Resizing (no x/y move) must not reshape the line."""
        existing = self._existing()
        patch = build_annotation_patch(existing, w=300)
        assert "to" not in patch

    def test_explicit_content_endpoint_overrides_translation(self):
        """An explicit content['to'] is the caller's intent and must win over
        the implicit translation computed from the position move."""
        existing = self._existing()
        patch = build_annotation_patch(
            existing, x=55, y=16, content={"to": {"x": 999, "y": 999}}
        )
        assert patch["to"] == {"x": 999, "y": 999}

    def test_position_move_on_non_line_type_does_not_add_endpoint_fields(self):
        existing = {
            "id": "shape-1",
            "type": "shape",
            "kind": "shape",
            "position": {"x": 0, "y": 0},
            "geometry": {"x": 0, "y": 0, "w": 10, "h": 10, "rotation": 0},
            "size": {"w": 10, "h": 10},
            "shape": "rectangle",
        }
        patch = build_annotation_patch(existing, x=10, y=10)
        assert "to" not in patch
        assert "from" not in patch


class TestProjectAnnotation:
    def test_projects_read_shape_with_content(self):
        annotation = {
            "id": "line-1",
            "type": "line",
            "position": {"x": 1, "y": 2},
            "geometry": {"x": 1, "y": 2, "w": 10, "h": 0, "rotation": 0},
            "to": {"x": 11, "y": 2},
            "endArrow": True,
            "style": {"stroke": "#000"},
            "z": 3,
            "locked": True,
            "created_at": "t1",
            "updated_at": "t2",
            "created_by": "mcp-agent",
        }
        projected = project_annotation(annotation)
        assert projected["id"] == "line-1"
        assert projected["type"] == "line"
        assert projected["x"] == 1 and projected["y"] == 2
        assert projected["w"] == 10 and projected["h"] == 0
        assert projected["style"] == {"stroke": "#000"}
        assert projected["z"] == 3
        assert projected["locked"] is True
        assert projected["content"] == {"to": {"x": 11, "y": 2}, "endArrow": True}
        assert projected["created_at"] == "t1"
        assert projected["updated_by"] is None

    def test_resolves_legacy_arrow_alias_in_type(self):
        annotation = {"id": "a1", "kind": "arrow", "geometry": {"x": 0, "y": 0}}
        projected = project_annotation(annotation)
        assert projected["type"] == "line"

    def test_missing_geometry_falls_back_to_defaults(self):
        projected = project_annotation({"id": "s1", "type": "shape"})
        assert projected["x"] == 0 and projected["y"] == 0
        assert projected["w"] == 0 and projected["h"] == 0
        assert projected["locked"] is False
        assert projected["content"] == {}
