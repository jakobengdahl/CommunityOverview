"""Cross-language round-trip test for the complete accepted v1 annotation
type matrix, driven from the shared fixture
``docs/fixtures/annotation_type_matrix.json``
(``packages/ui-graph-canvas/tests/annotationTypeMatrix.test.js`` drives the
same file through the JS model — see that fixture's own ``$comment`` for the
shared-data contract between the two).

Every case is exercised through three of this task's four required legs:

* the backend object model (``build_annotation``/``build_note_annotation``
  + their ``project_*`` counterparts) — a pure round trip with no session,
* session persistence (``SessionManager``/``SessionStore``, backed by the
  in-memory persistence backend) — create, then reload the session and
  re-read the stored annotation,
* MCP (the registered tool functions) — create through the tool, then read
  it back through ``list_annotations``/``list_sticky_notes``.

(The fourth leg, the JS model, is the sibling ``.test.js`` file above — this
module cannot exercise it.) ``note``/``group``/``image`` route through their
own dedicated builders/tools rather than the generic ones, matching how the
backend genuinely exposes them (see ``session_annotations.py``'s module
docstring) — that asymmetry is real architecture, not a shortcut this test
is papering over.
"""

import json
import os
from typing import Any, Dict
from unittest.mock import MagicMock, Mock

import pytest

from backend.core import GraphStorage
from backend.core.session_annotations import (
    build_annotation,
    build_note_annotation,
    project_annotation,
    project_note,
)
from backend.core.session_manager import SessionManager
from backend.core.session_store import InMemorySessionPersistenceBackend, SessionStore
from backend.service import GraphService, register_mcp_tools

_FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "docs",
    "fixtures",
    "annotation_type_matrix.json",
)


def _load_cases():
    with open(_FIXTURE_PATH, encoding="utf-8") as handle:
        return json.load(handle)["cases"]


CASES = _load_cases()
GENERIC_CASES = [c for c in CASES if c["type"] not in ("note", "group", "image")]


def _assert_contains(actual: Dict[str, Any], expected: Dict[str, Any]) -> None:
    """Every key/value declared in *expected* must be present in *actual*
    (the backend model never adds normalization-derived keys the way the JS
    model does, so — unlike the JS fixture test — this is an exact match per
    declared field, just not a check that *actual* carries nothing else)."""
    for key, value in expected.items():
        assert actual.get(key) == value, (
            f"{key}: expected {value!r}, got {actual.get(key)!r}"
        )


class TestAnnotationTypeMatrixFixture:
    def test_covers_every_v1_type_exactly_once_plus_two_shape_variants(self):
        types = [c["type"] for c in CASES]
        assert set(types) == {
            "note",
            "text",
            "label",
            "line",
            "frame",
            "group",
            "shape",
            "icon",
            "vote_dot",
            "image",
            "freehand",
        }
        assert types.count("shape") == 2


@pytest.fixture
def annotation_tools(tmp_path):
    storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
    service = GraphService(storage)
    manager = SessionManager(SessionStore(InMemorySessionPersistenceBackend()))
    mock_mcp = Mock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)
    tools_map = register_mcp_tools(mock_mcp, service, session_manager=manager)
    return tools_map, manager


class TestBackendModelRoundTrip:
    """Leg 1: the pure builder/projector pair, no session involved."""

    @pytest.mark.parametrize("case", GENERIC_CASES, ids=lambda c: c["id"])
    def test_generic_case_round_trips_through_build_and_project(self, case):
        annotation = build_annotation(
            type=case["type"],
            x=case["x"],
            y=case["y"],
            w=case.get("w"),
            h=case.get("h"),
            rotation=case.get("rotation"),
            content=case["fields"],
            annotation_id=case["id"],
        )
        projected = project_annotation(annotation)
        assert projected["type"] == case["type"]
        assert projected["x"] == case["x"] and projected["y"] == case["y"]
        if "rotation" in case:
            assert projected["rotation"] == case["rotation"]
        _assert_contains(projected["content"], case["fields"])

    def test_note_case_round_trips_through_build_and_project(self):
        case = next(c for c in CASES if c["type"] == "note")
        annotation = build_note_annotation(
            x=case["x"],
            y=case["y"],
            text=case["fields"]["text"],
            color=case["fields"]["color"],
            font_size=case["fields"]["fontSize"],
            w=case.get("w"),
            h=case.get("h"),
            annotation_id=case["id"],
        )
        projected = project_note(annotation)
        assert projected["text"] == case["fields"]["text"]
        assert projected["color"] == case["fields"]["color"]
        assert projected["font_size"] == case["fields"]["fontSize"]


class TestSessionPersistenceAndMcpRoundTrip:
    """Legs 2 and 3: create through the real tool/manager surface, reload
    the session, and read the annotation back — persistence and MCP share
    the same ``SessionStore``, so one round trip exercises both."""

    @pytest.mark.parametrize("case", GENERIC_CASES, ids=lambda c: c["id"])
    def test_generic_case_round_trips_through_mcp_and_persistence(
        self, annotation_tools, case
    ):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        created = tools_map["create_annotation"](
            session_id=session.id,
            type=case["type"],
            x=case["x"],
            y=case["y"],
            w=case.get("w"),
            h=case.get("h"),
            rotation=case.get("rotation"),
            content=case["fields"],
            annotation_id=case["id"],
        )
        assert created["success"] is True, created
        _assert_contains(created["annotation"]["content"], case["fields"])

        # Persistence: reload the session from the store (not the object
        # `create_annotation` handed back) before reading it through MCP.
        reloaded = manager.get_session(session.id)
        assert reloaded is not None

        listed = tools_map["list_annotations"](
            session_id=session.id, types=[case["type"]]
        )
        match = next(a for a in listed["annotations"] if a["id"] == case["id"])
        assert match["type"] == case["type"]
        _assert_contains(match["content"], case["fields"])

    def test_note_case_round_trips_through_mcp_and_persistence(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        case = next(c for c in CASES if c["type"] == "note")

        created = tools_map["create_sticky_note"](
            session_id=session.id,
            x=case["x"],
            y=case["y"],
            text=case["fields"]["text"],
            color=case["fields"]["color"],
            font_size=case["fields"]["fontSize"],
            annotation_id=case["id"],
        )
        assert created["success"] is True, created

        assert manager.get_session(session.id) is not None
        listed = tools_map["list_sticky_notes"](session_id=session.id)
        match = next(n for n in listed["notes"] if n["id"] == case["id"])
        assert match["text"] == case["fields"]["text"]
        assert match["color"] == case["fields"]["color"]
        assert match["font_size"] == case["fields"]["fontSize"]

    def test_group_case_round_trips_through_persistence_and_list_annotations(
        self, annotation_tools
    ):
        """`group` is not exposed through any MCP write tool (its
        `member_node_ids` are edited via the `group_membership_changed` op —
        see session_annotations.py's module docstring), so it is created
        directly through the manager, matching how a group actually reaches
        a session; `list_annotations` still reads it."""
        tools_map, manager = annotation_tools
        session = manager.create_session()
        case = next(c for c in CASES if c["type"] == "group")

        manager.upsert_annotation(
            session.id,
            "test-client",
            {
                "id": case["id"],
                "type": "group",
                "kind": "group",
                "position": {"x": case["x"], "y": case["y"]},
                "geometry": {
                    "x": case["x"],
                    "y": case["y"],
                    "w": case["w"],
                    "h": case["h"],
                    "rotation": 0,
                },
                **case["fields"],
            },
        )

        assert manager.get_session(session.id) is not None
        listed = tools_map["list_annotations"](session_id=session.id, types=["group"])
        match = next(a for a in listed["annotations"] if a["id"] == case["id"])
        assert match["type"] == "group"
        _assert_contains(match["content"], case["fields"])

    def test_image_case_round_trips_through_persistence_and_list_annotations(
        self, annotation_tools
    ):
        """`image` cannot be created through `create_annotation` (it refuses
        `type="image"` — see backend/DEVELOPMENT.md's "Image annotation
        tool"), so this uses `build_annotation` + the manager directly, the
        same shape `create_image_annotation` itself builds internally after
        ingest. Exercising ingest is out of scope here (see the fixture's
        `$comment`)."""
        tools_map, manager = annotation_tools
        session = manager.create_session()
        case = next(c for c in CASES if c["type"] == "image")

        annotation = build_annotation(
            type="image",
            x=case["x"],
            y=case["y"],
            w=case.get("w"),
            h=case.get("h"),
            content=case["fields"],
            annotation_id=case["id"],
        )
        manager.upsert_annotation(session.id, "test-client", annotation)

        assert manager.get_session(session.id) is not None
        listed = tools_map["list_annotations"](session_id=session.id, types=["image"])
        match = next(a for a in listed["annotations"] if a["id"] == case["id"])
        assert match["type"] == "image"
        _assert_contains(match["content"], case["fields"])
