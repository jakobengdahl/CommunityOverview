# covers: backend/service/mutations.py
"""
Tests for the ``batch_update_nodes`` MCP tool / service / mutation path.

Covers the same guarantees as ``update_node`` plus the batch-specific ones:
partial success, size cap, duplicate rejection, compact result shape.
"""

import json
from unittest.mock import Mock

import pytest

from backend.core import (
    GraphStorage,
    Node,
    NodeType,
)
from backend.runtime.authorization import (
    GRAPH_ACTION_MUTATE,
    GraphAccessNarrowing,
    GraphAuthorizationContext,
    GraphAuthorizationDecision,
)
from backend.service import GraphService, register_mcp_tools


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def batch_tools(tmp_path):
    """Tools map wired to a storage with a few nodes and an unscoped hook."""
    storage = GraphStorage(json_path=str(tmp_path / "graph.json"))
    storage.add_nodes(
        [
            Node(id="a", type=NodeType.ACTOR, name="Alice", tags=["old"]),
            Node(id="b", type=NodeType.ACTOR, name="Bob", tags=["old"]),
            Node(
                id="c",
                type=NodeType.ACTOR,
                name="Charlie",
                metadata={"origin_graph_id": "graph-beta"},
            ),
        ],
        [],
    )
    mcp = Mock()
    mcp.tool.return_value = lambda tool: tool
    tools = register_mcp_tools(mcp, GraphService(storage))
    try:
        yield tools, storage
    finally:
        storage.shutdown_events()


@pytest.fixture
def empty_service(tmp_path):
    """Empty service with default (permissive) authorization."""
    return GraphService(GraphStorage(json_path=str(tmp_path / "g.json")))


def _make_scoped_service(tmp_path, include_graph_ids: tuple) -> GraphService:
    """Multi-graph service narrowed to *include_graph_ids* (local always allowed)."""
    graph_file = tmp_path / "scoped.json"
    graph_file.write_text(
        json.dumps(
            {
                "nodes": [
                    {"id": "local-1", "type": "Actor", "name": "Local"},
                    {
                        "id": "alpha-1",
                        "type": "Actor",
                        "name": "Alpha",
                        "metadata": {"origin_graph_id": "graph-alpha"},
                    },
                    {
                        "id": "beta-1",
                        "type": "Actor",
                        "name": "Beta",
                        "metadata": {"origin_graph_id": "graph-beta"},
                    },
                ],
                "edges": [],
            }
        ),
        encoding="utf-8",
    )
    storage = GraphStorage(str(graph_file))

    class NarrowingHook:
        def evaluate(
            self, context: GraphAuthorizationContext
        ) -> GraphAuthorizationDecision:
            if context.action == GRAPH_ACTION_MUTATE:
                return GraphAuthorizationDecision(
                    allowed=True,
                    mode="fixed",
                    source="test",
                    graph_access=GraphAccessNarrowing(
                        enabled=True,
                        allow_local_graph=True,
                        include_graph_ids=include_graph_ids,
                    ),
                )
            return GraphAuthorizationDecision(
                allowed=True,
                mode="fixed",
                source="test",
                graph_access=GraphAccessNarrowing(
                    enabled=True,
                    allow_local_graph=True,
                    include_graph_ids=include_graph_ids,
                ),
            )

    return GraphService(storage, authorization_hook=NarrowingHook())


# ---------------------------------------------------------------------------
# 1. Happy path — metadata merge and null removal
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_updates_multiple_nodes(self, batch_tools):
        tools, storage = batch_tools
        updates = [
            {"node_id": "a", "updates": {"name": "Alice Updated"}},
            {"node_id": "b", "updates": {"name": "Bob Updated"}},
        ]
        result = tools["batch_update_nodes"](updates)

        assert result["success"] is True
        assert result["counts"]["ok"] == 2
        assert len(result["results"]) == 2
        assert storage.nodes["a"].name == "Alice Updated"
        assert storage.nodes["b"].name == "Bob Updated"

    def test_metadata_merge_is_honoured(self, batch_tools):
        tools, storage = batch_tools
        # Set initial metadata
        tools["batch_update_nodes"](
            [
                {"node_id": "a", "updates": {"metadata": {"k1": "v1", "k2": "v2"}}},
            ]
        )
        # Merge: update k1, remove k2 with null; k3 is not present so nothing to preserve
        tools["batch_update_nodes"](
            [
                {
                    "node_id": "a",
                    "updates": {"metadata": {"k1": "new", "k2": None}},
                    "metadata_merge": True,
                },
            ]
        )
        a_node = storage.nodes["a"]
        assert a_node.metadata == {"k1": "new"}

    def test_metadata_merge_is_skipped_when_false(self, batch_tools):
        tools, storage = batch_tools
        tools["batch_update_nodes"](
            [
                {"node_id": "a", "updates": {"metadata": {"k1": "v1", "k2": "v2"}}},
            ]
        )
        # Replace mode — k2 is gone, k3 is gone
        tools["batch_update_nodes"](
            [
                {
                    "node_id": "a",
                    "updates": {"metadata": {"only": "stay"}},
                    "metadata_merge": False,
                },
            ]
        )
        assert storage.nodes["a"].metadata == {"only": "stay"}


# ---------------------------------------------------------------------------
# 2. Tags replace semantics (same as update_node)
# ---------------------------------------------------------------------------


class TestTagsReplace:
    def test_tags_replace_not_append(self, batch_tools):
        tools, storage = batch_tools
        # a starts with tags=["old"]
        tools["batch_update_nodes"](
            [
                {"node_id": "a", "updates": {"tags": ["new-tag"]}},
            ]
        )
        assert storage.nodes["a"].tags == ["new-tag"]
        assert "old" not in storage.nodes["a"].tags


# ---------------------------------------------------------------------------
# 3. expected_revision conflict for one node while others succeed
# ---------------------------------------------------------------------------


class TestPartialSuccessRevisions:
    def test_one_conflict_others_succeed(self, batch_tools):
        tools, storage = batch_tools
        a_rev = storage.nodes["a"].revision
        b_rev = storage.nodes["b"].revision

        result = tools["batch_update_nodes"](
            [
                {
                    "node_id": "a",
                    "updates": {"name": "Alice"},
                    "expected_revision": a_rev + 99,  # stale
                },
                {
                    "node_id": "b",
                    "updates": {"name": "Bob"},
                    "expected_revision": b_rev,  # ok
                },
            ]
        )

        assert result["success"] is True
        counts = result["counts"]
        assert counts["ok"] == 1
        assert counts["conflict"] == 1
        # The conflicted node was NOT updated; the ok node was.
        assert storage.nodes["b"].name == "Bob"
        # Find the conflict result
        conflict_results = [r for r in result["results"] if r["status"] == "conflict"]
        assert len(conflict_results) == 1
        assert conflict_results[0]["node_id"] == "a"
        assert "current_revision" in conflict_results[0]

    def test_all_conflicts(self, batch_tools):
        tools, storage = batch_tools
        stale_rev = 999
        result = tools["batch_update_nodes"](
            [
                {
                    "node_id": "a",
                    "updates": {"name": "X"},
                    "expected_revision": stale_rev,
                },
                {
                    "node_id": "b",
                    "updates": {"name": "Y"},
                    "expected_revision": stale_rev,
                },
            ]
        )
        assert result["success"] is True
        assert result["counts"]["conflict"] == 2
        assert result["counts"]["ok"] == 0


# ---------------------------------------------------------------------------
# 4. Not-found
# ---------------------------------------------------------------------------


class TestNotFound:
    def test_missing_node_id_returns_not_found(self, batch_tools):
        tools, _ = batch_tools
        result = tools["batch_update_nodes"](
            [
                {"node_id": "nonexistent", "updates": {"name": "X"}},
            ]
        )
        assert result["success"] is True
        assert result["counts"]["not_found"] == 1
        nr = result["results"][0]
        assert nr["status"] == "not_found"

    def test_not_found_coexists_with_success(self, batch_tools):
        tools, storage = batch_tools
        result = tools["batch_update_nodes"](
            [
                {"node_id": "a", "updates": {"name": "Alice"}},
                {"node_id": "nope", "updates": {"name": "X"}},
            ]
        )
        assert result["success"] is True
        assert result["counts"]["ok"] == 1
        assert result["counts"]["not_found"] == 1
        assert storage.nodes["a"].name == "Alice"


# ---------------------------------------------------------------------------
# 5. Authorization — out-of-scope node refused, others succeed
# ---------------------------------------------------------------------------


class TestAuthorizationPartialSuccess:
    def test_out_of_scope_node_refused_while_others_succeed(self, tmp_path):
        service = _make_scoped_service(tmp_path, ("graph-alpha",))

        # batch_update_nodes on the service level
        result = service.batch_update_nodes(
            [
                {"node_id": "alpha-1", "updates": {"name": "Alpha Updated"}},
                {"node_id": "beta-1", "updates": {"name": "Beta Updated"}},
            ]
        )

        assert result["success"] is True
        counts = result["counts"]
        assert counts["ok"] == 1
        # Out-of-scope nodes return "not found" (the same path update_node uses)
        assert counts["not_found"] == 1
        # The in-scope node was updated
        assert service.storage.nodes["alpha-1"].name == "Alpha Updated"
        # The out-of-scope node was NOT changed
        assert service.storage.nodes["beta-1"].name == "Beta"

    def test_all_out_of_scope(self, tmp_path):
        service = _make_scoped_service(tmp_path, ("graph-alpha",))

        result = service.batch_update_nodes(
            [
                {"node_id": "beta-1", "updates": {"name": "X"}},
                {"node_id": "alpha-1", "updates": {"name": "Y"}},
            ]
        )
        assert result["success"] is True
        # alpha-1 is in scope so ok=1, beta-1 is out of scope → not_found=1
        counts = result["counts"]
        assert counts["not_found"] == 1  # beta-1
        assert counts["ok"] == 1  # alpha-1

    def test_local_node_always_visible(self, tmp_path):
        service = _make_scoped_service(tmp_path, ("graph-alpha",))

        # local-1 is always visible even when only graph-alpha is included
        result = service.batch_update_nodes(
            [
                {"node_id": "local-1", "updates": {"name": "Local Updated"}},
            ]
        )
        assert result["success"] is True
        assert result["counts"]["ok"] == 1
        assert service.storage.nodes["local-1"].name == "Local Updated"


# ---------------------------------------------------------------------------
# 6. Cap exceeded — writes nothing
# ---------------------------------------------------------------------------


class TestSizeCap:
    def test_exactly_at_cap_succeeds(self, batch_tools):
        tools, storage = batch_tools
        # Create enough nodes to hit exactly 100
        for i in range(97):
            nid = f"extra-{i}"
            storage.add_nodes([Node(id=nid, type=NodeType.ACTOR, name=nid)], [])
        batch = [
            {"node_id": f"extra-{i}", "updates": {"name": f"x{i}"}} for i in range(97)
        ]
        # 97 existing + 3 more = 100
        batch.append({"node_id": "a", "updates": {"name": "A"}})
        batch.append({"node_id": "b", "updates": {"name": "B"}})
        batch.append({"node_id": "c", "updates": {"name": "C"}})
        assert len(batch) == 100
        result = tools["batch_update_nodes"](batch)
        assert result["success"] is True
        assert result["counts"]["ok"] == 100

    def test_over_cap_refuses_all(self, batch_tools):
        tools, storage = batch_tools
        for i in range(99):
            nid = f"cap-{i}"
            storage.add_nodes([Node(id=nid, type=NodeType.ACTOR, name=nid)], [])
        batch = [{"node_id": f"cap-{i}", "updates": {"name": "x"}} for i in range(99)]
        batch.append({"node_id": "a", "updates": {"name": "X"}})
        assert len(batch) == 100
        # One more → 101
        batch.append({"node_id": "b", "updates": {"name": "Y"}})
        assert len(batch) == 101
        result = tools["batch_update_nodes"](batch)
        assert result["success"] is False
        assert "exceeds the limit of 100" in result["error"]
        assert result["results"] == []
        assert result["counts"] == {}
        # No writes happened — nodes a, b still unchanged
        assert storage.nodes["a"].name == "Alice"
        assert storage.nodes["b"].name == "Bob"


# ---------------------------------------------------------------------------
# 7. Duplicate ids refused
# ---------------------------------------------------------------------------


class TestDuplicateIds:
    def test_duplicate_node_ids_refused(self, batch_tools):
        tools, storage = batch_tools
        result = tools["batch_update_nodes"](
            [
                {"node_id": "a", "updates": {"name": "X"}},
                {"node_id": "a", "updates": {"name": "Y"}},
            ]
        )
        assert result["success"] is False
        assert "Duplicate node_id" in result["error"]
        assert result["results"] == []
        assert storage.nodes["a"].name == "Alice"

    def test_no_duplicates_allows_update(self, batch_tools):
        tools, storage = batch_tools
        result = tools["batch_update_nodes"](
            [
                {"node_id": "a", "updates": {"name": "Alice Updated"}},
                {"node_id": "b", "updates": {"name": "Bob Updated"}},
            ]
        )
        assert result["success"] is True
        assert result["counts"]["ok"] == 2


# ---------------------------------------------------------------------------
# 8. Compact result — no full node bodies
# ---------------------------------------------------------------------------


class TestCompactResult:
    def test_result_contains_no_full_node_bodies(self, batch_tools):
        tools, storage = batch_tools
        result = tools["batch_update_nodes"](
            [
                {"node_id": "a", "updates": {"name": "Alice Updated"}},
            ]
        )
        assert result["success"] is True
        for item in result["results"]:
            # Each result should NOT contain a full node dict
            assert "name" not in item
            assert "description" not in item
            assert "tags" not in item
            assert "metadata" not in item
            # Expected fields present
            assert "node_id" in item
            assert "status" in item

    def test_ok_result_has_revision_and_updated_at(self, batch_tools):
        tools, storage = batch_tools
        result = tools["batch_update_nodes"](
            [
                {"node_id": "a", "updates": {"name": "Alice Updated"}},
            ]
        )
        ok_items = [r for r in result["results"] if r["status"] == "ok"]
        assert len(ok_items) == 1
        assert "revision" in ok_items[0]
        assert "updated_at" in ok_items[0]


# ---------------------------------------------------------------------------
# 9. Service layer — direct path (bypassing MCP tool decorator)
# ---------------------------------------------------------------------------


class TestServiceLayer:
    def test_empty_batch_succeeds(self, empty_service):
        result = empty_service.batch_update_nodes([])
        assert result["success"] is True
        assert result["results"] == []
        assert result["counts"] == {
            "ok": 0,
            "conflict": 0,
            "not_found": 0,
            "forbidden": 0,
            "error": 0,
        }

    def test_over_cap_at_service_level(self, empty_service):
        batch = [
            {"node_id": f"n-{i}", "updates": {"name": f"x{i}"}} for i in range(101)
        ]
        result = empty_service.batch_update_nodes(batch)
        assert result["success"] is False
        assert "exceeds the limit of 100" in result["error"]

    def test_duplicate_at_service_level(self, empty_service):
        result = empty_service.batch_update_nodes(
            [
                {"node_id": "x", "updates": {"name": "X"}},
                {"node_id": "x", "updates": {"name": "Y"}},
            ]
        )
        assert result["success"] is False
        assert "Duplicate node_id" in result["error"]


# ---------------------------------------------------------------------------
# 10. Access-denied on the mutate action blocks the whole batch
# ---------------------------------------------------------------------------


class TestMutationDeny:
    def test_deny_mutations_blocks_batch(self, tmp_path):
        class DenyMutationsHook:
            def evaluate(
                self, context: GraphAuthorizationContext
            ) -> GraphAuthorizationDecision:
                if context.action == GRAPH_ACTION_MUTATE:
                    return GraphAuthorizationDecision(
                        allowed=False,
                        reason="Mutations disabled for this actor/workspace context.",
                        mode="custom",
                        source="test",
                    )
                return GraphAuthorizationDecision(
                    allowed=True, mode="custom", source="test"
                )

        service = GraphService(
            GraphStorage(json_path=str(tmp_path / "deny.json")),
            authorization_hook=DenyMutationsHook(),
        )
        result = service.batch_update_nodes(
            [
                {"node_id": "a", "updates": {"name": "X"}},
            ]
        )
        assert result["success"] is False
        assert result.get("error_code") == "access_denied"
        assert result.get("results") == []


# ---------------------------------------------------------------------------
# 11. MCP tool registration — callable and returns expected shape
# ---------------------------------------------------------------------------


class TestMcpToolRegistration:
    def test_tool_is_registered_and_callable(self, batch_tools):
        tools, _ = batch_tools
        assert "batch_update_nodes" in tools
        result = tools["batch_update_nodes"](
            [
                {"node_id": "a", "updates": {"name": "Test"}},
            ]
        )
        assert "success" in result
        assert "results" in result
        assert "counts" in result

    def test_tool_result_shape_on_empty_batch(self, batch_tools):
        tools, _ = batch_tools
        result = tools["batch_update_nodes"]([])
        assert result["success"] is True
        assert isinstance(result["results"], list)
        assert isinstance(result["counts"], dict)
        assert set(result["counts"].keys()) == {
            "ok",
            "conflict",
            "not_found",
            "forbidden",
            "error",
        }
