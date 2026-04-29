"""Targeted tests for the graph authorization seam."""

from backend.authorization import (
    GRAPH_ACTION_MUTATE,
    GraphAuthorizationContext,
    GraphAuthorizationDecision,
    use_request_authorization,
)
from backend.core import Node, NodeType
from backend.service import GraphService


class DenyMutationsHook:
    def __init__(self):
        self.seen_contexts = []

    def evaluate(self, context: GraphAuthorizationContext) -> GraphAuthorizationDecision:
        self.seen_contexts.append(context)
        if context.action == GRAPH_ACTION_MUTATE:
            return GraphAuthorizationDecision(
                allowed=False,
                reason="Mutations disabled for this actor/workspace context.",
                mode="custom",
                source="test",
            )
        return GraphAuthorizationDecision(allowed=True, mode="custom", source="test")


class TestGraphAuthorizationSeam:
    def test_default_service_behavior_remains_permissive(self, empty_service: GraphService):
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
