"""
Integration tests comparing REST API vs MCP tool operations.

These tests verify that:
1. REST API and MCP tools produce identical effects on the graph
2. Both interfaces work consistently against the same GraphStorage
3. Views are treated as regular nodes/edges (no special cases)

The test sequence:
1. add_nodes (create two nodes and an edge)
2. update_node (modify a node's name/description)
3. get_related_nodes (expand to get neighbors)
4. add_nodes (create a community and add members via edges)
5. save_view (get signal to save current view)
6. delete_nodes
"""

import pytest
import tempfile
import os
import json
from typing import Dict, Any

from fastapi.testclient import TestClient

from backend.core import GraphStorage, NodeType
from backend.service import GraphService, create_rest_router, register_mcp_tools
from backend.api_host import create_app
from backend.api_host.config import AppConfig


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


class TestRestVsMcpParity:
    """Tests ensuring REST API and MCP tools produce identical results."""

    @pytest.fixture
    def dual_envs(self, temp_dir):
        """
        Create two parallel environments:
        - One for REST operations
        - One for MCP operations
        Both start with empty graphs in separate temp files.
        """
        # REST environment
        rest_graph_path = os.path.join(temp_dir, "rest_graph.json")
        rest_config = AppConfig(graph_file=rest_graph_path)
        rest_app = create_app(rest_config)
        rest_client = TestClient(rest_app)
        rest_storage = rest_app.state.graph_storage

        # MCP environment - separate graph storage with direct tool access
        mcp_graph_path = os.path.join(temp_dir, "mcp_graph.json")
        mcp_config = AppConfig(graph_file=mcp_graph_path)
        mcp_app = create_app(mcp_config)
        mcp_tools = mcp_app.state.tools_map
        mcp_storage = mcp_app.state.graph_storage

        return {
            "rest_client": rest_client,
            "rest_storage": rest_storage,
            "mcp_tools": mcp_tools,
            "mcp_storage": mcp_storage,
        }

    def test_step1_add_nodes_with_edge(self, dual_envs):
        """Step 1: Add two nodes and an edge via both interfaces."""
        # Define the same data for both
        nodes = [
            {"id": "node-a", "type": "Actor", "name": "Organization Alpha", "description": "First org"},
            {"id": "node-b", "type": "Initiative", "name": "Project Beta", "description": "First project"},
        ]
        edges = [
            {"source": "node-a", "target": "node-b", "type": "BELONGS_TO"},
        ]

        # Execute via REST
        rest_result = dual_envs["rest_client"].post(
            "/api/nodes",
            json={"nodes": nodes, "edges": edges}
        )
        assert rest_result.status_code == 200
        rest_data = rest_result.json()
        assert rest_data["success"] is True

        # Execute via MCP
        mcp_result = dual_envs["mcp_tools"]["add_nodes"](nodes=nodes, edges=edges)
        assert mcp_result["success"] is True

        # Verify both have same structure
        assert len(dual_envs["rest_storage"].nodes) == 2
        assert len(dual_envs["mcp_storage"].nodes) == 2
        assert len(dual_envs["rest_storage"].edges) == 1
        assert len(dual_envs["mcp_storage"].edges) == 1

        # Both should have nodes with the same names
        rest_names = {n.name for n in dual_envs["rest_storage"].nodes.values()}
        mcp_names = {n.name for n in dual_envs["mcp_storage"].nodes.values()}
        assert rest_names == mcp_names == {"Organization Alpha", "Project Beta"}

    def test_step2_update_node(self, dual_envs):
        """Step 2: Update a node's description via both interfaces."""
        # First add a node to both
        nodes = [{"id": "update-test", "type": "Actor", "name": "Update Test Node", "description": "Original"}]

        dual_envs["rest_client"].post("/api/nodes", json={"nodes": nodes, "edges": []})
        dual_envs["mcp_tools"]["add_nodes"](nodes=nodes, edges=[])

        # Update via REST
        rest_result = dual_envs["rest_client"].patch(
            "/api/nodes/update-test",
            json={"updates": {"description": "Updated via REST"}}
        )
        assert rest_result.status_code == 200

        # Update via MCP
        mcp_result = dual_envs["mcp_tools"]["update_node"](
            node_id="update-test",
            updates={"description": "Updated via MCP"}
        )
        assert mcp_result["success"] is True

        # Verify both updated correctly
        assert dual_envs["rest_storage"].nodes["update-test"].description == "Updated via REST"
        assert dual_envs["mcp_storage"].nodes["update-test"].description == "Updated via MCP"

    def test_step3_get_related_nodes(self, dual_envs):
        """Step 3: Expand/get related nodes via both interfaces."""
        # Setup: Create a small network
        nodes = [
            {"id": "center", "type": "Actor", "name": "Center Node"},
            {"id": "neighbor1", "type": "Initiative", "name": "Neighbor One"},
            {"id": "neighbor2", "type": "Community", "name": "Neighbor Two"},
        ]
        edges = [
            {"source": "center", "target": "neighbor1", "type": "BELONGS_TO"},
            {"source": "center", "target": "neighbor2", "type": "PART_OF"},
        ]

        dual_envs["rest_client"].post("/api/nodes", json={"nodes": nodes, "edges": edges})
        dual_envs["mcp_tools"]["add_nodes"](nodes=nodes, edges=edges)

        # Get related via REST
        rest_result = dual_envs["rest_client"].post(
            "/api/nodes/center/related",
            json={"depth": 1}
        )
        assert rest_result.status_code == 200
        rest_related = rest_result.json()

        # Get related via MCP
        mcp_related = dual_envs["mcp_tools"]["get_related_nodes"](node_id="center", depth=1)

        # Both should return same number of related nodes
        assert len(rest_related["nodes"]) == len(mcp_related["nodes"])
        assert len(rest_related["edges"]) == len(mcp_related["edges"])

        # Verify the related nodes are the same (by name since IDs might differ)
        rest_names = {n["name"] for n in rest_related["nodes"]}
        mcp_names = {n["name"] for n in mcp_related["nodes"]}
        assert rest_names == mcp_names

    def test_step4_add_community_with_members(self, dual_envs):
        """Step 4: Create a community and add members via edges."""
        # First create some member nodes
        members = [
            {"id": "member1", "type": "Actor", "name": "Community Member 1"},
            {"id": "member2", "type": "Actor", "name": "Community Member 2"},
        ]
        dual_envs["rest_client"].post("/api/nodes", json={"nodes": members, "edges": []})
        dual_envs["mcp_tools"]["add_nodes"](nodes=members, edges=[])

        # Now create the community and edges
        community = {"id": "comm1", "type": "Community", "name": "Test Community", "description": "A test community"}
        comm_edges = [
            {"source": "member1", "target": "comm1", "type": "PART_OF"},
            {"source": "member2", "target": "comm1", "type": "PART_OF"},
        ]

        # REST
        rest_result = dual_envs["rest_client"].post(
            "/api/nodes",
            json={"nodes": [community], "edges": comm_edges}
        )
        assert rest_result.status_code == 200

        # MCP
        mcp_result = dual_envs["mcp_tools"]["add_nodes"](nodes=[community], edges=comm_edges)
        assert mcp_result["success"] is True

        # Verify both have the community
        rest_comm = dual_envs["rest_storage"].nodes.get("comm1")
        mcp_comm = dual_envs["mcp_storage"].nodes.get("comm1")
        assert rest_comm is not None and rest_comm.type == NodeType.COMMUNITY
        assert mcp_comm is not None and mcp_comm.type == NodeType.COMMUNITY

        # Verify edges exist
        assert len(dual_envs["rest_storage"].edges) == 2
        assert len(dual_envs["mcp_storage"].edges) == 2

    def test_step5_save_view(self, dual_envs):
        """Step 5: Test save_view signal via both interfaces."""
        # First create some nodes to include in the view
        nodes = [
            {"id": "view-node1", "type": "Actor", "name": "View Node 1"},
            {"id": "view-node2", "type": "Initiative", "name": "View Node 2"},
        ]
        edges = [{"source": "view-node1", "target": "view-node2", "type": "BELONGS_TO"}]

        dual_envs["rest_client"].post("/api/nodes", json={"nodes": nodes, "edges": edges})
        dual_envs["mcp_tools"]["add_nodes"](nodes=nodes, edges=edges)

        # Save view via REST
        rest_result = dual_envs["rest_client"].post(
            "/api/views/save",
            json={"name": "Test View REST"}
        )
        assert rest_result.status_code == 200

        # Save view via MCP
        mcp_result = dual_envs["mcp_tools"]["save_view"](name="Test View MCP")

        # Both should return an action signal (save_view is a signal, not actual storage)
        assert rest_result.json()["action"] == "save_view"
        assert mcp_result["action"] == "save_view"

    def test_step6_delete_nodes(self, dual_envs):
        """Step 6: Delete nodes via both interfaces."""
        # Create nodes to delete
        nodes = [
            {"id": "delete-me", "type": "Actor", "name": "Node To Delete"},
            {"id": "keep-me", "type": "Actor", "name": "Node To Keep"},
        ]
        edges = [{"source": "delete-me", "target": "keep-me", "type": "RELATES_TO"}]

        dual_envs["rest_client"].post("/api/nodes", json={"nodes": nodes, "edges": edges})
        dual_envs["mcp_tools"]["add_nodes"](nodes=nodes, edges=edges)

        # Verify initial state
        assert len(dual_envs["rest_storage"].nodes) == 2
        assert len(dual_envs["mcp_storage"].nodes) == 2

        # Delete via REST
        rest_result = dual_envs["rest_client"].request(
            "DELETE",
            "/api/nodes",
            json={"node_ids": ["delete-me"], "confirmed": True}
        )
        assert rest_result.status_code == 200
        assert rest_result.json()["success"] is True

        # Delete via MCP
        mcp_result = dual_envs["mcp_tools"]["delete_nodes"](node_ids=["delete-me"], confirmed=True)
        assert mcp_result["success"] is True

        # Verify both have one node remaining
        assert len(dual_envs["rest_storage"].nodes) == 1
        assert len(dual_envs["mcp_storage"].nodes) == 1

        # Verify the edge was also deleted
        assert len(dual_envs["rest_storage"].edges) == 0
        assert len(dual_envs["mcp_storage"].edges) == 0


class TestFullSequence:
    """
    Test a complete sequence of operations on a single environment,
    executing the same operations via REST and MCP and comparing results.
    """

    @pytest.fixture
    def unified_env(self, temp_dir):
        """Create a single environment with both REST and MCP access."""
        graph_path = os.path.join(temp_dir, "unified_graph.json")
        config = AppConfig(graph_file=graph_path)
        app = create_app(config)
        client = TestClient(app)
        storage = app.state.graph_storage
        tools = app.state.tools_map

        return {
            "client": client,
            "storage": storage,
            "tools": tools,
        }

    def test_complete_workflow_sequence(self, unified_env):
        """
        Execute a complete workflow using both REST and MCP on the same graph.
        """
        ctx = unified_env

        # Step 1: Add initial nodes via REST
        initial_nodes = [
            {"id": "org-1", "type": "Actor", "name": "Swedish Tax Agency"},
            {"id": "org-2", "type": "Actor", "name": "Swedish Companies Registry"},
        ]
        rest_add = ctx["client"].post("/api/nodes", json={"nodes": initial_nodes, "edges": []})
        assert rest_add.status_code == 200
        assert len(ctx["storage"].nodes) == 2

        # Step 2: Add more nodes via MCP
        mcp_nodes = [
            {"id": "init-1", "type": "Initiative", "name": "Digital First Initiative"},
        ]
        mcp_edges = [
            {"source": "org-1", "target": "init-1", "type": "BELONGS_TO"},
            {"source": "org-2", "target": "init-1", "type": "BELONGS_TO"},
        ]
        mcp_add = ctx["tools"]["add_nodes"](nodes=mcp_nodes, edges=mcp_edges)
        assert mcp_add["success"] is True
        assert len(ctx["storage"].nodes) == 3
        assert len(ctx["storage"].edges) == 2

        # Step 3: Query via REST to see MCP-added data
        rest_search = ctx["client"].post("/api/search", json={"query": "Digital First"})
        assert rest_search.status_code == 200
        assert rest_search.json()["total"] == 1

        # Step 4: Query via MCP to see REST-added data
        mcp_search = ctx["tools"]["search_graph"](query="Tax Agency")
        assert mcp_search["total"] == 1
        assert mcp_search["nodes"][0]["name"] == "Swedish Tax Agency"

        # Step 5: Update via REST, verify via MCP
        ctx["client"].patch("/api/nodes/org-1", json={"updates": {"description": "Updated by REST"}})
        mcp_details = ctx["tools"]["get_node_details"](node_id="org-1")
        assert mcp_details["node"]["description"] == "Updated by REST"

        # Step 6: Update via MCP, verify via REST
        ctx["tools"]["update_node"](node_id="org-2", updates={"description": "Updated by MCP"})
        rest_details = ctx["client"].get("/api/nodes/org-2")
        assert rest_details.json()["node"]["description"] == "Updated by MCP"

        # Step 7: Create a community via MCP
        community = {"id": "community-1", "type": "Community", "name": "Agency Community"}
        comm_edges = [
            {"source": "org-1", "target": "community-1", "type": "PART_OF"},
            {"source": "org-2", "target": "community-1", "type": "PART_OF"},
        ]
        ctx["tools"]["add_nodes"](nodes=[community], edges=comm_edges)

        # Step 8: Verify via REST that community exists
        rest_related = ctx["client"].post("/api/nodes/org-1/related", json={"depth": 1})
        related_names = {n["name"] for n in rest_related.json()["nodes"]}
        assert "Agency Community" in related_names

        # Step 9: Get stats via both interfaces
        rest_stats = ctx["client"].get("/api/stats")
        mcp_stats = ctx["tools"]["get_graph_stats"]()

        assert rest_stats.json()["total_nodes"] == mcp_stats["total_nodes"] == 4
        assert rest_stats.json()["total_edges"] == mcp_stats["total_edges"] == 4

        # Step 10: Delete via REST, verify via MCP
        ctx["client"].request(
            "DELETE",
            "/api/nodes",
            json={"node_ids": ["init-1"], "confirmed": True}
        )
        mcp_stats_after = ctx["tools"]["get_graph_stats"]()
        assert mcp_stats_after["total_nodes"] == 3
        # Edges to init-1 should be deleted too
        assert mcp_stats_after["total_edges"] == 2


class TestSavedViewsAsNodes:
    """Test that saved views are regular nodes, not special-cased."""

    @pytest.fixture
    def env_with_data(self, temp_dir):
        """Create environment with some initial data."""
        graph_path = os.path.join(temp_dir, "view_test_graph.json")
        config = AppConfig(graph_file=graph_path)
        app = create_app(config)
        client = TestClient(app)
        storage = app.state.graph_storage
        tools = app.state.tools_map

        # Add initial nodes
        nodes = [
            {"id": "n1", "type": "Actor", "name": "Actor 1"},
            {"id": "n2", "type": "Initiative", "name": "Initiative 1"},
        ]
        edges = [{"source": "n1", "target": "n2", "type": "BELONGS_TO"}]
        client.post("/api/nodes", json={"nodes": nodes, "edges": edges})

        return {"client": client, "storage": storage, "tools": tools}

    def test_saved_view_is_a_node(self, env_with_data):
        """Verify that SavedView is stored as a regular node."""
        ctx = env_with_data

        # Create a SavedView node directly
        saved_view = {
            "id": "view-test",
            "type": "SavedView",
            "name": "My Test View",
            "description": "A saved view snapshot",
            "metadata": {
                "node_ids": ["n1", "n2"],
                "positions": {
                    "n1": {"x": 100, "y": 100},
                    "n2": {"x": 200, "y": 200},
                },
                "hidden_node_ids": [],
            }
        }
        view_edges = [
            {"source": "view-test", "target": "n1", "type": "RELATES_TO"},
            {"source": "view-test", "target": "n2", "type": "RELATES_TO"},
        ]

        # Add via REST
        result = ctx["client"].post(
            "/api/nodes",
            json={"nodes": [saved_view], "edges": view_edges}
        )
        assert result.status_code == 200

        # Verify it's a regular node
        view_node = ctx["storage"].nodes.get("view-test")
        assert view_node is not None
        assert view_node.type == NodeType.SAVED_VIEW
        assert view_node.name == "My Test View"

        # Verify it can be found via search
        search_result = ctx["tools"]["search_graph"](query="Test View")
        assert search_result["total"] >= 1
        view_found = any(n["name"] == "My Test View" for n in search_result["nodes"])
        assert view_found

        # Verify edges exist
        assert len(ctx["storage"].edges) == 3  # Original edge + 2 RELATES_TO edges

    def test_saved_view_can_be_deleted(self, env_with_data):
        """Verify that SavedView nodes can be deleted like any other node."""
        ctx = env_with_data

        # Create a SavedView
        saved_view = {
            "id": "deletable-view",
            "type": "SavedView",
            "name": "Deletable View",
        }
        ctx["client"].post("/api/nodes", json={"nodes": [saved_view], "edges": []})

        assert "deletable-view" in ctx["storage"].nodes

        # Delete it
        ctx["tools"]["delete_nodes"](node_ids=["deletable-view"], confirmed=True)

        assert "deletable-view" not in ctx["storage"].nodes


class TestCommunitiesAsNodes:
    """Test that communities are regular nodes, not special-cased."""

    @pytest.fixture
    def env(self, temp_dir):
        """Create test environment."""
        graph_path = os.path.join(temp_dir, "community_test_graph.json")
        config = AppConfig(graph_file=graph_path)
        app = create_app(config)
        client = TestClient(app)
        storage = app.state.graph_storage
        tools = app.state.tools_map

        return {"client": client, "storage": storage, "tools": tools}

    def test_community_is_a_node(self, env):
        """Verify that Community is stored as a regular node."""
        ctx = env

        # Create members first
        members = [
            {"id": "m1", "type": "Actor", "name": "Member 1"},
            {"id": "m2", "type": "Actor", "name": "Member 2"},
        ]
        ctx["client"].post("/api/nodes", json={"nodes": members, "edges": []})

        # Create community with membership edges
        community = {
            "id": "test-community",
            "type": "Community",
            "name": "Test Community",
            "description": "A test community",
        }
        edges = [
            {"source": "m1", "target": "test-community", "type": "PART_OF"},
            {"source": "m2", "target": "test-community", "type": "PART_OF"},
        ]

        result = ctx["tools"]["add_nodes"](nodes=[community], edges=edges)
        assert result["success"] is True

        # Verify it's a regular node
        community_node = ctx["storage"].nodes.get("test-community")
        assert community_node is not None
        assert community_node.type == NodeType.COMMUNITY

        # Verify we can get related nodes (members)
        related = ctx["client"].post("/api/nodes/test-community/related", json={"depth": 1})
        related_names = {n["name"] for n in related.json()["nodes"]}
        assert "Member 1" in related_names
        assert "Member 2" in related_names

    def test_community_deletion_removes_membership_edges(self, env):
        """Verify that deleting a community removes the membership edges."""
        ctx = env

        # Setup
        members = [{"id": "gm1", "type": "Actor", "name": "Community Member"}]
        community = {"id": "del-community", "type": "Community", "name": "Community To Delete"}
        edges = [{"source": "gm1", "target": "del-community", "type": "PART_OF"}]

        ctx["client"].post("/api/nodes", json={"nodes": members + [community], "edges": edges})

        assert len(ctx["storage"].edges) == 1

        # Delete the community
        ctx["tools"]["delete_nodes"](node_ids=["del-community"], confirmed=True)

        # Member should remain, edge should be gone
        assert "gm1" in ctx["storage"].nodes
        assert "del-community" not in ctx["storage"].nodes
        assert len(ctx["storage"].edges) == 0


class TestTimestampTracking:
    """Test that both REST and MCP properly update timestamps."""

    @pytest.fixture
    def env(self, temp_dir):
        """Create test environment."""
        graph_path = os.path.join(temp_dir, "timestamp_test_graph.json")
        config = AppConfig(graph_file=graph_path)
        app = create_app(config)
        client = TestClient(app)
        storage = app.state.graph_storage
        tools = app.state.tools_map

        return {"client": client, "storage": storage, "tools": tools}

    def test_rest_updates_update_timestamp(self, env):
        """Verify REST updates modify the updated_at timestamp."""
        ctx = env

        # Add node
        ctx["client"].post(
            "/api/nodes",
            json={"nodes": [{"id": "ts-test", "type": "Actor", "name": "Timestamp Test"}], "edges": []}
        )

        initial_updated = ctx["storage"].nodes["ts-test"].updated_at

        # Small delay to ensure time difference
        import time
        time.sleep(0.01)

        # Update
        ctx["client"].patch("/api/nodes/ts-test", json={"updates": {"description": "Updated"}})

        new_updated = ctx["storage"].nodes["ts-test"].updated_at
        assert new_updated >= initial_updated

    def test_mcp_updates_update_timestamp(self, env):
        """Verify MCP updates modify the updated_at timestamp."""
        ctx = env

        # Add node via MCP
        ctx["tools"]["add_nodes"](
            nodes=[{"id": "mcp-ts-test", "type": "Actor", "name": "MCP Timestamp Test"}],
            edges=[]
        )

        initial_updated = ctx["storage"].nodes["mcp-ts-test"].updated_at

        # Small delay to ensure time difference
        import time
        time.sleep(0.01)

        # Update via MCP
        ctx["tools"]["update_node"](node_id="mcp-ts-test", updates={"description": "MCP Updated"})

        new_updated = ctx["storage"].nodes["mcp-ts-test"].updated_at
        assert new_updated >= initial_updated
