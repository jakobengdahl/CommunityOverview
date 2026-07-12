"""Targeted tests for the graph authorization seam."""

import json

from backend.runtime.authorization import (
    GRAPH_ACTION_MUTATE,
    GraphAccessNarrowing,
    GraphAuthorizationContext,
    GraphAuthorizationDecision,
    use_request_authorization,
)
from backend.core import Node, NodeType, GraphStorage
from backend.federation.config import FederationFileConfig
from backend.federation.manager import FederationManager
from backend.service import GraphService


class DenyMutationsHook:
    def __init__(self):
        self.seen_contexts = []

    def evaluate(
        self, context: GraphAuthorizationContext
    ) -> GraphAuthorizationDecision:
        self.seen_contexts.append(context)
        if context.action == GRAPH_ACTION_MUTATE:
            return GraphAuthorizationDecision(
                allowed=False,
                reason="Mutations disabled for this actor/workspace context.",
                mode="custom",
                source="test",
            )
        return GraphAuthorizationDecision(allowed=True, mode="custom", source="test")


class WorkspaceSelectionNarrowingHook:
    def __init__(self):
        self.seen_contexts = []

    def evaluate(
        self, context: GraphAuthorizationContext
    ) -> GraphAuthorizationDecision:
        self.seen_contexts.append(context)
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


class FixedNarrowingHook:
    def __init__(self, *, allow_local_graph: bool, include_graph_ids: tuple[str, ...]):
        self.allow_local_graph = allow_local_graph
        self.include_graph_ids = include_graph_ids

    def evaluate(
        self, context: GraphAuthorizationContext
    ) -> GraphAuthorizationDecision:
        return GraphAuthorizationDecision(
            allowed=True,
            mode="fixed",
            source="test",
            graph_access=GraphAccessNarrowing(
                enabled=True,
                allow_local_graph=self.allow_local_graph,
                include_graph_ids=self.include_graph_ids,
            ),
        )


def _make_multi_graph_service(tmp_path, hook) -> GraphService:
    graph_file = tmp_path / "graph.json"
    graph_file.write_text(
        json.dumps(
            {
                "nodes": [
                    {"id": "local-1", "type": "Actor", "name": "Local result"},
                ],
                "edges": [],
            }
        ),
        encoding="utf-8",
    )
    storage = GraphStorage(str(graph_file))

    config = FederationFileConfig.model_validate(
        {
            "federation": {
                "enabled": True,
                "graphs": [
                    {
                        "graph_id": "graph-alpha",
                        "display_name": "Alpha",
                        "enabled": True,
                        "capabilities": {"allow_adopt": True},
                        "endpoints": {
                            "graph_json_url": "https://example.invalid/alpha.json"
                        },
                    },
                    {
                        "graph_id": "graph-beta",
                        "display_name": "Beta",
                        "enabled": True,
                        "capabilities": {"allow_adopt": True},
                        "endpoints": {
                            "graph_json_url": "https://example.invalid/beta.json"
                        },
                    },
                ],
            }
        }
    )
    manager = FederationManager(config)

    for graph, node_id, name in (
        (config.federation.graphs[0], "remote-1", "Alpha result"),
        (config.federation.graphs[1], "remote-2", "Beta result"),
    ):
        cache_nodes, _ = manager._build_cache(
            graph,
            [{"id": node_id, "type": "Actor", "name": name}],
            [],
        )
        manager._cache[graph.graph_id].nodes = cache_nodes

    return GraphService(storage, federation_manager=manager, authorization_hook=hook)


def _make_saved_view_service(tmp_path, hook) -> GraphService:
    graph_file = tmp_path / "saved-view-graph.json"
    graph_file.write_text(
        json.dumps(
            {
                "nodes": [
                    {"id": "local-1", "type": "Actor", "name": "Local node"},
                    {
                        "id": "alpha-1",
                        "type": "Actor",
                        "name": "Alpha node",
                        "metadata": {"origin_graph_id": "graph-alpha"},
                    },
                    {
                        "id": "beta-1",
                        "type": "Actor",
                        "name": "Beta node",
                        "metadata": {"origin_graph_id": "graph-beta"},
                    },
                    {
                        "id": "view-1",
                        "type": "SavedView",
                        "name": "Scoped View",
                        "summary": "Scoped view summary",
                        "metadata": {
                            "node_ids": ["local-1", "alpha-1", "beta-1"],
                            "positions": {
                                "local-1": {"x": 1, "y": 1},
                                "alpha-1": {"x": 2, "y": 2},
                                "beta-1": {"x": 3, "y": 3},
                            },
                            "hidden_nodes": ["alpha-1", "beta-1"],
                            "parentIds": {
                                "local-1": "group-1",
                                "alpha-1": "group-1",
                                "beta-1": "group-2",
                            },
                            "groups": [
                                {
                                    "id": "group-1",
                                    "label": "Visible group",
                                    "position": {"x": 0, "y": 0},
                                },
                                {
                                    "id": "group-2",
                                    "label": "Hidden group",
                                    "position": {"x": 4, "y": 4},
                                },
                            ],
                        },
                    },
                ],
                "edges": [
                    {
                        "id": "edge-1",
                        "source": "local-1",
                        "target": "alpha-1",
                        "type": "RELATES_TO",
                    },
                    {
                        "id": "edge-2",
                        "source": "alpha-1",
                        "target": "beta-1",
                        "type": "RELATES_TO",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return GraphService(GraphStorage(str(graph_file)), authorization_hook=hook)


class TestGraphAuthorizationSeam:
    def test_default_service_behavior_remains_permissive(
        self, empty_service: GraphService
    ):
        result = empty_service.add_nodes(
            nodes=[{"type": "Actor", "name": "Standalone-safe actor"}],
            edges=[],
        )

        assert result["success"] is True
        assert len(result["added_node_ids"]) == 1

    def test_custom_hook_can_block_mutations_and_capture_context(self, empty_storage):
        hook = DenyMutationsHook()
        service = GraphService(empty_storage, authorization_hook=hook)

        with use_request_authorization(
            actor_id="member-123",
            actor_type="member",
            workspace_id="workspace-456",
            workspace_kind="team",
            graph_id="graph-789",
        ):
            result = service.add_nodes(
                nodes=[{"type": "Actor", "name": "Blocked actor"}],
                edges=[],
            )

        assert result["success"] is False
        assert result["error_code"] == "access_denied"
        assert result["authorization"] == {
            "action": "mutate",
            "target": "add_nodes",
            "mode": "custom",
            "source": "test",
        }

        assert len(hook.seen_contexts) == 1
        context = hook.seen_contexts[0]
        assert context.action == "mutate"
        assert context.target == "add_nodes"
        assert context.actor["actor_id"] == "member-123"
        assert context.actor["actor_type"] == "member"
        assert context.scope["workspace_id"] == "workspace-456"
        assert context.scope["workspace_kind"] == "team"
        assert context.scope["graph_id"] == "graph-789"

    def test_custom_hook_still_allows_reads(self, empty_storage):
        empty_storage.add_nodes(
            [Node(id="actor-1", type=NodeType.ACTOR, name="Skatteverket")],
            [],
        )
        service = GraphService(empty_storage, authorization_hook=DenyMutationsHook())

        result = service.search_graph(query="Skatteverket")

        assert result["total"] >= 1
        assert result["nodes"][0]["name"] == "Skatteverket"

    def test_selection_aware_hook_can_narrow_reads_to_selected_graph(self, tmp_path):
        hook = WorkspaceSelectionNarrowingHook()
        service = _make_multi_graph_service(tmp_path, hook)

        with use_request_authorization(
            workspace_id="workspace-1", graph_id="graph-beta"
        ):
            result = service.search_graph(
                query="result", node_types=["Actor"], limit=10
            )

        assert [node["name"] for node in result["nodes"]] == ["Beta result"]
        assert result["federation"]["federated_nodes"] == 1
        assert len(hook.seen_contexts) == 1
        assert hook.seen_contexts[0].scope["workspace_id"] == "workspace-1"
        assert hook.seen_contexts[0].scope["graph_id"] == "graph-beta"

    def test_search_limit_is_applied_after_narrowing(self, tmp_path):
        service = _make_multi_graph_service(tmp_path, WorkspaceSelectionNarrowingHook())

        with use_request_authorization(
            workspace_id="workspace-1", graph_id="graph-beta"
        ):
            result = service.search_graph(query="result", node_types=["Actor"], limit=1)

        assert [node["name"] for node in result["nodes"]] == ["Beta result"]
        assert result["total"] == 1
        assert result["federation"]["federated_nodes"] == 1

    def test_get_saved_view_filters_disallowed_graph_nodes(self, tmp_path):
        service = _make_saved_view_service(
            tmp_path,
            FixedNarrowingHook(
                allow_local_graph=True, include_graph_ids=("graph-alpha",)
            ),
        )

        result = service.get_saved_view("Scoped View")

        assert result["success"] is True
        assert [node["id"] for node in result["nodes"]] == ["local-1", "alpha-1"]
        assert [edge["id"] for edge in result["edges"]] == ["edge-1"]
        assert set(result["positions"].keys()) == {"local-1", "alpha-1"}
        assert result["hidden_node_ids"] == ["alpha-1"]
        assert result["parentIds"] == {"local-1": "group-1", "alpha-1": "group-1"}

    def test_request_bound_stats_and_saved_views_honor_narrowing(self, tmp_path):
        hook = FixedNarrowingHook(
            allow_local_graph=False, include_graph_ids=("graph-beta",)
        )
        service = _make_multi_graph_service(tmp_path, hook)
        saved_view_service = _make_saved_view_service(tmp_path, hook)

        stats = service.get_graph_stats()
        views = saved_view_service.list_saved_views()

        assert stats["total_nodes"] == 0
        assert stats["total_edges"] == 0
        assert stats["federation"] == {
            "local_graph_name": "",
            "max_selectable_depth": 1,
            "selectable_depth_levels": [1],
            "search_has_multiple_graphs": False,
            "graph_display_names": {"graph-beta": "Beta"},
        }
        assert views == {"success": True, "views": [], "total": 0}

    def test_selection_aware_hook_can_block_adoption_outside_selected_graph(
        self, tmp_path
    ):
        service = _make_multi_graph_service(tmp_path, WorkspaceSelectionNarrowingHook())

        with use_request_authorization(
            workspace_id="workspace-1", graph_id="graph-beta"
        ):
            result = service.adopt_federated_node("federated::graph-alpha::remote-1")

        assert result["success"] is False
        assert result["error_code"] == "access_denied"
        assert result["authorization"]["target"] == "adopt_federated_node"
        assert result["added_node_ids"] == []
        assert result["added_edge_ids"] == []
