"""Tests for config-driven dedicated REST interfaces per node/edge type.

Covers:
- config parsing/validation of RestInterfaceConfig (path, entity),
- endpoint generation from config (only registered when configured),
- tag-filter AND (tags_all) / OR (tags_any) semantics and their combination,
- subtype filtering for node interfaces,
- edge interfaces (type match + metadata-tag filter + endpoint visibility),
- access parity: a dedicated endpoint never returns more than a narrowed
  generic search would (graph-scope narrowing honored identically),
- the generic node/edge interface keeps working alongside dedicated ones.
"""

import pytest
from pydantic import ValidationError

from backend.config.config_loader import (
    RestInterfaceConfig,
    RestInterfaceFilterConfig,
)
from backend.core import Edge, GraphStorage, Node, NodeType
from backend.runtime.authorization import (
    GraphAccessNarrowing,
    GraphAuthorizationContext,
    GraphAuthorizationDecision,
    use_request_authorization,
)
from backend.service import GraphService, create_rest_router


# ==================== Config model tests ====================


class TestRestInterfaceConfig:
    def test_path_is_normalized(self):
        cfg = RestInterfaceConfig(path="/actors/", node_type="Actor")
        assert cfg.path == "actors"

    def test_nested_path_allowed(self):
        cfg = RestInterfaceConfig(path="people/actors", node_type="Actor")
        assert cfg.path == "people/actors"

    def test_empty_path_rejected(self):
        with pytest.raises(ValidationError):
            RestInterfaceConfig(path="   ", node_type="Actor")

    def test_uppercase_path_rejected(self):
        with pytest.raises(ValidationError):
            RestInterfaceConfig(path="Actors", node_type="Actor")

    def test_invalid_entity_rejected(self):
        with pytest.raises(ValidationError):
            RestInterfaceConfig(path="actors", entity="relationship")

    def test_entity_defaults_to_node(self):
        cfg = RestInterfaceConfig(path="actors", node_type="Actor")
        assert cfg.entity == "node"

    def test_limit_bounds_enforced(self):
        with pytest.raises(ValidationError):
            RestInterfaceConfig(path="actors", node_type="Actor", limit=0)


# ==================== Service-level filter tests ====================


@pytest.fixture
def tagged_service(empty_storage: GraphStorage) -> GraphService:
    empty_storage.add_nodes(
        [
            Node(
                id="a1", type=NodeType.ACTOR, name="Approved actor", tags=["approved"]
            ),
            Node(
                id="a2",
                type=NodeType.ACTOR,
                name="Processing actor",
                tags=["processing"],
            ),
            Node(
                id="a3",
                type=NodeType.ACTOR,
                name="Both actor",
                tags=["approved", "processing"],
                subtypes=["Agency"],
            ),
            Node(id="a4", type=NodeType.ACTOR, name="Untagged actor", tags=[]),
            Node(
                id="i1",
                type=NodeType.INITIATIVE,
                name="Approved initiative",
                tags=["approved"],
            ),
        ],
        [Edge(id="e1", source="a1", target="a3", type="RELATES_TO")],
    )
    return GraphService(empty_storage)


class TestListTypedNodes:
    def test_type_scoping_returns_only_that_type(self, tagged_service: GraphService):
        result = tagged_service.list_typed_nodes(node_type="Actor")
        names = {n["name"] for n in result["nodes"]}
        assert names == {
            "Approved actor",
            "Processing actor",
            "Both actor",
            "Untagged actor",
        }
        assert "Approved initiative" not in names
        assert result["total"] == 4

    def test_tags_all_is_and(self, tagged_service: GraphService):
        result = tagged_service.list_typed_nodes(
            node_type="Actor", tags_all=["approved", "processing"]
        )
        names = {n["name"] for n in result["nodes"]}
        assert names == {"Both actor"}

    def test_tags_any_is_or(self, tagged_service: GraphService):
        result = tagged_service.list_typed_nodes(
            node_type="Actor", tags_any=["approved", "processing"]
        )
        names = {n["name"] for n in result["nodes"]}
        assert names == {"Approved actor", "Processing actor", "Both actor"}

    def test_tags_all_and_any_combine(self, tagged_service: GraphService):
        # Must carry 'approved' (AND) and at least one of {processing} (OR).
        result = tagged_service.list_typed_nodes(
            node_type="Actor", tags_all=["approved"], tags_any=["processing"]
        )
        names = {n["name"] for n in result["nodes"]}
        assert names == {"Both actor"}

    def test_subtypes_any_filter(self, tagged_service: GraphService):
        result = tagged_service.list_typed_nodes(
            node_type="Actor", subtypes_any=["Agency"]
        )
        names = {n["name"] for n in result["nodes"]}
        assert names == {"Both actor"}

    def test_connecting_edges_within_subset_are_returned(
        self, tagged_service: GraphService
    ):
        result = tagged_service.list_typed_nodes(
            node_type="Actor", tags_any=["approved"]
        )
        # a1 (Approved) and a3 (Both) are both in the subset → edge e1 surfaces.
        assert [e["id"] for e in result["edges"]] == ["e1"]

    def test_edge_dropped_when_one_endpoint_filtered_out(
        self, tagged_service: GraphService
    ):
        # Only a3 matches tags_all; a1 is excluded, so e1 must not appear.
        result = tagged_service.list_typed_nodes(
            node_type="Actor", tags_all=["approved", "processing"]
        )
        assert result["edges"] == []

    def test_limit_is_applied(self, tagged_service: GraphService):
        result = tagged_service.list_typed_nodes(node_type="Actor", limit=2)
        assert result["total"] == 2


class TestListTypedEdges:
    @pytest.fixture
    def edge_service(self, empty_storage: GraphStorage) -> GraphService:
        empty_storage.add_nodes(
            [
                Node(id="n1", type=NodeType.ACTOR, name="N1"),
                Node(id="n2", type=NodeType.ACTOR, name="N2"),
                Node(id="n3", type=NodeType.ACTOR, name="N3"),
            ],
            [
                Edge(
                    id="r1",
                    source="n1",
                    target="n2",
                    type="RELATES_TO",
                    metadata={"tags": ["approved"]},
                ),
                Edge(
                    id="r2",
                    source="n2",
                    target="n3",
                    type="RELATES_TO",
                    metadata={"tags": ["processing"]},
                ),
                Edge(id="p1", source="n1", target="n3", type="PART_OF"),
            ],
        )
        return GraphService(empty_storage)

    def test_edge_type_scoping(self, edge_service: GraphService):
        result = edge_service.list_typed_edges(edge_type="RELATES_TO")
        assert {e["id"] for e in result["edges"]} == {"r1", "r2"}
        assert result["total"] == 2

    def test_edge_metadata_tag_filter(self, edge_service: GraphService):
        result = edge_service.list_typed_edges(
            edge_type="RELATES_TO", tags_any=["approved"]
        )
        assert {e["id"] for e in result["edges"]} == {"r1"}

    def test_endpoint_nodes_included(self, edge_service: GraphService):
        result = edge_service.list_typed_edges(
            edge_type="RELATES_TO", tags_any=["approved"]
        )
        assert {n["id"] for n in result["nodes"]} == {"n1", "n2"}


# ==================== Access-parity tests ====================


class SelectionAwareNarrowingHook:
    """Narrows to a single selected graph id when a full scope is supplied."""

    def evaluate(
        self, context: GraphAuthorizationContext
    ) -> GraphAuthorizationDecision:
        selected_graph_id = context.scope.get("graph_id", "")
        workspace_id = context.scope.get("workspace_id", "")
        if not selected_graph_id or not workspace_id:
            return GraphAuthorizationDecision(
                allowed=True, mode="selection-aware", source="test"
            )
        return GraphAuthorizationDecision(
            allowed=True,
            mode="selection-aware",
            source="test",
            graph_access=GraphAccessNarrowing(
                enabled=True,
                allow_local_graph=False,
                include_graph_ids=(selected_graph_id,),
            ),
        )


class TestAccessParity:
    @pytest.fixture
    def narrowed_service(self, empty_storage: GraphStorage) -> GraphService:
        empty_storage.add_nodes(
            [
                Node(
                    id="local-1", type=NodeType.ACTOR, name="Local", tags=["approved"]
                ),
                Node(
                    id="alpha-1",
                    type=NodeType.ACTOR,
                    name="Alpha",
                    tags=["approved"],
                    metadata={"origin_graph_id": "graph-alpha"},
                ),
                Node(
                    id="beta-1",
                    type=NodeType.ACTOR,
                    name="Beta",
                    tags=["approved"],
                    metadata={"origin_graph_id": "graph-beta"},
                ),
            ],
            [],
        )
        return GraphService(
            empty_storage, authorization_hook=SelectionAwareNarrowingHook()
        )

    def test_dedicated_node_endpoint_respects_narrowing(
        self, narrowed_service: GraphService
    ):
        with use_request_authorization(
            workspace_id="ws", workspace_kind="team", graph_id="graph-alpha"
        ):
            result = narrowed_service.list_typed_nodes(
                node_type="Actor", tags_any=["approved"]
            )
        # Same subset the narrowed generic search would return: only graph-alpha.
        assert [n["name"] for n in result["nodes"]] == ["Alpha"]

    def test_dedicated_endpoint_denied_when_generic_denied(
        self, empty_storage: GraphStorage
    ):
        class DenyHook:
            def evaluate(self, context):
                return GraphAuthorizationDecision(
                    allowed=False, mode="deny", source="test", reason="nope"
                )

        empty_storage.add_nodes([Node(id="a1", type=NodeType.ACTOR, name="A")], [])
        service = GraphService(empty_storage, authorization_hook=DenyHook())
        result = service.list_typed_nodes(node_type="Actor")
        assert result["error_code"] == "access_denied"


# ==================== HTTP / router registration tests ====================


class TestCustomInterfaceRouting:
    @pytest.fixture
    def client(self, empty_storage: GraphStorage):
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("FastAPI not installed")

        empty_storage.add_nodes(
            [
                Node(
                    id="a1",
                    type=NodeType.ACTOR,
                    name="Approved actor",
                    tags=["approved"],
                ),
                Node(
                    id="a2",
                    type=NodeType.ACTOR,
                    name="Draft actor",
                    tags=["draft"],
                ),
                Node(id="g1", type=NodeType.GOAL, name="A goal"),
            ],
            [],
        )
        service = GraphService(empty_storage)
        interfaces = [
            RestInterfaceConfig(
                path="actors",
                node_type="Actor",
                filters=RestInterfaceFilterConfig(tags_any=["approved"]),
            ),
            RestInterfaceConfig(
                path="disabled",
                node_type="Goal",
                enabled=False,
            ),
        ]
        router = create_rest_router(service, rest_interfaces=interfaces)

        app = FastAPI()
        app.include_router(router, prefix="/api")
        return TestClient(app)

    def test_configured_endpoint_returns_filtered_subset(self, client):
        resp = client.get("/api/actors")
        assert resp.status_code == 200
        body = resp.json()
        assert [n["name"] for n in body["nodes"]] == ["Approved actor"]
        assert body["node_type"] == "Actor"

    def test_disabled_interface_not_registered(self, client):
        assert client.get("/api/disabled").status_code == 404

    def test_generic_interface_still_works(self, client):
        # The generic node-details route is unaffected by the dedicated ones.
        resp = client.get("/api/nodes/a2")
        assert resp.status_code == 200
        assert resp.json()["node"]["name"] == "Draft actor"

    def test_unconfigured_type_has_no_dedicated_endpoint(self, client):
        assert client.get("/api/goals").status_code == 404
