"""Tests for the generic MCP annotation tools (``list_annotations``,
``create_annotation``, ``update_annotation``, ``delete_annotation``,
``reorder_annotation``, ``set_annotation_lock``, ``duplicate_annotation``)
registered in ``backend/service/mcp_tools.py``.

These extend the note-only MCP annotation access added in the sticky-note
tool set (see ``test_mcp_sticky_note_tools.py``) to the rest of the v1 model
(``text``/``label``/``line``/``frame``/``shape``/``icon``/``vote_dot``/
``freehand``); ``note``, ``group`` and ``image`` stay out of scope for
*creation* through these tools — each has its own dedicated tool set instead
(see ``backend/core/session_annotations.py``'s module docstring,
``test_mcp_group_annotation_tools.py`` for ``group``, and
``test_mcp_image_annotation_tool.py`` for the image ingest path an
`image` annotation must be created through). The tools are
thin wrappers over ``SessionManager.upsert_annotation``/``update_annotation``/
``delete_annotation`` plus the generic-shape helpers in
``backend/core/session_annotations.py``; the op semantics themselves are
covered in ``backend/core/tests/test_session_manager.py`` and the shape
building/patching in ``backend/core/tests/test_session_annotations_generic.py``.
"""

import os
from unittest.mock import MagicMock, Mock

import pytest

from backend.core import GraphStorage
from backend.core.session_manager import SessionManager
from backend.core.session_store import InMemorySessionPersistenceBackend, SessionStore
from backend.runtime.authorization import AUTHORIZATION_MODE_ENV
from backend.service import GraphService, register_mcp_tools


@pytest.fixture
def annotation_tools(tmp_path):
    """tools_map wired to an in-memory shared-session manager, plus that manager."""
    storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
    service = GraphService(storage)
    manager = SessionManager(SessionStore(InMemorySessionPersistenceBackend()))

    mock_mcp = Mock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)
    tools_map = register_mcp_tools(mock_mcp, service, session_manager=manager)
    return tools_map, manager


class TestListAnnotations:
    def test_empty_session(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["list_annotations"](session_id=session.id)

        assert result["session_id"] == session.id
        assert result["revision"] == 0
        assert result["annotations"] == []

    def test_lists_every_type_including_notes_and_groups(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        tools_map["create_sticky_note"](session_id=session.id, x=0, y=0, text="n")
        tools_map["create_annotation"](session_id=session.id, type="label", x=1, y=1)
        manager.upsert_annotation(
            session.id, "mcp-agent", {"id": "group-1", "type": "group"}
        )

        result = tools_map["list_annotations"](session_id=session.id)

        types = {a["type"] for a in result["annotations"]}
        assert types == {"note", "label", "group"}

    def test_filters_by_type(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        tools_map["create_annotation"](session_id=session.id, type="label", x=0, y=0)
        tools_map["create_annotation"](session_id=session.id, type="shape", x=0, y=0)

        result = tools_map["list_annotations"](session_id=session.id, types=["shape"])

        assert len(result["annotations"]) == 1
        assert result["annotations"][0]["type"] == "shape"

    def test_filter_accepts_arrow_alias(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        tools_map["create_annotation"](session_id=session.id, type="arrow", x=0, y=0)

        result = tools_map["list_annotations"](session_id=session.id, types=["arrow"])

        assert len(result["annotations"]) == 1
        assert result["annotations"][0]["type"] == "line"

    def test_unknown_type_filter_is_an_error(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["list_annotations"](session_id=session.id, types=["banana"])

        assert "error" in result

    def test_invalid_session_id(self, annotation_tools):
        tools_map, _ = annotation_tools
        assert "error" in tools_map["list_annotations"](session_id="nope")

    def test_unknown_session(self, annotation_tools):
        tools_map, _ = annotation_tools
        result = tools_map["list_annotations"](session_id="9999-9999")
        assert "not found" in result["error"]


class TestCreateAnnotation:
    @pytest.mark.parametrize(
        "ann_type",
        ["text", "label", "line", "frame", "shape", "icon", "vote_dot"],
    )
    def test_creates_each_generic_type(self, annotation_tools, ann_type):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["create_annotation"](
            session_id=session.id, type=ann_type, x=1, y=2
        )

        assert result["success"] is True
        assert result["annotation"]["type"] == ann_type
        assert result["annotation"]["x"] == 1 and result["annotation"]["y"] == 2
        assert (
            isinstance(result["annotation"]["id"], str) and result["annotation"]["id"]
        )

    def test_arrow_alias_normalizes_to_line(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["create_annotation"](
            session_id=session.id, type="arrow", x=0, y=0
        )

        assert result["success"] is True
        assert result["annotation"]["type"] == "line"

    def test_content_and_style_round_trip(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["create_annotation"](
            session_id=session.id,
            type="line",
            x=0,
            y=0,
            content={"to": {"x": 100, "y": 0}, "endArrow": True},
            style={"stroke": "#000"},
            z=2,
            locked=True,
            annotation_id="line-1",
        )

        annotation = result["annotation"]
        assert annotation["id"] == "line-1"
        assert annotation["content"] == {"to": {"x": 100, "y": 0}, "endArrow": True}
        assert annotation["style"] == {"stroke": "#000"}
        assert annotation["z"] == 2
        assert annotation["locked"] is True

    def test_rotation_round_trips_through_create_list_and_update(
        self, annotation_tools
    ):
        """A rotation an agent sets must still be readable after list/update.

        The canvas renders geometry.rotation for the types the contract
        accepts it for, so a rotation that survives the write but not the
        read would show as an annotation that silently straightens itself.
        """
        tools_map, manager = annotation_tools
        session = manager.create_session()

        created = tools_map["create_annotation"](
            session_id=session.id,
            type="shape",
            x=0,
            y=0,
            w=160,
            h=96,
            rotation=45,
            content={"shape": "process_arrow"},
            annotation_id="shape-1",
        )
        assert created["annotation"]["rotation"] == 45

        listed = tools_map["list_annotations"](session_id=session.id, types=["shape"])
        assert listed["annotations"][0]["rotation"] == 45
        assert listed["annotations"][0]["content"]["shape"] == "process_arrow"

        updated = tools_map["update_annotation"](
            session_id=session.id, annotation_id="shape-1", rotation=-90
        )
        assert updated["annotation"]["rotation"] == -90
        assert updated["annotation"]["w"] == 160
        assert updated["annotation"]["content"]["shape"] == "process_arrow"

    def test_shape_caption_round_trips_through_create_list_and_update(
        self, annotation_tools
    ):
        """A shape's optional `content.text` caption
        (task-annotation-doubleclick-to-edit-text) is free-form payload like
        every other content field outside shape/icon/attachment/line
        (`_validate_generic_content`'s docstring) — this pins that it
        actually survives create/list/update over the MCP surface, not just
        the frontend translators.
        """
        tools_map, manager = annotation_tools
        session = manager.create_session()

        created = tools_map["create_annotation"](
            session_id=session.id,
            type="shape",
            x=0,
            y=0,
            content={"shape": "triangle", "text": "Step 1"},
            annotation_id="shape-caption-1",
        )
        assert created["annotation"]["content"]["text"] == "Step 1"

        listed = tools_map["list_annotations"](session_id=session.id, types=["shape"])
        assert listed["annotations"][0]["content"]["text"] == "Step 1"

        updated = tools_map["update_annotation"](
            session_id=session.id,
            annotation_id="shape-caption-1",
            content={"text": "Step 2"},
        )
        assert updated["annotation"]["content"]["text"] == "Step 2"
        # The shape name is untouched by a caption-only update.
        assert updated["annotation"]["content"]["shape"] == "triangle"

    def test_text_typography_round_trips_through_create_list_and_update(
        self, annotation_tools
    ):
        """fontSize/font/textAlign (task-annotation-text-alignment-and-font)
        live under `style`, a generic, un-typed passthrough
        (`build_annotation`/`build_annotation_patch` in
        session_annotations.py) — this pins that all three actually survive
        create/list/update over the MCP surface, mirroring the shape-caption
        pin above for `content`.
        """
        tools_map, manager = annotation_tools
        session = manager.create_session()

        created = tools_map["create_annotation"](
            session_id=session.id,
            type="text",
            x=0,
            y=0,
            content={"text": "Heading"},
            style={
                "color": "#fff",
                "fontSize": 24,
                "font": "serif",
                "textAlign": "middle-center",
            },
            annotation_id="text-typography-1",
        )
        assert created["annotation"]["style"] == {
            "color": "#fff",
            "fontSize": 24,
            "font": "serif",
            "textAlign": "middle-center",
        }

        listed = tools_map["list_annotations"](session_id=session.id, types=["text"])
        assert listed["annotations"][0]["style"]["textAlign"] == "middle-center"

        updated = tools_map["update_annotation"](
            session_id=session.id,
            annotation_id="text-typography-1",
            style={
                "color": "#fff",
                "fontSize": 24,
                "font": "monospace",
                "textAlign": "top-left",
            },
        )
        assert updated["annotation"]["style"]["font"] == "monospace"
        assert updated["annotation"]["style"]["textAlign"] == "top-left"

    def test_invalid_type_is_rejected(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["create_annotation"](
            session_id=session.id, type="banana", x=0, y=0
        )

        assert result["success"] is False
        assert result["error"] == "invalid_type"

    def test_note_type_is_rejected_use_create_sticky_note(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["create_annotation"](
            session_id=session.id, type="note", x=0, y=0
        )

        assert result["success"] is False
        assert result["error"] == "invalid_type"

    def test_group_type_is_rejected(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["create_annotation"](
            session_id=session.id, type="group", x=0, y=0
        )

        assert result["success"] is False
        assert result["error"] == "invalid_type"

    def test_content_cannot_smuggle_reserved_fields(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["create_annotation"](
            session_id=session.id, type="label", x=0, y=0, content={"type": "note"}
        )

        assert result["success"] is False
        assert result["error"] == "invalid_content"
        assert session.state["annotations"] == []

    def test_non_string_shape_is_invalid_content(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["create_annotation"](
            session_id=session.id, type="shape", x=0, y=0, content={"shape": 7}
        )

        assert result["success"] is False
        assert result["error"] == "invalid_content"
        assert session.state["annotations"] == []

    def test_unrecognised_shape_name_is_accepted(self, annotation_tools):
        """Only the type of `content.shape` is validated, not membership in
        the accepted variant set — an unrecognised-but-stringy name is stored
        (docs/ANNOTATION_CONTRACT.md, backend/DEVELOPMENT.md)."""
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["create_annotation"](
            session_id=session.id, type="shape", x=0, y=0, content={"shape": "star"}
        )

        assert result["success"] is True
        assert result["annotation"]["content"]["shape"] == "star"

    def test_non_string_icon_is_invalid_content(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["create_annotation"](
            session_id=session.id, type="icon", x=0, y=0, content={"icon": ["flag"]}
        )

        assert result["success"] is False
        assert result["error"] == "invalid_content"

    @pytest.mark.parametrize("ann_type", ["label", "text", "icon", "vote_dot"])
    def test_attachment_round_trips_for_every_attachable_type(
        self, annotation_tools, ann_type
    ):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["create_annotation"](
            session_id=session.id,
            type=ann_type,
            x=0,
            y=0,
            content={"attachment": {"target_id": "node-1", "anchor": "right"}},
        )

        assert result["success"] is True
        attachment = result["annotation"]["content"]["attachment"]
        assert attachment["target_id"] == "node-1"
        assert attachment["anchor"] == "right"

    def test_malformed_attachment_target_id_is_invalid_content(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["create_annotation"](
            session_id=session.id,
            type="label",
            x=0,
            y=0,
            content={"attachment": {"anchor": "top"}},  # missing target_id
        )

        assert result["success"] is False
        assert result["error"] == "invalid_content"
        assert session.state["annotations"] == []

    def test_line_endpoint_attachment_round_trips(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["create_annotation"](
            session_id=session.id,
            type="line",
            x=0,
            y=0,
            content={
                "to": {"x": 100, "y": 0},
                "end": {"attachment": {"target_id": "node-2"}},
            },
        )

        assert result["success"] is True
        assert (
            result["annotation"]["content"]["end"]["attachment"]["target_id"]
            == "node-2"
        )

    def test_malformed_line_endpoint_is_invalid_content(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["create_annotation"](
            session_id=session.id,
            type="line",
            x=0,
            y=0,
            content={"start": {"point": {"x": "nope", "y": 0}}},
        )

        assert result["success"] is False
        assert result["error"] == "invalid_content"
        assert session.state["annotations"] == []

    def test_upserts_by_matching_id_and_same_type(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        tools_map["create_annotation"](
            session_id=session.id, type="shape", x=0, y=0, annotation_id="shape-1"
        )

        result = tools_map["create_annotation"](
            session_id=session.id, type="shape", x=5, y=5, annotation_id="shape-1"
        )

        assert result["success"] is True
        assert len(session.state["annotations"]) == 1
        assert result["annotation"]["x"] == 5

    def test_note_annotation_id_is_rejected_not_overwritten(self, annotation_tools):
        """create_annotation must never silently convert a note into a generic type."""
        tools_map, manager = annotation_tools
        session = manager.create_session()
        tools_map["create_sticky_note"](
            session_id=session.id, x=0, y=0, annotation_id="note-1"
        )

        result = tools_map["create_annotation"](
            session_id=session.id, type="label", x=0, y=0, annotation_id="note-1"
        )

        assert result["success"] is False
        assert result["error"] == "wrong_type"
        surviving = session.state["annotations"][0]
        assert surviving["type"] == "note"

    def test_cross_generic_type_collision_is_rejected(self, annotation_tools):
        """create_annotation must never silently convert a line into a shape."""
        tools_map, manager = annotation_tools
        session = manager.create_session()
        tools_map["create_annotation"](
            session_id=session.id, type="line", x=0, y=0, annotation_id="ann-1"
        )

        result = tools_map["create_annotation"](
            session_id=session.id, type="shape", x=0, y=0, annotation_id="ann-1"
        )

        assert result["success"] is False
        assert result["error"] == "wrong_type"
        surviving = session.state["annotations"][0]
        assert surviving["type"] == "line"

    def test_revision_conflict_is_reported(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        tools_map["create_annotation"](session_id=session.id, type="label", x=0, y=0)

        result = tools_map["create_annotation"](
            session_id=session.id, type="label", x=1, y=1, expected_revision=0
        )

        assert result["success"] is False
        assert result["error"] == "revision_conflict"
        assert result["current_revision"] == session.seq

    def test_busy_when_lock_held(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        class _HeldLock:
            def locked(self):
                return True

        manager._lock = lambda _sid: _HeldLock()
        result = tools_map["create_annotation"](
            session_id=session.id, type="label", x=0, y=0
        )
        assert result["success"] is False
        assert result["error"] == "busy"

    def test_invalid_session_id(self, annotation_tools):
        tools_map, _ = annotation_tools
        result = tools_map["create_annotation"](
            session_id="nope", type="label", x=0, y=0
        )
        assert result["success"] is False

    def test_missing_manager_is_reported(self):
        storage = GraphStorage(json_path="/tmp/does-not-matter-generic-ann.json")
        service = GraphService(storage)
        mock_mcp = Mock()
        mock_mcp.tool = MagicMock(return_value=lambda f: f)
        tools_map = register_mcp_tools(mock_mcp, service)  # no session_manager
        result = tools_map["create_annotation"](
            session_id="1111-2222", type="label", x=0, y=0
        )
        assert result["success"] is False


class TestUpdateAnnotation:
    def test_partial_geometry_update_preserves_content(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id,
            type="label",
            x=10,
            y=20,
            content={"text": "hello"},
        )
        ann_id = created["annotation"]["id"]

        result = tools_map["update_annotation"](
            session_id=session.id, annotation_id=ann_id, x=99
        )

        assert result["success"] is True
        assert result["annotation"]["x"] == 99 and result["annotation"]["y"] == 20
        assert result["annotation"]["content"] == {"text": "hello"}

    def test_content_update_replaces_given_keys_only(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id,
            type="line",
            x=0,
            y=0,
            content={"to": {"x": 100, "y": 0}, "endArrow": True},
        )
        ann_id = created["annotation"]["id"]

        result = tools_map["update_annotation"](
            session_id=session.id,
            annotation_id=ann_id,
            content={"to": {"x": 200, "y": 0}},
        )

        assert result["annotation"]["content"]["to"] == {"x": 200, "y": 0}
        assert result["annotation"]["content"]["endArrow"] is True

    def test_position_move_translates_line_endpoint(self, annotation_tools):
        """Moving a line via x/y must translate its `to` endpoint by the same
        delta, preserving the line's shape instead of stretching it."""
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id,
            type="line",
            x=0,
            y=0,
            content={"to": {"x": 100, "y": 0}, "endArrow": True},
        )
        ann_id = created["annotation"]["id"]

        result = tools_map["update_annotation"](
            session_id=session.id, annotation_id=ann_id, x=50, y=10
        )

        assert result["success"] is True
        assert result["annotation"]["x"] == 50 and result["annotation"]["y"] == 10
        assert result["annotation"]["content"]["to"] == {"x": 150, "y": 10}

    def test_style_update_replaces_whole_style_dict(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id, type="shape", x=0, y=0, style={"fill": "#000"}
        )

        result = tools_map["update_annotation"](
            session_id=session.id,
            annotation_id=created["annotation"]["id"],
            style={"fill": "#fff"},
        )

        assert result["annotation"]["style"] == {"fill": "#fff"}

    def test_no_fields_given_is_an_error(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id, type="label", x=0, y=0
        )

        result = tools_map["update_annotation"](
            session_id=session.id, annotation_id=created["annotation"]["id"]
        )

        assert result["success"] is False
        assert result["error"] == "no_fields_to_update"

    def test_unknown_annotation_id_is_not_found(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["update_annotation"](
            session_id=session.id, annotation_id="ghost", x=1
        )

        assert result["success"] is False
        assert result["error"] == "not_found"

    def test_note_annotation_id_is_not_found(self, annotation_tools):
        """A note must not be editable through the generic annotation tools."""
        tools_map, manager = annotation_tools
        session = manager.create_session()
        tools_map["create_sticky_note"](
            session_id=session.id, x=0, y=0, annotation_id="note-1"
        )

        result = tools_map["update_annotation"](
            session_id=session.id, annotation_id="note-1", x=1
        )

        assert result["success"] is False
        assert result["error"] == "not_found"

    def test_group_annotation_id_is_not_found(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        manager.upsert_annotation(
            session.id, "mcp-agent", {"id": "group-1", "type": "group"}
        )

        result = tools_map["update_annotation"](
            session_id=session.id, annotation_id="group-1", x=1
        )

        assert result["success"] is False
        assert result["error"] == "not_found"

    def test_revision_conflict_is_reported(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id, type="label", x=0, y=0
        )

        result = tools_map["update_annotation"](
            session_id=session.id,
            annotation_id=created["annotation"]["id"],
            x=1,
            expected_revision=0,
        )

        assert result["success"] is False
        assert result["error"] == "revision_conflict"

    def test_malformed_shape_update_is_rejected_without_mutating(
        self, annotation_tools
    ):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id,
            type="shape",
            x=0,
            y=0,
            content={"shape": "rectangle"},
        )

        result = tools_map["update_annotation"](
            session_id=session.id,
            annotation_id=created["annotation"]["id"],
            content={"shape": {"nope": True}},
        )

        assert result["success"] is False
        assert result["error"] == "invalid_content"
        stored = session.state["annotations"][0]
        assert stored["shape"] == "rectangle"

    def test_malformed_attachment_update_is_rejected_without_mutating(
        self, annotation_tools
    ):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id,
            type="icon",
            x=0,
            y=0,
            content={"attachment": {"target_id": "node-1"}},
        )

        result = tools_map["update_annotation"](
            session_id=session.id,
            annotation_id=created["annotation"]["id"],
            content={"attachment": {"target_id": ""}},
        )

        assert result["success"] is False
        assert result["error"] == "invalid_content"
        stored = session.state["annotations"][0]
        assert stored["attachment"]["target_id"] == "node-1"

    @pytest.mark.parametrize("ann_type", ["label", "text", "icon", "vote_dot"])
    def test_update_annotation_attaches_a_previously_unattached_annotation(
        self, annotation_tools, ann_type
    ):
        """create_annotation's create-time attachment round trip is covered
        above; this pins the update path — adding an attachment after
        creation — and confirms it survives an independent list_annotations
        read, not just the update call's own response."""
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id, type=ann_type, x=0, y=0
        )
        assert "attachment" not in created["annotation"]["content"]

        result = tools_map["update_annotation"](
            session_id=session.id,
            annotation_id=created["annotation"]["id"],
            content={"attachment": {"target_id": "node-9", "anchor": "top"}},
        )

        assert result["success"] is True
        assert result["annotation"]["content"]["attachment"] == {
            "target_id": "node-9",
            "anchor": "top",
        }
        listed = tools_map["list_annotations"](session_id=session.id)
        assert listed["annotations"][0]["content"]["attachment"]["target_id"] == (
            "node-9"
        )

    def test_update_annotation_changes_a_line_endpoint_attachment_target(
        self, annotation_tools
    ):
        """Docking a line's end onto a different node via update_annotation,
        then confirming the new target survives a fresh list_annotations
        read (end-to-end round trip, not just the update response)."""
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id,
            type="line",
            x=0,
            y=0,
            content={
                "to": {"x": 100, "y": 0},
                "end": {"attachment": {"target_id": "node-a"}},
            },
        )
        ann_id = created["annotation"]["id"]

        result = tools_map["update_annotation"](
            session_id=session.id,
            annotation_id=ann_id,
            content={"end": {"attachment": {"target_id": "node-b"}}},
        )

        assert result["success"] is True
        assert (
            result["annotation"]["content"]["end"]["attachment"]["target_id"]
            == "node-b"
        )
        listed = tools_map["list_annotations"](session_id=session.id, types=["line"])
        assert (
            listed["annotations"][0]["content"]["end"]["attachment"]["target_id"]
            == "node-b"
        )

    def test_update_annotation_can_detach_by_clearing_attachment(
        self, annotation_tools
    ):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id,
            type="label",
            x=0,
            y=0,
            content={"attachment": {"target_id": "node-1"}},
        )

        result = tools_map["update_annotation"](
            session_id=session.id,
            annotation_id=created["annotation"]["id"],
            content={"attachment": None},
        )

        assert result["success"] is True
        assert result["annotation"]["content"]["attachment"] is None


class TestReorderAnnotation:
    def test_sets_z(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id, type="shape", x=0, y=0
        )

        result = tools_map["reorder_annotation"](
            session_id=session.id, annotation_id=created["annotation"]["id"], z=7
        )

        assert result["success"] is True
        assert result["annotation"]["z"] == 7

    def test_unknown_annotation_id_is_not_found(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["reorder_annotation"](
            session_id=session.id, annotation_id="ghost", z=1
        )

        assert result["success"] is False
        assert result["error"] == "not_found"

    def test_revision_conflict_is_reported(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id, type="shape", x=0, y=0
        )

        result = tools_map["reorder_annotation"](
            session_id=session.id,
            annotation_id=created["annotation"]["id"],
            z=7,
            expected_revision=0,
        )

        assert result["success"] is False
        assert result["error"] == "revision_conflict"


class TestSetAnnotationLock:
    def test_locks_and_unlocks(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id, type="shape", x=0, y=0
        )
        ann_id = created["annotation"]["id"]

        locked = tools_map["set_annotation_lock"](
            session_id=session.id, annotation_id=ann_id, locked=True
        )
        assert locked["annotation"]["locked"] is True

        unlocked = tools_map["set_annotation_lock"](
            session_id=session.id, annotation_id=ann_id, locked=False
        )
        assert unlocked["annotation"]["locked"] is False

    def test_unknown_annotation_id_is_not_found(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["set_annotation_lock"](
            session_id=session.id, annotation_id="ghost", locked=True
        )

        assert result["success"] is False
        assert result["error"] == "not_found"


class TestDuplicateAnnotation:
    def test_duplicates_with_offset_and_new_id(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id,
            type="line",
            x=10,
            y=20,
            content={"to": {"x": 110, "y": 20}, "endArrow": True},
            style={"stroke": "#000"},
        )
        original_id = created["annotation"]["id"]

        result = tools_map["duplicate_annotation"](
            session_id=session.id,
            annotation_id=original_id,
            new_annotation_id="line-copy",
            dx=5,
            dy=5,
        )

        assert result["success"] is True
        copy = result["annotation"]
        assert copy["id"] == "line-copy"
        assert copy["x"] == 15 and copy["y"] == 25
        # The `to` endpoint must translate by the same (dx, dy) as the
        # position, so the duplicate keeps the original's line shape instead
        # of stretching it (the original was created at x=10,y=20 with
        # to=(110,20); a (5,5) offset moves both ends the same amount).
        assert copy["content"] == {"to": {"x": 115, "y": 25}, "endArrow": True}
        assert copy["style"] == {"stroke": "#000"}
        assert len(session.state["annotations"]) == 2
        # The original is untouched.
        original = next(
            a for a in session.state["annotations"] if a["id"] == original_id
        )
        assert original["geometry"]["x"] == 10

    def test_duplicate_without_new_id_gets_server_assigned_id(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id, type="shape", x=0, y=0
        )

        result = tools_map["duplicate_annotation"](
            session_id=session.id, annotation_id=created["annotation"]["id"]
        )

        assert result["success"] is True
        assert result["annotation"]["id"] != created["annotation"]["id"]
        assert len(session.state["annotations"]) == 2

    def test_new_id_collision_is_rejected(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        tools_map["create_annotation"](
            session_id=session.id, type="shape", x=0, y=0, annotation_id="shape-a"
        )
        tools_map["create_annotation"](
            session_id=session.id, type="shape", x=1, y=1, annotation_id="shape-b"
        )

        result = tools_map["duplicate_annotation"](
            session_id=session.id, annotation_id="shape-a", new_annotation_id="shape-b"
        )

        assert result["success"] is False
        assert result["error"] == "id_exists"
        assert len(session.state["annotations"]) == 2

    def test_unknown_annotation_id_is_not_found(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["duplicate_annotation"](
            session_id=session.id, annotation_id="ghost"
        )

        assert result["success"] is False
        assert result["error"] == "not_found"

    def test_note_annotation_id_is_not_found(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        tools_map["create_sticky_note"](
            session_id=session.id, x=0, y=0, annotation_id="note-1"
        )

        result = tools_map["duplicate_annotation"](
            session_id=session.id, annotation_id="note-1"
        )

        assert result["success"] is False
        assert result["error"] == "not_found"

    def test_duplicating_a_freehand_stroke_translates_its_points(
        self, annotation_tools
    ):
        """Regression test: duplicate_annotation translated a line's from/to
        endpoints by (dx, dy) but not a freehand stroke's `points`, so a
        duplicated stroke kept its original geometry at a moved envelope
        position (docs/ANNOTATION_CONTRACT.md's `freehand` row)."""
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id,
            type="freehand",
            x=0,
            y=0,
            content={"points": [{"x": 0, "y": 0}, {"x": 10, "y": 10}]},
        )

        result = tools_map["duplicate_annotation"](
            session_id=session.id,
            annotation_id=created["annotation"]["id"],
            dx=100,
            dy=-50,
        )

        assert result["success"] is True
        assert result["annotation"]["x"] == 100
        assert result["annotation"]["y"] == -50
        assert result["annotation"]["content"]["points"] == [
            {"x": 100, "y": -50},
            {"x": 110, "y": -40},
        ]


class TestFreehandOverGenericTools:
    """`freehand` has been a member of GENERIC_ANNOTATION_TYPES since #422
    (docs/ANNOTATION_CONTRACT.md's `freehand` row was stale in claiming it
    had no MCP tool at all) — these pin that create/update/style/reorder/
    lock/delete already work through the same generic tools as every other
    non-note, non-group, non-image type."""

    def test_create_with_points_and_style(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["create_annotation"](
            session_id=session.id,
            type="freehand",
            x=0,
            y=0,
            content={
                "points": [{"x": 0, "y": 0}, {"x": 5, "y": 5}],
                "strokeWidth": 4,
            },
            style={"stroke": "#123456"},
        )

        assert result["success"] is True
        assert result["annotation"]["content"]["points"] == [
            {"x": 0, "y": 0},
            {"x": 5, "y": 5},
        ]
        assert result["annotation"]["content"]["strokeWidth"] == 4
        assert result["annotation"]["style"] == {"stroke": "#123456"}

    def test_position_move_translates_points(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id,
            type="freehand",
            x=0,
            y=0,
            content={"points": [{"x": 0, "y": 0}, {"x": 10, "y": 0}]},
        )

        result = tools_map["update_annotation"](
            session_id=session.id,
            annotation_id=created["annotation"]["id"],
            x=20,
            y=30,
        )

        assert result["success"] is True
        assert result["annotation"]["x"] == 20 and result["annotation"]["y"] == 30
        assert result["annotation"]["content"]["points"] == [
            {"x": 20, "y": 30},
            {"x": 30, "y": 30},
        ]

    def test_style_update(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id,
            type="freehand",
            x=0,
            y=0,
            content={"points": [{"x": 0, "y": 0}]},
            style={"stroke": "#000000"},
        )

        result = tools_map["update_annotation"](
            session_id=session.id,
            annotation_id=created["annotation"]["id"],
            style={"stroke": "#ffffff"},
        )

        assert result["annotation"]["style"] == {"stroke": "#ffffff"}
        # content (points) is untouched by a style-only patch.
        assert result["annotation"]["content"]["points"] == [{"x": 0, "y": 0}]

    def test_rotation_round_trips_though_never_rendered(self, annotation_tools):
        """docs/ANNOTATION_CONTRACT.md: freehand's stored rotation is accepted
        and reported back over MCP, even though the canvas never draws it."""
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id,
            type="freehand",
            x=0,
            y=0,
            rotation=45,
            content={"points": [{"x": 0, "y": 0}]},
        )

        assert created["annotation"]["rotation"] == 45
        listed = tools_map["list_annotations"](session_id=session.id)
        assert listed["annotations"][0]["rotation"] == 45

    def test_resize_stores_geometry_but_never_reshapes_the_stroke(
        self, annotation_tools
    ):
        """A `freehand` stroke's shape is entirely in its `points`; unlike
        `frame`/`shape`/`image` it carries no box the renderer scales to. A
        w/h patch is therefore accepted and reported back, but changes
        nothing a viewer sees — the same "stored, never drawn" shape as
        freehand's rotation above, and documented alongside it in
        docs/ANNOTATION_CONTRACT.md. Pinned so a later resize *implementation*
        has to update the contract rather than silently changing what a
        stored w/h means.
        """
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id,
            type="freehand",
            x=0,
            y=0,
            content={"points": [{"x": 0, "y": 0}, {"x": 10, "y": 0}]},
        )

        result = tools_map["update_annotation"](
            session_id=session.id,
            annotation_id=created["annotation"]["id"],
            w=200,
            h=100,
        )

        assert result["success"] is True
        assert result["annotation"]["w"] == 200
        assert result["annotation"]["h"] == 100
        assert result["annotation"]["content"]["points"] == [
            {"x": 0, "y": 0},
            {"x": 10, "y": 0},
        ]

    def test_reorder_lock_and_delete(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id,
            type="freehand",
            x=0,
            y=0,
            content={"points": [{"x": 0, "y": 0}]},
        )
        ann_id = created["annotation"]["id"]

        reordered = tools_map["reorder_annotation"](
            session_id=session.id, annotation_id=ann_id, z=3
        )
        locked = tools_map["set_annotation_lock"](
            session_id=session.id, annotation_id=ann_id, locked=True
        )
        deleted = tools_map["delete_annotation"](
            session_id=session.id, annotation_id=ann_id
        )

        assert reordered["success"] is True
        assert locked["success"] is True
        assert deleted["success"] is True
        assert session.state["annotations"] == []


class TestDeleteAnnotation:
    def test_deletes_existing_annotation(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id, type="label", x=0, y=0
        )
        ann_id = created["annotation"]["id"]

        result = tools_map["delete_annotation"](
            session_id=session.id, annotation_id=ann_id
        )

        assert result["success"] is True
        assert result["annotation_id"] == ann_id
        assert session.state["annotations"] == []

    def test_unknown_annotation_id_is_not_found(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["delete_annotation"](
            session_id=session.id, annotation_id="ghost"
        )

        assert result["success"] is False
        assert result["error"] == "not_found"

    def test_note_annotation_id_is_not_found(self, annotation_tools):
        """A note must not be deletable through the generic annotation tools."""
        tools_map, manager = annotation_tools
        session = manager.create_session()
        tools_map["create_sticky_note"](
            session_id=session.id, x=0, y=0, annotation_id="note-1"
        )

        result = tools_map["delete_annotation"](
            session_id=session.id, annotation_id="note-1"
        )

        assert result["success"] is False
        assert result["error"] == "not_found"
        assert len(session.state["annotations"]) == 1

    def test_group_annotation_id_is_not_found(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        manager.upsert_annotation(
            session.id, "mcp-agent", {"id": "group-1", "type": "group"}
        )

        result = tools_map["delete_annotation"](
            session_id=session.id, annotation_id="group-1"
        )

        assert result["success"] is False
        assert result["error"] == "not_found"
        assert len(session.state["annotations"]) == 1

    def test_revision_conflict_is_reported(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id, type="label", x=0, y=0
        )

        result = tools_map["delete_annotation"](
            session_id=session.id,
            annotation_id=created["annotation"]["id"],
            expected_revision=0,
        )

        assert result["success"] is False
        assert result["error"] == "revision_conflict"
        assert len(session.state["annotations"]) == 1


class TestGenericAnnotationToolsAuthorization:
    """Same authorization seam as the sticky-note tools (see
    TestStickyNoteToolsAuthorization in test_mcp_sticky_note_tools.py)."""

    def test_deny_all_blocks_list(self, annotation_tools, monkeypatch):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "deny-all")

        result = tools_map["list_annotations"](session_id=session.id)

        assert result.get("error_code") == "access_denied"

    def test_deny_all_blocks_create(self, annotation_tools, monkeypatch):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "deny-all")

        result = tools_map["create_annotation"](
            session_id=session.id, type="label", x=0, y=0
        )

        assert result.get("error_code") == "access_denied"

    def test_read_only_blocks_create_and_creates_nothing(
        self, annotation_tools, monkeypatch
    ):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "read-only")

        result = tools_map["create_annotation"](
            session_id=session.id, type="label", x=0, y=0
        )

        assert result["success"] is False
        assert result.get("error_code") == "access_denied"
        assert session.state["annotations"] == []

    def test_read_only_still_allows_list(self, annotation_tools, monkeypatch):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "read-only")

        result = tools_map["list_annotations"](session_id=session.id)

        assert "error_code" not in result

    def test_permissive_default_allows_read_and_mutation(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        created = tools_map["create_annotation"](
            session_id=session.id, type="label", x=0, y=0
        )
        listed = tools_map["list_annotations"](session_id=session.id)

        assert "error_code" not in created
        assert "error_code" not in listed
        assert len(listed["annotations"]) == 1
