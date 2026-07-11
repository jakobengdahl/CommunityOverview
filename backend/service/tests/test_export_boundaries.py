"""Focused tests for export boundary behavior."""

from backend.runtime.authorization import GraphAccessNarrowing, GraphAuthorizationContext, GraphAuthorizationDecision, use_request_authorization
from backend.core import Edge, GraphStorage, Node, NodeType
from backend.service import GraphService


class SelectionAwareNarrowingHook:
    def evaluate(self, context: GraphAuthorizationContext) -> GraphAuthorizationDecision:
        selected_graph_id = context.scope.get("graph_id", "")
        workspace_id = context.scope.get("workspace_id", "")
        if not selected_graph_id or not workspace_id:
            return GraphAuthorizationDecision(allowed=True, mode="selection-aware", source="test")
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


def _build_service(empty_storage: GraphStorage, authorization_hook=None) -> GraphService:
    empty_storage.add_nodes(
        [
            Node(id="local-1", type=NodeType.ACTOR, name="Local export"),
            Node(
                id="alpha-1",
                type=NodeType.ACTOR,
                name="Alpha export",
                metadata={"origin_graph_id": "graph-alpha", "is_federated_reference": True},
            ),
            Node(
                id="beta-1",
                type=NodeType.ACTOR,
                name="Beta export",
                metadata={"origin_graph_id": "graph-beta", "is_federated_reference": True},
            ),
        ],
        [Edge(source="alpha-1", target="beta-1", type="RELATES_TO")],
    )
    return GraphService(empty_storage, authorization_hook=authorization_hook)


class TestExportBoundaries:
    def test_standalone_export_remains_full_and_sanitized(self, empty_storage: GraphStorage):
        service = _build_service(empty_storage)

        result = service.export_graph()

        assert [node["name"] for node in result["nodes"]] == ["Local export", "Alpha export", "Beta export"]
        assert result["total_nodes"] == 3
        assert result["total_edges"] == 1
        assert result["export_boundary"] == {
            "contract_version": "1.0",
            "export_kind": "full",
            "is_narrowed": False,
            "scope_kind": "standalone",
            "selection_mode": "default",
            "selection_source": "default",
            "has_workspace_selection": False,
            "has_graph_selection": False,
            "graph_scope": {
                "local_graph_included": True,
                "included_graph_count": 0,
            },
            "counts": {
                "nodes": 3,
                "edges": 1,
                "omitted_nodes": 0,
                "omitted_edges": 0,
            },
        }

    def test_request_bound_export_is_narrowed_without_leaking_ids(self, empty_storage: GraphStorage):
        service = _build_service(empty_storage, authorization_hook=SelectionAwareNarrowingHook())

        with use_request_authorization(
            workspace_id="workspace-secret",
            workspace_kind="team",
            graph_id="graph-alpha",
        ):
            result = service.export_graph()

        assert [node["name"] for node in result["nodes"]] == ["Alpha export"]
        assert result["edges"] == []
        assert result["export_boundary"] == {
            "contract_version": "1.0",
            "export_kind": "narrowed",
            "is_narrowed": True,
            "scope_kind": "team",
            "selection_mode": "workspace_graph",
            "selection_source": "override",
            "has_workspace_selection": True,
            "has_graph_selection": True,
            "graph_scope": {
                "local_graph_included": False,
                "included_graph_count": 1,
            },
            "counts": {
                "nodes": 1,
                "edges": 0,
                "omitted_nodes": 2,
                "omitted_edges": 1,
            },
        }
        assert "workspace-secret" not in str(result["export_boundary"])
        assert "graph-alpha" not in str(result["export_boundary"])
