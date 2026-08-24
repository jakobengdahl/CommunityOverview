"""Tests for the group MCP annotation tools (``create_group_annotation``,
``update_group_members``) registered in ``backend/service/mcp_tools.py``.

These are the MCP surface for ``group`` (node-membership box) annotations,
closing the "group boxes and membership changes through governed MCP
operations" item of ``task-mcp-full-annotation-crud`` — before this, `group`
had no MCP tool at all (docs/ANNOTATION_CONTRACT.md), only a GUI
toolbar action and the raw ``group_membership_changed`` op. `create_annotation`
still refuses `type="group"` (see ``test_mcp_generic_annotation_tools.py``);
these two tools are the only MCP entry point for the type.
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


class TestCreateGroupAnnotation:
    def test_creates_with_members(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["create_group_annotation"](
            session_id=session.id,
            x=10,
            y=20,
            label="Team A",
            description="the founding team",
            color="#ff0000",
            member_node_ids=["n1", "n2"],
        )

        assert result["success"] is True
        group = result["group"]
        assert group["type"] == "group"
        assert group["x"] == 10 and group["y"] == 20
        assert group["content"]["label"] == "Team A"
        assert group["content"]["description"] == "the founding team"
        assert group["content"]["color"] == "#ff0000"
        assert group["content"]["member_node_ids"] == ["n1", "n2"]
        assert group["style"] == {"color": "#ff0000"}

    def test_default_size_when_omitted(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["create_group_annotation"](session_id=session.id, x=0, y=0)

        assert result["group"]["w"] == 320
        assert result["group"]["h"] == 200
        # member_node_ids is omitted rather than defaulted to [] on the stored
        # annotation (see build_group_annotation's docstring) — the canvas and
        # the frontend model both already treat an absent list as empty.
        assert result["group"]["content"].get("member_node_ids", []) == []

    def test_explicit_id_is_returned_and_reusable(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["create_group_annotation"](
            session_id=session.id, x=0, y=0, annotation_id="group-1"
        )

        assert result["group"]["id"] == "group-1"

    def test_recreating_with_same_id_replaces_it_wrong_type_refused(
        self, annotation_tools
    ):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        tools_map["create_sticky_note"](
            session_id=session.id, x=0, y=0, annotation_id="note-1"
        )

        result = tools_map["create_group_annotation"](
            session_id=session.id, x=0, y=0, annotation_id="note-1"
        )

        assert result["success"] is False
        assert result["error"] == "wrong_type"
        assert session.state["annotations"][0]["type"] == "note"

    def test_upsert_without_member_node_ids_preserves_current_membership(
        self, annotation_tools
    ):
        """Recreating a group by id to change its label must not silently
        wipe out membership set separately via update_group_members —
        build_group_annotation only writes member_node_ids when given."""
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_group_annotation"](
            session_id=session.id,
            x=0,
            y=0,
            annotation_id="group-1",
            member_node_ids=["n1", "n2"],
        )
        assert created["group"]["content"]["member_node_ids"] == ["n1", "n2"]

        renamed = tools_map["create_group_annotation"](
            session_id=session.id,
            x=5,
            y=5,
            annotation_id="group-1",
            label="Renamed",
        )

        assert renamed["success"] is True
        assert renamed["group"]["content"]["label"] == "Renamed"
        assert renamed["group"]["content"]["member_node_ids"] == ["n1", "n2"]

    def test_upsert_with_explicit_member_node_ids_replaces_membership(
        self, annotation_tools
    ):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        tools_map["create_group_annotation"](
            session_id=session.id,
            x=0,
            y=0,
            annotation_id="group-1",
            member_node_ids=["n1", "n2"],
        )

        result = tools_map["create_group_annotation"](
            session_id=session.id,
            x=0,
            y=0,
            annotation_id="group-1",
            member_node_ids=["n3"],
        )

        assert result["group"]["content"]["member_node_ids"] == ["n3"]

    def test_non_list_member_node_ids_is_invalid_content(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["create_group_annotation"](
            session_id=session.id, x=0, y=0, member_node_ids="n1"
        )

        assert result["success"] is False
        assert result["error"] == "invalid_content"
        assert session.state["annotations"] == []

    def test_revision_conflict_is_reported(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["create_group_annotation"](
            session_id=session.id, x=0, y=0, expected_revision=5
        )

        assert result["success"] is False
        assert result["error"] == "revision_conflict"

    def test_session_not_found(self, annotation_tools):
        tools_map, _manager = annotation_tools

        result = tools_map["create_group_annotation"](
            session_id="0000-0000-0000-0000", x=0, y=0
        )

        assert result["success"] is False
        assert "not found" in result["error"]


class TestUpdateGroupMembers:
    def test_adds_members(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_group_annotation"](
            session_id=session.id, x=0, y=0, member_node_ids=["n1"]
        )
        gid = created["group"]["id"]

        result = tools_map["update_group_members"](
            session_id=session.id, group_id=gid, add_member_node_ids=["n2", "n3"]
        )

        assert result["success"] is True
        assert result["member_node_ids"] == ["n1", "n2", "n3"]
        assert result["group"]["content"]["member_node_ids"] == ["n1", "n2", "n3"]

    def test_removes_members(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_group_annotation"](
            session_id=session.id, x=0, y=0, member_node_ids=["n1", "n2", "n3"]
        )
        gid = created["group"]["id"]

        result = tools_map["update_group_members"](
            session_id=session.id, group_id=gid, remove_member_node_ids=["n2"]
        )

        assert result["success"] is True
        assert result["member_node_ids"] == ["n1", "n3"]

    def test_add_and_remove_in_one_call(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_group_annotation"](
            session_id=session.id, x=0, y=0, member_node_ids=["n1", "n2"]
        )
        gid = created["group"]["id"]

        result = tools_map["update_group_members"](
            session_id=session.id,
            group_id=gid,
            add_member_node_ids=["n3"],
            remove_member_node_ids=["n1"],
        )

        assert result["member_node_ids"] == ["n2", "n3"]

    def test_adding_an_existing_member_is_a_noop_not_a_duplicate(
        self, annotation_tools
    ):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_group_annotation"](
            session_id=session.id, x=0, y=0, member_node_ids=["n1"]
        )
        gid = created["group"]["id"]

        result = tools_map["update_group_members"](
            session_id=session.id, group_id=gid, add_member_node_ids=["n1"]
        )

        assert result["member_node_ids"] == ["n1"]

    def test_removing_an_absent_member_is_not_an_error(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_group_annotation"](
            session_id=session.id, x=0, y=0, member_node_ids=["n1"]
        )
        gid = created["group"]["id"]

        result = tools_map["update_group_members"](
            session_id=session.id, group_id=gid, remove_member_node_ids=["ghost"]
        )

        assert result["success"] is True
        assert result["member_node_ids"] == ["n1"]

    def test_no_fields_given_is_an_error(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_group_annotation"](session_id=session.id, x=0, y=0)

        result = tools_map["update_group_members"](
            session_id=session.id, group_id=created["group"]["id"]
        )

        assert result["success"] is False
        assert result["error"] == "no_fields_to_update"

    def test_unknown_group_id_is_not_found(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["update_group_members"](
            session_id=session.id, group_id="ghost", add_member_node_ids=["n1"]
        )

        assert result["success"] is False
        assert result["error"] == "not_found"

    def test_non_group_id_is_not_found(self, annotation_tools):
        """A note or generic-type id must not be editable as a group."""
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id, type="label", x=0, y=0
        )

        result = tools_map["update_group_members"](
            session_id=session.id,
            group_id=created["annotation"]["id"],
            add_member_node_ids=["n1"],
        )

        assert result["success"] is False
        assert result["error"] == "not_found"

    def test_invalid_add_list_is_invalid_content(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_group_annotation"](session_id=session.id, x=0, y=0)

        result = tools_map["update_group_members"](
            session_id=session.id,
            group_id=created["group"]["id"],
            add_member_node_ids=[1, 2],
        )

        assert result["success"] is False
        assert result["error"] == "invalid_content"

    def test_revision_conflict_is_reported(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_group_annotation"](session_id=session.id, x=0, y=0)

        result = tools_map["update_group_members"](
            session_id=session.id,
            group_id=created["group"]["id"],
            add_member_node_ids=["n1"],
            expected_revision=0,
        )

        assert result["success"] is False
        assert result["error"] == "revision_conflict"

    def test_two_sequential_calls_each_add_a_different_member(
        self, annotation_tools
    ):
        """Each call re-reads the group's current membership at call time,
        so a second add call (issued after the first has returned) sees the
        first call's result instead of a stale snapshot fetched earlier.
        This is not a concurrency guarantee — see update_group_members's
        docstring for the last-write-wins rule under a genuine race."""
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_group_annotation"](session_id=session.id, x=0, y=0)
        gid = created["group"]["id"]

        tools_map["update_group_members"](
            session_id=session.id, group_id=gid, add_member_node_ids=["n1"]
        )
        result = tools_map["update_group_members"](
            session_id=session.id, group_id=gid, add_member_node_ids=["n2"]
        )

        assert result["member_node_ids"] == ["n1", "n2"]


class TestListAnnotationsIncludesGroups:
    def test_group_appears_in_list_annotations_with_full_content(
        self, annotation_tools
    ):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        tools_map["create_group_annotation"](
            session_id=session.id,
            x=1,
            y=2,
            label="Team A",
            member_node_ids=["n1"],
            annotation_id="group-1",
        )

        listed = tools_map["list_annotations"](session_id=session.id)

        groups = [a for a in listed["annotations"] if a["type"] == "group"]
        assert len(groups) == 1
        assert groups[0]["id"] == "group-1"
        assert groups[0]["content"]["label"] == "Team A"
        assert groups[0]["content"]["member_node_ids"] == ["n1"]

    def test_filters_to_group_type_only(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        tools_map["create_group_annotation"](session_id=session.id, x=0, y=0)
        tools_map["create_annotation"](session_id=session.id, type="label", x=0, y=0)

        listed = tools_map["list_annotations"](session_id=session.id, types=["group"])

        assert {a["type"] for a in listed["annotations"]} == {"group"}


class TestGroupAnnotationToolsAuthorization:
    """Same authorization seam as the other generic annotation tools (see
    TestGenericAnnotationToolsAuthorization in
    test_mcp_generic_annotation_tools.py)."""

    def test_deny_all_blocks_create(self, annotation_tools, monkeypatch):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "deny-all")

        result = tools_map["create_group_annotation"](session_id=session.id, x=0, y=0)

        assert result.get("error_code") == "access_denied"
        assert session.state["annotations"] == []

    def test_read_only_blocks_create_and_creates_nothing(
        self, annotation_tools, monkeypatch
    ):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "read-only")

        result = tools_map["create_group_annotation"](session_id=session.id, x=0, y=0)

        assert result["success"] is False
        assert result.get("error_code") == "access_denied"
        assert session.state["annotations"] == []

    def test_deny_all_blocks_update_group_members(self, annotation_tools, monkeypatch):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_group_annotation"](session_id=session.id, x=0, y=0)
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "deny-all")

        result = tools_map["update_group_members"](
            session_id=session.id,
            group_id=created["group"]["id"],
            add_member_node_ids=["n1"],
        )

        assert result.get("error_code") == "access_denied"

    def test_read_only_blocks_update_group_members_and_changes_nothing(
        self, annotation_tools, monkeypatch
    ):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_group_annotation"](
            session_id=session.id, x=0, y=0, member_node_ids=["n0"]
        )
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "read-only")

        result = tools_map["update_group_members"](
            session_id=session.id,
            group_id=created["group"]["id"],
            add_member_node_ids=["n1"],
        )

        assert result["success"] is False
        assert result.get("error_code") == "access_denied"
        assert session.state["annotations"][0]["member_node_ids"] == ["n0"]
