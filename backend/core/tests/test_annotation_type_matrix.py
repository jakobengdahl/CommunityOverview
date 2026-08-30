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

    def test_group_case_round_trips_through_mcp_and_persistence(self, annotation_tools):
        """`group` has its own dedicated MCP tool set
        (`create_group_annotation`/`update_group_members`,
        task-mcp-full-annotation-crud) — this drives the fixture's group
        case through `create_group_annotation` rather than the manager
        directly, matching how a group actually reaches a session over MCP
        now that the tool exists."""
        tools_map, manager = annotation_tools
        session = manager.create_session()
        case = next(c for c in CASES if c["type"] == "group")

        created = tools_map["create_group_annotation"](
            session_id=session.id,
            x=case["x"],
            y=case["y"],
            w=case.get("w"),
            h=case.get("h"),
            label=case["fields"]["label"],
            member_node_ids=case["fields"]["member_node_ids"],
            annotation_id=case["id"],
        )

        assert created["success"] is True
        _assert_contains(created["group"]["content"], case["fields"])
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


class TestSequentialWritesDoNotRollBackEarlierSuccesses:
    """task-mcp-full-annotation-crud's "batch create/update ... partial
    success" requirement: one MCP write is one independent tool call over
    one annotation (there is no multi-item batch envelope on any create/
    update tool — the atomic, all-or-nothing unit is the raw
    ``apply_ops``/``POST /sessions/{id}/ops`` op batch, a different and
    deliberately *non*-partial-success mechanism pinned by
    ``test_a_poisoned_op_rolls_back_the_whole_batch`` in
    ``backend/core/tests/test_session_annotations_image_guard.py``). So "a
    batch where some entries are invalid saves the valid ones and returns a
    per-object error for the rest" is a property of a *sequence* of
    independent create/update calls, one per v1 type: a later call's error
    must never undo an earlier call's success, and the failing call's own
    error must be reported on its own response rather than swallowed.

    This drives one such sequence across the complete v1 type set — the
    original non-note types PR #415 covered (`create_annotation`'s
    text/label/line/shape/icon/vote_dot) plus every type this task
    added or extended MCP coverage for: `note`, `group`, `image`, and
    `freehand`. Each type contributes one guaranteed-valid create
    (interleaved with one guaranteed-invalid create for a *different*
    reason per type — invalid_content, wrong_type, revision_conflict,
    invalid_source — so the property holds regardless of which failure kind
    is in play) and the running state is checked after every single call.
    """

    def test_valid_creates_across_the_full_type_matrix_survive_interleaved_failures(
        self, annotation_tools
    ):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        confirmed_ids: list[str] = []

        def _assert_only_confirmed_ids_present():
            stored_ids = {a["id"] for a in session.state["annotations"]}
            assert stored_ids == set(confirmed_ids), (
                f"expected exactly {confirmed_ids}, session holds {sorted(stored_ids)}"
            )

        # note: valid create, then an invalid create that collides an id
        # already used by a different type (wrong_type).
        ok = tools_map["create_sticky_note"](
            session_id=session.id, x=0, y=0, text="hi", annotation_id="note-1"
        )
        assert ok["success"] is True
        confirmed_ids.append("note-1")
        _assert_only_confirmed_ids_present()

        # A different tool's create attempting to reuse note-1's id is the
        # per-object failure here (wrong_type) — create_sticky_note itself
        # would treat a repeat of the same id/type as an idempotent upsert,
        # not a failure, so it would not exercise this property at all.
        bad = tools_map["create_group_annotation"](
            session_id=session.id, x=0, y=0, annotation_id="note-1"
        )
        assert bad["success"] is False
        assert bad["error"] == "wrong_type"
        _assert_only_confirmed_ids_present()

        # text: valid create, then invalid_content (malformed attachment).
        ok = tools_map["create_annotation"](
            session_id=session.id, type="text", x=0, y=0, annotation_id="text-1"
        )
        assert ok["success"] is True
        confirmed_ids.append("text-1")
        _assert_only_confirmed_ids_present()

        bad = tools_map["create_annotation"](
            session_id=session.id,
            type="text",
            x=0,
            y=0,
            annotation_id="text-bad",
            content={"attachment": {"anchor": "top"}},  # missing target_id
        )
        assert bad["success"] is False
        assert bad["error"] == "invalid_content"
        _assert_only_confirmed_ids_present()

        # label: same invalid_content shape as text.
        ok = tools_map["create_annotation"](
            session_id=session.id, type="label", x=0, y=0, annotation_id="label-1"
        )
        assert ok["success"] is True
        confirmed_ids.append("label-1")
        _assert_only_confirmed_ids_present()

        bad = tools_map["create_annotation"](
            session_id=session.id,
            type="label",
            x=0,
            y=0,
            annotation_id="label-bad",
            content={"attachment": {"target_id": ""}},
        )
        assert bad["success"] is False
        assert bad["error"] == "invalid_content"
        _assert_only_confirmed_ids_present()

        # line: valid create, then a malformed endpoint.
        ok = tools_map["create_annotation"](
            session_id=session.id,
            type="line",
            x=0,
            y=0,
            annotation_id="line-1",
            content={"to": {"x": 100, "y": 0}},
        )
        assert ok["success"] is True
        confirmed_ids.append("line-1")
        _assert_only_confirmed_ids_present()

        bad = tools_map["create_annotation"](
            session_id=session.id,
            type="line",
            x=0,
            y=0,
            annotation_id="line-bad",
            content={"start": {"point": {"x": "nope", "y": 0}}},
        )
        assert bad["success"] is False
        assert bad["error"] == "invalid_content"
        _assert_only_confirmed_ids_present()

        # group: valid create, then invalid_content (non-list member_node_ids).
        ok = tools_map["create_group_annotation"](
            session_id=session.id,
            x=0,
            y=0,
            annotation_id="group-2",
            member_node_ids=["node-a"],
        )
        assert ok["success"] is True
        confirmed_ids.append("group-2")
        _assert_only_confirmed_ids_present()

        bad = tools_map["create_group_annotation"](
            session_id=session.id,
            x=0,
            y=0,
            annotation_id="group-bad",
            member_node_ids="node-a",
        )
        assert bad["success"] is False
        assert bad["error"] == "invalid_content"
        _assert_only_confirmed_ids_present()

        # shape: valid create, then invalid_content (non-string shape).
        ok = tools_map["create_annotation"](
            session_id=session.id,
            type="shape",
            x=0,
            y=0,
            annotation_id="shape-2",
            content={"shape": "rectangle"},
        )
        assert ok["success"] is True
        confirmed_ids.append("shape-2")
        _assert_only_confirmed_ids_present()

        bad = tools_map["create_annotation"](
            session_id=session.id,
            type="shape",
            x=0,
            y=0,
            annotation_id="shape-bad",
            content={"shape": 123},
        )
        assert bad["success"] is False
        assert bad["error"] == "invalid_content"
        _assert_only_confirmed_ids_present()

        # icon: valid create, then invalid_content (non-string icon).
        ok = tools_map["create_annotation"](
            session_id=session.id,
            type="icon",
            x=0,
            y=0,
            annotation_id="icon-1",
            content={"icon": "flag"},
        )
        assert ok["success"] is True
        confirmed_ids.append("icon-1")
        _assert_only_confirmed_ids_present()

        bad = tools_map["create_annotation"](
            session_id=session.id,
            type="icon",
            x=0,
            y=0,
            annotation_id="icon-bad",
            content={"icon": ["flag"]},
        )
        assert bad["success"] is False
        assert bad["error"] == "invalid_content"
        _assert_only_confirmed_ids_present()

        # vote_dot: valid create, then invalid_content (a reserved content
        # key — vote_dot is a plain coloured dot with no type-specific
        # content field left to validate, since task-annotation-vote-dot-
        # simplify retired both its `value` and its attachment behaviour;
        # see ATTACHABLE_ANNOTATION_TYPES in session_annotations.py).
        ok = tools_map["create_annotation"](
            session_id=session.id,
            type="vote_dot",
            x=0,
            y=0,
            annotation_id="vote-dot-1",
        )
        assert ok["success"] is True
        confirmed_ids.append("vote-dot-1")
        _assert_only_confirmed_ids_present()

        bad = tools_map["create_annotation"](
            session_id=session.id,
            type="vote_dot",
            x=0,
            y=0,
            annotation_id="vote-dot-bad",
            content={"locked": True},
        )
        assert bad["success"] is False
        assert bad["error"] == "invalid_content"
        _assert_only_confirmed_ids_present()

        # image: valid create through ingest, then invalid_source (neither
        # image_data nor image_url given).
        png_data_url = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4"
            "2mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        ok = tools_map["create_image_annotation"](
            session_id=session.id,
            x=0,
            y=0,
            image_data=png_data_url,
            annotation_id="image-1",
        )
        assert ok["success"] is True
        confirmed_ids.append("image-1")
        _assert_only_confirmed_ids_present()

        bad = tools_map["create_image_annotation"](
            session_id=session.id, x=0, y=0, annotation_id="image-bad"
        )
        assert bad["success"] is False
        assert bad["error"] == "invalid_source"
        _assert_only_confirmed_ids_present()

        # freehand: valid create, then a revision_conflict (freehand also has
        # no type-specific content validation to trip).
        ok = tools_map["create_annotation"](
            session_id=session.id,
            type="freehand",
            x=0,
            y=0,
            annotation_id="freehand-1",
            content={"points": [{"x": 0, "y": 0}]},
        )
        assert ok["success"] is True
        confirmed_ids.append("freehand-1")
        _assert_only_confirmed_ids_present()

        bad = tools_map["create_annotation"](
            session_id=session.id,
            type="freehand",
            x=0,
            y=0,
            annotation_id="freehand-bad",
            content={"points": [{"x": 0, "y": 0}]},
            expected_revision=0,
        )
        assert bad["success"] is False
        assert bad["error"] == "revision_conflict"
        _assert_only_confirmed_ids_present()

        # Final check: every type in the matrix contributed exactly one
        # surviving annotation, and no invalid call from any later type
        # reached back and undid an earlier type's success.
        assert len(confirmed_ids) == 10  # every v1 type except the second shape variant
        listed = tools_map["list_annotations"](session_id=session.id)
        assert {a["id"] for a in listed["annotations"]} == set(confirmed_ids)
