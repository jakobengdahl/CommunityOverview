"""MCP CRUD for the ``heatmap`` annotation type (docs/ANNOTATION_CONTRACT.md's
"Heat-map circles"): an integer ``content.intensity`` from 0 to 10, a
default diameter when no size is given, and the stored intensity surviving an
upsert-replace that does not resend it.
"""

import os
from unittest.mock import MagicMock, Mock

import pytest

from backend.core import GraphStorage
from backend.core.session_annotations import (
    HEATMAP_DEFAULT_DIAMETER,
    HEATMAP_DEFAULT_INTENSITY,
    build_annotation,
    build_annotation_patch,
)
from backend.core.session_manager import SessionManager
from backend.core.session_store import InMemorySessionPersistenceBackend, SessionStore
from backend.service import GraphService, register_mcp_tools


@pytest.fixture
def annotation_tools(tmp_path):
    storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
    service = GraphService(storage)
    manager = SessionManager(SessionStore(InMemorySessionPersistenceBackend()))
    mock_mcp = Mock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)
    tools_map = register_mcp_tools(mock_mcp, service, session_manager=manager)
    return tools_map, manager


def _listed(tools_map, session_id, annotation_id):
    result = tools_map["list_annotations"](session_id=session_id)
    return next(a for a in result["annotations"] if a["id"] == annotation_id)


class TestHeatmapContractValues:
    def test_defaults_are_the_documented_literals(self):
        # Pinned as literals, not against the constants themselves, so a
        # changed default cannot pass by moving the assertion with it. They
        # mirror HEATMAP_* in packages/ui-graph-canvas/src/utils/annotationModel.js.
        assert HEATMAP_DEFAULT_DIAMETER == 160
        assert HEATMAP_DEFAULT_INTENSITY == 5

    def test_other_generic_types_get_no_size_default(self):
        annotation = build_annotation(type="shape", x=0, y=0)
        assert annotation["geometry"]["w"] == 0
        assert annotation["geometry"]["h"] == 0
        assert "size" not in annotation


class TestHeatmapBuilder:
    def test_default_size_is_a_circle_of_the_default_diameter(self):
        annotation = build_annotation(type="heatmap", x=0, y=0)
        assert annotation["geometry"]["w"] == HEATMAP_DEFAULT_DIAMETER
        assert annotation["geometry"]["h"] == HEATMAP_DEFAULT_DIAMETER

    def test_one_given_dimension_sets_the_other(self):
        assert build_annotation(type="heatmap", x=0, y=0, w=90)["geometry"]["h"] == 90
        assert build_annotation(type="heatmap", x=0, y=0, h=70)["geometry"]["w"] == 70

    def test_builder_does_not_invent_an_intensity(self):
        # An upsert-replace goes through this builder; a default written here
        # would overwrite the stored intensity under the store's shallow merge.
        assert "intensity" not in build_annotation(type="heatmap", x=0, y=0)

    @pytest.mark.parametrize("value", [0, 10])
    def test_bounds_are_accepted(self, value):
        annotation = build_annotation(
            type="heatmap", x=0, y=0, content={"intensity": value}
        )
        assert annotation["intensity"] == value

    @pytest.mark.parametrize("value", [-1, 11, 7.5, True, "5", None])
    def test_out_of_contract_intensity_is_rejected(self, value):
        with pytest.raises(ValueError, match="intensity"):
            build_annotation(type="heatmap", x=0, y=0, content={"intensity": value})

    def test_patch_validates_intensity_too(self):
        existing = {**build_annotation(type="heatmap", x=0, y=0), "id": "h1"}
        with pytest.raises(ValueError, match="intensity"):
            build_annotation_patch(existing, content={"intensity": 12})
        assert build_annotation_patch(existing, content={"intensity": 3})[
            "intensity"
        ] == (3)

    def test_intensity_is_not_validated_on_other_types(self):
        annotation = build_annotation(
            type="shape", x=0, y=0, content={"intensity": "anything"}
        )
        assert annotation["intensity"] == "anything"


class TestHeatmapMcpCrud:
    def test_create_defaults_intensity_and_size_and_lists_them(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["create_annotation"](
            session_id=session.id, type="heatmap", x=10, y=20
        )

        assert result["success"] is True
        listed = _listed(tools_map, session.id, result["annotation"]["id"])
        assert listed["type"] == "heatmap"
        assert listed["content"]["intensity"] == HEATMAP_DEFAULT_INTENSITY
        assert (listed["w"], listed["h"]) == (
            HEATMAP_DEFAULT_DIAMETER,
            HEATMAP_DEFAULT_DIAMETER,
        )

    def test_create_with_explicit_intensity(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["create_annotation"](
            session_id=session.id,
            type="heatmap",
            x=0,
            y=0,
            w=240,
            h=240,
            content={"intensity": 9},
        )

        listed = _listed(tools_map, session.id, result["annotation"]["id"])
        assert listed["content"]["intensity"] == 9
        assert listed["w"] == 240

    def test_create_rejects_out_of_range_intensity(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["create_annotation"](
            session_id=session.id, type="heatmap", x=0, y=0, content={"intensity": 11}
        )

        assert result["success"] is False
        assert result["error"] == "invalid_content"
        assert tools_map["list_annotations"](session_id=session.id)["annotations"] == []

    @pytest.mark.parametrize("value", [7.5, True, "5", None, -1])
    def test_create_rejects_every_out_of_contract_intensity(
        self, annotation_tools, value
    ):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["create_annotation"](
            session_id=session.id,
            type="heatmap",
            x=0,
            y=0,
            content={"intensity": value},
        )

        assert result["success"] is False
        assert result["error"] == "invalid_content"
        assert tools_map["list_annotations"](session_id=session.id)["annotations"] == []

    @pytest.mark.parametrize("value", [7.5, False, "9", None, 11])
    def test_update_rejects_every_out_of_contract_intensity(
        self, annotation_tools, value
    ):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id, type="heatmap", x=0, y=0, content={"intensity": 4}
        )
        annotation_id = created["annotation"]["id"]

        result = tools_map["update_annotation"](
            session_id=session.id,
            annotation_id=annotation_id,
            content={"intensity": value},
        )

        assert result["success"] is False
        assert result["error"] == "invalid_content"
        listed = _listed(tools_map, session.id, annotation_id)
        assert listed["content"]["intensity"] == 4

    def test_create_starts_behind_graph_nodes(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()

        result = tools_map["create_annotation"](
            session_id=session.id, type="heatmap", x=0, y=0
        )

        assert _listed(tools_map, session.id, result["annotation"]["id"])["z"] == -1

    def test_upsert_without_content_keeps_the_stored_intensity(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        tools_map["create_annotation"](
            session_id=session.id,
            type="heatmap",
            x=0,
            y=0,
            content={"intensity": 8},
            annotation_id="heat-1",
        )

        result = tools_map["create_annotation"](
            session_id=session.id, type="heatmap", x=50, y=50, annotation_id="heat-1"
        )

        assert result["success"] is True
        listed = _listed(tools_map, session.id, "heat-1")
        assert listed["content"]["intensity"] == 8
        assert listed["x"] == 50

    def test_update_moves_resizes_and_sets_intensity(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id, type="heatmap", x=0, y=0
        )
        annotation_id = created["annotation"]["id"]

        result = tools_map["update_annotation"](
            session_id=session.id,
            annotation_id=annotation_id,
            x=30,
            y=40,
            w=200,
            h=200,
            content={"intensity": 0},
        )

        assert result["success"] is True
        listed = _listed(tools_map, session.id, annotation_id)
        assert (listed["x"], listed["y"], listed["w"], listed["h"]) == (
            30,
            40,
            200,
            200,
        )
        assert listed["content"]["intensity"] == 0

    def test_update_rejects_out_of_range_intensity(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id, type="heatmap", x=0, y=0, content={"intensity": 4}
        )
        annotation_id = created["annotation"]["id"]

        result = tools_map["update_annotation"](
            session_id=session.id,
            annotation_id=annotation_id,
            content={"intensity": -2},
        )

        assert result["success"] is False
        assert result["error"] == "invalid_content"
        assert (
            _listed(tools_map, session.id, annotation_id)["content"]["intensity"] == 4
        )

    def test_duplicate_and_delete(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        created = tools_map["create_annotation"](
            session_id=session.id, type="heatmap", x=0, y=0, content={"intensity": 6}
        )
        annotation_id = created["annotation"]["id"]

        duplicate = tools_map["duplicate_annotation"](
            session_id=session.id, annotation_id=annotation_id, dx=40, dy=0
        )
        assert duplicate["success"] is True
        copy_id = duplicate["annotation"]["id"]
        assert _listed(tools_map, session.id, copy_id)["content"]["intensity"] == 6

        deleted = tools_map["delete_annotation"](
            session_id=session.id, annotation_id=annotation_id
        )
        assert deleted["success"] is True
        remaining = tools_map["list_annotations"](session_id=session.id)["annotations"]
        assert [a["id"] for a in remaining] == [copy_id]

    def test_list_filters_by_heatmap_type(self, annotation_tools):
        tools_map, manager = annotation_tools
        session = manager.create_session()
        tools_map["create_annotation"](session_id=session.id, type="heatmap", x=0, y=0)
        tools_map["create_annotation"](session_id=session.id, type="shape", x=0, y=0)

        result = tools_map["list_annotations"](session_id=session.id, types=["heatmap"])

        assert [a["type"] for a in result["annotations"]] == ["heatmap"]
