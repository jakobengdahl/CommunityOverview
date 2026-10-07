"""The MCP surface for `reference` annotations.

A reference is one of the generic types, so it is created, moved, renamed,
reordered, locked, duplicated and deleted through the generic tool set like
every other one (``test_mcp_generic_annotation_tools.py`` covers that set's
mechanics). What is specific to a reference, and covered here, is:

* the tool layer reporting an unsafe-scheme target as ``invalid_content``
  rather than letting it reach storage — the gate itself and its other write
  paths are pinned in
  ``backend/core/tests/test_session_annotations_reference.py``,
* a partial update being genuinely partial (rename without resending the
  target, repoint without resending the label),
* a move preserving the payload,
* ``search_reference_target_sessions``, the discovery half of a session
  reference: what it matches on, what it excludes, and that it returns
  sessions and nothing else.
"""

import os
from unittest.mock import MagicMock, Mock

import pytest

from backend.core import GraphStorage
from backend.core.session_manager import SessionManager
from backend.core.session_store import InMemorySessionPersistenceBackend, SessionStore
from backend.service import GraphService, register_mcp_tools

UNSAFE_TARGETS = [
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "file:///etc/passwd",
    "vbscript:msgbox(1)",
    "java\tscript:alert(1)",
    "//evil.example/x",
    "/admin",
]


@pytest.fixture
def annotation_tools(tmp_path):
    storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
    service = GraphService(storage)
    manager = SessionManager(SessionStore(InMemorySessionPersistenceBackend()))
    mock_mcp = Mock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)
    tools_map = register_mcp_tools(mock_mcp, service, session_manager=manager)
    return tools_map, manager


def _create(tools_map, session_id, **content):
    return tools_map["create_annotation"](
        session_id=session_id,
        type="reference",
        x=10,
        y=20,
        content=content,
    )


class TestCreate:
    @pytest.mark.parametrize(
        "target_kind,target",
        [
            ("session", "8244-1742-3391-0057"),
            ("url", "https://example.org/handbook"),
            ("resource", "resource-method-guide"),
        ],
    )
    def test_each_target_kind_can_be_created(
        self, annotation_tools, target_kind, target
    ):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = _create(
            tools_map, session.id, target_kind=target_kind, target=target, label="L"
        )

        assert result["success"] is True, result
        annotation = result["annotation"]
        assert annotation["type"] == "reference"
        assert annotation["content"]["target_kind"] == target_kind
        assert annotation["content"]["target"] == target
        assert annotation["content"]["label"] == "L"

    def test_a_fresh_reference_gets_a_default_box(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = _create(tools_map, session.id, target_kind="resource", target="r1")

        assert result["annotation"]["w"] == 220
        assert result["annotation"]["h"] == 72

    def test_an_optional_preview_round_trips(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = _create(
            tools_map,
            session.id,
            target_kind="url",
            target="https://example.org",
            preview={"title": "Handbook", "site": "example.org"},
        )

        assert result["annotation"]["content"]["preview"] == {
            "title": "Handbook",
            "site": "example.org",
        }

    @pytest.mark.parametrize("target", UNSAFE_TARGETS)
    def test_an_unsafe_url_target_is_invalid_content(self, annotation_tools, target):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = _create(tools_map, session.id, target_kind="url", target=target)

        assert result["success"] is False
        assert result["error"] == "invalid_content"
        # And nothing reached storage.
        assert tools_map["list_annotations"](session_id=session.id)["annotations"] == []

    def test_a_create_missing_the_target_kind_is_refused(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = _create(tools_map, session.id, target="javascript:alert(1)")

        assert result["success"] is False
        assert result["error"] == "invalid_content"
        assert tools_map["list_annotations"](session_id=session.id)["annotations"] == []

    def test_a_create_missing_the_target_is_refused(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = _create(tools_map, session.id, target_kind="url")

        assert result["success"] is False
        assert result["error"] == "invalid_content"

    def test_an_unknown_target_kind_is_refused(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = _create(tools_map, session.id, target_kind="graph_node", target="x")

        assert result["success"] is False
        assert result["error"] == "invalid_content"

    def test_a_preview_field_outside_the_documented_set_is_refused(
        self, annotation_tools
    ):
        # The closed preview set is what keeps a reference from carrying a
        # remote URL every viewer's browser would fetch on open.
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = _create(
            tools_map,
            session.id,
            target_kind="url",
            target="https://example.org",
            preview={"image_url": "https://evil.example/x.png"},
        )

        assert result["success"] is False
        assert result["error"] == "invalid_content"

    def test_reference_is_offered_by_the_invalid_type_message(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["create_annotation"](
            session_id=session.id, type="not-a-type", x=0, y=0
        )

        assert result["error"] == "invalid_type"
        assert "reference" in result["message"]


class TestUpdate:
    def test_renaming_does_not_require_resending_the_target(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = _create(
            tools_map,
            session.id,
            target_kind="url",
            target="https://example.org/handbook",
            label="Old",
        )
        annotation_id = created["annotation"]["id"]

        result = tools_map["update_annotation"](
            session_id=session.id,
            annotation_id=annotation_id,
            content={"label": "New"},
        )

        assert result["success"] is True, result
        content = result["annotation"]["content"]
        assert content["label"] == "New"
        assert content["target"] == "https://example.org/handbook"
        assert content["target_kind"] == "url"

    def test_repointing_does_not_require_resending_the_label(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = _create(
            tools_map,
            session.id,
            target_kind="url",
            target="https://example.org/a",
            label="Keep me",
        )

        result = tools_map["update_annotation"](
            session_id=session.id,
            annotation_id=created["annotation"]["id"],
            content={"target": "https://example.org/b"},
        )

        assert result["success"] is True, result
        content = result["annotation"]["content"]
        assert content["target"] == "https://example.org/b"
        assert content["label"] == "Keep me"

    @pytest.mark.parametrize("target", UNSAFE_TARGETS)
    def test_an_unsafe_repoint_is_refused_without_resending_the_kind(
        self, annotation_tools, target
    ):
        # The patch carries no `target_kind`, so the gate has to read the
        # stored one. If it did not, this is the call that would get through.
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = _create(
            tools_map,
            session.id,
            target_kind="url",
            target="https://example.org",
        )

        result = tools_map["update_annotation"](
            session_id=session.id,
            annotation_id=created["annotation"]["id"],
            content={"target": target},
        )

        assert result["success"] is False
        assert result["error"] == "invalid_content"
        stored = tools_map["list_annotations"](session_id=session.id)["annotations"][0]
        assert stored["content"]["target"] == "https://example.org"

    def test_switching_a_session_reference_to_an_unsafe_url_is_refused(
        self, annotation_tools
    ):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = _create(
            tools_map, session.id, target_kind="session", target="8244-1742"
        )

        result = tools_map["update_annotation"](
            session_id=session.id,
            annotation_id=created["annotation"]["id"],
            content={"target_kind": "url", "target": "javascript:alert(1)"},
        )

        assert result["success"] is False
        assert result["error"] == "invalid_content"

    def test_switching_a_session_reference_to_a_safe_url_is_allowed(
        self, annotation_tools
    ):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = _create(
            tools_map, session.id, target_kind="session", target="8244-1742"
        )

        result = tools_map["update_annotation"](
            session_id=session.id,
            annotation_id=created["annotation"]["id"],
            content={"target_kind": "url", "target": "https://example.org"},
        )

        assert result["success"] is True, result
        assert result["annotation"]["content"]["target_kind"] == "url"
        assert result["annotation"]["content"]["target"] == "https://example.org"

    def test_a_move_preserves_the_payload_and_the_size(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id,
            type="reference",
            x=0,
            y=0,
            w=260,
            h=90,
            content={
                "target_kind": "session",
                "target": "8244-1742",
                "label": "Overview",
                "icon": "flag",
                "preview": {"title": "Overview"},
            },
        )

        result = tools_map["update_annotation"](
            session_id=session.id,
            annotation_id=created["annotation"]["id"],
            x=400,
            y=500,
        )

        assert result["success"] is True, result
        annotation = result["annotation"]
        assert (annotation["x"], annotation["y"]) == (400, 500)
        assert (annotation["w"], annotation["h"]) == (260, 90)
        assert annotation["content"]["target"] == "8244-1742"
        assert annotation["content"]["label"] == "Overview"
        assert annotation["content"]["icon"] == "flag"
        assert annotation["content"]["preview"] == {"title": "Overview"}


class TestDeleteAndDuplicate:
    def test_a_reference_can_be_deleted(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = _create(tools_map, session.id, target_kind="resource", target="r1")

        result = tools_map["delete_annotation"](
            session_id=session.id, annotation_id=created["annotation"]["id"]
        )

        assert result["success"] is True, result
        assert tools_map["list_annotations"](session_id=session.id)["annotations"] == []

    def test_a_duplicate_carries_the_payload_to_the_new_id(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = _create(
            tools_map,
            session.id,
            target_kind="url",
            target="https://example.org",
            label="Handbook",
        )

        result = tools_map["duplicate_annotation"](
            session_id=session.id, annotation_id=created["annotation"]["id"]
        )

        assert result["success"] is True, result
        copy = result["annotation"]
        assert copy["id"] != created["annotation"]["id"]
        assert copy["content"]["target"] == "https://example.org"
        assert copy["content"]["label"] == "Handbook"


class TestSearchReferenceTargetSessions:
    def test_lists_candidates_with_no_query(self, annotation_tools):
        tools_map, manager = annotation_tools
        a = manager.create_session()
        b = manager.create_session()

        result = tools_map["search_reference_target_sessions"]()

        assert result["success"] is True, result
        ids = {s["session_id"] for s in result["sessions"]}
        assert {a.id, b.id} <= ids
        assert result["count"] == len(result["sessions"])
        assert result["total_matches"] >= 2

    def test_a_returned_candidate_is_usable_as_a_reference_target(
        self, annotation_tools
    ):
        # The whole point of the tool: its `session_id` goes straight into a
        # reference's `content.target`.
        tools_map, manager = annotation_tools
        host = manager.create_session()
        target = manager.create_session()

        found = tools_map["search_reference_target_sessions"](
            exclude_session_id=host.id
        )
        candidate = next(s for s in found["sessions"] if s["session_id"] == target.id)

        created = _create(
            tools_map,
            host.id,
            target_kind="session",
            target=candidate["session_id"],
        )

        assert created["success"] is True, created
        assert created["annotation"]["content"]["target"] == target.id

    def test_matches_on_a_partial_id(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        needle = session.id[:4]
        result = tools_map["search_reference_target_sessions"](query=needle)

        assert session.id in {s["session_id"] for s in result["sessions"]}

    def test_matches_on_a_partial_name_case_insensitively(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        manager.rename_session_sync(session.id, "Programme Overview")

        result = tools_map["search_reference_target_sessions"](query="programme")

        assert session.id in {s["session_id"] for s in result["sessions"]}

    def test_a_query_matching_nothing_returns_nothing(self, annotation_tools):
        tools_map, manager = annotation_tools
        manager.create_session()

        result = tools_map["search_reference_target_sessions"](
            query="no-session-is-named-this"
        )

        assert result["success"] is True
        assert result["sessions"] == []
        assert result["count"] == 0
        assert result["total_matches"] == 0

    def test_the_excluded_session_is_never_returned(self, annotation_tools):
        # A tile pointing at the session it sits in goes nowhere.
        tools_map, manager = annotation_tools
        host = manager.create_session()
        other = manager.create_session()

        result = tools_map["search_reference_target_sessions"](
            exclude_session_id=host.id
        )

        ids = {s["session_id"] for s in result["sessions"]}
        assert host.id not in ids
        assert other.id in ids

    def test_the_exclusion_also_applies_when_it_matches_the_query(
        self, annotation_tools
    ):
        tools_map, manager = annotation_tools
        host = manager.create_session()

        result = tools_map["search_reference_target_sessions"](
            query=host.id, exclude_session_id=host.id
        )

        assert result["sessions"] == []

    def test_limit_caps_the_results_but_total_matches_reports_the_truth(
        self, annotation_tools
    ):
        tools_map, manager = annotation_tools
        for _ in range(5):
            manager.create_session()

        result = tools_map["search_reference_target_sessions"](limit=2)

        assert result["count"] == 2
        assert len(result["sessions"]) == 2
        assert result["total_matches"] >= 5

    @pytest.mark.parametrize("limit", [0, -1, 101, 1.5, "10", None, True])
    def test_an_out_of_range_limit_is_refused(self, annotation_tools, limit):
        tools_map, manager = annotation_tools
        manager.create_session()

        result = tools_map["search_reference_target_sessions"](limit=limit)

        assert result["success"] is False
        assert result["error"] == "invalid_limit"

    def test_every_result_is_a_session_projection(self, annotation_tools):
        # It reads the session index and nothing else, so it can never offer a
        # graph node as a session candidate.
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["search_reference_target_sessions"]()

        assert result["sessions"]
        for projection in result["sessions"]:
            assert set(projection) >= {
                "session_id",
                "name",
                "lifecycle_state",
                "revision",
            }
        assert session.id in {s["session_id"] for s in result["sessions"]}

    def test_a_blank_query_is_treated_as_no_query(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["search_reference_target_sessions"](query="   ")

        assert session.id in {s["session_id"] for s in result["sessions"]}
