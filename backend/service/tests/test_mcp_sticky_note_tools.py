"""
Tests for the MCP sticky-note tools (``list_sticky_notes``, ``create_sticky_note``,
``update_sticky_note``, ``delete_sticky_note``) registered in
``backend/service/mcp_tools.py``.

These tools are thin wrappers over ``SessionManager.upsert_annotation`` /
``update_annotation`` / ``delete_annotation`` plus the note-shape helpers in
``backend/core/session_annotations.py``; the op semantics themselves are
covered in ``backend/core/tests/test_session_manager.py`` and the note-shape
building/patching in ``backend/core/tests/test_session_annotations.py``.
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
def note_tools(tmp_path):
    """tools_map wired to an in-memory shared-session manager, plus that manager."""
    storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
    service = GraphService(storage)
    manager = SessionManager(SessionStore(InMemorySessionPersistenceBackend()))

    mock_mcp = Mock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)
    tools_map = register_mcp_tools(mock_mcp, service, session_manager=manager)
    return tools_map, manager


class TestListStickyNotes:
    def test_empty_session(self, note_tools):
        tools_map, manager = note_tools
        session = manager.create_session()

        result = tools_map["list_sticky_notes"](session_id=session.id)

        assert result["session_id"] == session.id
        assert result["revision"] == 0
        assert result["notes"] == []

    def test_lists_created_notes_and_ignores_other_annotation_types(self, note_tools):
        tools_map, manager = note_tools
        session = manager.create_session()
        tools_map["create_sticky_note"](session_id=session.id, x=1, y=2, text="hi")
        manager.upsert_annotation(
            session.id, "mcp-agent", {"id": "line-1", "type": "line"}
        )

        result = tools_map["list_sticky_notes"](session_id=session.id)

        assert len(result["notes"]) == 1
        assert result["notes"][0]["text"] == "hi"
        assert result["notes"][0]["x"] == 1
        assert result["notes"][0]["y"] == 2

    def test_invalid_session_id(self, note_tools):
        tools_map, _ = note_tools
        assert "error" in tools_map["list_sticky_notes"](session_id="nope")

    def test_unknown_session(self, note_tools):
        tools_map, _ = note_tools
        result = tools_map["list_sticky_notes"](session_id="9999-9999")
        assert "not found" in result["error"]

    def test_missing_manager_is_reported(self):
        storage = GraphStorage(json_path="/tmp/does-not-matter-notes.json")
        service = GraphService(storage)
        mock_mcp = Mock()
        mock_mcp.tool = MagicMock(return_value=lambda f: f)
        tools_map = register_mcp_tools(mock_mcp, service)  # no session_manager
        assert "error" in tools_map["list_sticky_notes"](session_id="1111-2222")


class TestCreateStickyNote:
    def test_creates_with_defaults(self, note_tools):
        tools_map, manager = note_tools
        session = manager.create_session()

        result = tools_map["create_sticky_note"](session_id=session.id, x=10, y=20)

        assert result["success"] is True
        assert result["note"]["x"] == 10 and result["note"]["y"] == 20
        assert result["note"]["w"] == 160 and result["note"]["h"] == 96
        assert result["note"]["text"] == ""
        assert isinstance(result["note"]["id"], str) and result["note"]["id"]
        assert result["revision"] == session.seq == 1

    def test_creates_with_explicit_fields(self, note_tools):
        tools_map, manager = note_tools
        session = manager.create_session()

        result = tools_map["create_sticky_note"](
            session_id=session.id,
            x=1,
            y=2,
            text="hello",
            color="#ff0000",
            font_size=18,
            w=300,
            h=150,
            annotation_id="note-1",
        )

        note = result["note"]
        assert note["id"] == "note-1"
        assert note["text"] == "hello"
        assert note["color"] == "#ff0000"
        assert note["font_size"] == 18
        assert note["w"] == 300 and note["h"] == 150

    def test_upserts_by_matching_id(self, note_tools):
        tools_map, manager = note_tools
        session = manager.create_session()
        tools_map["create_sticky_note"](
            session_id=session.id, x=0, y=0, text="v1", annotation_id="note-1"
        )

        result = tools_map["create_sticky_note"](
            session_id=session.id, x=5, y=5, text="v2", annotation_id="note-1"
        )

        assert result["success"] is True
        assert len(session.state["annotations"]) == 1
        assert result["note"]["text"] == "v2"
        assert result["note"]["x"] == 5

    def test_revision_conflict_is_reported(self, note_tools):
        tools_map, manager = note_tools
        session = manager.create_session()
        tools_map["create_sticky_note"](session_id=session.id, x=0, y=0)

        result = tools_map["create_sticky_note"](
            session_id=session.id, x=1, y=1, expected_revision=0
        )

        assert result["success"] is False
        assert result["error"] == "revision_conflict"
        assert result["current_revision"] == session.seq

    def test_busy_when_lock_held(self, note_tools):
        tools_map, manager = note_tools
        session = manager.create_session()

        class _HeldLock:
            def locked(self):
                return True

        manager._lock = lambda _sid: _HeldLock()
        result = tools_map["create_sticky_note"](session_id=session.id, x=0, y=0)
        assert result["success"] is False
        assert result["error"] == "busy"

    def test_invalid_session_id(self, note_tools):
        tools_map, _ = note_tools
        result = tools_map["create_sticky_note"](session_id="nope", x=0, y=0)
        assert result["success"] is False

    def test_unknown_session(self, note_tools):
        tools_map, _ = note_tools
        result = tools_map["create_sticky_note"](session_id="9999-9999", x=0, y=0)
        assert result["success"] is False

    def test_missing_manager_is_reported(self):
        storage = GraphStorage(json_path="/tmp/does-not-matter-notes2.json")
        service = GraphService(storage)
        mock_mcp = Mock()
        mock_mcp.tool = MagicMock(return_value=lambda f: f)
        tools_map = register_mcp_tools(mock_mcp, service)  # no session_manager
        result = tools_map["create_sticky_note"](session_id="1111-2222", x=0, y=0)
        assert result["success"] is False


class TestUpdateStickyNote:
    def test_partial_text_update_preserves_position_and_size(self, note_tools):
        tools_map, manager = note_tools
        session = manager.create_session()
        created = tools_map["create_sticky_note"](
            session_id=session.id, x=10, y=20, text="v1", w=300, h=150
        )
        note_id = created["note"]["id"]

        result = tools_map["update_sticky_note"](
            session_id=session.id, annotation_id=note_id, text="v2"
        )

        assert result["success"] is True
        assert result["note"]["text"] == "v2"
        assert result["note"]["x"] == 10 and result["note"]["y"] == 20
        assert result["note"]["w"] == 300 and result["note"]["h"] == 150

    def test_position_update_preserves_size(self, note_tools):
        tools_map, manager = note_tools
        session = manager.create_session()
        created = tools_map["create_sticky_note"](
            session_id=session.id, x=10, y=20, w=300, h=150
        )
        note_id = created["note"]["id"]

        result = tools_map["update_sticky_note"](
            session_id=session.id, annotation_id=note_id, x=99, y=88
        )

        assert result["note"]["x"] == 99 and result["note"]["y"] == 88
        assert result["note"]["w"] == 300 and result["note"]["h"] == 150

    def test_size_update_preserves_position(self, note_tools):
        tools_map, manager = note_tools
        session = manager.create_session()
        created = tools_map["create_sticky_note"](session_id=session.id, x=10, y=20)
        note_id = created["note"]["id"]

        result = tools_map["update_sticky_note"](
            session_id=session.id, annotation_id=note_id, w=500, h=400
        )

        assert result["note"]["w"] == 500 and result["note"]["h"] == 400
        assert result["note"]["x"] == 10 and result["note"]["y"] == 20

    def test_color_and_font_size_update(self, note_tools):
        tools_map, manager = note_tools
        session = manager.create_session()
        created = tools_map["create_sticky_note"](session_id=session.id, x=0, y=0)
        note_id = created["note"]["id"]

        result = tools_map["update_sticky_note"](
            session_id=session.id,
            annotation_id=note_id,
            color="#0000ff",
            font_size=22,
        )

        assert result["note"]["color"] == "#0000ff"
        assert result["note"]["font_size"] == 22

    def test_no_fields_given_is_an_error(self, note_tools):
        tools_map, manager = note_tools
        session = manager.create_session()
        created = tools_map["create_sticky_note"](session_id=session.id, x=0, y=0)

        result = tools_map["update_sticky_note"](
            session_id=session.id, annotation_id=created["note"]["id"]
        )

        assert result["success"] is False
        assert result["error"] == "no_fields_to_update"

    def test_unknown_annotation_id_is_not_found(self, note_tools):
        tools_map, manager = note_tools
        session = manager.create_session()

        result = tools_map["update_sticky_note"](
            session_id=session.id, annotation_id="ghost", text="x"
        )

        assert result["success"] is False
        assert result["error"] == "not_found"

    def test_non_note_annotation_id_is_not_found(self, note_tools):
        """A line annotation must not be editable through the sticky-note tools."""
        tools_map, manager = note_tools
        session = manager.create_session()
        manager.upsert_annotation(session.id, "mcp-agent", {"id": "line-1", "type": "line"})

        result = tools_map["update_sticky_note"](
            session_id=session.id, annotation_id="line-1", text="x"
        )

        assert result["success"] is False
        assert result["error"] == "not_found"

    def test_revision_conflict_is_reported(self, note_tools):
        tools_map, manager = note_tools
        session = manager.create_session()
        created = tools_map["create_sticky_note"](session_id=session.id, x=0, y=0)

        result = tools_map["update_sticky_note"](
            session_id=session.id,
            annotation_id=created["note"]["id"],
            text="x",
            expected_revision=0,
        )

        assert result["success"] is False
        assert result["error"] == "revision_conflict"

    def test_invalid_session_id(self, note_tools):
        tools_map, _ = note_tools
        result = tools_map["update_sticky_note"](
            session_id="nope", annotation_id="note-1", text="x"
        )
        assert result["success"] is False

    def test_unknown_session(self, note_tools):
        tools_map, _ = note_tools
        result = tools_map["update_sticky_note"](
            session_id="9999-9999", annotation_id="note-1", text="x"
        )
        assert result["success"] is False


class TestDeleteStickyNote:
    def test_deletes_existing_note(self, note_tools):
        tools_map, manager = note_tools
        session = manager.create_session()
        created = tools_map["create_sticky_note"](session_id=session.id, x=0, y=0)
        note_id = created["note"]["id"]

        result = tools_map["delete_sticky_note"](session_id=session.id, annotation_id=note_id)

        assert result["success"] is True
        assert result["annotation_id"] == note_id
        assert session.state["annotations"] == []

    def test_unknown_annotation_id_is_not_found(self, note_tools):
        tools_map, manager = note_tools
        session = manager.create_session()

        result = tools_map["delete_sticky_note"](session_id=session.id, annotation_id="ghost")

        assert result["success"] is False
        assert result["error"] == "not_found"

    def test_non_note_annotation_id_is_not_found(self, note_tools):
        tools_map, manager = note_tools
        session = manager.create_session()
        manager.upsert_annotation(session.id, "mcp-agent", {"id": "line-1", "type": "line"})

        result = tools_map["delete_sticky_note"](session_id=session.id, annotation_id="line-1")

        assert result["success"] is False
        assert result["error"] == "not_found"
        # The non-note annotation must survive an attempt to delete it as a note.
        assert len(session.state["annotations"]) == 1

    def test_revision_conflict_is_reported(self, note_tools):
        tools_map, manager = note_tools
        session = manager.create_session()
        created = tools_map["create_sticky_note"](session_id=session.id, x=0, y=0)

        result = tools_map["delete_sticky_note"](
            session_id=session.id,
            annotation_id=created["note"]["id"],
            expected_revision=0,
        )

        assert result["success"] is False
        assert result["error"] == "revision_conflict"
        # The rejected delete left the note in place.
        assert len(session.state["annotations"]) == 1

    def test_invalid_session_id(self, note_tools):
        tools_map, _ = note_tools
        result = tools_map["delete_sticky_note"](session_id="nope", annotation_id="note-1")
        assert result["success"] is False

    def test_unknown_session(self, note_tools):
        tools_map, _ = note_tools
        result = tools_map["delete_sticky_note"](
            session_id="9999-9999", annotation_id="note-1"
        )
        assert result["success"] is False


class TestStickyNoteToolsAuthorization:
    """The sticky-note tools must gate through the same authorization seam as
    the layout/session tools (see TestVisualizationToolsAuthorization in
    test_mcp_layout_tools.py for the read/mutate bypass this mirrors)."""

    def test_deny_all_blocks_list(self, note_tools, monkeypatch):
        tools_map, manager = note_tools
        session = manager.create_session()
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "deny-all")

        result = tools_map["list_sticky_notes"](session_id=session.id)

        assert result.get("error_code") == "access_denied"

    def test_deny_all_blocks_create(self, note_tools, monkeypatch):
        tools_map, manager = note_tools
        session = manager.create_session()
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "deny-all")

        result = tools_map["create_sticky_note"](session_id=session.id, x=0, y=0)

        assert result.get("error_code") == "access_denied"

    def test_read_only_blocks_create_and_creates_nothing(self, note_tools, monkeypatch):
        tools_map, manager = note_tools
        session = manager.create_session()
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "read-only")

        result = tools_map["create_sticky_note"](session_id=session.id, x=0, y=0)

        assert result["success"] is False
        assert result.get("error_code") == "access_denied"
        assert session.state["annotations"] == []

    def test_read_only_still_allows_list(self, note_tools, monkeypatch):
        tools_map, manager = note_tools
        session = manager.create_session()
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "read-only")

        result = tools_map["list_sticky_notes"](session_id=session.id)

        assert "error_code" not in result

    def test_permissive_default_allows_read_and_mutation(self, note_tools):
        tools_map, manager = note_tools
        session = manager.create_session()

        created = tools_map["create_sticky_note"](session_id=session.id, x=0, y=0)
        listed = tools_map["list_sticky_notes"](session_id=session.id)

        assert "error_code" not in created
        assert "error_code" not in listed
        assert len(listed["notes"]) == 1
