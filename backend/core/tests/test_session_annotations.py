"""Tests for the sticky-note builder/patch/projection helpers.

These are pure functions over plain dicts (no session/store dependency), so
they are tested in isolation from the op-application and MCP-tool layers that
consume them.
"""

from backend.core.session_annotations import (
    DEFAULT_NOTE_SIZE,
    build_note_annotation,
    build_note_patch,
    is_note,
    project_note,
)


class TestIsNote:
    def test_type_field(self):
        assert is_note({"type": "note"}) is True
        assert is_note({"type": "line"}) is False

    def test_falls_back_to_kind_alias(self):
        assert is_note({"kind": "note"}) is True

    def test_missing_type_and_kind(self):
        assert is_note({}) is False


class TestBuildNoteAnnotation:
    def test_defaults_size_and_text(self):
        annotation = build_note_annotation(x=10, y=20)
        assert annotation["type"] == "note"
        assert annotation["kind"] == "note"
        assert annotation["position"] == {"x": 10, "y": 20}
        assert annotation["size"] == DEFAULT_NOTE_SIZE
        assert annotation["geometry"] == {
            "x": 10,
            "y": 20,
            "w": 160,
            "h": 96,
            "rotation": 0,
        }
        assert annotation["text"] == ""
        assert "id" not in annotation

    def test_geometry_position_and_size_agree(self):
        annotation = build_note_annotation(x=1, y=2, w=300, h=150, text="hi")
        assert annotation["size"] == {"w": 300, "h": 150}
        assert annotation["geometry"]["w"] == 300
        assert annotation["geometry"]["h"] == 150
        assert annotation["geometry"]["x"] == 1
        assert annotation["geometry"]["y"] == 2

    def test_explicit_id_is_carried_through(self):
        annotation = build_note_annotation(x=0, y=0, annotation_id="note-1")
        assert annotation["id"] == "note-1"

    def test_color_sets_both_top_level_and_style(self):
        """The frontend note overlay reads ``a.color`` directly (annotations.js),
        while the v1 model also carries a ``style.color`` projection."""
        annotation = build_note_annotation(x=0, y=0, color="#ff0000")
        assert annotation["color"] == "#ff0000"
        assert annotation["style"] == {"color": "#ff0000"}

    def test_font_size_uses_camel_case_key(self):
        """Matches the frontend's ``fontSize`` field (annotationModel.js)."""
        annotation = build_note_annotation(x=0, y=0, font_size=18)
        assert annotation["fontSize"] == 18

    def test_no_color_or_font_size_omits_those_keys(self):
        annotation = build_note_annotation(x=0, y=0)
        assert "color" not in annotation
        assert "style" not in annotation
        assert "fontSize" not in annotation

    def test_rotation_z_and_locked_default_like_generic_builder(self):
        """Mirrors ``build_annotation``'s defaults for the same fields."""
        annotation = build_note_annotation(x=0, y=0)
        assert annotation["geometry"]["rotation"] == 0
        assert annotation["z"] == 0
        assert annotation["locked"] is False

    def test_explicit_rotation_z_and_locked(self):
        annotation = build_note_annotation(x=0, y=0, rotation=45, z=5, locked=True)
        assert annotation["geometry"]["rotation"] == 45
        assert annotation["z"] == 5
        assert annotation["locked"] is True


class TestBuildNotePatch:
    def _existing(self, **overrides):
        base = {
            "id": "note-1",
            "type": "note",
            "kind": "note",
            "position": {"x": 5, "y": 6},
            "geometry": {"x": 5, "y": 6, "w": 160, "h": 96, "rotation": 0},
            "size": {"w": 160, "h": 96},
            "text": "hello",
            "color": "#00ff00",
            "style": {"color": "#00ff00"},
        }
        base.update(overrides)
        return base

    def test_text_only_patch_touches_nothing_else(self):
        patch = build_note_patch(self._existing(), text="updated")
        assert patch == {
            "id": "note-1",
            "type": "note",
            "kind": "note",
            "text": "updated",
        }

    def test_position_patch_preserves_current_size_in_geometry(self):
        """A move-only patch must not drop w/h from geometry (shallow merge)."""
        patch = build_note_patch(self._existing(), x=100, y=200)
        assert patch["position"] == {"x": 100, "y": 200}
        assert patch["geometry"] == {
            "x": 100,
            "y": 200,
            "w": 160,
            "h": 96,
            "rotation": 0,
        }
        assert "size" not in patch

    def test_size_patch_preserves_current_position_in_geometry(self):
        """A resize-only patch must not drop x/y from geometry (shallow merge)."""
        patch = build_note_patch(self._existing(), w=300, h=150)
        assert patch["size"] == {"w": 300, "h": 150}
        assert patch["geometry"] == {"x": 5, "y": 6, "w": 300, "h": 150, "rotation": 0}
        assert "position" not in patch

    def test_partial_position_only_changes_given_axis(self):
        patch = build_note_patch(self._existing(), x=42)
        assert patch["position"] == {"x": 42, "y": 6}

    def test_position_and_size_together(self):
        patch = build_note_patch(self._existing(), x=1, y=2, w=3, h=4)
        assert patch["position"] == {"x": 1, "y": 2}
        assert patch["size"] == {"w": 3, "h": 4}
        assert patch["geometry"] == {"x": 1, "y": 2, "w": 3, "h": 4, "rotation": 0}

    def test_color_patch_preserves_other_style_fields(self):
        existing = self._existing(style={"color": "#00ff00", "opacity": 0.5})
        patch = build_note_patch(existing, color="#0000ff")
        assert patch["color"] == "#0000ff"
        assert patch["style"] == {"color": "#0000ff", "opacity": 0.5}

    def test_no_fields_given_yields_minimal_patch(self):
        patch = build_note_patch(self._existing())
        assert patch == {"id": "note-1", "type": "note", "kind": "note"}

    def test_rotation_only_patch(self):
        patch = build_note_patch(self._existing(), rotation=90)
        assert patch["geometry"]["rotation"] == 90
        assert patch["geometry"]["w"] == 160 and patch["geometry"]["h"] == 96
        assert "position" not in patch and "size" not in patch

    def test_rotation_combined_with_move(self):
        patch = build_note_patch(self._existing(), x=1, y=2, rotation=90)
        assert patch["geometry"] == {"x": 1, "y": 2, "w": 160, "h": 96, "rotation": 90}

    def test_z_and_locked_patch(self):
        patch = build_note_patch(self._existing(), z=7, locked=True)
        assert patch["z"] == 7
        assert patch["locked"] is True
        assert "geometry" not in patch

    def test_z_zero_and_locked_false_are_still_applied(self):
        """``z=0``/``locked=False`` must be distinguishable from "not given"."""
        patch = build_note_patch(self._existing(), z=0, locked=False)
        assert patch["z"] == 0
        assert patch["locked"] is False


class TestProjectNote:
    def test_projects_read_shape(self):
        annotation = {
            "id": "note-1",
            "type": "note",
            "position": {"x": 1, "y": 2},
            "size": {"w": 10, "h": 20},
            "geometry": {"x": 1, "y": 2, "w": 10, "h": 20, "rotation": 0},
            "text": "hi",
            "color": "#fff",
            "fontSize": 14,
            "z": 3,
            "locked": True,
            "created_at": "t1",
            "updated_at": "t2",
            "created_by": "mcp-agent",
        }
        projected = project_note(annotation)
        assert projected == {
            "id": "note-1",
            "text": "hi",
            "x": 1,
            "y": 2,
            "w": 10,
            "h": 20,
            "color": "#fff",
            "font_size": 14,
            "rotation": 0,
            "z": 3,
            "locked": True,
            "created_at": "t1",
            "updated_at": "t2",
            "created_by": "mcp-agent",
            "updated_by": None,
        }

    def test_reports_nonzero_rotation(self):
        annotation = {
            "id": "note-1",
            "type": "note",
            "geometry": {"x": 0, "y": 0, "w": 10, "h": 20, "rotation": 45},
        }
        projected = project_note(annotation)
        assert projected["rotation"] == 45

    def test_missing_geometry_falls_back_to_defaults(self):
        projected = project_note({"id": "n", "type": "note"})
        assert projected["x"] == 0 and projected["y"] == 0
        assert projected["w"] == DEFAULT_NOTE_SIZE["w"]
        assert projected["h"] == DEFAULT_NOTE_SIZE["h"]
        assert projected["text"] == ""
        assert projected["rotation"] == 0
        assert projected["locked"] is False
