"""
Client simulation tests for graph_services.

These tests simulate different types of clients interacting with the service:
- REST API clients (via FastAPI TestClient)
- MCP clients (via direct tool function calls)
- Multiple concurrent clients

This ensures the service works correctly from the perspective of actual consumers.
"""

import pytest
import tempfile
import os
from typing import Dict, Any
from unittest.mock import Mock, MagicMock

from backend.core import GraphStorage, Node, NodeType
from backend.service import GraphService, create_rest_router, register_mcp_tools


# ==================== REST API Client Tests ====================

class TestRESTAPIClient:
    """Tests simulating REST API client behavior."""

    @pytest.fixture
    def api_client(self, temp_dir):
        """Create a FastAPI test client."""
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("FastAPI not installed")

        json_path = os.path.join(temp_dir, "test.json")
        storage = GraphStorage(json_path=json_path)
        service = GraphService(storage)
        router = create_rest_router(service)

        app = FastAPI()
        app.include_router(router, prefix="/api/graph")

        return TestClient(app)

    @pytest.fixture
    def populated_api_client(self, temp_dir):
        """Create a FastAPI test client with pre-populated data."""
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("FastAPI not installed")

        json_path = os.path.join(temp_dir, "test.json")
        storage = GraphStorage(json_path=json_path)

        # Pre-populate with test data
        nodes = [
            Node(id="api-1", type=NodeType.ACTOR, name="API Actor"),
            Node(id="api-2", type=NodeType.INITIATIVE, name="API Initiative"),
        ]
        from backend.core import Edge, RelationshipType
        edges = [
            Edge(id="api-e1", source="api-1", target="api-2", type=RelationshipType.BELONGS_TO)
        ]
        storage.add_nodes(nodes, edges)

        service = GraphService(storage)
        router = create_rest_router(service)

        app = FastAPI()
        app.include_router(router, prefix="/api/graph")

        return TestClient(app)

    def test_search_endpoint(self, populated_api_client):
        """Test POST /api/graph/search endpoint."""
        response = populated_api_client.post(
            "/api/graph/search",
            json={"query": "API Actor"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] >= 1
        assert any(n["name"] == "API Actor" for n in data["nodes"])

    def test_get_node_endpoint(self, populated_api_client):
        """Test GET /api/graph/nodes/{id} endpoint."""
        response = populated_api_client.get("/api/graph/nodes/api-1")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["node"]["name"] == "API Actor"

    def test_get_node_not_found(self, populated_api_client):
        """Test 404 response for non-existent node."""
        response = populated_api_client.get("/api/graph/nodes/nonexistent")
        assert response.status_code == 404

    def test_add_nodes_endpoint(self, api_client):
        """Test POST /api/graph/nodes endpoint."""
        response = api_client.post(
            "/api/graph/nodes",
            json={
                "nodes": [{"type": "Actor", "name": "New REST Actor"}],
                "edges": []
            }
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert len(data["added_node_ids"]) == 1

    def test_update_node_endpoint(self, populated_api_client):
        """Test PATCH /api/graph/nodes/{id} endpoint."""
        response = populated_api_client.patch(
            "/api/graph/nodes/api-1",
            json={"updates": {"description": "Updated via REST"}}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["node"]["description"] == "Updated via REST"

    def test_delete_nodes_endpoint(self, populated_api_client):
        """Test DELETE /api/graph/nodes endpoint."""
        response = populated_api_client.request(
            "DELETE",
            "/api/graph/nodes",
            json={"node_ids": ["api-1"], "confirmed": True}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    def test_stats_endpoint(self, populated_api_client):
        """Test GET /api/graph/stats endpoint."""
        response = populated_api_client.get("/api/graph/stats")
        assert response.status_code == 200
        data = response.json()
        assert "total_nodes" in data
        assert "total_edges" in data

    def test_node_types_endpoint(self, api_client):
        """Test GET /api/graph/meta/node-types endpoint."""
        response = api_client.get("/api/graph/meta/node-types")
        assert response.status_code == 200
        data = response.json()
        assert "node_types" in data
        types = [t["type"] for t in data["node_types"]]
        assert "Actor" in types

    def test_export_endpoint(self, populated_api_client):
        """Test GET /api/graph/export endpoint."""
        response = populated_api_client.get("/api/graph/export")
        assert response.status_code == 200
        data = response.json()
        assert "version" in data
        assert "nodes" in data
        assert "edges" in data

    def test_similarity_endpoint(self, populated_api_client):
        """Test POST /api/graph/similar endpoint."""
        response = populated_api_client.post(
            "/api/graph/similar",
            json={"name": "API Act", "threshold": 0.5}
        )
        assert response.status_code == 200
        data = response.json()
        assert "similar_nodes" in data

    def test_batch_similarity_endpoint(self, populated_api_client):
        """Test POST /api/graph/similar/batch endpoint."""
        response = populated_api_client.post(
            "/api/graph/similar/batch",
            json={"names": ["API Actor", "Unknown"], "threshold": 0.5}
        )
        assert response.status_code == 200
        data = response.json()
        assert "results" in data
        assert "API Actor" in data["results"]


# ==================== MCP Client Tests ====================

class TestMCPClient:
    """Tests simulating MCP client behavior (LLM tool calls)."""

    @pytest.fixture
    def mcp_tools(self, temp_dir):
        """Create MCP tools with a mock MCP server."""
        json_path = os.path.join(temp_dir, "test.json")
        storage = GraphStorage(json_path=json_path)
        service = GraphService(storage)

        # Mock MCP server
        mock_mcp = Mock()
        mock_mcp.tool = MagicMock(return_value=lambda f: f)

        tools_map = register_mcp_tools(mock_mcp, service)
        return tools_map, service

    @pytest.fixture
    def populated_mcp_tools(self, temp_dir):
        """Create MCP tools with pre-populated data."""
        json_path = os.path.join(temp_dir, "test.json")
        storage = GraphStorage(json_path=json_path)

        # Pre-populate
        nodes = [
            Node(id="mcp-1", type=NodeType.ACTOR, name="MCP Actor"),
            Node(id="mcp-2", type=NodeType.INITIATIVE, name="MCP Initiative"),
        ]
        from backend.core import Edge, RelationshipType
        edges = [
            Edge(id="mcp-e1", source="mcp-1", target="mcp-2", type=RelationshipType.BELONGS_TO)
        ]
        storage.add_nodes(nodes, edges)

        service = GraphService(storage)

        mock_mcp = Mock()
        mock_mcp.tool = MagicMock(return_value=lambda f: f)

        tools_map = register_mcp_tools(mock_mcp, service)
        return tools_map, service

    def test_mcp_search_graph(self, populated_mcp_tools):
        """Test MCP search_graph tool."""
        tools_map, _ = populated_mcp_tools
        result = tools_map["search_graph"](query="MCP Actor")

        assert result["total"] >= 1
        assert any(n["name"] == "MCP Actor" for n in result["nodes"])

    def test_mcp_get_node_details(self, populated_mcp_tools):
        """Test MCP get_node_details tool."""
        tools_map, _ = populated_mcp_tools
        result = tools_map["get_node_details"](node_id="mcp-1")

        assert result["success"] is True
        assert result["node"]["name"] == "MCP Actor"

    def test_mcp_get_related_nodes(self, populated_mcp_tools):
        """Test MCP get_related_nodes tool."""
        tools_map, _ = populated_mcp_tools
        result = tools_map["get_related_nodes"](node_id="mcp-1", depth=1)

        assert "nodes" in result
        node_ids = [n["id"] for n in result["nodes"]]
        assert "mcp-2" in node_ids

    def test_mcp_add_nodes(self, mcp_tools):
        """Test MCP add_nodes tool."""
        tools_map, service = mcp_tools
        result = tools_map["add_nodes"](
            nodes=[{"type": "Actor", "name": "MCP Added Actor"}],
            edges=[]
        )

        assert result["success"] is True
        # Verify via service
        stats = service.get_graph_stats()
        assert stats["total_nodes"] == 1

    def test_mcp_update_node(self, populated_mcp_tools):
        """Test MCP update_node tool."""
        tools_map, _ = populated_mcp_tools
        result = tools_map["update_node"](
            node_id="mcp-1",
            updates={"description": "Updated via MCP"}
        )

        assert result["success"] is True
        assert result["node"]["description"] == "Updated via MCP"

    def test_mcp_delete_nodes(self, populated_mcp_tools):
        """Test MCP delete_nodes tool."""
        tools_map, service = populated_mcp_tools
        result = tools_map["delete_nodes"](
            node_ids=["mcp-1"],
            confirmed=True
        )

        assert result["success"] is True
        # Verify deletion
        node_result = service.get_node_details("mcp-1")
        assert node_result["success"] is False

    def test_mcp_find_similar_nodes(self, populated_mcp_tools):
        """Test MCP find_similar_nodes tool."""
        tools_map, _ = populated_mcp_tools
        result = tools_map["find_similar_nodes"](
            name="MCP Act",
            threshold=0.5
        )

        assert "similar_nodes" in result

    def test_mcp_find_similar_nodes_batch(self, populated_mcp_tools):
        """Test MCP find_similar_nodes_batch tool."""
        tools_map, _ = populated_mcp_tools
        result = tools_map["find_similar_nodes_batch"](
            names=["MCP Actor", "Unknown"],
            threshold=0.5
        )

        assert "results" in result
        assert "MCP Actor" in result["results"]

    def test_mcp_get_graph_stats(self, populated_mcp_tools):
        """Test MCP get_graph_stats tool."""
        tools_map, _ = populated_mcp_tools
        result = tools_map["get_graph_stats"]()

        assert result["total_nodes"] == 2
        assert result["total_edges"] == 1

    def test_mcp_list_node_types(self, mcp_tools):
        """Test MCP list_node_types tool."""
        tools_map, _ = mcp_tools
        result = tools_map["list_node_types"]()

        assert "node_types" in result
        types = [t["type"] for t in result["node_types"]]
        assert "Actor" in types

    def test_mcp_list_relationship_types(self, mcp_tools):
        """Test MCP list_relationship_types tool."""
        tools_map, _ = mcp_tools
        result = tools_map["list_relationship_types"]()

        assert "relationship_types" in result
        types = [t["type"] for t in result["relationship_types"]]
        assert "BELONGS_TO" in types

    def test_mcp_save_view(self, mcp_tools):
        """Test MCP save_view tool."""
        tools_map, _ = mcp_tools
        result = tools_map["save_view"](name="MCP Test View")

        assert result["action"] == "save_view"
        assert result["name"] == "MCP Test View"

    def test_mcp_tools_registered(self, mcp_tools):
        """Verify all expected tools are registered."""
        tools_map, _ = mcp_tools

        expected_tools = [
            "search_graph",
            "get_node_details",
            "get_related_nodes",
            "find_similar_nodes",
            "find_similar_nodes_batch",
            "add_nodes",
            "update_node",
            "delete_nodes",
            "get_graph_stats",
            "list_node_types",
            "list_relationship_types",
            "save_view",
            "get_saved_view",
            "list_saved_views",
        ]

        for tool_name in expected_tools:
            assert tool_name in tools_map, f"Tool {tool_name} not registered"


# ==================== Multi-Client Tests ====================

class TestMultiClientScenarios:
    """Tests simulating multiple clients interacting with the same service."""

    def test_shared_service_state(self, temp_dir):
        """Test that multiple clients share the same service state."""
        json_path = os.path.join(temp_dir, "test.json")
        storage = GraphStorage(json_path=json_path)
        service = GraphService(storage)

        # Simulate two "clients" using the same service
        mock_mcp = Mock()
        mock_mcp.tool = MagicMock(return_value=lambda f: f)
        mcp_tools = register_mcp_tools(mock_mcp, service)

        # Client 1 (MCP) adds a node
        mcp_tools["add_nodes"](
            nodes=[{"id": "shared-1", "type": "Actor", "name": "Shared Node"}],
            edges=[]
        )

        # Client 2 should see the node
        result = service.get_node_details("shared-1")
        assert result["success"] is True

    def test_llm_workflow_simulation(self, temp_dir):
        """Simulate a typical LLM workflow using MCP tools."""
        json_path = os.path.join(temp_dir, "test.json")
        storage = GraphStorage(json_path=json_path)
        service = GraphService(storage)

        mock_mcp = Mock()
        mock_mcp.tool = MagicMock(return_value=lambda f: f)
        tools = register_mcp_tools(mock_mcp, service)

        # Step 1: LLM wants to add a new entity, first checks for duplicates
        similar = tools["find_similar_nodes"](name="Skatteverket", threshold=0.7)
        assert similar["total"] == 0  # No existing nodes

        # Step 2: LLM adds the new node
        add_result = tools["add_nodes"](
            nodes=[{
                "type": "Actor",
                "name": "Skatteverket",
                "description": "Swedish Tax Agency",
                "tags": ["government", "tax"]
            }],
            edges=[]
        )
        assert add_result["success"] is True

        # Step 3: LLM searches for related concepts
        search_result = tools["search_graph"](
            query="tax",
            node_types=["Actor"]
        )
        assert search_result["total"] >= 1

        # Step 4: LLM updates the node with more info
        node_id = add_result["added_node_ids"][0]
        tools["update_node"](
            node_id=node_id,
            updates={"tags": ["government", "tax", "swedish"]}
        )

        # Step 5: LLM gets final node details
        final = tools["get_node_details"](node_id=node_id)
        assert "government" in final["node"]["tags"]

    def test_frontend_workflow_simulation(self, temp_dir):
        """Simulate a typical frontend workflow."""
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("FastAPI not installed")

        json_path = os.path.join(temp_dir, "test.json")
        storage = GraphStorage(json_path=json_path)
        service = GraphService(storage)
        router = create_rest_router(service)

        app = FastAPI()
        app.include_router(router, prefix="/api/graph")
        client = TestClient(app)

        # Step 1: Frontend loads initial graph
        response = client.get("/api/graph/export")
        assert response.status_code == 200
        assert response.json()["total_nodes"] == 0

        # Step 2: User adds a node via UI
        response = client.post(
            "/api/graph/nodes",
            json={
                "nodes": [{
                    "type": "Actor",
                    "name": "User Added Node",
                    "description": "Added via frontend"
                }],
                "edges": []
            }
        )
        assert response.status_code == 200
        node_id = response.json()["added_node_ids"][0]

        # Step 3: User searches for the node
        response = client.post(
            "/api/graph/search",
            json={"query": "User Added"}
        )
        assert response.status_code == 200
        assert response.json()["total"] == 1

        # Step 4: User views node details
        response = client.get(f"/api/graph/nodes/{node_id}")
        assert response.status_code == 200

        # Step 5: User updates the node
        response = client.patch(
            f"/api/graph/nodes/{node_id}",
            json={"updates": {"summary": "Updated via frontend"}}
        )
        assert response.status_code == 200

        # Step 6: User exports updated graph
        response = client.get("/api/graph/export")
        assert response.status_code == 200
        export = response.json()
        assert export["total_nodes"] == 1
        assert export["nodes"][0]["summary"] == "Updated via frontend"
