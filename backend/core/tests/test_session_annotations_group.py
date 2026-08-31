"""Tests for the group (node-membership box) builder helpers.

Pure functions over plain dicts (no session/store dependency), mirroring
``test_session_annotations.py``'s coverage of the note-specific siblings.
"""

import pytest

from backend.core.session_annotations import (
    DEFAULT_GROUP_SIZE,
    build_group_annotation,
    is_group,
)


class TestIsGroup:
    def test_type_field(self):
        assert is_group({"type": "group"}) is True
        assert is_group({"type": "line"}) is False

    def test_falls_back_to_kind_alias(self):
        assert is_group({"kind": "group"}) is True

    def test_missing_type_and_kind(self):
        assert is_group({}) is False


class TestBuildGroupAnnotation:
    def test_defaults(self):
        annotation = build_group_annotation(x=10, y=20)
        assert annotation["type"] == "group"
        assert annotation["kind"] == "group"
        assert annotation["position"] == {"x": 10, "y": 20}
        assert annotation["size"] == DEFAULT_GROUP_SIZE
        assert annotation["geometry"] == {
            "x": 10,
            "y": 20,
            "w": DEFAULT_GROUP_SIZE["w"],
            "h": DEFAULT_GROUP_SIZE["h"],
            "rotation": 0,
        }
        assert annotation["label"] == ""
        assert annotation["description"] == ""
        assert annotation["z"] == 0
        assert annotation["locked"] is False
        assert "id" not in annotation
        assert "color" not in annotation
        assert "style" not in annotation

    def test_member_node_ids_omitted_by_default(self):
        """Only set when explicitly given — see the function's docstring for
        why a shallow dict.update upsert must not silently reset membership."""
        annotation = build_group_annotation(x=0, y=0)
        assert "member_node_ids" not in annotation

    def test_member_node_ids_when_given(self):
        annotation = build_group_annotation(x=0, y=0, member_node_ids=["n1", "n2"])
        assert annotation["member_node_ids"] == ["n1", "n2"]

    def test_empty_member_node_ids_list_is_still_set(self):
        """An explicit [] is a real 'clear membership' instruction, distinct
        from omitting the argument entirely."""
        annotation = build_group_annotation(x=0, y=0, member_node_ids=[])
        assert annotation["member_node_ids"] == []

    def test_member_node_ids_is_copied_not_aliased(self):
        source = ["n1"]
        annotation = build_group_annotation(x=0, y=0, member_node_ids=source)
        annotation["member_node_ids"].append("n2")
        assert source == ["n1"]

    @pytest.mark.parametrize("bad", ["n1", 123, {"n1": True}, [1, 2]])
    def test_non_list_of_strings_member_node_ids_raises(self, bad):
        with pytest.raises(ValueError):
            build_group_annotation(x=0, y=0, member_node_ids=bad)

    def test_geometry_position_and_size_agree(self):
        annotation = build_group_annotation(x=1, y=2, w=300, h=150)
        assert annotation["size"] == {"w": 300, "h": 150}
        assert annotation["geometry"]["w"] == 300
        assert annotation["geometry"]["h"] == 150

    def test_color_also_sets_style(self):
        annotation = build_group_annotation(x=0, y=0, color="#00ff00")
        assert annotation["color"] == "#00ff00"
        assert annotation["style"] == {"color": "#00ff00"}

    def test_annotation_id_included_only_when_given(self):
        without_id = build_group_annotation(x=0, y=0)
        assert "id" not in without_id
        with_id = build_group_annotation(x=0, y=0, annotation_id="group-1")
        assert with_id["id"] == "group-1"

    def test_label_and_description_default_to_empty_string_not_none(self):
        annotation = build_group_annotation(x=0, y=0, label=None, description=None)
        assert annotation["label"] == ""
        assert annotation["description"] == ""

    def test_z_and_locked(self):
        annotation = build_group_annotation(x=0, y=0, z=5, locked=True)
        assert annotation["z"] == 5
        assert annotation["locked"] is True
